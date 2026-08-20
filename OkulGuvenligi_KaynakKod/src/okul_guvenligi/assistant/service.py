from __future__ import annotations

from typing import Any

from ..core.models import ChangePlan, PlanOperation
from ..core.safety import SafetyEngine, SafetyError
from .openai_vision import OpenAIUnavailable, OpenAIVisionPlanner


class LogosAssistantService:
    """OpenAI aklı + yerel LOGOS güvenlik motoru + çevrimdışı geri dönüş."""

    def __init__(self, db, platform, health, *, planner: OpenAIVisionPlanner | None = None):
        self.db = db
        self.platform = platform
        self.health = health
        self.planner = planner or OpenAIVisionPlanner()
        self.safety = SafetyEngine(db, platform, health)

    def status(self) -> dict[str, Any]:
        return {**self.planner.status(), "safe_core": True, "direct_code_access": False, "direct_sql_access": False}

    def _legacy_to_plan(self, command: str) -> ChangePlan | None:
        """4.5 yerel sözlüğünü AI yokken de kullanılabilir tutar."""
        preview = self.platform.preview_command(command)
        if preview.get("assistant_only"):
            return ChangePlan(message=preview.get("summary", ""), assistant_text=preview.get("summary", ""), source="local")
        op = str(preview.get("operation", ""))
        payload = preview.get("payload", {}) or {}
        # Yerel eski plan zaten PlatformEngine tarafından güvenli şekilde doğrulandı. Bu işaret
        # doğrudan semantik çevrilemeyen legacy komutları kaybetmemek için service seviyesinde tutulur.
        plan = ChangePlan(message=preview.get("summary", ""), assistant_text="Yerel güvenli komut motoru tarafından anlaşıldı.", source="local")
        setattr(plan, "_legacy_preview", preview)  # yalnız proses içi, kullanıcı girdisi değil
        return plan

    def preview(self, message: str, *, image_data_url: str = "", screen_context: dict[str, Any] | None = None) -> dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            return {
                "assistant_only": True, "executable": False,
                "summary": "Ne yapmak istediğinizi yazın. İsterseniz ekran görüntüsü de ekleyebilirsiniz.",
                "impact": 0, "warnings": [],
                "suggestions": ["Stok bölümünde Sil butonunu sayfa üstünde göster", "Stok bölümüne Raf No alanı ekle"],
                "source": "local", "vision_used": False,
            }
        if image_data_url and not self.planner.configured:
            return {
                "assistant_only": True, "executable": False,
                "summary": "Görseli değerlendirmek için OpenAI bağlantısı gerekiyor; programda hiçbir değişiklik yapılmadı.",
                "impact": 0,
                "warnings": ["OPENAI_API_KEY yapılandırılmadığı için görsel OpenAI'ye gönderilmedi."],
                "suggestions": ["Görselsiz komutu yazın", "OpenAI bağlantısını yapılandırın"],
                "source": "local", "vision_used": False,
            }
        if self.planner.configured:
            try:
                plan = self.planner.plan(text, image_data_url=image_data_url, screen_context=screen_context)
                return self.safety.preview_plan(plan)
            except (OpenAIUnavailable, ValueError, SafetyError) as exc:
                if image_data_url:
                    return {
                        "assistant_only": True, "executable": False,
                        "summary": "Görsel destekli plan güvenli biçimde oluşturulamadı; programda hiçbir değişiklik yapılmadı.",
                        "impact": 0, "warnings": [str(exc)],
                        "suggestions": ["Daha kısa bir komut yazın", "Hedef bölümü adıyla belirtin"],
                        "source": "openai", "vision_used": True,
                    }
                # Metin komutunda ağ/model sorunu olursa çevrimdışı asistan devralır.
        legacy = self._legacy_to_plan(text)
        if legacy is None:
            raise SafetyError("Komut planlanamadı.")
        legacy_preview = getattr(legacy, "_legacy_preview", None)
        if legacy_preview:
            return {**legacy_preview, "source": "local", "vision_used": False, "legacy": True}
        return {
            "assistant_only": True, "executable": False,
            "summary": legacy.assistant_text or legacy.message,
            "impact": 0, "warnings": [], "suggestions": ["yardım"],
            "source": "local", "vision_used": False,
        }

    def commit(self, preview: dict[str, Any]) -> dict[str, Any]:
        if preview.get("legacy"):
            # legacy ön izleme zaten PlatformEngine çıktısıdır; yine sağlık kontrolü + yedek altında çalıştır.
            self.health.require_writable()
            backup = self.db.backup("logos_asistan_legacy_oncesi", retain=40)
            try:
                result = self.platform.apply_change(preview)
                if self.db.integrity_check() != "ok":
                    raise SafetyError("Yayın sonrası bütünlük kontrolü başarısız.")
                return {"ok": True, "summary": preview.get("summary", "Değişiklik uygulandı."), "backup": backup.name, "result_count": 1, "results": [result]}
            except Exception as exc:
                self.db.restore_backup(backup.name)
                raise SafetyError(f"İşlem başarısız oldu; otomatik geri dönüş yapıldı. Hata: {exc}") from exc
        return self.safety.commit_plan(preview)
