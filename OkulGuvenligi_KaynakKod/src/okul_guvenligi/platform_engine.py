from __future__ import annotations

import ast
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .database import Database, DatabaseError, normalize_text, search_key, utc_now


PLATFORM_SCHEMA_VERSION = 7
FIELD_TYPES = {
    "text", "longtext", "number", "money", "date", "boolean",
    "select", "multiselect", "relation", "formula",
}
AGGREGATES = {"none", "sum", "avg", "min", "max", "count"}
RULE_OPERATORS = {"eq", "ne", "contains", "gt", "gte", "lt", "lte", "empty", "not_empty"}
ACTION_TYPES = {"new_record", "edit_record", "save_record", "archive_record", "duplicate_record", "export_xlsx", "cancel_form"}
ACTION_PLACEMENTS = {"page_top", "row", "form_header", "form_footer"}
ACTION_STYLES = {"primary", "secondary", "ghost", "danger"}
ACTION_PLACEMENT_RULES = {
    "new_record": {"page_top"},
    "export_xlsx": {"page_top"},
    "edit_record": {"page_top", "row"},
    "archive_record": {"page_top", "row"},
    "duplicate_record": {"page_top", "row"},
    "save_record": {"form_header", "form_footer"},
    "cancel_form": {"form_header", "form_footer"},
}


PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_modules (
    id TEXT PRIMARY KEY,
    module_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    singular_label TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT '▦',
    description TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#15865d',
    menu_visible INTEGER NOT NULL DEFAULT 1 CHECK(menu_visible IN (0,1)),
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dynamic_module_fields (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL REFERENCES dynamic_modules(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    field_key TEXT NOT NULL,
    label TEXT NOT NULL,
    data_type TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    relation_target TEXT NOT NULL DEFAULT '',
    formula TEXT NOT NULL DEFAULT '',
    default_value TEXT NOT NULL DEFAULT '',
    placeholder TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0,1)),
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
    filterable INTEGER NOT NULL DEFAULT 1 CHECK(filterable IN (0,1)),
    is_title INTEGER NOT NULL DEFAULT 0 CHECK(is_title IN (0,1)),
    aggregate_type TEXT NOT NULL DEFAULT 'none',
    decimal_places INTEGER NOT NULL DEFAULT 2,
    width INTEGER NOT NULL DEFAULT 160,
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(module_id, field_key)
);

