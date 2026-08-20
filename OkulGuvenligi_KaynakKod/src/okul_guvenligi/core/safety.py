from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .catalog import FIELD_TYPES, key, resolve_action_name, resolve_placement
from .models import ChangePlan, PlanOperation


class SafetyError(ValueError):
    pass


RISK_SCORE = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class TranslatedOperation:
    semantic: PlanOperation
    preview: dict[str, Any]
    risk: str = "low"


class SafetyEngine:
    """AI ile program arasındaki zorunlu güvenlik duvarı.

    AI hiçbir SQL/Python/JS çalıştıramaz. Yalnızca burada açıkça izin verilen semantik
    işlemler, mevcut PlatformEngine ön izleme/doğrulama katmanına çevrilebilir.
    """

    MAX_PLAN_OPERATIONS = 12
    ALLOWED_KINDS = {
        "none",
        "module_create", "module_archive", "module_restore", "module_move",
        "field_add", "field_archive", "field_restore", "field_move", "field_set_property",
        "action_add_or_move", "action_archive", "action_restore", "action_move",
    }

    def __init__(self, db, platform, health):
        self.db = db
        self.platform = platform
        self.health = health

    def _modules(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        return self.platform.list_modules(include_inactive)

    def _find_module(self, name: str, *, include_inactive: bool = True) -> dict[str, Any]:
        wanted = key(name)
        if not wanted:
            raise SafetyError("Hangi bölümün değiştirileceği belirtilmedi.")
        modules = self._modules(include_inactive)
        exact = [m for m in modules if wanted in {key(m.get("label")), key(m.get("module_key")), key(m.get("singular_label"))}]
        if len(exact) == 1:
            return exact[0]
        partial = [m for m in modules if wanted in key(m.get("label")) or key(m.get("label")) in wanted]
        if len(partial) == 1:
            return partial[0]
        core_aliases = {"kurum": "Kurumlar", "kurumlar": "Kurumlar", "finans": "Finans", "prim": "Prim"}
        if wanted in core_aliases:
            raise SafetyError(
                f"{core_aliases[wanted]} henüz LOGOS 5 dinamik çekirdeğine taşınmamış korunan ana bölümdür. "
                "AI bu bölümün kaynak kodunu veya veritabanı şemasını doğrudan değiştiremez."
            )
        raise SafetyError(f"Bölüm bulunamadı veya belirsiz: {name}")

    @staticmethod
    def _find_named(items: list[dict[str, Any]], name: str, *, fields: tuple[str, ...]) -> dict[str, Any]:
        wanted = key(name)
        exact = [item for item in items if wanted and wanted in {key(item.get(field)) for field in fields}]
        if len(exact) == 1:
            return exact[0]
        partial = [
            item for item in items
            if wanted and any(wanted in key(item.get(field)) or key(item.get(field)) in wanted for field in fields if item.get(field))
        ]
        if len(partial) == 1:
            return partial[0]
        raise SafetyError(f"Hedef bulunamadı veya belirsiz: {name}")

    def _field(self, module: dict[str, Any], name: str) -> dict[str, Any]:
        return self._find_named(
            [*(module.get("fields") or []), *(module.get("archived_fields") or [])],
            name, fields=("label", "field_key"),
        )

    def _action(self, module: dict[str, Any], name: str) -> dict[str, Any] | None:
        items = [*(module.get("actions") or []), *(module.get("archived_actions") or [])]
        try:
            return self._find_named(items, name, fields=("label", "action_key", "action_type"))
        except SafetyError:
            standard = resolve_action_name(name)
            if standard:
                return next((item for item in items if item.get("action_type") == standard.action_type), None)
            return None

    @staticmethod
    def _swap(ids: list[str], index: int, direction: str) -> list[str]:
        if direction not in {"up", "down"}:
            raise SafetyError("Taşıma yönü yalnızca up veya down olabilir.")
        other = index - 1 if direction == "up" else index + 1
        if other < 0 or other >= len(ids):
            return ids
        result = list(ids)
        result[index], result[other] = result[other], result[index]
        return result

    def _translate(self, op: PlanOperation) -> TranslatedOperation | None:
        if op.kind not in self.ALLOWED_KINDS:
            raise SafetyError(f"Asistanın kullanmasına izin verilmeyen işlem: {op.kind}")
        if op.kind == "none":
            return None

        if op.kind == "module_create":
            label = op.label or op.target or op.module
            if not label:
                raise SafetyError("Yeni bölüm adı belirtilmedi.")
            return TranslatedOperation(op, self.platform.preview_change("module.save", {"label": label, "singular_label": label}), "low")

        if op.kind in {"module_archive", "module_restore", "module_move"}:
            module = self._find_module(op.module or op.target, include_inactive=True)
            if op.kind == "module_archive":
                risk = "high" if int(module.get("record_count", 0)) else "medium"
                return TranslatedOperation(op, self.platform.preview_change("module.archive", {"id": module["id"]}), risk)
            if op.kind == "module_restore":
                return TranslatedOperation(op, self.platform.preview_change("module.restore", {"id": module["id"]}), "low")
            active = [m for m in self._modules(False)]
            ids = [m["id"] for m in active]
            if module["id"] not in ids:
                raise SafetyError("Arşivlenmiş bölüm sıralanamaz; önce geri yükleyin.")
            new_ids = self._swap(ids, ids.index(module["id"]), op.direction)
            return TranslatedOperation(op, self.platform.preview_change("modules.reorder", {"ids": new_ids}), "low")

        module = self._find_module(op.module, include_inactive=False)

        if op.kind == "field_add":
            label = op.label or op.target
            if not label:
                raise SafetyError("Yeni alan adı belirtilmedi.")
            dtype = key(op.data_type).replace(" ", "") or "text"
            aliases = {"metin": "text", "uzunmetin": "longtext", "sayi": "number", "para": "money", "tutar": "money", "tarih": "date", "evethayir": "boolean", "secim": "select"}
            dtype = aliases.get(dtype, dtype)
            if dtype not in FIELD_TYPES or dtype == "formula":
                if dtype == "formula":
                    raise SafetyError("Formül alanı AI tarafından tek adımda oluşturulmaz; formül önce ayrıca doğrulanmalıdır.")
                raise SafetyError(f"Desteklenmeyen alan tipi: {op.data_type}")
            payload = {"module_id": module["id"], "label": label, "data_type": dtype}
            return TranslatedOperation(op, self.platform.preview_change("field.save", payload), "low")

        if op.kind in {"field_archive", "field_restore", "field_move", "field_set_property"}:
            field = self._field(module, op.target or op.label)
            if op.kind == "field_archive":
                return TranslatedOperation(op, self.platform.preview_change("field.archive", {"id": field["id"]}), "medium")
            if op.kind == "field_restore":
                return TranslatedOperation(op, self.platform.preview_change("field.restore", {"id": field["id"]}), "low")
            if op.kind == "field_move":
                active = module.get("fields") or []
                ids = [item["id"] for item in active]
                if field["id"] not in ids:
                    raise SafetyError("Arşivlenmiş alan sıralanamaz; önce geri yükleyin.")
                new_ids = self._swap(ids, ids.index(field["id"]), op.direction)
                return TranslatedOperation(op, self.platform.preview_change("fields.reorder", {"module_id": module["id"], "ids": new_ids}), "low")
            prop = key(op.label or "")
            allowed = {"gorunur": "visible", "gizli": "visible", "zorunlu": "required", "filtrelenebilir": "filterable", "ad": "label", "baslik": "label", "genislik": "width"}
            db_prop = allowed.get(prop, prop if prop in {"visible", "required", "filterable", "label", "width"} else "")
            if not db_prop:
                raise SafetyError("AI bu alan özelliğini değiştiremez.")
            updated = {**field, "module_id": module["id"]}
            value = op.value
            if db_prop in {"visible", "required", "filterable"}:
                if isinstance(value, str):
                    value = key(value) in {"1", "true", "evet", "acik", "goster", "zorunlu"}
                value = bool(value)
                if prop == "gizli":
                    value = False
            if db_prop == "width":
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise SafetyError("Alan genişliği sayı olmalıdır.") from exc
            if db_prop == "label":
                value = str(value or "").strip()
                if not value:
                    raise SafetyError("Alan adı boş olamaz.")
            updated[db_prop] = value
            return TranslatedOperation(op, self.platform.preview_change("field.save", updated), "low")

        if op.kind in {"action_add_or_move", "action_archive", "action_restore", "action_move"}:
            action = self._action(module, op.target or op.label)
            standard = resolve_action_name(op.target or op.label)
            if op.kind == "action_add_or_move":
                if not standard and not action:
                    raise SafetyError("Yalnızca LOGOS güvenli eylem kataloğundaki butonlar eklenebilir.")
                placement = resolve_placement(op.placement) or (action or {}).get("placement") or (standard.placement if standard else "")
                if action:
                    payload = {**action, "module_id": module["id"], "placement": placement, "active": True}
                else:
                    assert standard is not None
                    payload = {
                        "module_id": module["id"], "action_key": standard.action_key, "label": standard.label,
                        "action_type": standard.action_type, "placement": placement, "icon": standard.icon,
                        "style": standard.style, "confirmation_text": standard.confirmation_text, "active": True,
                    }
                return TranslatedOperation(op, self.platform.preview_change("action.save", payload), "low")
            if not action:
                raise SafetyError("Belirtilen eylem bu bölümde bulunamadı.")
            if op.kind == "action_archive":
                return TranslatedOperation(op, self.platform.preview_change("action.archive", {"id": action["id"]}), "medium")
            if op.kind == "action_restore":
                return TranslatedOperation(op, self.platform.preview_change("action.restore", {"id": action["id"]}), "low")
            active = module.get("actions") or []
            ids = [item["id"] for item in active]
            if action["id"] not in ids:
                raise SafetyError("Arşivlenmiş eylem sıralanamaz; önce geri yükleyin.")
            new_ids = self._swap(ids, ids.index(action["id"]), op.direction)
            return TranslatedOperation(op, self.platform.preview_change("actions.reorder", {"module_id": module["id"], "ids": new_ids}), "low")

        raise SafetyError(f"İşlem henüz çevrilemedi: {op.kind}")

    def preview_plan(self, plan: ChangePlan) -> dict[str, Any]:
        if len(plan.operations) > self.MAX_PLAN_OPERATIONS:
            raise SafetyError(f"Tek seferde en fazla {self.MAX_PLAN_OPERATIONS} değişiklik planlanabilir. İşlemi parçalara bölün.")

        # 5.0 plan ön izlemesi gerçek DB üzerinde değil, SQLite'ın tutarlı backup API'siyle
        # oluşturulmuş geçici bir kopya üzerinde sırasıyla denenir. Böylece "yeni bölüm oluştur
        # + alan ekle + butonu taşı" gibi birbirine bağlı işlemler dahi gerçek veriye dokunmadan
        # baştan sona doğrulanabilir.
        translated: list[TranslatedOperation] = []
        with tempfile.TemporaryDirectory(prefix="logos_plan_") as temp_name:
            temp_dir = Path(temp_name)
            shadow_path = temp_dir / "okul_guvenligi.db"
            source = self.db.connect()
            dest = sqlite3.connect(shadow_path)
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
                source.close()
            shadow_db = self.db.__class__(temp_dir)
            shadow_platform = self.platform.__class__(shadow_db)
            shadow_safety = SafetyEngine(shadow_db, shadow_platform, self.health)
            for semantic in plan.operations:
                item = shadow_safety._translate(semantic)
                if not item:
                    continue
                translated.append(item)
                # Sonraki adımlar güncellenmiş gölge durumunu görsün.
                shadow_platform.apply_change(item.preview)
            if shadow_db.integrity_check() != "ok":
                raise SafetyError("Güvenli ön izleme kopyası bütünlük kontrolünden geçemedi.")

        if not translated:
            return {
                "assistant_only": True,
                "executable": False,
                "summary": plan.assistant_text or plan.message or "Programda değişiklik gerektiren güvenli bir işlem bulunmadı.",
                "impact": 0,
                "warnings": [],
                "suggestions": [],
                "source": plan.source,
                "vision_used": plan.vision_used,
            }
        max_risk = max((item.risk for item in translated), key=lambda value: RISK_SCORE[value])
        impact = sum(max(0, int(item.preview.get("impact", 0) or 0)) for item in translated)
        warnings: list[str] = []
        for item in translated:
            warnings.extend(str(w) for w in item.preview.get("warnings", []) if w)
        if max_risk in {"high", "critical"}:
            warnings.insert(0, "Bu plan yüksek etkili işlem içeriyor; yayınlama öncesinde otomatik tam veritabanı yedeği alınacak.")
        return {
            "assistant_only": False,
            "executable": True,
            "summary": plan.message or plan.assistant_text or f"{len(translated)} güvenli değişiklik hazırlandı.",
            "assistant_text": plan.assistant_text,
            "impact": impact,
            "warnings": list(dict.fromkeys(warnings)),
            "risk": max_risk,
            "requires_double_confirmation": max_risk in {"high", "critical"},
            "source": plan.source,
            "vision_used": plan.vision_used,
            "operations": [
                {
                    "semantic": item.semantic.to_dict(),
                    "summary": item.preview.get("summary", ""),
                    "impact": item.preview.get("impact", 0),
                    "warnings": item.preview.get("warnings", []),
                    "risk": item.risk,
                }
                for item in translated
            ],
        }

    def commit_plan(self, preview: dict[str, Any]) -> dict[str, Any]:
        if not preview.get("executable"):
            raise SafetyError("Bu plan uygulanabilir bir değişiklik içermiyor.")
        self.health.require_writable()
        operations = preview.get("operations") or []
        if not isinstance(operations, list) or not operations:
            raise SafetyError("Uygulanacak güvenli işlem bulunamadı.")
        backup_path = self.db.backup("logos_asistan_plan_oncesi", retain=40)
        results: list[Any] = []
        try:
            # Ön izlemede kullanılan semantik işlemler gerçek durumda tekrar çözülür. Böylece yeni
            # oluşturulan modül/alan kimlikleri sonraki işlemlere güvenli biçimde aktarılır.
            for item in operations:
                semantic = PlanOperation.from_dict(item.get("semantic") or {})
                translated = self._translate(semantic)
                if not translated:
                    continue
                results.append(self.platform.apply_change(translated.preview))
            integrity = self.db.integrity_check()
            if integrity != "ok":
                raise SafetyError(f"Yayın sonrası bütünlük kontrolü başarısız: {integrity}")
            return {
                "ok": True,
                "summary": preview.get("summary", "Değişiklikler uygulandı."),
                "result_count": len(results),
                "backup": backup_path.name,
                "integrity": integrity,
                "results": results,
            }
        except Exception as exc:
            try:
                self.db.restore_backup(backup_path.name)
            except Exception as restore_exc:
                raise SafetyError(
                    f"Değişiklik tamamlanamadı ve otomatik geri dönüş de başarısız oldu. "
                    f"Programı kapatın; yedek: {backup_path.name}. Hata: {exc}; geri dönüş: {restore_exc}"
                ) from restore_exc
            raise SafetyError(f"Değişiklik tamamlanamadı; program otomatik olarak işlem öncesi yedeğe döndü. Hata: {exc}") from exc