CREATE TABLE IF NOT EXISTS dynamic_records (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL REFERENCES dynamic_modules(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    title TEXT NOT NULL DEFAULT '',
    values_json TEXT NOT NULL DEFAULT '{}',
    search_text TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dynamic_views (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL REFERENCES dynamic_modules(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    name TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    columns_json TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(module_id, name)
);

CREATE TABLE IF NOT EXISTS dynamic_rules (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL REFERENCES dynamic_modules(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    name TEXT NOT NULL,
    trigger_name TEXT NOT NULL DEFAULT 'save',
    conditions_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dynamic_actions (
    id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL REFERENCES dynamic_modules(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    action_key TEXT NOT NULL,
    label TEXT NOT NULL,
    action_type TEXT NOT NULL,
    placement TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT '',
    style TEXT NOT NULL DEFAULT 'secondary',
    confirmation_text TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(module_id, action_key)
);

CREATE TABLE IF NOT EXISTS platform_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'Yerel Kullanıcı',
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT 'null',
    after_json TEXT NOT NULL DEFAULT 'null'
);

CREATE INDEX IF NOT EXISTS idx_dynamic_modules_order ON dynamic_modules(active, sort_order, label);
CREATE INDEX IF NOT EXISTS idx_dynamic_fields_module ON dynamic_module_fields(module_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_dynamic_records_module ON dynamic_records(module_id, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dynamic_records_search ON dynamic_records(module_id, search_text);
CREATE INDEX IF NOT EXISTS idx_dynamic_rules_module ON dynamic_rules(module_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_dynamic_actions_module ON dynamic_actions(module_id, active, placement, sort_order);
CREATE INDEX IF NOT EXISTS idx_platform_history_time ON platform_history(occurred_at DESC);
"""


DEFAULT_MODULES = [
    {
        "module_key": "servis", "label": "Servis", "singular_label": "Servis Kaydı", "icon": "⌁",
        "description": "Teknik servis talepleri, görevlendirme, maliyet ve çözüm takibi.", "color": "#2469d8",
        "fields": [
            ("kayit_no", "Servis No", "text", {"required": True, "is_title": True, "width": 130}),
            ("kurum", "Kurum", "relation", {"relation_target": "institutions", "required": True, "width": 260}),
            ("durum", "Durum", "select", {"options": ["YENİ", "PLANLANDI", "İŞLEMDE", "BEKLEMEDE", "TAMAMLANDI", "İPTAL"], "default_value": "YENİ"}),
            ("oncelik", "Öncelik", "select", {"options": ["DÜŞÜK", "NORMAL", "YÜKSEK", "ACİL"], "default_value": "NORMAL"}),
            ("acilis_tarihi", "Açılış Tarihi", "date", {"required": True}),
            ("teknik_personel", "Teknik Personel", "text", {}),
            ("sorun", "Sorun", "longtext", {"required": True, "width": 280}),
            ("cozum", "Çözüm", "longtext", {"width": 280}),
            ("malzeme_tutari", "Malzeme Tutarı", "money", {"aggregate_type": "sum"}),
            ("iscilik_tutari", "İşçilik Tutarı", "money", {"aggregate_type": "sum"}),
            ("toplam_maliyet", "Toplam Maliyet", "formula", {"formula": "malzeme_tutari + iscilik_tutari", "aggregate_type": "sum"}),
            ("kapanis_tarihi", "Kapanış Tarihi", "date", {}),
        ],
    },
    {
        "module_key": "sozlesmeler", "label": "Sözleşmeler", "singular_label": "Sözleşme", "icon": "▤",
        "description": "Kurum sözleşmeleri, tarihler, tahsilat ve kalan bakiye.", "color": "#7553b8",
        "fields": [
            ("sozlesme_no", "Sözleşme No", "text", {"required": True, "is_title": True}),
            ("kurum", "Kurum", "relation", {"relation_target": "institutions", "required": True, "width": 260}),
            ("baslangic", "Başlangıç", "date", {"required": True}),
            ("bitis", "Bitiş", "date", {}),
            ("durum", "Durum", "select", {"options": ["TASLAK", "AKTİF", "SÜRESİ DOLDU", "İPTAL"], "default_value": "AKTİF"}),
            ("sozlesme_tutari", "Sözleşme Tutarı", "money", {"aggregate_type": "sum"}),
            ("tahsil_edilen", "Tahsil Edilen", "money", {"aggregate_type": "sum"}),
            ("bakiye", "Bakiye", "formula", {"formula": "sozlesme_tutari - tahsil_edilen", "aggregate_type": "sum"}),
            ("yenileme_notu", "Yenileme Notu", "longtext", {"width": 260}),
        ],
    },
    {
        "module_key": "stok", "label": "Stok", "singular_label": "Stok Kartı", "icon": "▣",
        "description": "Ürün, giriş, çıkış, mevcut stok ve kritik seviye takibi.", "color": "#d17a16",
        "fields": [
            ("urun_kodu", "Ürün Kodu", "text", {"required": True}),
            ("urun_adi", "Ürün Adı", "text", {"required": True, "is_title": True, "width": 260}),
            ("kategori", "Kategori", "select", {"options": ["TURNİKE", "KART", "PANEL", "KAMERA", "AĞ", "DİĞER"]}),
            ("birim", "Birim", "select", {"options": ["ADET", "PAKET", "METRE", "KUTU"], "default_value": "ADET"}),
            ("giris", "Toplam Giriş", "number", {"aggregate_type": "sum", "decimal_places": 0}),
            ("cikis", "Toplam Çıkış", "number", {"aggregate_type": "sum", "decimal_places": 0}),
            ("mevcut_stok", "Mevcut Stok", "formula", {"formula": "giris - cikis", "aggregate_type": "sum", "decimal_places": 0}),
            ("kritik_seviye", "Kritik Seviye", "number", {"decimal_places": 0}),
            ("kritik", "Kritik mi?", "formula", {"formula": "EGER(mevcut_stok <= kritik_seviye, 1, 0)", "decimal_places": 0}),
            ("alis_fiyati", "Alış Fiyatı", "money", {}),
            ("stok_degeri", "Stok Değeri", "formula", {"formula": "mevcut_stok * alis_fiyati", "aggregate_type": "sum"}),
            ("depo", "Depo / Konum", "text", {}),
        ],
    },
]


DEFAULT_ACTIONS = [
    ("new_record", "Yeni Kayıt", "new_record", "page_top", "＋", "primary", ""),
    ("export_xlsx", "Excel’e İndir", "export_xlsx", "page_top", "⇩", "secondary", ""),
    ("edit_record", "Düzenle", "edit_record", "row", "✎", "secondary", ""),
    ("duplicate_record", "Kopyala", "duplicate_record", "row", "⧉", "secondary", ""),
    ("archive_record", "Sil", "archive_record", "row", "⌫", "danger", "Bu kayıt arşive alınsın mı? Kayıt geri yüklenebilir ve geçmiş korunur."),
    ("cancel_form", "Vazgeç", "cancel_form", "form_footer", "×", "ghost", ""),
    ("save_record", "Kaydet", "save_record", "form_footer", "✓", "primary", ""),
]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str, fallback: Any) -> Any:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return result


def _slug(value: Any, fallback: str = "alan") -> str:
    key = search_key(value).replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", key).strip("_") or fallback


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    text = normalize_text(value)
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        return Decimal("0")


def _strict_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    text = normalize_text(value)
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise DatabaseError(f"{label} geçerli bir sayı olmalıdır.") from exc


class SafeFormula:
    """Serbest kod çalıştırmadan satır formüllerini değerlendiren küçük motor."""

    FUNCTIONS = {"EGER", "IFX", "YUVARLA", "ROUND", "MIN", "MAX", "MUTLAK", "ABS", "BOSSA", "COALESCE"}

    @classmethod
    def names(cls, expression: str) -> set[str]:
        try:
            tree = ast.parse(expression or "0", mode="eval")
        except SyntaxError as exc:
            raise DatabaseError("Formül yazımı geçersiz.") from exc
        return {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id.upper() not in cls.FUNCTIONS
        }

    @classmethod
    def evaluate(cls, expression: str, values: dict[str, Any]) -> Decimal:
        try:
            tree = ast.parse(expression or "0", mode="eval")
        except SyntaxError as exc:
            raise DatabaseError("Formül yazımı geçersiz.") from exc

        def visit(node: ast.AST) -> Any:
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool, str)):
                return node.value if isinstance(node.value, str) else _decimal(node.value)
            if isinstance(node, ast.Name):
                return values.get(node.id, 0)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
                value = visit(node.operand)
                if isinstance(node.op, ast.Not):
                    return not bool(value)
                return _decimal(value) if isinstance(node.op, ast.UAdd) else -_decimal(value)
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = _decimal(visit(node.left)), _decimal(visit(node.right))
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                return Decimal("0") if right == 0 else left / right
            if isinstance(node, ast.Compare):
                left = visit(node.left)
                for operator, comparator in zip(node.ops, node.comparators):
                    right = visit(comparator)
                    if isinstance(operator, ast.Eq): ok = left == right
                    elif isinstance(operator, ast.NotEq): ok = left != right
                    elif isinstance(operator, ast.Lt): ok = _decimal(left) < _decimal(right)
                    elif isinstance(operator, ast.LtE): ok = _decimal(left) <= _decimal(right)
                    elif isinstance(operator, ast.Gt): ok = _decimal(left) > _decimal(right)
                    elif isinstance(operator, ast.GtE): ok = _decimal(left) >= _decimal(right)
                    else: raise DatabaseError("Formüldeki karşılaştırma desteklenmiyor.")
                    if not ok: return False
                    left = right
                return True
            if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
                results = [bool(visit(item)) for item in node.values]
                return all(results) if isinstance(node.op, ast.And) else any(results)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
                name = node.func.id.upper()
                if name not in cls.FUNCTIONS:
                    raise DatabaseError(f"Formülde {node.func.id} işlevine izin verilmez.")
                args = [visit(item) for item in node.args]
                if name in {"EGER", "IFX"} and len(args) == 3:
                    return args[1] if bool(args[0]) else args[2]
                if name in {"YUVARLA", "ROUND"} and args:
                    places = max(0, min(6, int(_decimal(args[1])) if len(args) > 1 else 2))
                    return _decimal(args[0]).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)
                if name == "MIN" and args: return min(_decimal(item) for item in args)
                if name == "MAX" and args: return max(_decimal(item) for item in args)
                if name in {"MUTLAK", "ABS"} and len(args) == 1: return abs(_decimal(args[0]))
                if name in {"BOSSA", "COALESCE"} and args:
                    return next((item for item in args if normalize_text(item) not in {"", "0"}), 0)
            raise DatabaseError("Formülde yalnızca alanlar, sayılar, + - * /, karşılaştırma ve güvenli işlevler kullanılabilir.")

        return _decimal(visit(tree)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class PlatformEngine:
    def __init__(self, database: Database):
        self.db = database
        self.initialize()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def initialize(self) -> None:
        with self.db.connect() as conn:
            current = int(conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
            missing = not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dynamic_modules'").fetchone()
        if current < PLATFORM_SCHEMA_VERSION or missing:
            self.db.backup("dinamik_platform_v3_oncesi")
        with self.db.connect() as conn:
            conn.executescript(PLATFORM_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)", (PLATFORM_SCHEMA_VERSION, utc_now()))
            count = int(conn.execute("SELECT COUNT(*) FROM dynamic_modules").fetchone()[0])
        if not count:
            self._seed_defaults()
        self._ensure_default_actions()

    def _seed_defaults(self) -> None:
        with self.db.transaction() as conn:
            now = utc_now()
            for order, definition in enumerate(DEFAULT_MODULES, start=1):
                module_id = _new_id("mod")
                conn.execute(
                    """INSERT INTO dynamic_modules
                       (id,module_key,label,singular_label,icon,description,color,menu_visible,sort_order,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (module_id, definition["module_key"], definition["label"], definition["singular_label"],
                     definition["icon"], definition["description"], definition["color"], 1, order * 10, now, now),
                )
                for field_order, (key, label, data_type, settings) in enumerate(definition["fields"], start=1):
                    options = settings.get("options", [])
                    conn.execute(
                        """INSERT INTO dynamic_module_fields
                           (id,module_id,field_key,label,data_type,options_json,relation_target,formula,default_value,
                            placeholder,required,visible,filterable,is_title,aggregate_type,decimal_places,width,
                            sort_order,active,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (_new_id("fld"), module_id, key, label, data_type, _json(options),
                         settings.get("relation_target", ""), settings.get("formula", ""),
                         str(settings.get("default_value", "")), settings.get("placeholder", ""),
                         int(bool(settings.get("required"))), int(settings.get("visible", True)),
                         int(settings.get("filterable", True)), int(bool(settings.get("is_title"))),
                         settings.get("aggregate_type", "none"), int(settings.get("decimal_places", 2)),
                         int(settings.get("width", 160)), field_order * 10, now, now),
                    )
                self._history(conn, "module", module_id, "seed", f"{definition['label']} başlangıç modülü oluşturuldu", None, definition)

    def _ensure_default_actions(self) -> None:
        """Eylem motoruna ilk kez geçen modüllere güvenli varsayılan düğmeleri ekler."""
        with self.db.transaction() as conn:
            now = utc_now()
            modules = [dict(row) for row in conn.execute("SELECT id,label FROM dynamic_modules")]
            for module in modules:
                exists = int(conn.execute("SELECT COUNT(*) FROM dynamic_actions WHERE module_id=?", (module["id"],)).fetchone()[0])
                if exists:
                    continue
                for order, (action_key, label, action_type, placement, icon, style, confirmation) in enumerate(DEFAULT_ACTIONS, start=1):
                    conn.execute(
                        """INSERT INTO dynamic_actions
                           (id,module_id,action_key,label,action_type,placement,icon,style,confirmation_text,config_json,
                            sort_order,active,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,'{}',?,1,?,?)""",
                        (_new_id("act"), module["id"], action_key, label, action_type, placement, icon, style,
                         confirmation, order * 10, now, now),
                    )

    def _history(self, conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str,
                 summary: str, before: Any, after: Any, actor: str = "Yerel Kullanıcı") -> None:
        conn.execute(
            "INSERT INTO platform_history(occurred_at,actor,entity_type,entity_id,action,summary,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
            (utc_now(), actor, entity_type, entity_id, action, summary, _json(before), _json(after)),
        )

    def _module_row(self, conn: sqlite3.Connection, module_ref: str, include_inactive: bool = False) -> dict[str, Any]:
        sql = "SELECT * FROM dynamic_modules WHERE (id=? OR module_key=?)"
        if not include_inactive:
            sql += " AND active=1"
        row = conn.execute(sql, (module_ref, module_ref)).fetchone()
        if not row:
            raise FileNotFoundError("Dinamik modül bulunamadı.")
        return dict(row)

    def _field_rows(self, conn: sqlite3.Connection, module_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dynamic_module_fields WHERE module_id=?"
        if not include_inactive:
            sql += " AND active=1"
        sql += " ORDER BY sort_order,label"
        rows = [dict(row) for row in conn.execute(sql, (module_id,))]
        for row in rows:
            row["options"] = _load(row.pop("options_json"), [])
            for key in ("required", "visible", "filterable", "is_title", "active"):
                row[key] = bool(row[key])
        return rows

    def _rule_rows(self, conn: sqlite3.Connection, module_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dynamic_rules WHERE module_id=?"
        if not include_inactive:
            sql += " AND active=1"
        sql += " ORDER BY sort_order,name"
        rows = [dict(row) for row in conn.execute(sql, (module_id,))]
        for row in rows:
            row["conditions"] = _load(row.pop("conditions_json"), [])
            row["actions"] = _load(row.pop("actions_json"), [])
            row["active"] = bool(row["active"])
        return rows

    def _action_rows(self, conn: sqlite3.Connection, module_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dynamic_actions WHERE module_id=?"
        if not include_inactive:
            sql += " AND active=1"
        sql += " ORDER BY placement,sort_order,label"
        rows = [dict(row) for row in conn.execute(sql, (module_id,))]
        for row in rows:
            row["config"] = _load(row.pop("config_json"), {})
            row["active"] = bool(row["active"])
        return rows

    def get_module(self, module_ref: str, include_inactive: bool = False) -> dict[str, Any]:
        with self.db.connect() as conn:
            module = self._module_row(conn, module_ref, include_inactive)
            all_fields = self._field_rows(conn, module["id"], True)
            all_rules = self._rule_rows(conn, module["id"], True)
            all_actions = self._action_rows(conn, module["id"], True)
            module["fields"] = [item for item in all_fields if item["active"]]
            module["archived_fields"] = [item for item in all_fields if not item["active"]]
            module["rules"] = [item for item in all_rules if item["active"]]
            module["archived_rules"] = [item for item in all_rules if not item["active"]]
            module["actions"] = [item for item in all_actions if item["active"]]
            module["archived_actions"] = [item for item in all_actions if not item["active"]]
            module["record_count"] = int(conn.execute(
                "SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)
            ).fetchone()[0])
            for key in ("menu_visible", "active"):
                module[key] = bool(module[key])
            return module

    def list_modules(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            sql = "SELECT * FROM dynamic_modules"
            if not include_inactive:
                sql += " WHERE active=1"
            sql += " ORDER BY sort_order,label"
            result = []
            for raw in conn.execute(sql):
                module = dict(raw)
                all_fields = self._field_rows(conn, module["id"], True)
                all_rules = self._rule_rows(conn, module["id"], True)
                all_actions = self._action_rows(conn, module["id"], True)
                module["fields"] = [item for item in all_fields if item["active"]]
                module["archived_fields"] = [item for item in all_fields if not item["active"]]
                module["rules"] = [item for item in all_rules if item["active"]]
                module["archived_rules"] = [item for item in all_rules if not item["active"]]
                module["actions"] = [item for item in all_actions if item["active"]]
                module["archived_actions"] = [item for item in all_actions if not item["active"]]
                module["record_count"] = int(conn.execute(
                    "SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)
                ).fetchone()[0])
                module["menu_visible"] = bool(module["menu_visible"])
                module["active"] = bool(module["active"])
                result.append(module)
            return result

    def bootstrap(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            institutions = [dict(row) for row in conn.execute(
                "SELECT id,name,city,district FROM institutions WHERE active=1 ORDER BY name"
            )]
        all_modules = self.list_modules(True)
        return {
            "modules": [item for item in all_modules if item["active"]],
            "archived_modules": [item for item in all_modules if not item["active"]],
            "relations": {"institutions": institutions},
            "core_modules": [
                {"key": "institutions", "label": "Kurumlar", "icon": "▦", "description": "Kurum, grup, panel ve turnike ana kayıtları."},
                {"key": "finance", "label": "Finans", "icon": "₺", "description": "Dinamik finans alanları ve hesaplamalar."},
                {"key": "commissions", "label": "Prim", "icon": "%", "description": "Koşullu prim kuralları ve sonuçları."},
            ],
        }

    def _sanitize_module(self, data: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        label = normalize_text(data.get("label", current["label"] if current else ""))
        if not label:
            raise DatabaseError("Modül adı zorunludur.")
        # Teknik anahtar bağlantıları korur; yalnızca yeni modül oluşturulurken üretilir.
        key = current["module_key"] if current else _slug(data.get("module_key", label), "modul")
        color = normalize_text(data.get("color", current["color"] if current else "#15865d"))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise DatabaseError("Modül rengi geçersiz.")
        return {
            "id": current["id"] if current else normalize_text(data.get("id")),
            "module_key": key,
            "label": label,
            "singular_label": normalize_text(data.get("singular_label", current["singular_label"] if current else label)) or label,
            "icon": normalize_text(data.get("icon", current["icon"] if current else "▦"))[:4] or "▦",
            "description": normalize_text(data.get("description", current["description"] if current else "")),
            "color": color,
            "menu_visible": bool(data.get("menu_visible", current["menu_visible"] if current else True)),
            "sort_order": max(0, int(data.get("sort_order", current["sort_order"] if current else 100))),
            "active": bool(data.get("active", current["active"] if current else True)),
        }

    def _sanitize_field(self, data: dict[str, Any], module_id: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
        label = normalize_text(data.get("label", current["label"] if current else ""))
        if not label:
            raise DatabaseError("Alan adı zorunludur.")
        # Görünen ad serbestçe değişebilir, teknik anahtar formül ve eski değerleri korumak için sabittir.
        field_key = current["field_key"] if current else _slug(data.get("field_key", label), "alan")
        data_type = normalize_text(data.get("data_type", current["data_type"] if current else "text"))
        if data_type not in FIELD_TYPES:
            raise DatabaseError("Alan türü desteklenmiyor.")
        aggregate = normalize_text(data.get("aggregate_type", current["aggregate_type"] if current else "none"))
        if aggregate not in AGGREGATES:
            aggregate = "none"
        options = data.get("options", current.get("options", []) if current else [])
        if isinstance(options, str):
            options = [normalize_text(item) for item in options.split(",") if normalize_text(item)]
        if not isinstance(options, list):
            options = []
        options = list(dict.fromkeys(normalize_text(item) for item in options if normalize_text(item)))[:200]
        result = {
            "id": current["id"] if current else normalize_text(data.get("id")),
            "module_id": module_id,
            "field_key": field_key,
            "label": label,
            "data_type": data_type,
            "options": options,
            "relation_target": normalize_text(data.get("relation_target", current["relation_target"] if current else "")),
            "formula": normalize_text(data.get("formula", current["formula"] if current else "")),
            "default_value": normalize_text(data.get("default_value", current["default_value"] if current else "")),
            "placeholder": normalize_text(data.get("placeholder", current["placeholder"] if current else "")),
            "required": bool(data.get("required", current["required"] if current else False)),
            "visible": bool(data.get("visible", current["visible"] if current else True)),
            "filterable": bool(data.get("filterable", current["filterable"] if current else True)),
            "is_title": bool(data.get("is_title", current["is_title"] if current else False)),
            "aggregate_type": aggregate,
            "decimal_places": max(0, min(6, int(data.get("decimal_places", current["decimal_places"] if current else 2)))),
            "width": max(80, min(520, int(data.get("width", current["width"] if current else 160)))),
            "sort_order": max(0, int(data.get("sort_order", current["sort_order"] if current else 100))),
            "active": bool(data.get("active", current["active"] if current else True)),
        }
        if data_type == "formula" and not result["formula"]:
            raise DatabaseError("Formül alanında formül zorunludur.")
        if data_type == "relation" and not result["relation_target"]:
            raise DatabaseError("İlişki alanında hedef modül seçilmelidir.")
        return result

    def _validate_formula_graph(self, fields: list[dict[str, Any]]) -> None:
        active = {field["field_key"]: field for field in fields if field.get("active", True)}
        graph: dict[str, set[str]] = {}
        for key, field in active.items():
            if field["data_type"] != "formula":
                continue
            names = SafeFormula.names(field["formula"])
            missing = sorted(name for name in names if name not in active)
            if missing:
                raise DatabaseError(f"{field['label']} formülünde bulunmayan alan: {', '.join(missing)}")
            graph[key] = {name for name in names if active[name]["data_type"] == "formula"}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise DatabaseError("Formüller birbirini döngüsel olarak çağırıyor.")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph.get(key, set()):
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)

    def preview_change(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        operation = normalize_text(operation)
        with self.db.connect() as conn:
            if operation == "module.save":
                current = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (payload.get("id", ""),)).fetchone())
                clean = self._sanitize_module(payload, current)
                duplicate = conn.execute("SELECT id FROM dynamic_modules WHERE module_key=? AND id<>?", (clean["module_key"], clean.get("id") or "")).fetchone()
                if duplicate:
                    raise DatabaseError("Bu modül anahtarı zaten kullanılıyor.")
                summary = f"{clean['label']} modülü {'güncellenecek' if current else 'oluşturulacak'}."
                return {"operation": operation, "payload": clean, "summary": summary, "impact": 0, "warnings": []}
            if operation == "module.archive":
                module = self._module_row(conn, str(payload.get("id", "")))
                count = int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)).fetchone()[0])
                return {"operation": operation, "payload": {"id": module["id"]}, "summary": f"{module['label']} menüden ve kullanımdan kaldırılacak.", "impact": count, "warnings": ["Kayıtlar silinmeyecek; modül arşive alınacak."]}
            if operation == "module.restore":
                module = self._module_row(conn, str(payload.get("id", "")), True)
                if module["active"]:
                    raise DatabaseError("Modül zaten aktif.")
                return {"operation": operation, "payload": {"id": module["id"]}, "summary": f"{module['label']} yeniden kullanıma açılacak.", "impact": 0, "warnings": []}
            if operation == "module.delete_permanent":
                module = self._module_row(conn, str(payload.get("id", "")), True)
                if module["active"]:
                    raise DatabaseError("Kalıcı silme için bölüm önce arşive alınmalıdır.")
                if search_key(payload.get("confirmation", "")) != "sil":
                    raise DatabaseError("Kalıcı silme onayı için SIL yazılmalıdır.")
                record_count = int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=?", (module["id"],)).fetchone()[0])
                field_count = int(conn.execute("SELECT COUNT(*) FROM dynamic_module_fields WHERE module_id=?", (module["id"],)).fetchone()[0])
                rule_count = int(conn.execute("SELECT COUNT(*) FROM dynamic_rules WHERE module_id=?", (module["id"],)).fetchone()[0])
                action_count = int(conn.execute("SELECT COUNT(*) FROM dynamic_actions WHERE module_id=?", (module["id"],)).fetchone()[0])
                view_count = int(conn.execute("SELECT COUNT(*) FROM dynamic_views WHERE module_id=?", (module["id"],)).fetchone()[0])
                impact = record_count + field_count + rule_count + action_count + view_count + 1
                return {
                    "operation": operation,
                    "payload": {"id": module["id"], "confirmation": "SIL"},
                    "summary": f"{module['label']} bölümü ve bağlı platform verileri kalıcı olarak silinecek.",
                    "impact": impact,
                    "warnings": [
                        f"{record_count} kayıt, {field_count} alan, {rule_count} kural, {action_count} eylem ve {view_count} görünüm silinecek.",
                        "İşlemden hemen önce otomatik veritabanı yedeği alınacaktır.",
                        "Bu işlem arşivden geri yüklenemez; yalnızca alınan yedekten geri dönülebilir.",
                    ],
                }
            if operation == "field.save":
                module = self._module_row(conn, str(payload.get("module_id", "")))
                current_raw = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (payload.get("id", ""),)).fetchone())
                current = None
                if current_raw:
                    current = dict(current_raw)
                    current["options"] = _load(current.pop("options_json"), [])
                clean = self._sanitize_field(payload, module["id"], current)
                duplicate = conn.execute("SELECT id FROM dynamic_module_fields WHERE module_id=? AND field_key=? AND id<>?", (module["id"], clean["field_key"], clean.get("id") or "")).fetchone()
                if duplicate:
                    raise DatabaseError("Bu alan anahtarı modülde zaten kullanılıyor.")
                fields = self._field_rows(conn, module["id"], True)
                fields = [field for field in fields if field["id"] != clean.get("id")] + [clean]
                self._validate_formula_graph(fields)
                count = int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)).fetchone()[0])
                warnings = [f"Formül mevcut ve yeni {count} kayda otomatik uygulanacak."] if clean["data_type"] == "formula" else []
                return {"operation": operation, "payload": clean, "summary": f"{module['label']} bölümünde {clean['label']} alanı {'güncellenecek' if current else 'eklenecek'}.", "impact": count, "warnings": warnings}
            if operation == "field.archive":
                field_raw = conn.execute("SELECT * FROM dynamic_module_fields WHERE id=? AND active=1", (payload.get("id", ""),)).fetchone()
                if not field_raw:
                    raise FileNotFoundError("Alan bulunamadı.")
                field = dict(field_raw)
                module = self._module_row(conn, field["module_id"])
                dependencies = []
                for other in self._field_rows(conn, module["id"]):
                    if other["data_type"] == "formula" and field["field_key"] in SafeFormula.names(other["formula"]):
                        dependencies.append(other["label"])
                if dependencies:
                    raise DatabaseError(f"Alan şu formüllerde kullanılıyor: {', '.join(dependencies)}")
                count = int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)).fetchone()[0])
                return {"operation": operation, "payload": {"id": field["id"]}, "summary": f"{field['label']} alanı arşive alınacak.", "impact": count, "warnings": ["Kayıtlı değerler korunacak."]}
            if operation == "field.restore":
                field = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=? AND active=0", (payload.get("id", ""),)).fetchone())
                if not field:
                    raise FileNotFoundError("Arşivlenmiş alan bulunamadı.")
                module = self._module_row(conn, field["module_id"])
                return {"operation": operation, "payload": {"id": field["id"]}, "summary": f"{field['label']} alanı {module['label']} bölümüne geri getirilecek.", "impact": 0, "warnings": ["Korunmuş eski değerler yeniden görünür olacaktır."]}
            if operation == "rule.save":
                module = self._module_row(conn, str(payload.get("module_id", "")))
                clean = self._sanitize_rule(payload, module)
                return {"operation": operation, "payload": clean, "summary": f"{module['label']} için {clean['name']} kuralı kaydedilecek.", "impact": int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=? AND active=1", (module["id"],)).fetchone()[0]), "warnings": ["Kural bundan sonraki kayıt ve güncellemelerde çalışır."]}
            if operation in {"rule.archive", "rule.restore"}:
                desired = 1 if operation == "rule.restore" else 0
                rule = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (payload.get("id", ""),)).fetchone())
                if not rule:
                    raise FileNotFoundError("İş kuralı bulunamadı.")
                return {"operation": operation, "payload": {"id": rule["id"]}, "summary": f"{rule['name']} kuralı {'yeniden etkinleştirilecek' if desired else 'arşive alınacak'}.", "impact": 0, "warnings": []}
            if operation == "action.save":
                module = self._module_row(conn, str(payload.get("module_id", "")))
                current_raw = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (payload.get("id", ""),)).fetchone())
                current = None
                if current_raw:
                    current = dict(current_raw)
                    current["config"] = _load(current.pop("config_json"), {})
                clean = self._sanitize_action(payload, module, current)
                duplicate = conn.execute(
                    "SELECT id FROM dynamic_actions WHERE module_id=? AND action_key=? AND id<>?",
                    (module["id"], clean["action_key"], clean.get("id") or ""),
                ).fetchone()
                if duplicate:
                    raise DatabaseError("Bu eylem anahtarı bölümde zaten kullanılıyor.")
                return {
                    "operation": operation, "payload": clean,
                    "summary": f"{module['label']} bölümünde {clean['label']} eylemi {'güncellenecek' if current else 'eklenecek'}.",
                    "impact": 0,
                    "warnings": ["Eylem yalnızca izin verilen arayüz konumunda çalışacaktır."] if current else [],
                }
            if operation in {"action.archive", "action.restore"}:
                desired = 1 if operation == "action.restore" else 0
                action = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (payload.get("id", ""),)).fetchone())
                if not action:
                    raise FileNotFoundError("Eylem bulunamadı.")
                return {
                    "operation": operation, "payload": {"id": action["id"]},
                    "summary": f"{action['label']} eylemi {'yeniden etkinleştirilecek' if desired else 'arşive alınacak'}.",
                    "impact": 0, "warnings": ["Eylemin yaptığı kayıt işlemleri silinmez; yalnızca düğme kullanımdan kalkar."] if not desired else [],
                }
            if operation in {"modules.reorder", "fields.reorder", "actions.reorder"}:
                ids = [normalize_text(item) for item in payload.get("ids", []) if normalize_text(item)]
                return {"operation": operation, "payload": {"ids": ids, "module_id": normalize_text(payload.get("module_id", ""))}, "summary": "Yeni sıralama yayınlanacak.", "impact": len(ids), "warnings": []}
            if operation == "history.restore":
                history = self._row(conn.execute("SELECT * FROM platform_history WHERE id=?", (int(payload.get("id", 0)),)).fetchone())
                if not history or history["entity_type"] not in {"module", "field", "rule", "action"}:
                    raise DatabaseError("Bu geçmiş kaydı geri yüklenemez.")
                if history.get("action") == "purge":
                    raise DatabaseError("Kalıcı silme geçmişten geri yüklenemez. Gerekirse işlem öncesi otomatik yedeği kullanın.")
                return {"operation": operation, "payload": {"id": history["id"]}, "summary": f"Önceki yapı geri yüklenecek: {history['summary']}", "impact": 1, "warnings": ["Mevcut hâl ayrıca geçmişe kaydedilecektir."]}
        raise DatabaseError("Desteklenmeyen platform değişikliği.")

    def apply_change(self, preview: dict[str, Any]) -> Any:
        operation, payload = preview["operation"], preview["payload"]
        if operation in {"module.save", "module.archive", "module.restore", "module.delete_permanent", "field.save", "field.archive", "field.restore", "rule.save", "rule.archive", "rule.restore", "action.save", "action.archive", "action.restore", "history.restore"}:
            self.db.backup(f"platform_{operation.replace('.', '_')}")
        if operation == "module.save": return self.save_module(payload)
        if operation == "module.archive": return self.archive_module(payload["id"])
        if operation == "module.restore": return self.restore_module(payload["id"])
        if operation == "module.delete_permanent": return self.purge_module(payload["id"])
        if operation == "field.save": return self.save_field(payload)
        if operation == "field.archive": return self.archive_field(payload["id"])
        if operation == "field.restore": return self.restore_field(payload["id"])
        if operation == "rule.save": return self.save_rule(payload)
        if operation == "rule.archive": return self.set_rule_active(payload["id"], False)
        if operation == "rule.restore": return self.set_rule_active(payload["id"], True)
        if operation == "action.save": return self.save_action(payload)
        if operation == "action.archive": return self.set_action_active(payload["id"], False)
        if operation == "action.restore": return self.set_action_active(payload["id"], True)
        if operation == "modules.reorder": return self.reorder_modules(payload["ids"])
        if operation == "fields.reorder": return self.reorder_fields(payload["module_id"], payload["ids"])
        if operation == "actions.reorder": return self.reorder_actions(payload["module_id"], payload["ids"])
        if operation == "history.restore": return self.restore_history(payload["id"])
        raise DatabaseError("Değişiklik uygulanamadı.")

    def save_module(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            current = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (data.get("id", ""),)).fetchone())
            clean = self._sanitize_module(data, current)
            now = utc_now()
            if current:
                conn.execute(
                    """UPDATE dynamic_modules SET module_key=?,label=?,singular_label=?,icon=?,description=?,color=?,
                       menu_visible=?,sort_order=?,active=?,updated_at=?,row_version=row_version+1 WHERE id=?""",
                    (clean["module_key"], clean["label"], clean["singular_label"], clean["icon"], clean["description"],
                     clean["color"], int(clean["menu_visible"]), clean["sort_order"], int(clean["active"]), now, current["id"]),
                )
                module_id, action = current["id"], "update"
            else:
                module_id, action = _new_id("mod"), "create"
                conn.execute(
                    """INSERT INTO dynamic_modules(id,module_key,label,singular_label,icon,description,color,menu_visible,
                       sort_order,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (module_id, clean["module_key"], clean["label"], clean["singular_label"], clean["icon"], clean["description"],
                     clean["color"], int(clean["menu_visible"]), clean["sort_order"], int(clean["active"]), now, now),
                )
            after = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (module_id,)).fetchone())
            self._history(conn, "module", module_id, action, f"{clean['label']} modülü kaydedildi", current, after)
        if action == "create":
            # 5.0: Yeni bir bölüm, kullanıcı ayrıca kod/ayar yazmadan temel güvenli eylemlerle doğar.
            self._ensure_default_actions()
        return self.get_module(module_id, True)

    def archive_module(self, module_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._module_row(conn, module_id)
            conn.execute("UPDATE dynamic_modules SET active=0,menu_visible=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), before["id"]))
            after = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (before["id"],)).fetchone())
            self._history(conn, "module", before["id"], "archive", f"{before['label']} modülü arşivlendi", before, after)
            return {"id": before["id"], "archived": True}

    def restore_module(self, module_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._module_row(conn, module_id, True)
            conn.execute("UPDATE dynamic_modules SET active=1,menu_visible=1,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), before["id"]))
            after = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (before["id"],)).fetchone())
            self._history(conn, "module", before["id"], "restore", f"{before['label']} modülü geri yüklendi", before, after)
        return self.get_module(module_id)

    def purge_module(self, module_id: str) -> dict[str, Any]:
        """Yalnızca arşivlenmiş dinamik bölümü ve ona bağlı platform verilerini kalıcı siler."""
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_id, True)
            if module["active"]:
                raise DatabaseError("Aktif bölüm kalıcı silinemez; önce arşive alınmalıdır.")
            counts = {
                "records": int(conn.execute("SELECT COUNT(*) FROM dynamic_records WHERE module_id=?", (module["id"],)).fetchone()[0]),
                "fields": int(conn.execute("SELECT COUNT(*) FROM dynamic_module_fields WHERE module_id=?", (module["id"],)).fetchone()[0]),
                "rules": int(conn.execute("SELECT COUNT(*) FROM dynamic_rules WHERE module_id=?", (module["id"],)).fetchone()[0]),
                "actions": int(conn.execute("SELECT COUNT(*) FROM dynamic_actions WHERE module_id=?", (module["id"],)).fetchone()[0]),
                "views": int(conn.execute("SELECT COUNT(*) FROM dynamic_views WHERE module_id=?", (module["id"],)).fetchone()[0]),
            }
            self._history(conn, "module", module["id"], "purge", f"{module['label']} modülü kalıcı silindi", module, counts)
            conn.execute("DELETE FROM dynamic_views WHERE module_id=?", (module["id"],))
            conn.execute("DELETE FROM dynamic_rules WHERE module_id=?", (module["id"],))
            conn.execute("DELETE FROM dynamic_actions WHERE module_id=?", (module["id"],))
            conn.execute("DELETE FROM dynamic_records WHERE module_id=?", (module["id"],))
            conn.execute("DELETE FROM dynamic_module_fields WHERE module_id=?", (module["id"],))
            conn.execute("DELETE FROM dynamic_modules WHERE id=?", (module["id"],))
        return {"id": module_id, "purged": True, **counts}

    def save_field(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, str(data.get("module_id", "")))
            current_raw = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (data.get("id", ""),)).fetchone())
            current = None
            if current_raw:
                current = dict(current_raw)
                current["options"] = _load(current.pop("options_json"), [])
            clean = self._sanitize_field(data, module["id"], current)
            now = utc_now()
            if clean["is_title"]:
                conn.execute("UPDATE dynamic_module_fields SET is_title=0 WHERE module_id=?", (module["id"],))
            if current:
                field_id, action = current["id"], "update"
                conn.execute(
                    """UPDATE dynamic_module_fields SET field_key=?,label=?,data_type=?,options_json=?,relation_target=?,formula=?,
                       default_value=?,placeholder=?,required=?,visible=?,filterable=?,is_title=?,aggregate_type=?,decimal_places=?,
                       width=?,sort_order=?,active=?,updated_at=?,row_version=row_version+1 WHERE id=?""",
                    (clean["field_key"], clean["label"], clean["data_type"], _json(clean["options"]), clean["relation_target"],
                     clean["formula"], clean["default_value"], clean["placeholder"], int(clean["required"]), int(clean["visible"]),
                     int(clean["filterable"]), int(clean["is_title"]), clean["aggregate_type"], clean["decimal_places"],
                     clean["width"], clean["sort_order"], int(clean["active"]), now, field_id),
                )
            else:
                field_id, action = _new_id("fld"), "create"
                conn.execute(
                    """INSERT INTO dynamic_module_fields(id,module_id,field_key,label,data_type,options_json,relation_target,formula,
                       default_value,placeholder,required,visible,filterable,is_title,aggregate_type,decimal_places,width,sort_order,
                       active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (field_id, module["id"], clean["field_key"], clean["label"], clean["data_type"], _json(clean["options"]),
                     clean["relation_target"], clean["formula"], clean["default_value"], clean["placeholder"], int(clean["required"]),
                     int(clean["visible"]), int(clean["filterable"]), int(clean["is_title"]), clean["aggregate_type"],
                     clean["decimal_places"], clean["width"], clean["sort_order"], int(clean["active"]), now, now),
                )
            after_raw = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (field_id,)).fetchone())
            self._history(conn, "field", field_id, action, f"{clean['label']} alanı kaydedildi", current_raw, after_raw)
        return next(field for field in self.get_module(module["id"], True)["fields"] if field["id"] == field_id)

    def archive_field(self, field_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=? AND active=1", (field_id,)).fetchone())
            if not before:
                raise FileNotFoundError("Alan bulunamadı.")
            conn.execute("UPDATE dynamic_module_fields SET active=0,visible=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), field_id))
            after = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (field_id,)).fetchone())
            self._history(conn, "field", field_id, "archive", f"{before['label']} alanı arşivlendi", before, after)
            return {"id": field_id, "archived": True}

    def restore_field(self, field_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=? AND active=0", (field_id,)).fetchone())
            if not before:
                raise FileNotFoundError("Arşivlenmiş alan bulunamadı.")
            conn.execute("UPDATE dynamic_module_fields SET active=1,visible=1,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), field_id))
            after = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (field_id,)).fetchone())
            self._history(conn, "field", field_id, "restore", f"{before['label']} alanı geri yüklendi", before, after)
        return next(item for item in self.get_module(before["module_id"])["fields"] if item["id"] == field_id)

    def reorder_modules(self, ids: list[str]) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            for index, module_id in enumerate(ids, start=1):
                conn.execute("UPDATE dynamic_modules SET sort_order=?,updated_at=? WHERE id=?", (index * 10, utc_now(), module_id))
            self._history(conn, "module_order", "all", "reorder", "Modül sırası güncellendi", None, ids)
        return self.list_modules()

    def reorder_fields(self, module_id: str, ids: list[str]) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_id)
            for index, field_id in enumerate(ids, start=1):
                conn.execute("UPDATE dynamic_module_fields SET sort_order=?,updated_at=? WHERE id=? AND module_id=?", (index * 10, utc_now(), field_id, module["id"]))
            self._history(conn, "field_order", module["id"], "reorder", f"{module['label']} alan sırası güncellendi", None, ids)
        return self.get_module(module_id)["fields"]

    def _sanitize_rule(self, data: dict[str, Any], module: dict[str, Any]) -> dict[str, Any]:
        name = normalize_text(data.get("name"))
        if not name:
            raise DatabaseError("Kural adı zorunludur.")
        fields = {field["field_key"]: field for field in self._field_rows_from_module(module["id"])}
        conditions = data.get("conditions", [])
        actions = data.get("actions", [])
        if not isinstance(conditions, list) or not isinstance(actions, list) or not actions:
            raise DatabaseError("Kuralda en az bir işlem bulunmalıdır.")
        clean_conditions = []
        for item in conditions:
            field = normalize_text(item.get("field"))
            operator = normalize_text(item.get("operator", "eq"))
            if field not in fields or operator not in RULE_OPERATORS:
                raise DatabaseError("Kural koşulunda geçersiz alan veya işlem var.")
            clean_conditions.append({"field": field, "operator": operator, "value": item.get("value", "")})
        clean_actions = []
        for item in actions:
            action_type = normalize_text(item.get("type", "set"))
            field = normalize_text(item.get("field"))
            if field not in fields or fields[field]["data_type"] == "formula":
                raise DatabaseError("Kural işlemi için düzenlenebilir bir alan seçilmelidir.")
            if action_type not in {"set", "copy", "formula"}:
                raise DatabaseError("Kural işlem türü desteklenmiyor.")
            value = item.get("value", "")
            if action_type == "copy" and normalize_text(value) not in fields:
                raise DatabaseError("Kopyalanacak kaynak alan bulunamadı.")
            if action_type == "formula":
                missing = SafeFormula.names(str(value)) - set(fields)
                if missing:
                    raise DatabaseError(f"Kural formülünde bulunmayan alan: {', '.join(sorted(missing))}")
            clean_actions.append({"type": action_type, "field": field, "value": value})
        return {
            "id": normalize_text(data.get("id")), "module_id": module["id"], "name": name,
            "trigger_name": "save", "conditions": clean_conditions, "actions": clean_actions,
            "sort_order": max(0, int(data.get("sort_order", 100))), "active": bool(data.get("active", True)),
        }

    def _field_rows_from_module(self, module_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return self._field_rows(conn, module_id)

    def save_rule(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, str(data.get("module_id", "")))
            clean = self._sanitize_rule(data, module)
            before = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (clean["id"],)).fetchone())
            now = utc_now()
            if before:
                rule_id, action = before["id"], "update"
                conn.execute(
                    """UPDATE dynamic_rules SET name=?,conditions_json=?,actions_json=?,sort_order=?,active=?,updated_at=?,
                       row_version=row_version+1 WHERE id=?""",
                    (clean["name"], _json(clean["conditions"]), _json(clean["actions"]), clean["sort_order"], int(clean["active"]), now, rule_id),
                )
            else:
                rule_id, action = _new_id("rul"), "create"
                conn.execute(
                    """INSERT INTO dynamic_rules(id,module_id,name,trigger_name,conditions_json,actions_json,sort_order,active,
                       created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (rule_id, module["id"], clean["name"], "save", _json(clean["conditions"]), _json(clean["actions"]),
                     clean["sort_order"], int(clean["active"]), now, now),
                )
            after = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (rule_id,)).fetchone())
            self._history(conn, "rule", rule_id, action, f"{clean['name']} kuralı kaydedildi", before, after)
        return next(rule for rule in self.get_module(module["id"])["rules"] if rule["id"] == rule_id)

    def set_rule_active(self, rule_id: str, active: bool) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (rule_id,)).fetchone())
            if not before:
                raise FileNotFoundError("İş kuralı bulunamadı.")
            conn.execute(
                "UPDATE dynamic_rules SET active=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                (int(active), utc_now(), rule_id),
            )
            after = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (rule_id,)).fetchone())
            action = "restore" if active else "archive"
            self._history(conn, "rule", rule_id, action, f"{before['name']} kuralı {'geri yüklendi' if active else 'arşivlendi'}", before, after)
            return {"id": rule_id, "active": active}

    def _sanitize_action(self, data: dict[str, Any], module: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        label = normalize_text(data.get("label", current["label"] if current else ""))
        if not label:
            raise DatabaseError("Eylem adı zorunludur.")
        action_type = normalize_text(data.get("action_type", current["action_type"] if current else ""))
        if action_type not in ACTION_TYPES:
            raise DatabaseError("Desteklenmeyen eylem türü.")
        placement = normalize_text(data.get("placement", current["placement"] if current else ""))
        if placement not in ACTION_PLACEMENTS:
            raise DatabaseError("Eylem konumu geçersiz.")
        allowed = ACTION_PLACEMENT_RULES.get(action_type, set())
        if placement not in allowed:
            readable = {"page_top": "sayfa üstü", "row": "satır", "form_header": "form üstü", "form_footer": "form altı"}
            choices = ", ".join(readable[item] for item in sorted(allowed))
            raise DatabaseError(f"{label} eylemi yalnızca şu alanlarda kullanılabilir: {choices}.")
        style = normalize_text(data.get("style", current["style"] if current else "secondary"))
        if style not in ACTION_STYLES:
            raise DatabaseError("Eylem görünümü geçersiz.")
        config = data.get("config", current.get("config", {}) if current else {})
        if not isinstance(config, dict):
            raise DatabaseError("Eylem ayarları nesne biçiminde olmalıdır.")
        action_key = current["action_key"] if current else _slug(data.get("action_key", label), action_type)
        return {
            "id": current["id"] if current else normalize_text(data.get("id")),
            "module_id": module["id"], "action_key": action_key, "label": label,
            "action_type": action_type, "placement": placement,
            "icon": normalize_text(data.get("icon", current["icon"] if current else ""))[:4],
            "style": style,
            "confirmation_text": normalize_text(data.get("confirmation_text", current["confirmation_text"] if current else "")),
            "config": config, "sort_order": max(0, int(data.get("sort_order", current["sort_order"] if current else 100))),
            "active": bool(data.get("active", current["active"] if current else True)),
        }

    def save_action(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, str(data.get("module_id", "")))
            current_raw = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (data.get("id", ""),)).fetchone())
            current = None
            if current_raw:
                current = dict(current_raw)
                current["config"] = _load(current.pop("config_json"), {})
            clean = self._sanitize_action(data, module, current)
            duplicate = conn.execute(
                "SELECT id FROM dynamic_actions WHERE module_id=? AND action_key=? AND id<>?",
                (module["id"], clean["action_key"], clean.get("id") or ""),
            ).fetchone()
            if duplicate:
                raise DatabaseError("Bu eylem anahtarı bölümde zaten kullanılıyor.")
            now = utc_now()
            if current:
                action_id, history_action = current["id"], "update"
                conn.execute(
                    """UPDATE dynamic_actions SET label=?,action_type=?,placement=?,icon=?,style=?,confirmation_text=?,
                       config_json=?,sort_order=?,active=?,updated_at=?,row_version=row_version+1 WHERE id=?""",
                    (clean["label"], clean["action_type"], clean["placement"], clean["icon"], clean["style"],
                     clean["confirmation_text"], _json(clean["config"]), clean["sort_order"], int(clean["active"]), now, action_id),
                )
            else:
                action_id, history_action = _new_id("act"), "create"
                conn.execute(
                    """INSERT INTO dynamic_actions(id,module_id,action_key,label,action_type,placement,icon,style,
                       confirmation_text,config_json,sort_order,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (action_id, module["id"], clean["action_key"], clean["label"], clean["action_type"], clean["placement"],
                     clean["icon"], clean["style"], clean["confirmation_text"], _json(clean["config"]), clean["sort_order"],
                     int(clean["active"]), now, now),
                )
            after = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (action_id,)).fetchone())
            self._history(conn, "action", action_id, history_action, f"{clean['label']} eylemi kaydedildi", current_raw, after)
        actions = self.get_module(module["id"], True)["actions"] + self.get_module(module["id"], True)["archived_actions"]
        return next(item for item in actions if item["id"] == action_id)

    def set_action_active(self, action_id: str, active: bool) -> dict[str, Any]:
        with self.db.transaction() as conn:
            before = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (action_id,)).fetchone())
            if not before:
                raise FileNotFoundError("Eylem bulunamadı.")
            conn.execute(
                "UPDATE dynamic_actions SET active=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                (int(active), utc_now(), action_id),
            )
            after = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (action_id,)).fetchone())
            history_action = "restore" if active else "archive"
            self._history(conn, "action", action_id, history_action,
                          f"{before['label']} eylemi {'geri yüklendi' if active else 'arşivlendi'}", before, after)
        return {"id": action_id, "active": active}

    def reorder_actions(self, module_id: str, ids: list[str]) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_id)
            for index, action_id in enumerate(ids, start=1):
                conn.execute(
                    "UPDATE dynamic_actions SET sort_order=?,updated_at=? WHERE id=? AND module_id=?",
                    (index * 10, utc_now(), action_id, module["id"]),
                )
            self._history(conn, "action_order", module["id"], "reorder", f"{module['label']} eylem sırası güncellendi", None, ids)
        return self.get_module(module_id)["actions"]

    def _find_module_for_command(self, conn: sqlite3.Connection, phrase: str, include_inactive: bool = False) -> dict[str, Any]:
        wanted = search_key(phrase)
        for suffix in (" bolumunde", " bolumune", " bolumunun", " bolumu", " bolum", " modulunde", " modulune", " modulunun", " modulu", " modul"):
            if wanted.endswith(suffix):
                wanted = wanted[:-len(suffix)].strip()
        sql = "SELECT * FROM dynamic_modules"
        if not include_inactive:
            sql += " WHERE active=1"
        rows = [dict(row) for row in conn.execute(sql)]
        exact = [row for row in rows if search_key(row["label"]) == wanted or search_key(row["module_key"]) == wanted]
        if len(exact) == 1:
            return exact[0]
        partial = [row for row in rows if wanted and (wanted in search_key(row["label"]) or search_key(row["label"]) in wanted)]
        if len(partial) == 1:
            return partial[0]
        raise DatabaseError(f"Komutta belirtilen bölüm bulunamadı veya belirsiz: {phrase}")

    def _find_field_for_command(self, conn: sqlite3.Connection, module_id: str, phrase: str, include_inactive: bool = False) -> dict[str, Any]:
        wanted = search_key(phrase)
        for suffix in (" alani", " alan", " sutunu", " sutun"):
            if wanted.endswith(suffix):
                wanted = wanted[:-len(suffix)].strip()
        fields = self._field_rows(conn, module_id, include_inactive)
        exact = [item for item in fields if search_key(item["label"]) == wanted or search_key(item["field_key"]) == wanted]
        if len(exact) == 1:
            return exact[0]
        partial = [item for item in fields if wanted and (wanted in search_key(item["label"]) or search_key(item["label"]) in wanted)]
        if len(partial) == 1:
            return partial[0]
        raise DatabaseError(f"Komutta belirtilen alan bulunamadı veya belirsiz: {phrase}")

    @staticmethod
    def _assistant_message(summary: str, suggestions: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
        return {
            "assistant_only": True,
            "executable": False,
            "operation": "",
            "payload": {},
            "summary": summary,
            "impact": 0,
            "warnings": warnings or [],
            "suggestions": suggestions or [],
        }

    def preview_command(self, command: str) -> dict[str, Any]:
        """Türkçe komutu güvenli değişiklik ön izlemesine çevirir; anlamadığında hata yerine yol gösterir."""
        raw = normalize_text(command)
        if not raw:
            return self._assistant_message("Komut alanı boş. Ne yapmak istediğinizi kısa bir cümleyle yazabilirsiniz.", [
                "Stok bölümüne Raf No metin alanı ekle",
                "Stok bölümünde Sil butonunu sayfa üstünde göster",
                "Teklifler adında yeni bölüm oluştur",
            ])
        key = search_key(raw)
        if key in {"yardim", "yardim et", "ne yapabilirsin", "neler yapabilirsin", "komutlar", "ornek komutlar"}:
            return self._assistant_message("LOGOS Asistan güvenli yapısal işlemleri önce ön izlemeye çevirir. Bölüm ve alan oluşturabilir, alan görünürlüğünü değiştirebilir, alanı arşivleyebilir, bölümü arşivleyebilir ve Sil/Düzenle/Kaydet gibi eylemleri uygun konumlara taşıyabilirim. Anlamadığım komutta da hata verip susmak yerine açıklama ve örnek sunarım.", [
                "Teklifler adında yeni bölüm oluştur",
                "Stok bölümüne Raf No metin alanı ekle",
                "Stok bölümünde Raf No alanını gizle",
                "Stok bölümünde Sil butonunu sayfa üstünde göster",
                "Stok bölümünü arşivle",
                "Kaç kurum var?",
            ])
        if any(phrase in key for phrase in ("kac kurum", "kurum sayisi", "toplam kurum")):
            dashboard = self.db.dashboard()
            return self._assistant_message(
                f"Sistemde {int(dashboard.get('institution_count', 0))} tekil kurum grubu ve {int(dashboard.get('panel_count', 0))} panel bulunuyor.",
                ["Kaç panel var?", "Hangi bölümler var?", "yardım"],
            )
        if any(phrase in key for phrase in ("kac panel", "panel sayisi", "toplam panel")):
            dashboard = self.db.dashboard()
            return self._assistant_message(
                f"Sistemde {int(dashboard.get('panel_count', 0))} panel bulunuyor.",
                ["Kaç kurum var?", "Hangi bölümler var?", "yardım"],
            )
        if any(phrase in key for phrase in ("hangi bolumler", "bolumler neler", "bolumleri goster")):
            labels = [item["label"] for item in self.list_modules()]
            return self._assistant_message(
                "Korunan ana bölümler: Kurumlar, Finans, Prim. Dinamik bölümler: " + (", ".join(labels) if labels else "henüz yok") + ".",
                ["Stok bölümünde Sil butonunu sayfa üstünde göster", "Teklifler adında yeni bölüm oluştur", "yardım"],
            )

        with self.db.connect() as conn:
            # Yeni bölüm
            match = re.fullmatch(r"(.+?)\s+adında\s+yeni\s+bölüm\s+(?:oluştur|ekle)[.!]?", raw, re.IGNORECASE)
            if not match:
                match = re.fullmatch(r"yeni\s+bölüm\s+(.+?)\s+(?:oluştur|ekle)[.!]?", raw, re.IGNORECASE)
            if match:
                label = match.group(1).strip()
                return self.preview_change("module.save", {"label": label, "singular_label": label})

            # Yeni alan
            match = re.fullmatch(r"(.+?)\s+bölüm(?:üne|e)\s+(.+?)\s+alanı\s+ekle[.!]?", raw, re.IGNORECASE)
            if match:
                try:
                    module = self._find_module_for_command(conn, match.group(1))
                except DatabaseError as exc:
                    return self._assistant_message(str(exc), ["Platform Stüdyosu > Bölümler alanından bölüm adını kontrol edin."])
                field_phrase = match.group(2).strip()
                type_map = {
                    "sayı": "number", "sayi": "number", "para": "money", "tutar": "money", "tarih": "date", "metin": "text",
                    "uzun metin": "longtext", "evet hayır": "boolean", "evet/hayır": "boolean", "secim": "select", "seçim": "select",
                }
                data_type, label = "text", field_phrase
                normalized_phrase = search_key(field_phrase)
                for type_label, mapped in sorted(type_map.items(), key=lambda item: len(search_key(item[0])), reverse=True):
                    type_key = search_key(type_label)
                    if normalized_phrase.endswith(" " + type_key) or normalized_phrase == type_key:
                        data_type = mapped
                        label_key = normalized_phrase[:-len(type_key)].strip()
                        # Türkçe görünen adı korumak için tip sözcüklerini sondan kaldır.
                        remove_count = len(type_label.replace("/", " ").split())
                        words = field_phrase.split()
                        label = " ".join(words[:-remove_count]).strip() if len(words) > remove_count else label_key or field_phrase
                        break
                return self.preview_change("field.save", {"module_id": module["id"], "label": label, "data_type": data_type})

            # Bölüm arşivle / geri yükle
            match = re.fullmatch(r"(.+?)\s+bölüm(?:ünü|u)\s+(arşivle|arsivle|kaldır|kaldir)[.!]?", raw, re.IGNORECASE)
            if match:
                try:
                    module = self._find_module_for_command(conn, match.group(1))
                    return self.preview_change("module.archive", {"id": module["id"]})
                except DatabaseError as exc:
                    return self._assistant_message(str(exc))
            match = re.fullmatch(r"(.+?)\s+bölüm(?:ünü|u)\s+geri\s+(?:yükle|getir|aç)[.!]?", raw, re.IGNORECASE)
            if match:
                try:
                    module = self._find_module_for_command(conn, match.group(1), True)
                    return self.preview_change("module.restore", {"id": module["id"]})
                except DatabaseError as exc:
                    return self._assistant_message(str(exc))

            # Alan görünürlüğü / zorunluluk / arşiv
            match = re.fullmatch(r"(.+?)\s+bölümünde\s+(.+?)\s+alanını\s+(gizle|göster|goster|zorunlu yap|zorunluluğu kaldır|zorunlulugu kaldir|arşivle|arsivle|kaldır|kaldir)[.!]?", raw, re.IGNORECASE)
            if match:
                try:
                    module = self._find_module_for_command(conn, match.group(1))
                    field = self._find_field_for_command(conn, module["id"], match.group(2))
                except DatabaseError as exc:
                    return self._assistant_message(str(exc))
                verb = search_key(match.group(3))
                if verb in {"arsivle", "kaldir"}:
                    return self.preview_change("field.archive", {"id": field["id"]})
                updated = {**field, "module_id": module["id"]}
                if verb == "gizle":
                    updated["visible"] = False
                elif verb == "goster":
                    updated["visible"] = True
                elif verb == "zorunlu yap":
                    updated["required"] = True
                else:
                    updated["required"] = False
                return self.preview_change("field.save", updated)

            # Eylem taşıma. Önce Unicode regex, sonra tamamen normalize edilmiş Türkçe-toleranslı çözümleme.
            match = re.fullmatch(r"(.+?)\s+bölümünde\s+(.+?)\s+butonunu\s+(.+?)\s+(?:göster|yerleştir|taşı|al)[.!]?", raw, re.IGNORECASE)
            module_phrase = action_phrase_raw = place_phrase_raw = ""
            if match:
                module_phrase, action_phrase_raw, place_phrase_raw = match.group(1), match.group(2), match.group(3)
            else:
                normalized_match = re.fullmatch(r"(.+?)\s+bolumunde\s+(.+?)\s+butonunu\s+(.+?)\s+(?:goster|yerlestir|tasi|al)", key)
                if normalized_match:
                    module_phrase, action_phrase_raw, place_phrase_raw = normalized_match.groups()
            if module_phrase:
                try:
                    module = self._find_module_for_command(conn, module_phrase)
                except DatabaseError as exc:
                    core = {"kurum": "Kurumlar", "kurumlar": "Kurumlar", "finans": "Finans", "prim": "Prim"}
                    core_name = next((label for alias, label in core.items() if alias in search_key(module_phrase)), "")
                    if core_name:
                        return self._assistant_message(
                            f"{core_name} şu anda korunan ana bölüm. Bu sürümde dinamik buton taşıma motoru Servis, Sözleşmeler, Stok ve sizin oluşturduğunuz bölümlerde çalışıyor.",
                            ["Platform Stüdyosu'nda dinamik bir bölüm seçin", "Kurum/Finans/Prim'i tam dinamik çekirdeğe taşıma sonraki mimari adımıdır."],
                        )
                    return self._assistant_message(str(exc))
                action_phrase = search_key(action_phrase_raw)
                place_phrase = search_key(place_phrase_raw)
                aliases = {
                    "sil": "archive_record", "silme": "archive_record", "arsivle": "archive_record",
                    "duzenle": "edit_record", "degistir": "edit_record",
                    "kaydet": "save_record", "yeni kayit": "new_record", "yeni": "new_record",
                    "excel": "export_xlsx", "indir": "export_xlsx", "kopyala": "duplicate_record", "cogalt": "duplicate_record",
                    "vazgec": "cancel_form",
                }
                action_type = next((mapped for alias, mapped in aliases.items() if alias in action_phrase), "")
                actions = self._action_rows(conn, module["id"], True)
                action = next((item for item in actions if item["action_type"] == action_type), None)
                if not action:
                    return self._assistant_message("Belirttiğiniz butonu bu bölümde bulamadım.", [
                        "Sil", "Düzenle", "Kaydet", "Yeni Kayıt", "Excel", "Kopyala", "Vazgeç",
                    ])
                if any(word in place_phrase for word in ("satir", "kayit satiri")):
                    placement = "row"
                elif "form" in place_phrase and any(word in place_phrase for word in ("ust", "bas")):
                    placement = "form_header"
                elif "form" in place_phrase and any(word in place_phrase for word in ("alt", "footer", "son")):
                    placement = "form_footer"
                elif any(word in place_phrase for word in ("ust", "sayfa", "toolbar", "arac")):
                    placement = "page_top"
                else:
                    return self._assistant_message("Butonun yerini tam anlayamadım.", ["sayfa üstü", "satır", "form üstü", "form altı"])
                try:
                    return self.preview_change("action.save", {**action, "module_id": module["id"], "placement": placement, "active": True})
                except DatabaseError as exc:
                    return self._assistant_message(str(exc), ["Bu butonun izin verilen konumlarından birini seçin."])

            # Daha serbest kısa ifade: "stokta sili yukarı taşı"
            short_match = re.fullmatch(r"(.+?)(?:ta|te|da|de)\s+(.+?)\s+(?:butonunu\s+)?(?:yukarı|yukari|üste|uste|üstte|ustte)\s+(?:taşı|tasi|al)[.!]?", raw, re.IGNORECASE)
            if short_match:
                rewritten = f"{short_match.group(1)} bölümünde {short_match.group(2)} butonunu sayfa üstünde taşı"
                return self.preview_command(rewritten)

            # Serbest sözcük sırası: "stok sil üstte", "stok kaydet form altında" gibi kısa komutlar.
            module = None
            for candidate in self.list_modules():
                aliases = {search_key(candidate["label"]), search_key(candidate["module_key"]), search_key(candidate["singular_label"])}
                if any(alias and re.search(rf"(^|\s){re.escape(alias)}($|\s)", key) for alias in aliases):
                    module = candidate
                    break
            if module:
                action_aliases = {
                    "sil": "archive_record", "arsivle": "archive_record",
                    "duzenle": "edit_record", "degistir": "edit_record",
                    "kaydet": "save_record", "kopyala": "duplicate_record", "cogalt": "duplicate_record",
                    "excel": "export_xlsx", "indir": "export_xlsx", "vazgec": "cancel_form", "yeni kayit": "new_record",
                }
                action_type = next((mapped for alias, mapped in action_aliases.items() if re.search(rf"(^|\s){re.escape(alias)}($|\s)", key)), "")
                if action_type:
                    if any(word in key for word in ("satir", "satirda", "kayit satiri")):
                        placement = "row"
                    elif "form" in key and any(word in key for word in ("ust", "yukari", "bas")):
                        placement = "form_header"
                    elif "form" in key and any(word in key for word in ("alt", "asagi", "son")):
                        placement = "form_footer"
                    elif any(word in key for word in ("ust", "yukari", "sayfa", "toolbar")):
                        placement = "page_top"
                    else:
                        placement = ""
                    if placement:
                        action = next((item for item in self._action_rows(conn, module["id"], True) if item["action_type"] == action_type), None)
                        if action:
                            try:
                                return self.preview_change("action.save", {**action, "module_id": module["id"], "placement": placement, "active": True})
                            except DatabaseError as exc:
                                return self._assistant_message(str(exc), ["sayfa üstü", "satır", "form üstü", "form altı"])

        core_aliases = (("kurumlar", "Kurumlar"), ("kurum", "Kurumlar"), ("finans", "Finans"), ("prim", "Prim"))
        core_name = next((label for alias, label in core_aliases if alias in key), "")
        if core_name:
            return self._assistant_message(
                f"{core_name} korunan ana bölüm olduğu için bu komutu doğrudan yapısal değişikliğe çevirmedim. Programda hiçbir değişiklik yapılmadı.",
                [
                    "Platform Stüdyosu'nda dinamik bölüm seçin",
                    "Kurum/Finans/Prim'i tam dinamik çekirdeğe taşıma planına ekle",
                    "yardım",
                ],
                ["Korunan ana bölümlerde veri kaybı riski oluşturan serbest komutlar onaysız uygulanmaz."],
            )

        return self._assistant_message(
            "Bu cümleyi güvenli bir değişiklik işlemine kesin olarak çeviremedim; bu yüzden programda hiçbir değişiklik yapmadım.",
            [
                "Stok bölümüne Raf No metin alanı ekle",
                "Stok bölümünde Raf No alanını gizle",
                "Stok bölümünde Sil butonunu sayfa üstünde göster",
                "Teklifler adında yeni bölüm oluştur",
                "yardım",
            ],
            ["Asistan anlamadığı komutta artık kırmızı hata verip susmak yerine ne yapabildiğini gösterecek."],
        )

    @staticmethod
    def _condition_matches(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "empty": return normalize_text(actual) == ""
        if operator == "not_empty": return normalize_text(actual) != ""
        if operator == "contains": return search_key(expected) in search_key(actual)
        if operator == "eq": return normalize_text(actual) == normalize_text(expected)
        if operator == "ne": return normalize_text(actual) != normalize_text(expected)
        left, right = _decimal(actual), _decimal(expected)
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}.get(operator, False)

    def _apply_rules(self, values: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
        applied = []
        for rule in rules:
            if not rule["active"] or not all(self._condition_matches(values.get(item["field"]), item["operator"], item.get("value")) for item in rule["conditions"]):
                continue
            for action in rule["actions"]:
                if action["type"] == "set": values[action["field"]] = action.get("value", "")
                elif action["type"] == "copy": values[action["field"]] = values.get(str(action.get("value", "")), "")
                else: values[action["field"]] = str(SafeFormula.evaluate(str(action.get("value", "0")), values))
            applied.append(rule["name"])
        return values, applied

    def _formula_order(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key = {field["field_key"]: field for field in fields}
        formulas = {key: field for key, field in by_key.items() if field["data_type"] == "formula"}
        result: list[dict[str, Any]] = []
        visited: set[str] = set()
        visiting: set[str] = set()
        def visit(key: str) -> None:
            if key in visiting: raise DatabaseError("Formül döngüsü algılandı.")
            if key in visited: return
            visiting.add(key)
            for dependency in SafeFormula.names(formulas[key]["formula"]):
                if dependency in formulas: visit(dependency)
            visiting.remove(key); visited.add(key); result.append(formulas[key])
        for key in formulas: visit(key)
        return result

    def _calculate_formulas(self, values: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
        for field in self._formula_order(fields):
            value = SafeFormula.evaluate(field["formula"], values)
            places = field["decimal_places"]
            values[field["field_key"]] = str(value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))
        return values

    def _normalize_record_values(self, values: dict[str, Any], fields: list[dict[str, Any]], creating: bool) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise DatabaseError("Kayıt değerleri geçersiz.")
        result: dict[str, Any] = {}
        for field in fields:
            key, data_type = field["field_key"], field["data_type"]
            if data_type == "formula": continue
            value = values.get(key, field["default_value"] if creating else "")
            if data_type == "boolean": value = bool(value)
            elif data_type == "multiselect":
                if isinstance(value, str): value = [item.strip() for item in value.split(",") if item.strip()]
                if not isinstance(value, list): value = []
            else: value = normalize_text(value)
            if field["required"] and (value is False or value == "" or value == []):
                raise DatabaseError(f"{field['label']} zorunludur.")
            if data_type in {"number", "money"} and value != "":
                value = str(_strict_decimal(value, field["label"]))
            if data_type == "date" and value:
                try: datetime.strptime(str(value), "%Y-%m-%d")
                except ValueError as exc: raise DatabaseError(f"{field['label']} geçerli bir tarih olmalıdır.") from exc
            if data_type == "select" and value and field["options"] and value not in field["options"]:
                raise DatabaseError(f"{field['label']} için listedeki seçeneklerden biri kullanılmalıdır.")
            result[key] = value
        return result

    def _relation_label(self, conn: sqlite3.Connection, target: str, value: Any) -> str:
        if not value: return ""
        if target == "institutions":
            row = conn.execute("SELECT name FROM institutions WHERE id=?", (str(value),)).fetchone()
            return row[0] if row else str(value)
        module = conn.execute("SELECT id FROM dynamic_modules WHERE id=? OR module_key=?", (target, target)).fetchone()
        if not module: return str(value)
        row = conn.execute("SELECT title FROM dynamic_records WHERE id=? AND module_id=?", (str(value), module[0])).fetchone()
        return row[0] if row else str(value)

    def save_record(self, module_ref: str, data: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_ref)
            fields = self._field_rows(conn, module["id"])
            rules = self._rule_rows(conn, module["id"])
            before_row = self._row(conn.execute("SELECT * FROM dynamic_records WHERE id=? AND module_id=?", (record_id or "", module["id"])).fetchone())
            before_values = _load(before_row["values_json"], {}) if before_row else {}
            incoming = data.get("values", data)
            merged = {**before_values, **incoming}
            values = self._normalize_record_values(merged, fields, not bool(before_row))
            for field in fields:
                if field["data_type"] != "relation" or not values.get(field["field_key"]):
                    continue
                relation_value = str(values[field["field_key"]])
                if field["relation_target"] == "institutions":
                    exists = conn.execute("SELECT 1 FROM institutions WHERE id=? AND active=1", (relation_value,)).fetchone()
                else:
                    target = conn.execute(
                        "SELECT id FROM dynamic_modules WHERE (id=? OR module_key=?) AND active=1",
                        (field["relation_target"], field["relation_target"]),
                    ).fetchone()
                    exists = target and conn.execute(
                        "SELECT 1 FROM dynamic_records WHERE id=? AND module_id=? AND active=1",
                        (relation_value, target[0]),
                    ).fetchone()
                if not exists:
                    raise DatabaseError(f"{field['label']} için seçilen bağlantı artık mevcut değil.")
            values = self._calculate_formulas(values, fields)
            values, applied_rules = self._apply_rules(values, rules)
            # İş kuralları kullanıcı girişini değiştirebildiği için kurallardan sonra zorunlu
            # olarak yeniden doğrula. Böylece sayı alanına "ABC" yazdıran bir kural kaydolamaz.
            values = self._normalize_record_values(values, fields, False)
            values = self._calculate_formulas(values, fields)
            title_field = next((field for field in fields if field["is_title"]), None) or next((field for field in fields if field["data_type"] != "formula"), None)
            title = normalize_text(values.get(title_field["field_key"], "")) if title_field else "Kayıt"
            title = title or f"{module['singular_label']} kaydı"
            search_parts = [title]
            for field in fields:
                value = values.get(field["field_key"], "")
                if field["data_type"] == "relation": search_parts.append(self._relation_label(conn, field["relation_target"], value))
                elif isinstance(value, list): search_parts.extend(value)
                else: search_parts.append(str(value))
            search_text = search_key(" ".join(search_parts))
            now = utc_now()
            if before_row:
                supplied_version = int(data.get("row_version", before_row["row_version"]))
                if supplied_version != int(before_row["row_version"]):
                    raise DatabaseError("Kayıt başka bir işlemde değiştirildi. Sayfayı yenileyin.")
                conn.execute("UPDATE dynamic_records SET title=?,values_json=?,search_text=?,updated_at=?,row_version=row_version+1 WHERE id=?", (title, _json(values), search_text, now, before_row["id"]))
                item_id, action = before_row["id"], "update"
            else:
                item_id, action = _new_id("rec"), "create"
                conn.execute("INSERT INTO dynamic_records(id,module_id,title,values_json,search_text,active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)", (item_id, module["id"], title, _json(values), search_text, now, now))
            after = self._row(conn.execute("SELECT * FROM dynamic_records WHERE id=?", (item_id,)).fetchone())
            self._history(conn, "record", item_id, action, f"{module['singular_label']}: {title}", before_row, after)
        result = self.get_record(module["id"], item_id)
        result["applied_rules"] = applied_rules
        return result

    def get_record(self, module_ref: str, record_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            module = self._module_row(conn, module_ref)
            fields = self._field_rows(conn, module["id"])
            row = conn.execute("SELECT * FROM dynamic_records WHERE id=? AND module_id=?", (record_id, module["id"])).fetchone()
            if not row: raise FileNotFoundError("Kayıt bulunamadı.")
            item = dict(row); item["values"] = self._calculate_formulas(_load(item.pop("values_json"), {}), fields); item["active"] = bool(item["active"])
            return item

    def list_records(self, module_ref: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        with self.db.connect() as conn:
            module = self._module_row(conn, module_ref)
            fields = self._field_rows(conn, module["id"])
            rows = [dict(row) for row in conn.execute("SELECT * FROM dynamic_records WHERE module_id=? AND active=1 ORDER BY updated_at DESC,title", (module["id"],))]
            query = search_key((filters.get("query") or [""])[0] if isinstance(filters.get("query"), list) else filters.get("query", ""))
            selected: dict[str, list[str]] = {}
            ranges: dict[str, tuple[str, str]] = {}
            for field in fields:
                raw = filters.get(field["field_key"], [])
                selected[field["field_key"]] = raw if isinstance(raw, list) else [raw]
                selected[field["field_key"]] = [normalize_text(item) for item in selected[field["field_key"]] if normalize_text(item)]
                if field["data_type"] in {"number", "money", "formula", "date"}:
                    prefix_from = "from" if field["data_type"] == "date" else "min"
                    prefix_to = "to" if field["data_type"] == "date" else "max"
                    low = filters.get(f"{prefix_from}.{field['field_key']}", [""])
                    high = filters.get(f"{prefix_to}.{field['field_key']}", [""])
                    ranges[field["field_key"]] = (
                        normalize_text(low[0] if isinstance(low, list) else low),
                        normalize_text(high[0] if isinstance(high, list) else high),
                    )
            items = []
            for row in rows:
                if query and query not in row["search_text"]: continue
                values = self._calculate_formulas(_load(row.pop("values_json"), {}), fields)
                if any(options and normalize_text(values.get(key)) not in options for key, options in selected.items()): continue
                outside = False
                for field in fields:
                    low, high = ranges.get(field["field_key"], ("", ""))
                    if not low and not high:
                        continue
                    actual = normalize_text(values.get(field["field_key"]))
                    if field["data_type"] == "date":
                        outside = bool((low and actual < low) or (high and actual > high))
                    else:
                        outside = bool((low and _decimal(actual) < _decimal(low)) or (high and _decimal(actual) > _decimal(high)))
                    if outside:
                        break
                if outside:
                    continue
                row["values"] = values; row["active"] = bool(row["active"])
                display_values = dict(values)
                for field in fields:
                    if field["data_type"] == "relation":
                        display_values[field["field_key"]] = self._relation_label(conn, field["relation_target"], values.get(field["field_key"]))
                row["display_values"] = display_values
                items.append(row)
            aggregates = self._aggregates(items, fields)
            filter_options = {}
            for field in fields:
                if field["filterable"] and field["data_type"] in {"select", "boolean", "relation"}:
                    counts: dict[str, int] = {}
                    for item in items:
                        value = normalize_text(item["values"].get(field["field_key"]))
                        if value: counts[value] = counts.get(value, 0) + 1
                    filter_options[field["field_key"]] = [{"value": key, "label": self._relation_label(conn, field["relation_target"], key) if field["data_type"] == "relation" else key, "count": value} for key, value in sorted(counts.items())]
            return {"module": module, "fields": fields, "items": items, "total": len(items), "aggregates": aggregates, "filter_options": filter_options}

    def export_rows(self, module_ref: str, filters: dict[str, Any] | None = None,
                    columns: list[str] | None = None) -> tuple[str, list[str], list[list[Any]]]:
        data = self.list_records(module_ref, filters)
        allowed = {field["field_key"]: field for field in data["fields"]}
        keys = [key for key in (columns or []) if key in allowed]
        if not keys:
            keys = [field["field_key"] for field in data["fields"] if field["visible"]]
        fields = [allowed[key] for key in keys]
        headers = [field["label"] for field in fields]
        rows = [[item["display_values"].get(field["field_key"], "") for field in fields] for item in data["items"]]
        if data["aggregates"]:
            total_row: list[Any] = []
            for index, field in enumerate(fields):
                aggregate = data["aggregates"].get(field["field_key"])
                total_row.append(aggregate["value"] if aggregate else ("GENEL TOPLAM" if index == 0 else ""))
            rows.append(total_row)
        return data["module"]["label"], headers, rows

    @staticmethod
    def _aggregates(items: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        for field in fields:
            operation = field["aggregate_type"]
            if operation == "none": continue
            raw = [item["values"].get(field["field_key"]) for item in items]
            numbers = [_decimal(value) for value in raw if normalize_text(value) != ""]
            if operation == "count": value = len([item for item in raw if normalize_text(item) != ""])
            elif not numbers: value = Decimal("0")
            elif operation == "sum": value = sum(numbers, Decimal("0"))
            elif operation == "avg": value = sum(numbers, Decimal("0")) / len(numbers)
            elif operation == "min": value = min(numbers)
            else: value = max(numbers)
            result[field["field_key"]] = {"label": field["label"], "operation": operation, "value": str(value), "data_type": field["data_type"], "decimal_places": field["decimal_places"]}
        return result

    def archive_record(self, module_ref: str, record_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_ref)
            before = self._row(conn.execute("SELECT * FROM dynamic_records WHERE id=? AND module_id=? AND active=1", (record_id, module["id"])).fetchone())
            if not before: raise FileNotFoundError("Kayıt bulunamadı.")
            conn.execute("UPDATE dynamic_records SET active=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), record_id))
            after = self._row(conn.execute("SELECT * FROM dynamic_records WHERE id=?", (record_id,)).fetchone())
            self._history(conn, "record", record_id, "archive", f"{before['title']} arşivlendi", before, after)
            return {"id": record_id, "archived": True}

    def archive_records(self, module_ref: str, record_ids: list[str]) -> dict[str, Any]:
        ids = [normalize_text(item) for item in record_ids if normalize_text(item)]
        ids = list(dict.fromkeys(ids))
        if not ids:
            raise DatabaseError("Arşivlenecek kayıt seçilmedi.")
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_ref)
            archived = []
            for record_id in ids:
                before = self._row(conn.execute(
                    "SELECT * FROM dynamic_records WHERE id=? AND module_id=? AND active=1",
                    (record_id, module["id"]),
                ).fetchone())
                if not before:
                    continue
                conn.execute(
                    "UPDATE dynamic_records SET active=0,updated_at=?,row_version=row_version+1 WHERE id=?",
                    (utc_now(), record_id),
                )
                after = self._row(conn.execute("SELECT * FROM dynamic_records WHERE id=?", (record_id,)).fetchone())
                self._history(conn, "record", record_id, "archive", f"{before['title']} arşivlendi", before, after)
                archived.append(record_id)
            if not archived:
                raise FileNotFoundError("Seçilen aktif kayıtlar bulunamadı.")
        return {"archived": archived, "count": len(archived)}

    def list_views(self, module_ref: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            module = self._module_row(conn, module_ref)
            rows = [dict(row) for row in conn.execute("SELECT * FROM dynamic_views WHERE module_id=? AND active=1 ORDER BY is_default DESC,name", (module["id"],))]
            for row in rows:
                row["filters"] = _load(row.pop("filters_json"), {})
                row["columns"] = _load(row.pop("columns_json"), [])
                row["is_default"] = bool(row["is_default"])
            return rows

    def save_view(self, module_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            module = self._module_row(conn, module_ref)
            name = normalize_text(data.get("name"))
            if not name: raise DatabaseError("Görünüm adı zorunludur.")
            view_id = normalize_text(data.get("id"))
            before = self._row(conn.execute("SELECT * FROM dynamic_views WHERE id=? AND module_id=?", (view_id, module["id"])).fetchone())
            default = bool(data.get("is_default"))
            if default: conn.execute("UPDATE dynamic_views SET is_default=0 WHERE module_id=?", (module["id"],))
            now = utc_now()
            if before:
                conn.execute("UPDATE dynamic_views SET name=?,filters_json=?,columns_json=?,is_default=?,updated_at=? WHERE id=?", (name, _json(data.get("filters", {})), _json(data.get("columns", [])), int(default), now, view_id))
            else:
                view_id = _new_id("viw")
                conn.execute("INSERT INTO dynamic_views(id,module_id,name,filters_json,columns_json,is_default,active,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?)", (view_id, module["id"], name, _json(data.get("filters", {})), _json(data.get("columns", [])), int(default), now, now))
            after = self._row(conn.execute("SELECT * FROM dynamic_views WHERE id=?", (view_id,)).fetchone())
            self._history(conn, "view", view_id, "update" if before else "create", f"{name} görünümü kaydedildi", before, after)
        return next(item for item in self.list_views(module["id"]) if item["id"] == view_id)

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM platform_history ORDER BY id DESC LIMIT ?", (max(1, min(500, limit)),))]
        for row in rows:
            row["before"] = _load(row.pop("before_json"), None)
            row["after"] = _load(row.pop("after_json"), None)
        return rows

    def restore_history(self, history_id: int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            history = self._row(conn.execute("SELECT * FROM platform_history WHERE id=?", (int(history_id),)).fetchone())
            if not history or history["entity_type"] not in {"module", "field", "rule", "action"}:
                raise DatabaseError("Bu geçmiş kaydı geri yüklenemez.")
            snapshot = _load(history["before_json"], None)
            entity_type, entity_id = history["entity_type"], history["entity_id"]
            if entity_type == "module":
                current = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (entity_id,)).fetchone())
                if snapshot is None:
                    conn.execute("UPDATE dynamic_modules SET active=0,menu_visible=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), entity_id))
                else:
                    conn.execute(
                        """UPDATE dynamic_modules SET module_key=?,label=?,singular_label=?,icon=?,description=?,color=?,
                           menu_visible=?,sort_order=?,active=?,updated_at=?,row_version=row_version+1 WHERE id=?""",
                        (snapshot["module_key"], snapshot["label"], snapshot["singular_label"], snapshot["icon"],
                         snapshot["description"], snapshot["color"], int(snapshot["menu_visible"]), snapshot["sort_order"],
                         int(snapshot["active"]), utc_now(), entity_id),
                    )
                after = self._row(conn.execute("SELECT * FROM dynamic_modules WHERE id=?", (entity_id,)).fetchone())
            elif entity_type == "field":
                current = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (entity_id,)).fetchone())
                if snapshot is None:
                    conn.execute("UPDATE dynamic_module_fields SET active=0,visible=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), entity_id))
                else:
                    columns = ("module_id", "field_key", "label", "data_type", "options_json", "relation_target", "formula",
                               "default_value", "placeholder", "required", "visible", "filterable", "is_title",
                               "aggregate_type", "decimal_places", "width", "sort_order", "active")
                    conn.execute(
                        f"UPDATE dynamic_module_fields SET {','.join(f'{key}=?' for key in columns)},updated_at=?,row_version=row_version+1 WHERE id=?",
                        tuple(snapshot[key] for key in columns) + (utc_now(), entity_id),
                    )
                    self._validate_formula_graph(self._field_rows(conn, snapshot["module_id"], True))
                after = self._row(conn.execute("SELECT * FROM dynamic_module_fields WHERE id=?", (entity_id,)).fetchone())
            elif entity_type == "rule":
                current = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (entity_id,)).fetchone())
                if snapshot is None:
                    conn.execute("UPDATE dynamic_rules SET active=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), entity_id))
                else:
                    module = self._module_row(conn, snapshot["module_id"])
                    self._sanitize_rule({
                        "id": entity_id, "module_id": snapshot["module_id"], "name": snapshot["name"],
                        "conditions": _load(snapshot["conditions_json"], []),
                        "actions": _load(snapshot["actions_json"], []),
                        "sort_order": snapshot["sort_order"], "active": bool(snapshot["active"]),
                    }, module)
                    columns = ("module_id", "name", "trigger_name", "conditions_json", "actions_json", "sort_order", "active")
                    conn.execute(
                        f"UPDATE dynamic_rules SET {','.join(f'{key}=?' for key in columns)},updated_at=?,row_version=row_version+1 WHERE id=?",
                        tuple(snapshot[key] for key in columns) + (utc_now(), entity_id),
                    )
                after = self._row(conn.execute("SELECT * FROM dynamic_rules WHERE id=?", (entity_id,)).fetchone())
            else:
                current = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (entity_id,)).fetchone())
                if snapshot is None:
                    conn.execute("UPDATE dynamic_actions SET active=0,updated_at=?,row_version=row_version+1 WHERE id=?", (utc_now(), entity_id))
                else:
                    module = self._module_row(conn, snapshot["module_id"])
                    current_clean = None
                    if current:
                        current_clean = dict(current)
                        current_clean["config"] = _load(current_clean.pop("config_json"), {})
                    self._sanitize_action({
                        "id": entity_id, "module_id": snapshot["module_id"], "label": snapshot["label"],
                        "action_type": snapshot["action_type"], "placement": snapshot["placement"], "icon": snapshot["icon"],
                        "style": snapshot["style"], "confirmation_text": snapshot["confirmation_text"],
                        "config": _load(snapshot["config_json"], {}), "sort_order": snapshot["sort_order"],
                        "active": bool(snapshot["active"]),
                    }, module, current_clean)
                    columns = ("module_id", "action_key", "label", "action_type", "placement", "icon", "style",
                               "confirmation_text", "config_json", "sort_order", "active")
                    conn.execute(
                        f"UPDATE dynamic_actions SET {','.join(f'{key}=?' for key in columns)},updated_at=?,row_version=row_version+1 WHERE id=?",
                        tuple(snapshot[key] for key in columns) + (utc_now(), entity_id),
                    )
                after = self._row(conn.execute("SELECT * FROM dynamic_actions WHERE id=?", (entity_id,)).fetchone())
            self._history(conn, entity_type, entity_id, "history_restore", f"Geçmiş sürüm geri yüklendi: {history['summary']}", current, after)
            return {"entity_type": entity_type, "entity_id": entity_id, "restored_history_id": history_id}
