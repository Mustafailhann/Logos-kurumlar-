from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()


def search_key(value: Any) -> str:
    text = normalize_text(value).casefold().translate(str.maketrans({
        "ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u",
    }))
    text = "".join(
        character for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def money_to_minor(value: Any) -> int:
    try:
        amount = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise DatabaseError("Geçerli bir tutar girilmelidir.") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_iso_date(value: Any, label: str, required: bool = False) -> str:
    text = normalize_text(value)
    if not text:
        if required:
            raise DatabaseError(f"{label} zorunludur.")
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise DatabaseError(f"{label} YYYY-AA-GG biçiminde olmalıdır.") from exc
    return text


def _as_values(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [normalize_text(item) for item in values if normalize_text(item)]


def _decimal(value: Any) -> Decimal:
    text = normalize_text(value).replace(".", "").replace(",", ".") if isinstance(value, str) and "," in value else normalize_text(value)
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        return Decimal("0")


def _strict_decimal(value: Any, label: str) -> Decimal:
    text = normalize_text(value)
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise DatabaseError(f"{label} için geçerli bir sayı girilmelidir.") from exc


FORMULA_FUNCTIONS = {"round", "yuvarla", "min", "max", "maks", "abs", "mutlak", "coalesce", "bossa"}


LOOKUP_CATEGORIES = (
    ("city", "İller", "institution", "city", "", 10),
    ("district", "İlçeler", "institution", "district", "city", 20),
    ("school_type", "Okul Türleri", "institution", "school_type", "", 30),
    ("sales_period", "Satış Dönemleri", "institution", "sales_period", "", 40),
    ("sales_person", "Satış Temsilcileri", "institution", "sales_person", "", 50),
    ("dealer", "Bayiler", "institution", "dealer", "", 60),
    ("marketing_person", "Pazarlama Temsilcileri", "institution", "marketing_person", "", 70),
    ("technical_person", "Teknik Servisler", "institution", "technical_person", "", 80),
    ("accounting_person", "Muhasebe Temsilcileri", "institution", "accounting_person", "", 90),
    ("customer_person", "Müşteri Temsilcileri", "institution", "customer_person", "", 100),
    ("customer_status", "Müşteri Durumları", "institution", "customer_status", "", 110),
    ("payment_status", "Ödeme Durumları", "institution", "payment_status", "", 120),
    ("health_status", "Çalışma Durumları", "institution", "health_status", "", 130),
    ("sms_status", "SMS Durumları", "institution", "sms_status", "", 140),
    ("rental_status", "Kiralama Durumları", "institution", "rental_status", "", 150),
    ("panel_status", "Panel Durumları", "panel", "status", "", 160),
)

LOOKUP_FIELD_MAP = {
    key: ("institutions" if entity == "institution" else "panels", field)
    for key, _label, entity, field, _parent, _order in LOOKUP_CATEGORIES
}

DEFAULT_LOOKUP_VALUES = {
    "customer_status": (
        ("AKTİF", "#15865d"), ("PARA KAZANDIRMIYOR", "#d48a19"),
        ("GEÇİCİ KULLANIM DIŞI", "#687871"), ("PAZARLAMA AŞAMASINDA İPTAL", "#c34242"),
        ("KULLANIMI BIRAKANLAR", "#c34242"), ("RAKİBE GİDENLER", "#9b3d91"),
        ("ŞİRKET HESAPLARI", "#2469d8"),
    ),
    "health_status": (("Kurumda sorun yok", "#15865d"), ("Kurumda Hatalar Var", "#c34242")),
    "panel_status": (
        ("AKTİF", "#15865d"), ("PASİF", "#687871"), ("ARIZALI", "#c34242"),
        ("KURULUM AŞAMASINDA", "#d48a19"),
    ),
}

DEFAULT_INSTITUTION_FORM_FIELDS = [
    {"key": "name", "label": "Kurum adı", "control": "text", "required": True, "visible": True, "width": "wide", "placeholder": "", "default_value": ""},
    {"key": "group_number", "label": "Grup numarası", "control": "number", "required": False, "visible": True, "width": "half", "placeholder": "Boşsa yeni grup", "default_value": ""},
    {"key": "sequence_number", "label": "Sıra numarası", "control": "number", "required": False, "visible": True, "width": "half", "placeholder": "Otomatik verilir", "default_value": ""},
    {"key": "portal_id", "label": "Portal kurum ID", "control": "text", "required": False, "visible": True, "width": "half", "placeholder": "", "default_value": ""},
    {"key": "institution_code", "label": "Kurum kodu", "control": "text", "required": False, "visible": True, "width": "half", "placeholder": "", "default_value": ""},
    {"key": "city", "label": "İl", "control": "lookup", "lookup_category": "city", "required": False, "visible": True, "width": "half", "placeholder": "İl seçin", "default_value": ""},
    {"key": "district", "label": "İlçe", "control": "lookup", "lookup_category": "district", "required": False, "visible": True, "width": "half", "placeholder": "Önce il seçin", "default_value": ""},
    {"key": "school_type", "label": "Okul türü", "control": "lookup", "lookup_category": "school_type", "required": False, "visible": True, "width": "half", "placeholder": "Okul türü seçin", "default_value": ""},
    {"key": "sales_period", "label": "Satış dönemi", "control": "lookup", "lookup_category": "sales_period", "required": False, "visible": True, "width": "half", "placeholder": "Dönem seçin", "default_value": ""},
    {"key": "sales_person", "label": "Satışı yapan kişi", "control": "lookup", "lookup_category": "sales_person", "required": False, "visible": True, "width": "half", "placeholder": "Temsilci seçin", "default_value": ""},
    {"key": "dealer", "label": "Bayi", "control": "lookup", "lookup_category": "dealer", "required": False, "visible": True, "width": "half", "placeholder": "Bayi seçin", "default_value": ""},
    {"key": "marketing_person", "label": "Pazarlama temsilcisi", "control": "lookup", "lookup_category": "marketing_person", "required": False, "visible": False, "width": "half", "placeholder": "Temsilci seçin", "default_value": ""},
    {"key": "technical_person", "label": "Teknik servis", "control": "lookup", "lookup_category": "technical_person", "required": False, "visible": True, "width": "half", "placeholder": "Teknik servis seçin", "default_value": ""},
    {"key": "accounting_person", "label": "Muhasebe temsilcisi", "control": "lookup", "lookup_category": "accounting_person", "required": False, "visible": True, "width": "half", "placeholder": "Temsilci seçin", "default_value": ""},
    {"key": "customer_person", "label": "Müşteri temsilcisi", "control": "lookup", "lookup_category": "customer_person", "required": False, "visible": True, "width": "half", "placeholder": "Temsilci seçin", "default_value": ""},
    {"key": "customer_status", "label": "Müşteri durumu", "control": "lookup", "lookup_category": "customer_status", "required": False, "visible": True, "width": "half", "placeholder": "Durum seçin", "default_value": ""},
    {"key": "payment_status", "label": "Ödeme durumu", "control": "lookup", "lookup_category": "payment_status", "required": False, "visible": True, "width": "half", "placeholder": "Durum seçin", "default_value": ""},
    {"key": "health_status", "label": "Çalışma durumu", "control": "lookup", "lookup_category": "health_status", "required": False, "visible": True, "width": "half", "placeholder": "Durum seçin", "default_value": ""},
    {"key": "sms_status", "label": "SMS durumu", "control": "lookup", "lookup_category": "sms_status", "required": False, "visible": False, "width": "half", "placeholder": "Durum seçin", "default_value": ""},
    {"key": "rental_status", "label": "Kiralama durumu", "control": "lookup", "lookup_category": "rental_status", "required": False, "visible": False, "width": "half", "placeholder": "Durum seçin", "default_value": ""},
    {"key": "rating", "label": "Değerlendirme", "control": "number", "required": False, "visible": False, "width": "half", "placeholder": "0-5", "default_value": ""},
    {"key": "pilot", "label": "Pilot kurum", "control": "checkbox", "required": False, "visible": True, "width": "half", "placeholder": "", "default_value": False},
    {"key": "notes", "label": "Notlar", "control": "textarea", "required": False, "visible": True, "width": "wide", "placeholder": "", "default_value": ""},
]

DEFAULT_FINANCE_SUMMARY_CARDS = [
    {"id": "card_institution", "label": "Kurum", "metric": "institution_count", "format": "number", "subtitle": "Çatı kurum sayısı", "color": "neutral", "visible": True},
    {"id": "card_revenue", "label": "Toplam Ciro", "metric": "field:toplam_ciro", "format": "money", "subtitle": "Filtrelenmiş sonuç", "color": "neutral", "visible": True},
    {"id": "card_paid", "label": "Tahsil Edilen", "metric": "field:tahsilat", "format": "money", "subtitle": "Filtrelenmiş sonuç", "color": "success", "visible": True},
    {"id": "card_balance", "label": "Bakiye", "metric": "field:bakiye", "format": "money", "subtitle": "Filtrelenmiş sonuç", "color": "danger", "visible": True},
]

DEFAULT_NAVIGATION_PREFERENCES = [
    {"key": "institutions", "label": "Kurumlar", "icon": "▦", "title": "Kurum ve panel takibi", "subtitle": "Çatı kurumlar tek kurum sayılır; alt kurumların ID ve panelleri ayrı izlenir.", "visible": True},
    {"key": "finance", "label": "Finans", "icon": "₺", "title": "Finans takibi", "subtitle": "Bütün kurumlar otomatik görünür; finans alanları ve formüller sizin tarafınızdan yönetilir.", "visible": True},
    {"key": "commissions", "label": "Prim", "icon": "%", "title": "Dinamik prim hesabı", "subtitle": "Ölçütleri, koşulları, prim tabanını ve hesaplama şeklini siz belirlersiniz.", "visible": True},
    {"key": "backups", "label": "Yedekler", "icon": "⟳", "title": "Yedekler", "subtitle": "", "visible": True},
    {"key": "settings", "label": "Ayarlar", "icon": "⚙", "title": "Ayarlar merkezi", "subtitle": "Kurum ve finans yapısını kod değişikliği gerektirmeden tek yerden yönetin.", "visible": True},
]

DEFAULT_THEME_PREFERENCES = {
    "background": "#f4f7f6", "surface": "#ffffff", "text": "#15231e", "muted": "#687871",
    "primary": "#15865d", "primary_dark": "#0d6847", "danger": "#c34242", "sidebar": "#0d2a21",
    "font_scale": 100, "density": "normal", "radius": 12, "sidebar_width": 220,
}


def formula_names(expression: str) -> set[str]:
    """Formülde kullanılan alan anahtarlarını güvenli AST üzerinden döndürür."""
    try:
        tree = ast.parse(expression or "0", mode="eval")
    except SyntaxError as exc:
        raise DatabaseError("Finans formülü geçersiz.") from exc
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return {name for name in names if search_key(name) not in FORMULA_FUNCTIONS}


def evaluate_formula(expression: str, values: dict[str, Any]) -> Decimal:
    """Alan adı, dört işlem ve sınırlı güvenli fonksiyonları kabul eden formül motoru."""
    try:
        tree = ast.parse(expression or "0", mode="eval")
    except SyntaxError as exc:
        raise DatabaseError("Finans formülü geçersiz.") from exc

    def visit(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            return _decimal(values.get(node.id, 0))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = visit(node.operand)
            return result if isinstance(node.op, ast.UAdd) else -result
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                return Decimal("0")
            return left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            function = search_key(node.func.id)
            arguments = [visit(argument) for argument in node.args]
            if function in {"round", "yuvarla"} and arguments:
                places = int(arguments[1]) if len(arguments) > 1 else 2
                places = min(6, max(0, places))
                return arguments[0].quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)
            if function == "min" and arguments:
                return min(arguments)
            if function in {"max", "maks"} and arguments:
                return max(arguments)
            if function in {"abs", "mutlak"} and len(arguments) == 1:
                return abs(arguments[0])
            if function in {"coalesce", "bossa"} and arguments:
                return next((argument for argument in arguments if argument != 0), Decimal("0"))
        raise DatabaseError("Formülde yalnızca alan adları, sayılar, + - * / ve güvenli fonksiyonlar kullanılabilir.")

    return visit(tree).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campuses (
    id TEXT PRIMARY KEY,
    campus_code TEXT UNIQUE,
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    portal_id TEXT UNIQUE,
    institution_code TEXT UNIQUE,
    campus_id TEXT REFERENCES campuses(id) ON UPDATE CASCADE ON DELETE SET NULL,
    sequence_number INTEGER,
    group_number INTEGER,
    search_text TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    school_type TEXT NOT NULL DEFAULT '',
    sms_status TEXT NOT NULL DEFAULT '',
    rental_status TEXT NOT NULL DEFAULT '',
    customer_status TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT '',
    sales_period TEXT NOT NULL DEFAULT '',
    sales_person TEXT NOT NULL DEFAULT '',
    dealer TEXT NOT NULL DEFAULT '',
    marketing_person TEXT NOT NULL DEFAULT '',
    technical_person TEXT NOT NULL DEFAULT '',
    accounting_person TEXT NOT NULL DEFAULT '',
    customer_person TEXT NOT NULL DEFAULT '',
    pilot INTEGER NOT NULL DEFAULT 0 CHECK(pilot IN (0, 1)),
    health_status TEXT NOT NULL DEFAULT '',
    rating INTEGER CHECK(rating IS NULL OR (rating BETWEEN 0 AND 5)),
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_row INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS panels (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    panel_key TEXT NOT NULL,
    physical_system_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    gate_name TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT 'Kapı Kontrol',
    turnstile_count INTEGER CHECK(turnstile_count IS NULL OR turnstile_count >= 0),
    turnstile_label TEXT NOT NULL DEFAULT '',
    local_ip TEXT NOT NULL DEFAULT '',
    external_ip TEXT NOT NULL DEFAULT '',
    software_version TEXT NOT NULL DEFAULT '',
    database_version TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    modem TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    entry_camera_status TEXT NOT NULL DEFAULT '',
    entry_camera_ip TEXT NOT NULL DEFAULT '',
    entry_camera_rtsp TEXT NOT NULL DEFAULT '',
    exit_camera_status TEXT NOT NULL DEFAULT '',
    exit_camera_ip TEXT NOT NULL DEFAULT '',
    exit_camera_rtsp TEXT NOT NULL DEFAULT '',
    installation_date TEXT NOT NULL DEFAULT '',
    installed_by TEXT NOT NULL DEFAULT '',
    warranty_end TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    raw_detail TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(institution_id, panel_key)
);

CREATE TABLE IF NOT EXISTS custom_field_definitions (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('institution', 'panel')),
    field_key TEXT NOT NULL,
    label TEXT NOT NULL,
    data_type TEXT NOT NULL CHECK(data_type IN ('text', 'date', 'number', 'list', 'boolean')),
    options_json TEXT NOT NULL DEFAULT '[]',
    required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity_type, field_key)
);

CREATE TABLE IF NOT EXISTS custom_values (
    field_id TEXT NOT NULL REFERENCES custom_field_definitions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('institution', 'panel')),
    entity_id TEXT NOT NULL,
    value_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(field_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'Yerel Kullanıcı',
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    changes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS import_runs (
    id TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    total_records INTEGER NOT NULL,
    new_records INTEGER NOT NULL,
    updated_records INTEGER NOT NULL,
    unchanged_records INTEGER NOT NULL,
    panel_records INTEGER NOT NULL,
    result TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_setting_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setting_key TEXT NOT NULL,
    value TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lookup_categories (
    category_key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('institution','panel')),
    field_key TEXT NOT NULL,
    parent_category_key TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lookup_items (
    id TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES lookup_categories(category_key) ON UPDATE CASCADE ON DELETE RESTRICT,
    item_key TEXT NOT NULL,
    label TEXT NOT NULL,
    parent_item_key TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category_key, item_key, parent_item_key)
);

CREATE TABLE IF NOT EXISTS institution_groups (
    id TEXT PRIMARY KEY,
    group_number INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS finance_field_definitions (
    id TEXT PRIMARY KEY,
    field_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    data_type TEXT NOT NULL CHECK(data_type IN ('text','date','number','money','list','multiselect','boolean','formula')),
    options_json TEXT NOT NULL DEFAULT '[]',
    formula TEXT NOT NULL DEFAULT '',
    default_value TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0,1)),
    decimal_places INTEGER NOT NULL DEFAULT 2 CHECK(decimal_places BETWEEN 0 AND 6),
    aggregate_type TEXT NOT NULL DEFAULT 'none' CHECK(aggregate_type IN ('none','sum','avg','min','max','count')),
    system_field INTEGER NOT NULL DEFAULT 0 CHECK(system_field IN (0,1)),
    formula_version INTEGER NOT NULL DEFAULT 1,
    filterable INTEGER NOT NULL DEFAULT 1 CHECK(filterable IN (0,1)),
    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
    sort_order INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_formula_versions (
    id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL REFERENCES finance_field_definitions(id) ON UPDATE CASCADE ON DELETE CASCADE,
    version INTEGER NOT NULL,
    formula TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(field_id, version)
);

CREATE TABLE IF NOT EXISTS finance_values (
    institution_id TEXT NOT NULL REFERENCES institutions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    field_id TEXT NOT NULL REFERENCES finance_field_definitions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    value_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(institution_id, field_id)
);

CREATE TABLE IF NOT EXISTS commission_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sales_person TEXT NOT NULL DEFAULT '',
    conditions_json TEXT NOT NULL DEFAULT '[]',
    base_field_key TEXT NOT NULL,
    calculation_type TEXT NOT NULL CHECK(calculation_type IN ('percent','fixed','per_unit')),
    rate_text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS commission_runs (
    id TEXT PRIMARY KEY,
    period_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL,
    total_minor INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
    approved_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS finance_accounts (
    id TEXT PRIMARY KEY,
    account_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    search_text TEXT NOT NULL DEFAULT '',
    tax_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    billing_address TEXT NOT NULL DEFAULT '',
    sales_person TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'AKTİF' CHECK(status IN ('AKTİF', 'PASİF')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS finance_account_institutions (
    account_id TEXT NOT NULL REFERENCES finance_accounts(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    institution_id TEXT NOT NULL REFERENCES institutions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, institution_id)
);

CREATE TABLE IF NOT EXISTS finance_contracts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES finance_accounts(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    contract_no TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    billing_cycle TEXT NOT NULL DEFAULT 'AYLIK' CHECK(billing_cycle IN ('AYLIK', 'YILLIK', 'TEK SEFER')),
    base_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(base_amount_minor >= 0),
    vat_rate_basis INTEGER NOT NULL DEFAULT 0 CHECK(vat_rate_basis BETWEEN 0 AND 10000),
    commission_rate_basis INTEGER NOT NULL DEFAULT 0 CHECK(commission_rate_basis BETWEEN 0 AND 10000),
    status TEXT NOT NULL DEFAULT 'AKTİF' CHECK(status IN ('AKTİF', 'TAMAMLANDI', 'İPTAL')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS finance_transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES finance_accounts(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    institution_id TEXT REFERENCES institutions(id) ON UPDATE CASCADE ON DELETE SET NULL,
    contract_id TEXT REFERENCES finance_contracts(id) ON UPDATE CASCADE ON DELETE SET NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('FATURA', 'ÖDEME', 'İNDİRİM', 'DÜZELTME')),
    document_no TEXT NOT NULL DEFAULT '',
    transaction_date TEXT NOT NULL,
    due_date TEXT NOT NULL DEFAULT '',
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'TRY',
    status TEXT NOT NULL DEFAULT 'KAYITLI' CHECK(status IN ('KAYITLI', 'İPTAL')),
    description TEXT NOT NULL DEFAULT '',
    reversal_reason TEXT NOT NULL DEFAULT '',
    reversed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_institutions_city ON institutions(city, district);
CREATE INDEX IF NOT EXISTS idx_institutions_health ON institutions(health_status);
CREATE INDEX IF NOT EXISTS idx_institutions_active ON institutions(active);
CREATE INDEX IF NOT EXISTS idx_panels_institution ON panels(institution_id);
CREATE INDEX IF NOT EXISTS idx_panels_physical_system ON panels(physical_system_key);
CREATE INDEX IF NOT EXISTS idx_panels_status ON panels(status);
CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_finance_link_institution ON finance_account_institutions(institution_id);
CREATE INDEX IF NOT EXISTS idx_finance_transactions_account ON finance_transactions(account_id, transaction_date);
CREATE INDEX IF NOT EXISTS idx_finance_transactions_due ON finance_transactions(due_date, status);
CREATE INDEX IF NOT EXISTS idx_groups_number ON institution_groups(group_number);
CREATE INDEX IF NOT EXISTS idx_finance_values_institution ON finance_values(institution_id);
CREATE INDEX IF NOT EXISTS idx_setting_history_key ON app_setting_history(setting_key, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_formula_versions_field ON finance_formula_versions(field_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_lookup_items_category ON lookup_items(category_key, active, sort_order, label);
CREATE INDEX IF NOT EXISTS idx_lookup_items_parent ON lookup_items(category_key, parent_item_key, active);
"""


INSTITUTION_FIELDS = {
    "portal_id", "institution_code", "campus_id", "sequence_number", "group_number", "name", "city", "district",
    "school_type", "sms_status", "rental_status", "customer_status", "payment_status",
    "sales_period", "sales_person", "dealer", "marketing_person", "technical_person",
    "accounting_person", "customer_person", "pilot", "health_status", "rating", "notes",
    "source", "source_row", "active",
}

PANEL_FIELDS = {
    "institution_id", "panel_key", "physical_system_key", "name", "gate_name",
    "product_name", "turnstile_count", "turnstile_label", "local_ip", "external_ip",
    "software_version", "database_version", "last_seen", "modem", "operator", "phone",
    "entry_camera_status", "entry_camera_ip", "entry_camera_rtsp", "exit_camera_status",
    "exit_camera_ip", "exit_camera_rtsp", "installation_date", "installed_by", "warranty_end",
    "status", "notes", "raw_detail", "active",
}

# Sürüm 1.0 veritabanlarında portalın birleşik grup/sıra değeri saklanmıyordu.
# Yalnızca ilk teslimdeki 276 portal ID'sinin sırası tam olarak eşleşirse kullanılır.
LEGACY_PORTAL_ORDER_SHA256 = "3a9f39066f5520f9d7e61fa714eae6614dd0f28c0bb0dba5c135285a6e0459e4"
LEGACY_EXCEL_GROUPS = (
    1, 2, 3, 4, 5, 6, 7, 8, 8, 9, 9, 10, 11, 12, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28, 29, 30, 30, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,
    44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 61, 61, 62, 63, 64, 65,
    66, 67, 67, 67, 67, 68, 68, 68, 69, 69, 69, 70, 70, 71, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80,
    81, 82, 83, 84, 85, 85, 86, 87, 88, 89, 90, 90, 91, 92, 92, 92, 93, 94, 95, 96, 97, 98, 98, 98,
    98, 98, 99, 100, 101, 101, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 111, 112, 113, 114, 115, 116, 117,
    118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 133, 133, 134, 134, 135, 136, 137, 138,
    139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 148, 148, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159,
    160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183,
    184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207,
    208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 217, 217, 217, 218, 219, 219, 219, 220, 221, 222, 223, 224, 225, 226,
    227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238,
)


class DatabaseError(RuntimeError):
    pass


class ConflictError(DatabaseError):
    pass


class ClosingConnection(sqlite3.Connection):
    """`with db.connect()` blokları bittiğinde bağlantıyı gerçekten kapatır."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class Database:
    def __init__(self, data_dir: Path):
        path = Path(data_dir)
        if path.is_file() or path.name.endswith(".db"):
            self.data_dir = path.parent
            self.path = path
        else:
            self.data_dir = path
            self.path = self.data_dir / "okul_guvenligi.db"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.data_dir / "yedekler"
        self.backup_dir.mkdir(exist_ok=True)
        self.media_dir = self.data_dir / "medya"
        self.media_dir.mkdir(exist_ok=True)
        self._backup_lock = threading.Lock()
        self.initialize()


    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        needs_upgrade = False
        if self.path.exists() and self.path.stat().st_size:
            # Var olan veritabanına herhangi bir migration/yazma yapmadan önce fail-closed kontrol.
            preflight = self.integrity_check()
            if preflight != "ok":
                raise DatabaseError(
                    f"Güvenli açılış durduruldu: mevcut veritabanı bütünlük kontrolü başarısız: {preflight}"
                )
            with self.connect() as conn:
                table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='institutions'").fetchone()
                if table:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(institutions)")}
                    media_missing = not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_assets'"
                    ).fetchone()
                    version_row = conn.execute(
                        "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
                    ).fetchone() if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone() else (0,)
                    needs_upgrade = (
                        "sequence_number" not in columns
                        or "search_text" not in columns
                        or int(version_row[0]) < 5
                        or media_missing
                    )
        if needs_upgrade:
            self.backup("veritabani_yukseltme_oncesi")
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            self._migrate_v2(conn)
            self._migrate_v3(conn)
            self._migrate_v4(conn)
            self._migrate_v5(conn)
            self._migrate_v6(conn)
        self.recalculate_health_statuses()
        result = self.integrity_check()
        if result != "ok":
            raise DatabaseError(f"Veritabanı bütünlük kontrolü başarısız: {result}")


    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(institutions)")}
        additions = {
            "sequence_number": "INTEGER",
            "group_number": "INTEGER",
            "search_text": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE institutions ADD COLUMN {column} {definition}")
        finance_columns = {row[1] for row in conn.execute("PRAGMA table_info(finance_accounts)")}
        if "search_text" not in finance_columns:
            conn.execute("ALTER TABLE finance_accounts ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
        self._backfill_numbers(conn)
        self._rebuild_all_search_text(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_institutions_group ON institutions(group_number, sequence_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_institutions_search ON institutions(search_text)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_institutions_sequence_unique ON institutions(sequence_number) WHERE sequence_number IS NOT NULL")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
            (utc_now(),),
        )

    @staticmethod
    def _suggest_group_name(names: list[str]) -> str:
        cleaned = []
        suffix = re.compile(
            r"\s*[-–—]\s*(İLKOKULU?|ORTAOKULU?|LİSESİ?|ANAOKULU?|KURS MERKEZİ(?:\s+(?:İLKOKUL|ORTAOKUL|LİSE))?)\s*$",
            re.IGNORECASE,
        )
        for name in names:
            cleaned.append(suffix.sub("", normalize_text(name)).strip(" -–—"))
        if cleaned and len(set(cleaned)) == 1:
            return cleaned[0]
        if not names:
            return "Adsız kurum grubu"
        common = os.path.commonprefix([normalize_text(name) for name in names]).rstrip(" -–—")
        common = common.rsplit(" ", 1)[0] if common and not all(name.startswith(common + " ") or name == common for name in names) else common
        return common if len(common) >= 5 else min(names, key=len)

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        for group_number_row in conn.execute(
            "SELECT DISTINCT group_number FROM institutions WHERE active=1 AND group_number IS NOT NULL ORDER BY group_number"
        ).fetchall():
            group_number = int(group_number_row[0])
            members = conn.execute(
                "SELECT name, city, district FROM institutions WHERE active=1 AND group_number=? ORDER BY sequence_number",
                (group_number,),
            ).fetchall()
            name = self._suggest_group_name([row["name"] for row in members])
            city = next((row["city"] for row in members if row["city"]), "")
            district = next((row["district"] for row in members if row["district"]), "")
            conn.execute("""
                INSERT INTO institution_groups(id, group_number, name, city, district, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_number) DO NOTHING
            """, (f"grup_{group_number}", group_number, name, city, district, now, now))

        defaults = [
            ("sozlesme_tarihi", "Söz. Tarihi", "date", "", 10),
            ("sozlesme_kart_sayisi", "Söz. K. Say.", "number", "", 20),
            ("birinci_kart_fiyati", "Birim 1. Kart Fiyatı", "money", "", 30),
            ("ikinci_kart_fiyati", "Birim 2. Kart Fiyatı", "money", "", 40),
            ("sozlesme_cirosu", "Söz. Ciro", "formula", "sozlesme_kart_sayisi * birinci_kart_fiyati", 50),
            ("basilan_birinci_kart_sayisi", "Bas. 1. Kart K. Say.", "number", "", 60),
            ("basilan_ikinci_kart_sayisi", "Bas. 2. Kart K. Say.", "number", "", 70),
            ("ikinci_kart_cirosu", "2. Kart Ciro", "formula", "basilan_ikinci_kart_sayisi * ikinci_kart_fiyati", 80),
            ("toplam_ciro", "Toplam Ciro", "formula", "sozlesme_cirosu + ikinci_kart_cirosu", 90),
            ("tahsilat", "Tahsil", "money", "", 100),
            ("bakiye", "Bakiye", "formula", "toplam_ciro - tahsilat", 110),
            ("pazarlama", "Pazarlama", "list", "", 120),
        ]
        marketing_options = json.dumps(["Görüşülmedi", "Görüşülüyor", "Teklif verildi", "Kazanıldı", "Kaybedildi"], ensure_ascii=False)
        for key, label, data_type, formula, order in defaults:
            conn.execute("""
                INSERT INTO finance_field_definitions(
                    id, field_key, label, data_type, options_json, formula,
                    filterable, visible, sort_order, active, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,1,1,?,1,?,?) ON CONFLICT(field_key) DO NOTHING
            """, (f"ffin_{key}", key, label, data_type, marketing_options if key == "pazarlama" else "[]", formula, order, now, now))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now,))

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Dinamik finans, güvenli formül sürümleri ve ayar geçmişi."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(finance_field_definitions)")}
        additions = {
            "default_value": "TEXT NOT NULL DEFAULT ''",
            "required": "INTEGER NOT NULL DEFAULT 0",
            "decimal_places": "INTEGER NOT NULL DEFAULT 2",
            "aggregate_type": "TEXT NOT NULL DEFAULT 'none'",
            "system_field": "INTEGER NOT NULL DEFAULT 0",
            "formula_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, definition in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE finance_field_definitions ADD COLUMN {column} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finance_formula_versions (
                id TEXT PRIMARY KEY,
                field_id TEXT NOT NULL REFERENCES finance_field_definitions(id) ON UPDATE CASCADE ON DELETE CASCADE,
                version INTEGER NOT NULL,
                formula TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(field_id, version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_setting_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL,
                value TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_setting_history_key ON app_setting_history(setting_key, changed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_formula_versions_field ON finance_formula_versions(field_id, version DESC)")
        default_keys = {
            "sozlesme_tarihi", "sozlesme_kart_sayisi", "birinci_kart_fiyati", "ikinci_kart_fiyati",
            "sozlesme_cirosu", "basilan_birinci_kart_sayisi", "basilan_ikinci_kart_sayisi",
            "ikinci_kart_cirosu", "toplam_ciro", "tahsilat", "bakiye", "pazarlama",
        }
        placeholders = ",".join("?" for _ in default_keys)
        conn.execute(
            f"UPDATE finance_field_definitions SET system_field=1 WHERE field_key IN ({placeholders})",
            sorted(default_keys),
        )
        conn.execute("""
            UPDATE finance_field_definitions
            SET aggregate_type='sum'
            WHERE data_type IN ('number','money','formula') AND aggregate_type='none'
        """)
        now = utc_now()
        for row in conn.execute("SELECT id,formula,formula_version FROM finance_field_definitions WHERE data_type='formula'"):
            conn.execute("""
                INSERT OR IGNORE INTO finance_formula_versions(id,field_id,version,formula,created_at)
                VALUES(?,?,?,?,?)
            """, (f"ffver_{row['id']}_{row['formula_version']}", row["id"], row["formula_version"], row["formula"], now))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)", (now,))

    @staticmethod
    def _lookup_item_id(category_key: str, item_key: str, parent_item_key: str = "") -> str:
        digest = hashlib.sha256(f"{category_key}\0{parent_item_key}\0{item_key}".encode("utf-8")).hexdigest()[:24]
        return f"look_{digest}"

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        """Tek kaynaklı tanım listeleri ve güvenli arayüz yapılandırması."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lookup_categories (
                category_key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('institution','panel')),
                field_key TEXT NOT NULL,
                parent_category_key TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 100,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lookup_items (
                id TEXT PRIMARY KEY,
                category_key TEXT NOT NULL REFERENCES lookup_categories(category_key) ON UPDATE CASCADE ON DELETE RESTRICT,
                item_key TEXT NOT NULL,
                label TEXT NOT NULL,
                parent_item_key TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 100,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(category_key, item_key, parent_item_key)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lookup_items_category ON lookup_items(category_key, active, sort_order, label)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lookup_items_parent ON lookup_items(category_key, parent_item_key, active)")
        now = utc_now()
        for key, label, entity, field, parent, order in LOOKUP_CATEGORIES:
            conn.execute("""
                INSERT INTO lookup_categories(
                    category_key,label,entity_type,field_key,parent_category_key,sort_order,active,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,1,?,?)
                ON CONFLICT(category_key) DO UPDATE SET
                    entity_type=excluded.entity_type,field_key=excluded.field_key,parent_category_key=excluded.parent_category_key
            """, (key, label, entity, field, parent, order, now, now))
        for category_key, values in DEFAULT_LOOKUP_VALUES.items():
            for order, (label, color) in enumerate(values, 1):
                conn.execute("""
                    INSERT OR IGNORE INTO lookup_items(
                        id,category_key,item_key,label,parent_item_key,color,sort_order,active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,1,?,?)
                """, (self._lookup_item_id(category_key, label), category_key, label, label, "", color, order * 10, now, now))
        self._sync_lookup_values(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)", (now,))

    def _migrate_v6(self, conn: sqlite3.Connection) -> None:
        """Kurum fotoğrafı ile başlayıp gelecekte diğer kayıt türlerine de açılabilecek medya motoru."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS media_assets (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                slot TEXT NOT NULL DEFAULT 'primary',
                original_name TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, slot)
            );
            CREATE INDEX IF NOT EXISTS idx_media_assets_entity
            ON media_assets(entity_type, entity_id, active, slot);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)", (utc_now(),))

    def _sync_lookup_values(self, conn: sqlite3.Connection) -> None:
        """Mevcut ve içe aktarılan serbest metinleri veri kaybetmeden tanım listelerine ekler."""
        now = utc_now()
        category_rows = conn.execute(
            "SELECT category_key,entity_type,field_key FROM lookup_categories WHERE active=1 AND category_key<>'district'"
        ).fetchall()
        for category in category_rows:
            table = "institutions" if category["entity_type"] == "institution" else "panels"
            column = category["field_key"]
            for row in conn.execute(f"SELECT DISTINCT {column} AS value FROM {table} WHERE {column}<>'' ORDER BY {column}"):
                value = normalize_text(row["value"])
                if not value:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO lookup_items(
                        id,category_key,item_key,label,parent_item_key,color,sort_order,active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'',1000,1,?,?)
                """, (self._lookup_item_id(category["category_key"], value), category["category_key"], value, value, "", now, now))
        for row in conn.execute("""
            SELECT DISTINCT city,district FROM institutions
            WHERE city<>'' AND district<>'' ORDER BY city,district
        """):
            city, district = normalize_text(row["city"]), normalize_text(row["district"])
            conn.execute("""
                INSERT OR IGNORE INTO lookup_items(
                    id,category_key,item_key,label,parent_item_key,color,sort_order,active,created_at,updated_at
                ) VALUES(?,?,?,?,?,'',1000,1,?,?)
            """, (self._lookup_item_id("district", district, city), "district", district, district, city, now, now))

    def _backfill_numbers(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("""
            SELECT id, portal_id, source_row, sequence_number, group_number
            FROM institutions
            ORDER BY CASE WHEN source_row IS NULL THEN 1 ELSE 0 END, source_row, created_at, id
        """).fetchall()
        if not rows:
            return
        legacy_rows = [row for row in rows if row["source_row"] is not None and row["portal_id"]]
        legacy_numbers: dict[str, tuple[int, int]] = {}
        if len(legacy_rows) == len(LEGACY_EXCEL_GROUPS):
            order_digest = hashlib.sha256(
                "\n".join(row["portal_id"] for row in legacy_rows).encode("utf-8")
            ).hexdigest()
            if order_digest == LEGACY_PORTAL_ORDER_SHA256:
                legacy_numbers = {
                    row["id"]: (index, LEGACY_EXCEL_GROUPS[index - 1])
                    for index, row in enumerate(legacy_rows, 1)
                }
        parent = {row["id"]: row["id"] for row in rows}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        by_system: dict[str, str] = {}
        for panel in conn.execute("SELECT institution_id, physical_system_key FROM panels WHERE active=1 AND physical_system_key<>''"):
            first = by_system.setdefault(panel["physical_system_key"], panel["institution_id"])
            if first in parent and panel["institution_id"] in parent:
                union(first, panel["institution_id"])
        group_by_root: dict[str, int] = {}
        next_group = 1
        for index, row in enumerate(rows, 1):
            known = legacy_numbers.get(row["id"])
            sequence = row["sequence_number"] or (known[0] if known else index)
            group = row["group_number"] or (known[1] if known else None)
            if not group:
                root = find(row["id"])
                if root not in group_by_root:
                    group_by_root[root] = next_group
                    next_group += 1
                group = group_by_root[root]
            conn.execute(
                "UPDATE institutions SET sequence_number=?, group_number=? WHERE id=?",
                (sequence, group, row["id"]),
            )

    def _institution_search_text(self, conn: sqlite3.Connection, institution_id: str) -> str:
        institution = conn.execute("SELECT * FROM institutions WHERE id=?", (institution_id,)).fetchone()
        if not institution:
            return ""
        values = [
            institution["name"], institution["city"], institution["district"], institution["school_type"],
            institution["portal_id"], institution["institution_code"], institution["sales_person"],
            institution["dealer"], institution["technical_person"], institution["accounting_person"],
            institution["customer_person"], institution["sequence_number"], institution["group_number"],
        ]
        for panel in conn.execute("""
            SELECT name, panel_key, gate_name, local_ip, external_ip, phone, installed_by,
                   entry_camera_ip, entry_camera_rtsp, exit_camera_ip, exit_camera_rtsp
            FROM panels WHERE institution_id=? AND active=1
        """, (institution_id,)):
            values.extend(panel)
        for custom in conn.execute("""
            SELECT value_text FROM custom_values WHERE entity_type='institution' AND entity_id=?
        """, (institution_id,)):
            values.append(custom[0])
        return search_key(" ".join(normalize_text(value) for value in values if value is not None))

    def _rebuild_search_text(self, conn: sqlite3.Connection, institution_id: str) -> None:
        conn.execute(
            "UPDATE institutions SET search_text=? WHERE id=?",
            (self._institution_search_text(conn, institution_id), institution_id),
        )
        for row in conn.execute(
            "SELECT account_id FROM finance_account_institutions WHERE institution_id=?",
            (institution_id,),
        ):
            self._rebuild_finance_account_search(conn, row[0])

    def _rebuild_all_search_text(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT id FROM institutions"):
            self._rebuild_search_text(conn, row[0])

    def integrity_check(self, path: Path | None = None) -> str:
        target = path or self.path
        conn = None
        try:
            conn = sqlite3.connect(target)
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row else "sonuç yok"
        except sqlite3.DatabaseError as exc:
            # Sağlık kontrolü çağıran katmanların fail-closed karar verebilmesi için exception yerine sonuç döndür.
            return f"sqlite_error: {exc}"
        finally:
            if conn is not None:
                conn.close()

    def backup(self, reason: str = "manuel", retain: int = 30) -> Path:
        with self._backup_lock:
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:40] or "yedek"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            temp_path = self.backup_dir / f".{stamp}_{safe_reason}.tmp"
            final_path = self.backup_dir / f"{stamp}_{safe_reason}.db"
            source = self.connect()
            dest = sqlite3.connect(temp_path)
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
                source.close()
            if self.integrity_check(temp_path) != "ok":
                temp_path.unlink(missing_ok=True)
                raise DatabaseError("Oluşturulan yedek bütünlük kontrolünden geçemedi.")
            os.replace(temp_path, final_path)
            digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
            final_path.with_suffix(".sha256").write_text(digest, encoding="utf-8")
            backups = sorted(self.backup_dir.glob("*.db"), reverse=True)
            for old in backups[retain:]:
                old.unlink(missing_ok=True)
                old.with_suffix(".sha256").unlink(missing_ok=True)
            return final_path

    def list_backups(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.backup_dir.glob("*.db"), reverse=True):
            items.append({
                "name": path.name,
                "size": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
        return items

    def restore_backup(self, name: str) -> None:
        if Path(name).name != name or not name.endswith(".db"):
            raise DatabaseError("Geçersiz yedek adı.")
        source = self.backup_dir / name
        if not source.exists() or self.integrity_check(source) != "ok":
            raise DatabaseError("Yedek bulunamadı veya bozuk.")
        self.backup("geri_yukleme_oncesi")
        with self._backup_lock:
            temp = self.data_dir / ".restore.tmp"
            shutil.copy2(source, temp)
            if self.integrity_check(temp) != "ok":
                temp.unlink(missing_ok=True)
                raise DatabaseError("Geri yüklenecek dosya doğrulanamadı.")
            with self.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.replace(temp, self.path)
            self.path.with_name(self.path.name + "-wal").unlink(missing_ok=True)
            self.path.with_name(self.path.name + "-shm").unlink(missing_ok=True)
            self.initialize()

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def _institution_filter_sql(self, filters: dict[str, Any] | None = None) -> tuple[str, list[Any]]:
        filters = filters or {}
        clauses = ["i.active = 1"]
        params: list[Any] = []
        query = search_key(_as_values(filters.get("query"))[0] if _as_values(filters.get("query")) else "")
        if query:
            clauses.append("i.search_text LIKE ?")
            params.append(f"%{query}%")
        for key, column in (
            ("city", "i.city"), ("district", "i.district"),
            ("sales_person", "i.sales_person"), ("technical_person", "i.technical_person"),
            ("health", "i.health_status"), ("school_type", "i.school_type"),
            ("sales_period", "i.sales_period"), ("dealer", "i.dealer"),
            ("marketing_person", "i.marketing_person"), ("accounting_person", "i.accounting_person"),
            ("customer_person", "i.customer_person"), ("payment_status", "i.payment_status"),
            ("sms_status", "i.sms_status"), ("rental_status", "i.rental_status"),
        ):
            values = _as_values(filters.get(key))
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        statuses = _as_values(filters.get("status"))
        if statuses:
            clauses.append(f"i.customer_status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        for key, raw in filters.items():
            if not key.startswith("custom."):
                continue
            field_id, values = key[7:], _as_values(raw)
            if values:
                clauses.append(f"EXISTS (SELECT 1 FROM custom_values cv WHERE cv.entity_type='institution' AND cv.entity_id=i.id AND cv.field_id=? AND cv.value_text IN ({','.join('?' for _ in values)}))")
                params.extend([field_id, *values])
        return " AND ".join(clauses), params

    def dashboard(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        where, params = self._institution_filter_sql(filters)
        with self.connect() as conn:
            summary = conn.execute(f"""
                SELECT
                    COUNT(DISTINCT i.group_number) AS institution_count,
                    COALESCE(SUM(CASE WHEN health_status LIKE '%sorun yok%' THEN 1 ELSE 0 END), 0) AS healthy_count,
                    COUNT(DISTINCT CASE WHEN health_status LIKE '%Hata%' THEN i.group_number END) AS error_institution_count,
                    COALESCE(SUM(CASE WHEN customer_status LIKE '%AKTİF%' OR rental_status LIKE '%AKTİF%' THEN 1 ELSE 0 END), 0) AS active_count
                FROM institutions i WHERE {where}
            """, params).fetchone()
            panels = conn.execute(f"""
                SELECT COUNT(p.id) AS panel_count,
                       COALESCE(SUM(CASE WHEN i.health_status LIKE '%Hata%' THEN 1 ELSE 0 END), 0) AS error_panel_count,
                       COALESCE(SUM(p.turnstile_count), 0) AS logical_turnstile_count
                FROM institutions i
                LEFT JOIN panels p ON p.institution_id=i.id AND p.active=1
                WHERE {where}
            """, params).fetchone()
            cities = [dict(row) for row in conn.execute(f"""
                SELECT i.city, COUNT(*) AS count FROM institutions i
                WHERE {where} AND i.city <> '' GROUP BY i.city ORDER BY count DESC, i.city LIMIT 8
            """, params)]
            result = {**dict(summary), **dict(panels), "cities": cities}
            result["error_count"] = result["error_panel_count"]
            return result

    def list_filters(self) -> dict[str, Any]:
        with self.connect() as conn:
            def values(column: str) -> list[str]:
                return [row[0] for row in conn.execute(
                    f"SELECT DISTINCT {column} FROM institutions WHERE active=1 AND {column}<>'' ORDER BY {column}"
                )]
            result = {
                "cities": values("city"),
                "districts": values("district"),
                "sales_people": values("sales_person"),
                "technical_people": values("technical_person"),
                "health_statuses": values("health_status"),
                "customer_statuses": values("customer_status"),
                "finance_fields": [dict(row) for row in conn.execute(
                    "SELECT id, field_key, label, data_type FROM finance_field_definitions WHERE active=1 AND filterable=1 ORDER BY sort_order, label"
                )],
                "districts_by_city": {},
                "custom_fields": [],
            }
            for row in conn.execute("SELECT city,district FROM institutions WHERE active=1 AND city<>'' AND district<>'' GROUP BY city,district ORDER BY city,district"):
                result["districts_by_city"].setdefault(row["city"], []).append(row["district"])
            for row in conn.execute("SELECT id,label,data_type FROM custom_field_definitions WHERE entity_type='institution' AND active=1 ORDER BY sort_order,label"):
                item = dict(row)
                item["values"] = [value[0] for value in conn.execute("SELECT DISTINCT value_text FROM custom_values WHERE field_id=? AND value_text<>'' ORDER BY value_text", (row["id"],))]
                result["custom_fields"].append(item)
        lookups = self.list_lookup_categories()
        result["lookups"] = {
            category["category_key"]: [item for item in category["items"] if item["active"]]
            for category in lookups if category["active"]
        }
        return result

    def list_institutions(self, filters: dict[str, Any]) -> dict[str, Any]:
        where, params = self._institution_filter_sql(filters)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(DISTINCT i.group_number) FROM institutions i WHERE {where}", params).fetchone()[0]
            rows = [dict(row) for row in conn.execute(f"""
                SELECT i.*, g.id AS group_id, COALESCE(g.name, i.name) AS group_name,
                       EXISTS(
                           SELECT 1 FROM media_assets ma
                           WHERE ma.entity_type='institution' AND ma.entity_id=i.id AND ma.slot='primary' AND ma.active=1
                       ) AS has_photo,
                       COUNT(p.id) AS panel_count,
                       COUNT(DISTINCT NULLIF(p.physical_system_key, '')) AS physical_system_count,
                       COALESCE(SUM(p.turnstile_count), 0) AS turnstile_count,
                       MAX(p.last_seen) AS last_seen,
                       MAX(p.installation_date) AS installation_date,
                       MAX(p.installed_by) AS installed_by
                FROM institutions i
                LEFT JOIN panels p ON p.institution_id=i.id AND p.active=1
                LEFT JOIN institution_groups g ON g.group_number=i.group_number AND g.active=1
                WHERE {where}
                GROUP BY i.id
                ORDER BY COALESCE(i.group_number, 999999), COALESCE(i.sequence_number, 999999), i.city, i.district, i.name
                LIMIT 5000
            """, params)]
            if rows:
                row_by_id = {row["id"]: row for row in rows}
                placeholders = ",".join("?" for _ in rows)
                for custom in conn.execute(f"""
                    SELECT entity_id, field_id, value_text FROM custom_values
                    WHERE entity_type='institution' AND entity_id IN ({placeholders})
                """, list(row_by_id)):
                    row_by_id[custom["entity_id"]].setdefault("custom_values", {})[custom["field_id"]] = custom["value_text"]
            groups: list[dict[str, Any]] = []
            by_group: dict[int, dict[str, Any]] = {}
            # Pre-scan: find which group_numbers have at least one BİRİM-numbered record
            import re as _re
            _birim_re = _re.compile(r'-\s*B[İI]R[İI]M\s+\d+\s*$', _re.IGNORECASE)
            groups_with_birim: set[int] = set()
            for row in rows:
                if _birim_re.search(row.get("name", "")):
                    gn = int(row.get("group_number") if row.get("group_number") is not None else (row.get("sequence_number") or 0))
                    groups_with_birim.add(gn)

            for row in rows:
                group_number = int(row.get("group_number") if row.get("group_number") is not None else (row.get("sequence_number") or 0))

                group = by_group.get(group_number)
                if not group:
                    group = {
                        "id": row.get("group_id") or f"grup_{group_number}",
                        "group_number": group_number,
                        "name": row.get("group_name") or row["name"],
                        "city": row["city"], "district": row["district"],
                        "panel_count": 0, "turnstile_count": 0,
                        "error_panel_count": 0, "children": [],
                    }
                    by_group[group_number] = group
                    groups.append(group)

                # If this group has BİRİM records and this row is the BİRİMsiz base,
                # use it only for group header metadata — do NOT add as a child row
                is_base_record = not _birim_re.search(row.get("name", ""))
                if is_base_record and group_number in groups_with_birim:
                    # Update group name/city/district from the base record if available
                    group["name"] = row.get("group_name") or row["name"]
                    group["city"] = row["city"]
                    group["district"] = row["district"]
                    continue  # Skip adding as child — avoids phantom duplicate

                group["children"].append(row)
                group["panel_count"] += int(row.get("panel_count") or 0)
                group["turnstile_count"] += int(row.get("turnstile_count") or 0)
                if "HATA" in normalize_text(row.get("health_status")).upper():
                    group["error_panel_count"] += int(row.get("panel_count") or 0)

            return {"items": rows, "groups": groups, "total": total, "record_total": len(rows), "all_shown": len(rows) <= 5000}

    def recalculate_health_statuses(self) -> int:
        """Evaluates customer_status, payment_status, rating, notes, and camera/panel health to accurately categorize health_status."""
        with self.transaction() as conn:
            rows = conn.execute("""
                SELECT i.id, i.name, i.health_status, i.customer_status, i.payment_status, i.rental_status, i.rating, i.notes,
                       p.local_ip, p.software_version, p.entry_camera_status, p.exit_camera_status
                FROM institutions i
                LEFT JOIN panels p ON p.institution_id=i.id AND p.active=1
                WHERE i.active=1
            """).fetchall()

            updated_count = 0
            for r in rows:
                inst_id = r["id"]
                health = str(r["health_status"] or "")
                cust = str(r["customer_status"] or "").upper()
                pay = str(r["payment_status"] or "").upper()
                rent = str(r["rental_status"] or "").upper()
                rating = int(r["rating"] or 5)
                notes = str(r["notes"] or "").upper()
                ip = str(r["local_ip"] or "").strip()
                e_cam = str(r["entry_camera_status"] or "").upper()
                x_cam = str(r["exit_camera_status"] or "").upper()

                has_error = False
                if any(k in cust for k in ["İPTAL", "PASİF", "DURDURULDU", "SÖZLEŞME İPTAL"]):
                    has_error = True
                if any(k in pay for k in ["ÖDEMEDİ", "BORÇ", "GECİKTİ", "ÖDEME YAPMADI"]):
                    has_error = True
                if any(k in rent for k in ["İPTAL", "PASİF"]):
                    has_error = True
                if rating <= 2:
                    has_error = True
                if any(k in notes for k in ["HATA", "ARIZA", "ÇALIŞMIYOR", "KESİNTİ", "KAPALI", "YOKLAMA", "SINIF", "İPTAL", "BAŞLAMADI"]):
                    has_error = True
                if "HATA" in e_cam or "PASİF" in e_cam or "HATA" in x_cam or "PASİF" in x_cam:
                    has_error = True
                if "HATA" in health.upper() or "SORUNLU" in health.upper() or "İPTAL" in health.upper():
                    has_error = True

                new_health = "Kurumda Hatalar Var" if has_error else "Kurumda sorun yok"
                if health != new_health:
                    conn.execute("UPDATE institutions SET health_status=?, updated_at=? WHERE id=?", (new_health, utc_now(), inst_id))
                    updated_count += 1

            return updated_count

    def get_faulty_institutions_log(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT i.id, i.name, i.city, i.district, i.health_status, i.customer_status, i.payment_status, i.updated_at, i.rating, i.notes,
                       p.local_ip, p.entry_camera_status, p.exit_camera_status
                FROM institutions i
                LEFT JOIN panels p ON p.institution_id=i.id AND p.active=1
                WHERE i.active=1 AND (
                    i.health_status LIKE '%Hata%' OR
                    (i.notes IS NOT NULL AND i.notes != '' AND i.notes != 'Hata Yok') OR
                    i.customer_status LIKE '%İPTAL%' OR
                    i.customer_status LIKE '%PASİF%' OR
                    i.customer_status LIKE '%DURDURULDU%' OR
                    i.payment_status LIKE '%ÖDEMEDİ%' OR
                    i.payment_status LIKE '%BORÇ%' OR
                    i.rating <= 2
                )

                GROUP BY i.id
                ORDER BY i.updated_at DESC, i.name
            """).fetchall()


            result = []
            for r in rows:
                c_status = str(r["customer_status"] or "").upper()
                p_status = str(r["payment_status"] or "").upper()
                rating = int(r["rating"] or 5)
                e_cam = str(r["entry_camera_status"] or "").upper()
                x_cam = str(r["exit_camera_status"] or "").upper()

                reasons = []
                notes_val = str(r["notes"] or "").strip()
                if notes_val:
                    reasons.append(notes_val)

                if "İPTAL" in c_status or "PASİF" in c_status:
                    if not any("İPTAL" in x for x in reasons):
                        reasons.append(f"Müşteri Durumu: {r['customer_status']}")
                if "ÖDEMEDİ" in p_status or "BORÇ" in p_status:
                    reasons.append(f"Ödeme: {r['payment_status']}")
                if rating <= 2:
                    reasons.append(f"Düşük Puan ({rating})")
                if "HATA" in e_cam or "PASİF" in e_cam or "HATA" in x_cam or "PASİF" in x_cam:
                    reasons.append("Kamera Arızası")
                if not reasons:
                    reasons.append("Sistem Hatası / İptal Takibi")


                updated_at_str = str(r["updated_at"] or "")
                time_display = updated_at_str.split("T")[-1][:5] if "T" in updated_at_str else (updated_at_str.split(" ")[-1][:5] if " " in updated_at_str else updated_at_str)

                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "city": r["city"],
                    "district": r["district"],
                    "health_status": r["health_status"] or "Kurumda Hatalar Var",
                    "customer_status": r["customer_status"],
                    "updated_at": updated_at_str,
                    "time_display": time_display or "Son Çekim",
                    "reasons": ", ".join(reasons)
                })

            return result

    def get_institution(self, institution_id: str) -> dict[str, Any] | None:


        with self.connect() as conn:
            institution = self._row_to_dict(conn.execute(
                "SELECT * FROM institutions WHERE id=?", (institution_id,)
            ).fetchone())
            if not institution:
                return None
            institution["panels"] = [dict(row) for row in conn.execute(
                "SELECT * FROM panels WHERE institution_id=? AND active=1 ORDER BY name, panel_key",
                (institution_id,),
            )]
            institution["custom_values"] = {
                row["field_id"]: row["value_text"] for row in conn.execute(
                    "SELECT field_id, value_text FROM custom_values WHERE entity_type='institution' AND entity_id=?",
                    (institution_id,),
                )
            }
            photo = conn.execute(
                """SELECT id,slot,original_name,mime_type,sha256,size_bytes,updated_at
                   FROM media_assets
                   WHERE entity_type='institution' AND entity_id=? AND slot='primary' AND active=1""",
                (institution_id,),
            ).fetchone()
            institution["photo"] = dict(photo) if photo else None
            return institution

    @staticmethod
    def _validate_image_bytes(data: bytes, mime_type: str) -> tuple[str, str]:
        """Tarayıcı beyanına güvenmeden temel dosya imzasını doğrular."""
        if not data:
            raise DatabaseError("Fotoğraf dosyası boş.")
        if len(data) > 8 * 1024 * 1024:
            raise DatabaseError("Kurum fotoğrafı en fazla 8 MB olabilir.")
        claimed = normalize_text(mime_type).lower()
        if data.startswith(b"\xff\xd8\xff"):
            detected, extension = "image/jpeg", ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            detected, extension = "image/png", ".png"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            detected, extension = "image/webp", ".webp"
        else:
            raise DatabaseError("Yalnızca JPG, PNG veya WebP fotoğraf yüklenebilir.")
        if claimed and claimed not in {detected, "image/jpg" if detected == "image/jpeg" else detected}:
            raise DatabaseError("Fotoğraf türü ile dosya içeriği uyuşmuyor.")
        return detected, extension

    def get_media_asset(self, entity_type: str, entity_id: str, slot: str = "primary") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM media_assets
                   WHERE entity_type=? AND entity_id=? AND slot=? AND active=1""",
                (normalize_text(entity_type), normalize_text(entity_id), normalize_text(slot) or "primary"),
            ).fetchone()
            return dict(row) if row else None

    def read_media_asset(self, entity_type: str, entity_id: str, slot: str = "primary") -> tuple[bytes, str] | None:
        item = self.get_media_asset(entity_type, entity_id, slot)
        if not item:
            return None
        target = (self.data_dir / item["relative_path"]).resolve()
        media_root = self.media_dir.resolve()
        if media_root not in target.parents or not target.is_file():
            raise FileNotFoundError("Fotoğraf dosyası bulunamadı.")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise DatabaseError("Fotoğraf bütünlük doğrulaması başarısız.")
        return data, item["mime_type"]

    def save_media_asset(
        self,
        entity_type: str,
        entity_id: str,
        slot: str,
        original_name: str,
        mime_type: str,
        data: bytes,
        actor: str = "Yerel Kullanıcı",
    ) -> dict[str, Any]:
        entity_type = normalize_text(entity_type)
        entity_id = normalize_text(entity_id)
        slot = normalize_text(slot) or "primary"
        if entity_type != "institution":
            raise DatabaseError("Bu sürümde medya yükleme yalnızca kurum kayıtlarında etkindir.")
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM institutions WHERE id=? AND active=1", (entity_id,)).fetchone()
        if not exists:
            raise FileNotFoundError("Fotoğraf eklenecek kurum bulunamadı.")
        detected_mime, extension = self._validate_image_bytes(data, mime_type)
        digest = hashlib.sha256(data).hexdigest()
        entity_dir = self.media_dir / "institutions" / entity_id
        entity_dir.mkdir(parents=True, exist_ok=True)
        media_id = new_id("media")
        filename = f"{slot}_{media_id}{extension}"
        target = entity_dir / filename
        temp_target = target.with_suffix(target.suffix + ".tmp")
        temp_target.write_bytes(data)
        relative_path = target.relative_to(self.data_dir).as_posix()
        old_relative = ""
        now = utc_now()
        try:
            with self.transaction() as conn:
                current = conn.execute(
                    "SELECT * FROM media_assets WHERE entity_type=? AND entity_id=? AND slot=?",
                    (entity_type, entity_id, slot),
                ).fetchone()
                old_relative = current["relative_path"] if current else ""
                temp_target.replace(target)
                if current:
                    media_id = current["id"]
                    conn.execute(
                        """UPDATE media_assets SET original_name=?,mime_type=?,relative_path=?,sha256=?,size_bytes=?,
                           active=1,updated_at=? WHERE id=?""",
                        (normalize_text(original_name), detected_mime, relative_path, digest, len(data), now, media_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO media_assets(
                           id,entity_type,entity_id,slot,original_name,mime_type,relative_path,sha256,size_bytes,
                           active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (media_id, entity_type, entity_id, slot, normalize_text(original_name), detected_mime,
                         relative_path, digest, len(data), now, now),
                    )
                self._audit(conn, actor, "media_save", entity_type, entity_id, {
                    "slot": slot, "mime_type": detected_mime, "size_bytes": len(data), "sha256": digest,
                })
        except Exception:
            if temp_target.exists():
                temp_target.unlink(missing_ok=True)
            if target.exists() and old_relative != relative_path:
                target.unlink(missing_ok=True)
            raise
        if old_relative and old_relative != relative_path:
            old_target = (self.data_dir / old_relative).resolve()
            if self.media_dir.resolve() in old_target.parents:
                old_target.unlink(missing_ok=True)
        return self.get_media_asset(entity_type, entity_id, slot) or {}

    def delete_media_asset(
        self, entity_type: str, entity_id: str, slot: str = "primary", actor: str = "Yerel Kullanıcı"
    ) -> dict[str, Any]:
        entity_type = normalize_text(entity_type)
        entity_id = normalize_text(entity_id)
        slot = normalize_text(slot) or "primary"
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM media_assets WHERE entity_type=? AND entity_id=? AND slot=? AND active=1",
                (entity_type, entity_id, slot),
            ).fetchone()
            if not current:
                raise FileNotFoundError("Silinecek fotoğraf bulunamadı.")
            conn.execute("UPDATE media_assets SET active=0,updated_at=? WHERE id=?", (utc_now(), current["id"]))
            self._audit(conn, actor, "media_remove", entity_type, entity_id, {"slot": slot, "media_id": current["id"]})
        return {"removed": True, "id": current["id"]}

    def _apply_institution_form_rules(self, data: dict[str, Any], creating: bool) -> dict[str, Any]:
        clean_data = dict(data)
        fields = self.get_setting("institution_form_fields", DEFAULT_INSTITUTION_FORM_FIELDS)
        for field in fields if isinstance(fields, list) else DEFAULT_INSTITUTION_FORM_FIELDS:
            key = field.get("key")
            if key not in INSTITUTION_FIELDS:
                continue
            if creating and key not in clean_data and field.get("default_value") not in (None, ""):
                clean_data[key] = field.get("default_value")
            if field.get("required") and not normalize_text(clean_data.get(key)):
                raise DatabaseError(f"{normalize_text(field.get('label')) or key} zorunludur.")
        return clean_data

    def create_institution(self, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        data = self._apply_institution_form_rules(data, True)
        name = normalize_text(data.get("name"))
        if not name:
            raise DatabaseError("Kurum adı zorunludur.")
        self.backup("kurum_ekleme_oncesi")
        now = utc_now()
        institution_id = new_id("kurum")
        clean = {k: data.get(k) for k in INSTITUTION_FIELDS if k in data}
        clean["name"] = name
        clean.setdefault("source", "manual")
        clean.setdefault("active", 1)
        try:
            with self.transaction() as conn:
                if not clean.get("sequence_number"):
                    clean["sequence_number"] = conn.execute("SELECT COALESCE(MAX(sequence_number), 0)+1 FROM institutions").fetchone()[0]
                if not clean.get("group_number"):
                    clean["group_number"] = conn.execute("SELECT COALESCE(MAX(group_number), 0)+1 FROM institutions").fetchone()[0]
                columns = ["id", *clean.keys(), "created_at", "updated_at", "row_version"]
                values = [institution_id, *clean.values(), now, now, 1]
                conn.execute(
                    f"INSERT INTO institutions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    values,
                )
                conn.execute("""
                    INSERT INTO institution_groups(id,group_number,name,city,district,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_number) DO NOTHING
                """, (new_id("grup"), clean["group_number"], name, normalize_text(clean.get("city")), normalize_text(clean.get("district")), now, now))
                self._set_custom_values(conn, "institution", institution_id, data.get("custom_values", {}))
                self._sync_lookup_values(conn)
                self._rebuild_search_text(conn, institution_id)
                self._audit(conn, actor, "create", "institution", institution_id, clean)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Portal ID, kurum kodu veya sıra numarası daha önce kullanılmış.") from exc
        return self.get_institution(institution_id) or {}

    def update_institution(self, institution_id: str, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        current = self.get_institution(institution_id)
        if not current:
            raise DatabaseError("Kurum bulunamadı.")
        data = self._apply_institution_form_rules({**current, **data}, False)
        expected_version = int(data.get("row_version", current["row_version"]))
        clean = {k: data[k] for k in INSTITUTION_FIELDS if k in data}
        if "name" in clean and not normalize_text(clean["name"]):
            raise DatabaseError("Kurum adı boş bırakılamaz.")
        self.backup("kurum_guncelleme_oncesi")
        now = utc_now()
        try:
            with self.transaction() as conn:
                if clean:
                    assignments = ", ".join(f"{key}=?" for key in clean)
                    cursor = conn.execute(
                        f"UPDATE institutions SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=? AND row_version=?",
                        [*clean.values(), now, institution_id, expected_version],
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("Kayıt başka bir işlem tarafından değiştirildi. Sayfayı yenileyin.")
                    if clean.get("group_number"):
                        conn.execute("""
                            INSERT INTO institution_groups(id,group_number,name,city,district,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_number) DO NOTHING
                        """, (new_id("grup"), int(clean["group_number"]), normalize_text(clean.get("name") or current["name"]), normalize_text(clean.get("city") or current["city"]), normalize_text(clean.get("district") or current["district"]), now, now))
                self._set_custom_values(conn, "institution", institution_id, data.get("custom_values", {}))
                self._sync_lookup_values(conn)
                self._rebuild_search_text(conn, institution_id)
                self._audit(conn, actor, "update", "institution", institution_id, clean)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Portal ID, kurum kodu veya sıra numarası başka bir kayıtta kullanılıyor.") from exc
        return self.get_institution(institution_id) or {}

    def list_archived_institutions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT i.id, i.group_number, i.sequence_number, i.name, i.city, i.district, i.school_type, i.updated_at,
                       (SELECT COUNT(*) FROM panels p WHERE p.institution_id=i.id) AS panel_count,
                       (SELECT COUNT(*) FROM finance_values fv WHERE fv.institution_id=i.id) AS finance_value_count,
                       (SELECT COUNT(*) FROM finance_transactions ft WHERE ft.institution_id=i.id) AS finance_transaction_count
                FROM institutions i WHERE i.active=0
                ORDER BY updated_at DESC, name
            """)]

    def archive_institutions(self, institution_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in institution_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Silinecek kurum seçilmedi.")
        self.backup("kurum_silme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, group_number, name FROM institutions WHERE active=1 AND id IN ({placeholders})",
                ids,
            ).fetchall()
            if not rows:
                raise DatabaseError("Silinecek aktif kurum bulunamadı.")
            valid_ids = [row["id"] for row in rows]
            valid_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(
                f"UPDATE institutions SET active=0, updated_at=?, row_version=row_version+1 WHERE id IN ({valid_placeholders})",
                [now, *valid_ids],
            )
            affected_groups = sorted({int(row["group_number"]) for row in rows if row["group_number"] is not None})
            for group_number in affected_groups:
                if not conn.execute(
                    "SELECT 1 FROM institutions WHERE active=1 AND group_number=? LIMIT 1", (group_number,)
                ).fetchone():
                    conn.execute(
                        "UPDATE institution_groups SET active=0, updated_at=?, row_version=row_version+1 WHERE group_number=? AND active=1",
                        (now, group_number),
                    )
            self._audit(conn, actor, "archive", "institution", "bulk", {
                "institution_ids": valid_ids,
                "names": [row["name"] for row in rows],
            })
        return {"archived": len(valid_ids), "institution_ids": valid_ids}

    def restore_institutions(self, institution_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in institution_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Geri yüklenecek kurum seçilmedi.")
        self.backup("kurum_geri_yukleme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, group_number FROM institutions WHERE active=0 AND id IN ({placeholders})", ids
            ).fetchall()
            if not rows:
                raise DatabaseError("Geri yüklenecek kurum bulunamadı.")
            valid_ids = [row["id"] for row in rows]
            valid_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(
                f"UPDATE institutions SET active=1, updated_at=?, row_version=row_version+1 WHERE id IN ({valid_placeholders})",
                [now, *valid_ids],
            )
            for group_number in {int(row["group_number"]) for row in rows if row["group_number"] is not None}:
                conn.execute(
                    "UPDATE institution_groups SET active=1, updated_at=?, row_version=row_version+1 WHERE group_number=?",
                    (now, group_number),
                )
            for institution_id in valid_ids:
                self._rebuild_search_text(conn, institution_id)
            self._audit(conn, actor, "restore", "institution", "bulk", {"institution_ids": valid_ids})
        return {"restored": len(valid_ids), "institution_ids": valid_ids}

    def purge_institutions(self, institution_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        """Yalnızca çöp kutusundaki kurumları ve bağlı yerel verilerini kalıcı siler."""
        ids = list(dict.fromkeys(normalize_text(item) for item in institution_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Kalıcı silinecek kurum seçilmedi.")
        self.backup("kurum_kalici_silme_oncesi")
        media_paths: list[str] = []
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id,name,group_number FROM institutions WHERE active=0 AND id IN ({placeholders})", ids
            ).fetchall()
            if not rows:
                raise DatabaseError("Kalıcı silinecek çöp kutusu kaydı bulunamadı.")
            valid_ids = [row["id"] for row in rows]
            valid_placeholders = ",".join("?" for _ in valid_ids)
            counts = {
                "panels": conn.execute(f"SELECT COUNT(*) FROM panels WHERE institution_id IN ({valid_placeholders})", valid_ids).fetchone()[0],
                "finance_values": conn.execute(f"SELECT COUNT(*) FROM finance_values WHERE institution_id IN ({valid_placeholders})", valid_ids).fetchone()[0],
                "finance_transactions": conn.execute(f"SELECT COUNT(*) FROM finance_transactions WHERE institution_id IN ({valid_placeholders})", valid_ids).fetchone()[0],
                "media_assets": conn.execute(
                    f"SELECT COUNT(*) FROM media_assets WHERE entity_type='institution' AND entity_id IN ({valid_placeholders})",
                    valid_ids,
                ).fetchone()[0],
            }
            media_paths = [row[0] for row in conn.execute(
                f"SELECT relative_path FROM media_assets WHERE entity_type='institution' AND entity_id IN ({valid_placeholders})",
                valid_ids,
            )]
            conn.execute(f"DELETE FROM custom_values WHERE entity_type='institution' AND entity_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM media_assets WHERE entity_type='institution' AND entity_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM finance_values WHERE institution_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM finance_account_institutions WHERE institution_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"UPDATE finance_transactions SET institution_id=NULL WHERE institution_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM panels WHERE institution_id IN ({valid_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM institutions WHERE id IN ({valid_placeholders})", valid_ids)
            for group_number in {row["group_number"] for row in rows if row["group_number"] is not None}:
                if not conn.execute("SELECT 1 FROM institutions WHERE group_number=? LIMIT 1", (group_number,)).fetchone():
                    conn.execute("DELETE FROM institution_groups WHERE group_number=?", (group_number,))
            self._audit(conn, actor, "purge", "institution", "bulk", {
                "institution_ids": valid_ids, "names": [row["name"] for row in rows], **counts,
            })
        media_root = self.media_dir.resolve()
        for relative_path in media_paths:
            target = (self.data_dir / relative_path).resolve()
            if media_root in target.parents:
                target.unlink(missing_ok=True)
        return {"purged": len(valid_ids), "institution_ids": valid_ids, **counts}

    def create_or_update_panel(self, data: dict[str, Any], panel_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        institution_id = normalize_text(data.get("institution_id"))
        panel_key = normalize_text(data.get("panel_key")) or new_id("panelkod")
        if not institution_id:
            raise DatabaseError("Panel için kurum seçilmelidir.")
        clean = {k: data.get(k) for k in PANEL_FIELDS if k in data}
        clean["institution_id"] = institution_id
        clean["panel_key"] = panel_key
        now = utc_now()
        self.backup("panel_kayit_oncesi")
        with self.transaction() as conn:
            if panel_id:
                current = conn.execute("SELECT row_version FROM panels WHERE id=?", (panel_id,)).fetchone()
                if not current:
                    raise DatabaseError("Panel bulunamadı.")
                expected = int(data.get("row_version", current[0]))
                assignments = ", ".join(f"{key}=?" for key in clean)
                cursor = conn.execute(
                    f"UPDATE panels SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=? AND row_version=?",
                    [*clean.values(), now, panel_id, expected],
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Panel başka bir işlem tarafından değiştirildi.")
                action = "update"
            else:
                panel_id = new_id("panel")
                columns = ["id", *clean.keys(), "created_at", "updated_at", "row_version"]
                conn.execute(
                    f"INSERT INTO panels({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [panel_id, *clean.values(), now, now, 1],
                )
                action = "create"
            self._set_custom_values(conn, "panel", panel_id, data.get("custom_values", {}))
            self._rebuild_search_text(conn, institution_id)
            self._audit(conn, actor, action, "panel", panel_id, clean)
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone())

    def list_custom_fields(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if entity_type:
                rows = conn.execute(
                    "SELECT * FROM custom_field_definitions WHERE active=1 AND entity_type=? ORDER BY sort_order, label",
                    (entity_type,),
                )
            else:
                rows = conn.execute(
                    "SELECT * FROM custom_field_definitions WHERE active=1 ORDER BY entity_type, sort_order, label"
                )
            result = []
            for row in rows:
                item = dict(row)
                item["options"] = json.loads(item.pop("options_json") or "[]")
                result.append(item)
            return result

    def add_custom_field(self, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        entity_type = normalize_text(data.get("entity_type"))
        label = normalize_text(data.get("label"))
        data_type = normalize_text(data.get("data_type"))
        if entity_type not in {"institution", "panel"}:
            raise DatabaseError("Alan türü kurum veya panel olmalıdır.")
        if data_type not in {"text", "date", "number", "list", "boolean"}:
            raise DatabaseError("Geçersiz veri türü.")
        if not label:
            raise DatabaseError("Alan adı zorunludur.")
        field_key = normalize_text(data.get("field_key")) or re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        if not field_key:
            field_key = uuid.uuid4().hex[:10]
        now = utc_now()
        field_id = new_id("alan")
        self.backup("ozel_alan_ekleme_oncesi")
        try:
            with self.transaction() as conn:
                conn.execute("""
                    INSERT INTO custom_field_definitions(
                        id, entity_type, field_key, label, data_type, options_json,
                        required, sort_order, active, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,1,?,?)
                """, (
                    field_id, entity_type, field_key, label, data_type,
                    json.dumps(data.get("options", []), ensure_ascii=False),
                    1 if data.get("required") else 0,
                    int(data.get("sort_order", 100)), now, now,
                ))
                self._audit(conn, actor, "create", "custom_field", field_id, {"label": label, "data_type": data_type})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Aynı isimde bir özel alan zaten var.") from exc
        return next(item for item in self.list_custom_fields() if item["id"] == field_id)

    def update_custom_field(self, field_id: str, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        label = normalize_text(data.get("label"))
        data_type = normalize_text(data.get("data_type"))
        if not label:
            raise DatabaseError("Alan adı zorunludur.")
        if data_type not in {"text", "date", "number", "list", "boolean"}:
            raise DatabaseError("Geçersiz veri türü.")
        options = data.get("options", [])
        if not isinstance(options, list):
            raise DatabaseError("Alan seçenekleri liste biçiminde olmalıdır.")
        self.backup("ozel_alan_degisikligi_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            current = conn.execute("SELECT * FROM custom_field_definitions WHERE id=? AND active=1", (field_id,)).fetchone()
            if not current:
                raise DatabaseError("Özel alan bulunamadı.")
            conn.execute("""
                UPDATE custom_field_definitions
                SET label=?,data_type=?,options_json=?,required=?,sort_order=?,updated_at=?
                WHERE id=?
            """, (
                label, data_type, json.dumps(options, ensure_ascii=False),
                1 if data.get("required") else 0,
                int(data.get("sort_order", current["sort_order"])), now, field_id,
            ))
            self._audit(conn, actor, "update", "custom_field", field_id, {"label": label, "data_type": data_type})
        return next(item for item in self.list_custom_fields() if item["id"] == field_id)

    def list_archived_custom_fields(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            sql = "SELECT * FROM custom_field_definitions WHERE active=0"
            params: list[Any] = []
            if entity_type:
                sql += " AND entity_type=?"
                params.append(entity_type)
            result = []
            for row in conn.execute(sql + " ORDER BY updated_at DESC,label", params):
                item = dict(row)
                item["options"] = json.loads(item.pop("options_json") or "[]")
                result.append(item)
            return result

    def archive_custom_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Arşivlenecek alan seçilmedi.")
        self.backup("ozel_alan_arsivleme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id,label FROM custom_field_definitions WHERE active=1 AND id IN ({placeholders})", ids).fetchall()
            valid = [row["id"] for row in rows]
            if not valid:
                raise DatabaseError("Arşivlenecek aktif alan bulunamadı.")
            marks = ",".join("?" for _ in valid)
            conn.execute(f"UPDATE custom_field_definitions SET active=0,updated_at=? WHERE id IN ({marks})", [now, *valid])
            self._audit(conn, actor, "archive", "custom_field", "bulk", {"field_ids": valid, "labels": [row["label"] for row in rows]})
        return {"archived": len(valid), "field_ids": valid}

    def restore_custom_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Geri yüklenecek alan seçilmedi.")
        self.backup("ozel_alan_geri_yukleme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id FROM custom_field_definitions WHERE active=0 AND id IN ({placeholders})", ids).fetchall()
            valid = [row["id"] for row in rows]
            if not valid:
                raise DatabaseError("Geri yüklenecek alan bulunamadı.")
            marks = ",".join("?" for _ in valid)
            conn.execute(f"UPDATE custom_field_definitions SET active=1,updated_at=? WHERE id IN ({marks})", [now, *valid])
            self._audit(conn, actor, "restore", "custom_field", "bulk", {"field_ids": valid})
        return {"restored": len(valid), "field_ids": valid}

    def purge_custom_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Kalıcı silinecek alan seçilmedi.")
        self.backup("ozel_alan_kalici_silme_oncesi")
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id FROM custom_field_definitions WHERE active=0 AND id IN ({placeholders})", ids).fetchall()
            valid = [row["id"] for row in rows]
            if not valid:
                raise DatabaseError("Kalıcı silinecek arşivlenmiş alan bulunamadı.")
            marks = ",".join("?" for _ in valid)
            value_count = conn.execute(f"SELECT COUNT(*) FROM custom_values WHERE field_id IN ({marks})", valid).fetchone()[0]
            conn.execute(f"DELETE FROM custom_values WHERE field_id IN ({marks})", valid)
            conn.execute(f"DELETE FROM custom_field_definitions WHERE id IN ({marks})", valid)
            self._audit(conn, actor, "purge", "custom_field", "bulk", {"field_ids": valid, "value_count": value_count})
        return {"purged": len(valid), "value_count": value_count}

    def reorder_custom_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> list[dict[str, Any]]:
        ids = [normalize_text(item) for item in field_ids if normalize_text(item)]
        with self.transaction() as conn:
            for index, field_id in enumerate(ids, 1):
                conn.execute("UPDATE custom_field_definitions SET sort_order=?,updated_at=? WHERE id=? AND active=1", (index * 10, utc_now(), field_id))
            self._audit(conn, actor, "reorder", "custom_field", "bulk", {"field_ids": ids})
        return self.list_custom_fields()

    def _set_custom_values(self, conn: sqlite3.Connection, entity_type: str, entity_id: str, values: dict[str, Any]) -> None:
        if not isinstance(values, dict):
            return
        now = utc_now()
        for field_id, value in values.items():
            field = conn.execute(
                "SELECT required, data_type, options_json FROM custom_field_definitions WHERE id=? AND entity_type=? AND active=1",
                (field_id, entity_type),
            ).fetchone()
            if not field:
                continue
            text = normalize_text(value)
            if field[0] and not text:
                raise DatabaseError("Zorunlu özel alan boş bırakılamaz.")
            if field[1] == "number" and text:
                try:
                    float(text.replace(",", "."))
                except ValueError as exc:
                    raise DatabaseError("Sayısal alana geçerli bir sayı girilmelidir.") from exc
            if field[1] == "list" and text:
                options = json.loads(field[2] or "[]")
                if options and text not in options:
                    raise DatabaseError("Liste alanında tanımlı seçeneklerden biri kullanılmalıdır.")
            conn.execute("""
                INSERT INTO custom_values(field_id, entity_type, entity_id, value_text, updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(field_id, entity_type, entity_id)
                DO UPDATE SET value_text=excluded.value_text, updated_at=excluded.updated_at
            """, (field_id, entity_type, entity_id, text, now))

    @staticmethod
    def _audit(conn: sqlite3.Connection, actor: str, action: str, entity_type: str, entity_id: str, changes: dict[str, Any]) -> None:
        conn.execute("""
            INSERT INTO audit_log(occurred_at, actor, action, entity_type, entity_id, changes_json)
            VALUES(?,?,?,?,?,?)
        """, (utc_now(), actor, action, entity_type, entity_id, json.dumps(changes, ensure_ascii=False, default=str)))

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(500, max(1, limit)),)
            )]

    def list_lookup_categories(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            categories = [dict(row) for row in conn.execute(
                "SELECT * FROM lookup_categories ORDER BY sort_order,label"
            )]
            usage_maps: dict[str, dict[tuple[str, str], int]] = {}
            filter_maps: dict[str, dict[tuple[str, str], int]] = {}
            for category in categories:
                key = category["category_key"]
                table, column = LOOKUP_FIELD_MAP.get(key, ("", ""))
                counts: dict[tuple[str, str], int] = {}
                filter_counts: dict[tuple[str, str], int] = {}
                if key == "district":
                    for row in conn.execute("""
                        SELECT city,district,COUNT(*) AS count FROM institutions
                        WHERE active=1 AND district<>'' GROUP BY city,district
                    """):
                        counts[(normalize_text(row["district"]), normalize_text(row["city"]))] = int(row["count"])
                    for row in conn.execute("""
                        SELECT city,district,COUNT(DISTINCT group_number) AS count FROM institutions
                        WHERE active=1 AND district<>'' GROUP BY city,district
                    """):
                        filter_counts[(normalize_text(row["district"]), normalize_text(row["city"]))] = int(row["count"])
                elif table and column:
                    active_clause = "active=1 AND " if table in {"institutions", "panels"} else ""
                    for row in conn.execute(
                        f"SELECT {column} AS value,COUNT(*) AS count FROM {table} WHERE {active_clause}{column}<>'' GROUP BY {column}"
                    ):
                        counts[(normalize_text(row["value"]), "")] = int(row["count"])
                    if table == "institutions":
                        for row in conn.execute(
                            f"SELECT {column} AS value,COUNT(DISTINCT group_number) AS count FROM institutions "
                            f"WHERE active=1 AND {column}<>'' GROUP BY {column}"
                        ):
                            filter_counts[(normalize_text(row["value"]), "")] = int(row["count"])
                    elif table == "panels":
                        # Panel tabanlı tanımlar kurum filtresinde kullanılmıyor; yine de anlamlı tekil kurum sayısı verilir.
                        for row in conn.execute(
                            f"SELECT p.{column} AS value,COUNT(DISTINCT i.group_number) AS count "
                            f"FROM panels p JOIN institutions i ON i.id=p.institution_id AND i.active=1 "
                            f"WHERE p.active=1 AND p.{column}<>'' GROUP BY p.{column}"
                        ):
                            filter_counts[(normalize_text(row["value"]), "")] = int(row["count"])
                usage_maps[key] = counts
                filter_maps[key] = filter_counts
            items_by_category: dict[str, list[dict[str, Any]]] = {}
            for row in conn.execute("SELECT * FROM lookup_items ORDER BY category_key,sort_order,label"):
                item = dict(row)
                item["usage_count"] = usage_maps.get(item["category_key"], {}).get(
                    (item["item_key"], item["parent_item_key"]), 0
                )
                item["filter_count"] = filter_maps.get(item["category_key"], {}).get(
                    (item["item_key"], item["parent_item_key"]), item["usage_count"]
                )
                items_by_category.setdefault(item["category_key"], []).append(item)
            for category in categories:
                category["items"] = items_by_category.get(category["category_key"], [])
            return categories

    def get_lookup_item(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM lookup_items WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def create_or_update_lookup_item(
        self, data: dict[str, Any], item_id: str | None = None, actor: str = "Yerel Kullanıcı"
    ) -> dict[str, Any]:
        category_key = normalize_text(data.get("category_key"))
        label = normalize_text(data.get("label"))
        parent_item_key = normalize_text(data.get("parent_item_key"))
        color = normalize_text(data.get("color"))
        if not category_key or not label:
            raise DatabaseError("Tanım grubu ve görünen ad zorunludur.")
        if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise DatabaseError("Renk #RRGGBB biçiminde olmalıdır.")
        with self.connect() as conn:
            category = conn.execute(
                "SELECT * FROM lookup_categories WHERE category_key=? AND active=1", (category_key,)
            ).fetchone()
            current = conn.execute("SELECT * FROM lookup_items WHERE id=?", (item_id,)).fetchone() if item_id else None
        if not category:
            raise DatabaseError("Tanım grubu bulunamadı.")
        if item_id and not current:
            raise DatabaseError("Tanım değeri bulunamadı.")
        if category["parent_category_key"] and not parent_item_key:
            raise DatabaseError("Bu tanım için üst değer seçilmelidir.")
        if not category["parent_category_key"]:
            parent_item_key = ""
        self.backup("tanim_degisikligi_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            if current:
                conn.execute("""
                    UPDATE lookup_items SET label=?,parent_item_key=?,color=?,active=?,updated_at=? WHERE id=?
                """, (label, parent_item_key, color, int(bool(data.get("active", current["active"]))), now, item_id))
                self._audit(conn, actor, "update", "lookup_item", item_id or "", {
                    "category_key": category_key, "label": label, "parent_item_key": parent_item_key,
                })
            else:
                item_key = normalize_text(data.get("item_key")) or label
                base_key, suffix = item_key, 2
                while conn.execute(
                    "SELECT 1 FROM lookup_items WHERE category_key=? AND item_key=? AND parent_item_key=?",
                    (category_key, item_key, parent_item_key),
                ).fetchone():
                    item_key = f"{base_key} #{suffix}"
                    suffix += 1
                item_id = new_id("tanim")
                next_order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order),0)+10 FROM lookup_items WHERE category_key=? AND parent_item_key=?",
                    (category_key, parent_item_key),
                ).fetchone()[0]
                conn.execute("""
                    INSERT INTO lookup_items(
                        id,category_key,item_key,label,parent_item_key,color,sort_order,active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,1,?,?)
                """, (item_id, category_key, item_key, label, parent_item_key, color, next_order, now, now))
                self._audit(conn, actor, "create", "lookup_item", item_id, {
                    "category_key": category_key, "item_key": item_key, "label": label,
                })
        return self.get_lookup_item(item_id or "") or {}

    def update_lookup_category(self, category_key: str, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        label = normalize_text(data.get("label"))
        if not label:
            raise DatabaseError("Tanım grubu adı zorunludur.")
        self.backup("tanim_grubu_degisikligi_oncesi")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE lookup_categories SET label=?,updated_at=? WHERE category_key=?",
                (label, utc_now(), category_key),
            )
            if cursor.rowcount != 1:
                raise DatabaseError("Tanım grubu bulunamadı.")
            self._audit(conn, actor, "update", "lookup_category", category_key, {"label": label})
        return next(item for item in self.list_lookup_categories() if item["category_key"] == category_key)

    def archive_lookup_items(self, item_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in item_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Arşivlenecek tanım seçilmedi.")
        self.backup("tanim_arsivleme_oncesi")
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id FROM lookup_items WHERE active=1 AND id IN ({placeholders})", ids
            ).fetchall()
            valid = [row["id"] for row in rows]
            if not valid:
                raise DatabaseError("Arşivlenecek aktif tanım bulunamadı.")
            marks = ",".join("?" for _ in valid)
            conn.execute(f"UPDATE lookup_items SET active=0,updated_at=? WHERE id IN ({marks})", [utc_now(), *valid])
            self._audit(conn, actor, "archive", "lookup_item", "bulk", {"item_ids": valid})
        return {"archived": len(valid), "item_ids": valid}

    def restore_lookup_items(self, item_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in item_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Geri yüklenecek tanım seçilmedi.")
        self.backup("tanim_geri_yukleme_oncesi")
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id FROM lookup_items WHERE active=0 AND id IN ({placeholders})", ids).fetchall()
            valid = [row["id"] for row in rows]
            if not valid:
                raise DatabaseError("Geri yüklenecek tanım bulunamadı.")
            marks = ",".join("?" for _ in valid)
            conn.execute(f"UPDATE lookup_items SET active=1,updated_at=? WHERE id IN ({marks})", [utc_now(), *valid])
            self._audit(conn, actor, "restore", "lookup_item", "bulk", {"item_ids": valid})
        return {"restored": len(valid), "item_ids": valid}

    def reorder_lookup_items(self, category_key: str, item_ids: list[str], actor: str = "Yerel Kullanıcı") -> list[dict[str, Any]]:
        ids = [normalize_text(item) for item in item_ids if normalize_text(item)]
        with self.transaction() as conn:
            for index, item_id in enumerate(ids, 1):
                conn.execute(
                    "UPDATE lookup_items SET sort_order=?,updated_at=? WHERE id=? AND category_key=?",
                    (index * 10, utc_now(), item_id, category_key),
                )
            self._audit(conn, actor, "reorder", "lookup_item", category_key, {"item_ids": ids})
        return next((item["items"] for item in self.list_lookup_categories() if item["category_key"] == category_key), [])

    def merge_lookup_items(self, source_id: str, target_id: str, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        if source_id == target_id:
            raise DatabaseError("Birleştirme hedefi kaynakla aynı olamaz.")
        with self.connect() as conn:
            source = conn.execute("SELECT * FROM lookup_items WHERE id=?", (source_id,)).fetchone()
            target = conn.execute("SELECT * FROM lookup_items WHERE id=? AND active=1", (target_id,)).fetchone()
        if not source or not target or source["category_key"] != target["category_key"]:
            raise DatabaseError("Birleştirilecek tanımlar aynı grupta bulunmalıdır.")
        if source["category_key"] == "district" and source["parent_item_key"] != target["parent_item_key"]:
            raise DatabaseError("İlçeler yalnızca aynı il içinde birleştirilebilir.")
        self.backup("tanim_birlestirme_oncesi")
        category_key = source["category_key"]
        table, column = LOOKUP_FIELD_MAP[category_key]
        with self.transaction() as conn:
            if category_key == "district":
                cursor = conn.execute(
                    "UPDATE institutions SET district=?,updated_at=?,row_version=row_version+1 WHERE city=? AND district=?",
                    (target["item_key"], utc_now(), source["parent_item_key"], source["item_key"]),
                )
            else:
                cursor = conn.execute(
                    f"UPDATE {table} SET {column}=?,updated_at=?,row_version=row_version+1 WHERE {column}=?",
                    (target["item_key"], utc_now(), source["item_key"]),
                )
                if category_key == "city":
                    conn.execute("UPDATE institution_groups SET city=?,updated_at=? WHERE city=?", (target["item_key"], utc_now(), source["item_key"]))
                    for district in conn.execute(
                        "SELECT id,item_key FROM lookup_items WHERE category_key='district' AND parent_item_key=?",
                        (source["item_key"],),
                    ).fetchall():
                        duplicate = conn.execute("""
                            SELECT id FROM lookup_items
                            WHERE category_key='district' AND parent_item_key=? AND item_key=?
                        """, (target["item_key"], district["item_key"])).fetchone()
                        if duplicate:
                            conn.execute("UPDATE lookup_items SET active=0,updated_at=? WHERE id=?", (utc_now(), district["id"]))
                        else:
                            conn.execute("UPDATE lookup_items SET parent_item_key=?,updated_at=? WHERE id=?", (target["item_key"], utc_now(), district["id"]))
            conn.execute("UPDATE lookup_items SET active=0,updated_at=? WHERE id=?", (utc_now(), source_id))
            self._rebuild_all_search_text(conn)
            self._audit(conn, actor, "merge", "lookup_item", source_id, {
                "target_id": target_id, "changed_records": cursor.rowcount,
            })
        return {"merged": 1, "changed_records": cursor.rowcount, "target_id": target_id}

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return default

    @staticmethod
    def _sanitize_setting(key: str, value: Any) -> Any:
        if key == "institution_form_fields":
            if not isinstance(value, list):
                raise DatabaseError("Kurum formu ayarı liste biçiminde olmalıdır.")
            defaults = {item["key"]: item for item in DEFAULT_INSTITUTION_FORM_FIELDS}
            result, seen = [], set()
            for raw in value:
                if not isinstance(raw, dict) or raw.get("key") not in defaults or raw["key"] in seen:
                    continue
                base = defaults[raw["key"]]
                item = dict(base)
                item.update({
                    "label": normalize_text(raw.get("label"))[:80] or base["label"],
                    "required": bool(raw.get("required", base["required"])),
                    "visible": bool(raw.get("visible", base["visible"])),
                    "width": "wide" if raw.get("width") == "wide" else "half",
                    "placeholder": normalize_text(raw.get("placeholder"))[:120],
                    "default_value": raw.get("default_value", base["default_value"]),
                })
                if item["key"] == "name":
                    item["required"] = True
                    if not item["visible"] and not normalize_text(item.get("default_value")):
                        item["visible"] = True
                result.append(item)
                seen.add(item["key"])
            for default in DEFAULT_INSTITUTION_FORM_FIELDS:
                if default["key"] not in seen:
                    result.append(dict(default))
            return result
        if key == "finance_summary_cards":
            if not isinstance(value, list):
                raise DatabaseError("Finans kartı ayarı liste biçiminde olmalıdır.")
            result = []
            for index, raw in enumerate(value[:20]):
                if not isinstance(raw, dict):
                    continue
                metric = normalize_text(raw.get("metric"))
                if metric not in {"institution_count", "record_count"} and not re.fullmatch(r"field:[a-z0-9_]+", metric):
                    continue
                result.append({
                    "id": normalize_text(raw.get("id"))[:80] or f"card_{index + 1}",
                    "label": normalize_text(raw.get("label"))[:80] or "Özet",
                    "metric": metric,
                    "format": raw.get("format") if raw.get("format") in {"number", "money", "percent"} else "number",
                    "subtitle": normalize_text(raw.get("subtitle"))[:120],
                    "color": raw.get("color") if raw.get("color") in {"neutral", "success", "warning", "danger", "blue"} else "neutral",
                    "visible": bool(raw.get("visible", True)),
                })
            return result
        if key == "navigation_preferences":
            if not isinstance(value, list):
                raise DatabaseError("Menü ayarı liste biçiminde olmalıdır.")
            defaults = {item["key"]: item for item in DEFAULT_NAVIGATION_PREFERENCES}
            result, seen = [], set()
            for raw in value:
                if not isinstance(raw, dict) or raw.get("key") not in defaults or raw["key"] in seen:
                    continue
                base = defaults[raw["key"]]
                result.append({
                    "key": raw["key"], "label": normalize_text(raw.get("label"))[:40] or base["label"],
                    "icon": normalize_text(raw.get("icon"))[:4] or base["icon"],
                    "title": normalize_text(raw.get("title"))[:100] or base["title"],
                    "subtitle": normalize_text(raw.get("subtitle"))[:240],
                    "visible": bool(raw.get("visible", True)),
                })
                seen.add(raw["key"])
            for default in DEFAULT_NAVIGATION_PREFERENCES:
                if default["key"] not in seen:
                    result.append(dict(default))
            return result
        if key == "theme_preferences":
            if not isinstance(value, dict):
                raise DatabaseError("Tema ayarı nesne biçiminde olmalıdır.")
            result = dict(DEFAULT_THEME_PREFERENCES)
            for color_key in ("background", "surface", "text", "muted", "primary", "primary_dark", "danger", "sidebar"):
                candidate = normalize_text(value.get(color_key))
                if re.fullmatch(r"#[0-9A-Fa-f]{6}", candidate):
                    result[color_key] = candidate.lower()
            result["font_scale"] = min(120, max(85, int(value.get("font_scale", 100))))
            result["density"] = value.get("density") if value.get("density") in {"compact", "normal", "comfortable"} else "normal"
            result["radius"] = min(24, max(0, int(value.get("radius", 12))))
            result["sidebar_width"] = min(320, max(190, int(value.get("sidebar_width", 220))))
            return result
        return value

    def set_setting(self, key: str, value: Any, actor: str = "Yerel Kullanıcı") -> Any:
        value = self._sanitize_setting(key, value)
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > 100_000:
            raise DatabaseError("Ayar verisi çok büyük.")
        allowed = {
            "table_columns", "finance_table_columns", "dynamic_filters", "finance_filters",
            "finance_saved_views", "finance_summary_cards", "general_preferences",
            "institution_form_fields", "navigation_preferences", "theme_preferences",
            "portal_auto_sync_config",
        }

        if key not in allowed:
            raise DatabaseError("Bu ayar değiştirilemez.")
        self.backup("ayar_degisikligi_oncesi")
        with self.transaction() as conn:
            current = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if current:
                conn.execute(
                    "INSERT INTO app_setting_history(setting_key,value,changed_at) VALUES(?,?,?)",
                    (key, current[0], utc_now()),
                )
            conn.execute("""
                INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, encoded, utc_now()))
            self._audit(conn, actor, "update", "app_setting", key, {"value": value})
        return value

    def list_setting_history(self, key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            result = []
            for row in conn.execute(
                "SELECT id,setting_key,value,changed_at FROM app_setting_history WHERE setting_key=? ORDER BY id DESC LIMIT ?",
                (key, min(100, max(1, limit))),
            ):
                item = dict(row)
                try:
                    item["value"] = json.loads(item["value"])
                except json.JSONDecodeError:
                    pass
                result.append(item)
            return result

    def restore_setting_history(self, history_id: int, actor: str = "Yerel Kullanıcı") -> Any:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT setting_key,value FROM app_setting_history WHERE id=?", (int(history_id),)
            ).fetchone()
        if not row:
            raise DatabaseError("Geri yüklenecek ayar sürümü bulunamadı.")
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError as exc:
            raise DatabaseError("Ayar geçmişi bozuk.") from exc
        return self.set_setting(row["setting_key"], value, actor)

    def compare_import(self, records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        new_count = updated = unchanged = 0
        samples = []
        compare_keys = sorted(INSTITUTION_FIELDS - {"source", "source_row", "active", "campus_id"})
        with self.connect() as conn:
            for record in records:
                current = conn.execute(
                    "SELECT * FROM institutions WHERE portal_id=? OR institution_code=? LIMIT 1",
                    (record.get("portal_id"), record.get("institution_code")),
                ).fetchone()
                if not current:
                    new_count += 1
                    if len(samples) < 20:
                        samples.append({"type": "new", "name": record.get("name"), "portal_id": record.get("portal_id")})
                    continue
                changes = {
                    key: {"old": normalize_text(current[key]), "new": normalize_text(record.get(key))}
                    for key in compare_keys
                    if key in current.keys() and normalize_text(current[key]) != normalize_text(record.get(key))
                }
                if changes:
                    updated += 1
                    if len(samples) < 20:
                        samples.append({"type": "update", "name": record.get("name"), "portal_id": record.get("portal_id"), "changes": changes})
                else:
                    unchanged += 1
        return {"new": new_count, "updated": updated, "unchanged": unchanged, "samples": samples}

    def import_records(self, records: list[dict[str, Any]], file_name: str, file_sha256: str, actor: str = "Excel İçe Aktarma") -> dict[str, Any]:
        comparison = self.compare_import(records)
        self.backup("excel_aktarim_oncesi")
        now = utc_now()
        panel_count = 0
        with self.transaction() as conn:
            conn.execute("UPDATE institutions SET sequence_number=NULL")
            for record in records:
                panels = record.get("panels", [])
                existing = conn.execute(
                    "SELECT id FROM institutions WHERE portal_id=? OR institution_code=? LIMIT 1",
                    (record.get("portal_id"), record.get("institution_code")),
                ).fetchone()
                clean = {k: record.get(k) for k in INSTITUTION_FIELDS if k in record}
                clean["source"] = "excel"
                clean["active"] = 1
                if existing:
                    institution_id = existing[0]
                    assignments = ", ".join(f"{key}=?" for key in clean)
                    conn.execute(
                        f"UPDATE institutions SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=?",
                        [*clean.values(), now, institution_id],
                    )
                else:
                    institution_id = new_id("kurum")
                    columns = ["id", *clean.keys(), "created_at", "updated_at", "row_version"]
                    conn.execute(
                        f"INSERT INTO institutions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [institution_id, *clean.values(), now, now, 1],
                    )
                seen_panel_keys = []
                for panel in panels:
                    panel_count += 1
                    clean_panel = {k: panel.get(k) for k in PANEL_FIELDS if k in panel}
                    clean_panel["institution_id"] = institution_id
                    clean_panel["active"] = 1
                    panel_key = normalize_text(clean_panel.get("panel_key")) or f"aktarim_{panel_count}"
                    clean_panel["panel_key"] = panel_key
                    seen_panel_keys.append(panel_key)
                    existing_panel = conn.execute(
                        "SELECT id FROM panels WHERE institution_id=? AND panel_key=?",
                        (institution_id, panel_key),
                    ).fetchone()
                    if existing_panel:
                        assignments = ", ".join(f"{key}=?" for key in clean_panel)
                        conn.execute(
                            f"UPDATE panels SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=?",
                            [*clean_panel.values(), now, existing_panel[0]],
                        )
                    else:
                        panel_id = new_id("panel")
                        columns = ["id", *clean_panel.keys(), "created_at", "updated_at", "row_version"]
                        conn.execute(
                            f"INSERT INTO panels({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                            [panel_id, *clean_panel.values(), now, now, 1],
                        )
                if seen_panel_keys:
                    placeholders = ",".join("?" for _ in seen_panel_keys)
                    conn.execute(
                        f"UPDATE panels SET active=0, updated_at=?, row_version=row_version+1 WHERE institution_id=? AND panel_key NOT IN ({placeholders})",
                        [now, institution_id, *seen_panel_keys],
                    )
                else:
                    conn.execute(
                        "UPDATE panels SET active=0, updated_at=?, row_version=row_version+1 WHERE institution_id=?",
                        (now, institution_id),
                    )
                self._rebuild_search_text(conn, institution_id)
                self._audit(conn, actor, "import_upsert", "institution", institution_id, {"portal_id": record.get("portal_id")})
            next_sequence = conn.execute("SELECT COALESCE(MAX(sequence_number), 0)+1 FROM institutions").fetchone()[0]
            for manual in conn.execute("SELECT id FROM institutions WHERE sequence_number IS NULL ORDER BY created_at, id").fetchall():
                conn.execute("UPDATE institutions SET sequence_number=? WHERE id=?", (next_sequence, manual[0]))
                self._rebuild_search_text(conn, manual[0])
                next_sequence += 1
            self._migrate_v3(conn)
            self._sync_lookup_values(conn)
            self.recalculate_health_statuses()
            run_id = new_id("aktarim")

            conn.execute("""
                INSERT INTO import_runs(
                    id, imported_at, file_name, file_sha256, total_records, new_records,
                    updated_records, unchanged_records, panel_records, result, details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                run_id, now, file_name, file_sha256, len(records), comparison["new"],
                comparison["updated"], comparison["unchanged"], panel_count, "success",
                json.dumps({"samples": comparison["samples"]}, ensure_ascii=False),
            ))
        return {**comparison, "total": len(records), "panels": panel_count}

    @staticmethod
    def _finance_effect_sql(alias: str = "t") -> str:
        return (
            f"CASE {alias}.transaction_type "
            f"WHEN 'FATURA' THEN ABS({alias}.amount_minor) "
            f"WHEN 'ÖDEME' THEN -ABS({alias}.amount_minor) "
            f"WHEN 'İNDİRİM' THEN -ABS({alias}.amount_minor) "
            f"WHEN 'DÜZELTME' THEN {alias}.amount_minor ELSE 0 END"
        )

    def _finance_account_search_text(self, conn: sqlite3.Connection, account_id: str) -> str:
        account = conn.execute("SELECT * FROM finance_accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            return ""
        values = [account["account_code"], account["name"], account["tax_id"], account["phone"], account["email"], account["sales_person"]]
        for institution in conn.execute("""
            SELECT i.name, i.city, i.district, i.portal_id, i.institution_code
            FROM finance_account_institutions l
            JOIN institutions i ON i.id=l.institution_id WHERE l.account_id=?
        """, (account_id,)):
            values.extend(institution)
        return search_key(" ".join(normalize_text(value) for value in values if value is not None))

    def _rebuild_finance_account_search(self, conn: sqlite3.Connection, account_id: str) -> None:
        conn.execute(
            "UPDATE finance_accounts SET search_text=? WHERE id=?",
            (self._finance_account_search_text(conn, account_id), account_id),
        )

    def finance_dashboard(self) -> dict[str, Any]:
        effect = self._finance_effect_sql("t")
        today = datetime.now().date().isoformat()
        with self.connect() as conn:
            summary = conn.execute(f"""
                SELECT
                    (SELECT COUNT(*) FROM finance_accounts WHERE status='AKTİF') AS account_count,
                    COALESCE(SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' THEN ABS(t.amount_minor) ELSE 0 END), 0) AS invoiced_minor,
                    COALESCE(SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='ÖDEME' THEN ABS(t.amount_minor) ELSE 0 END), 0) AS paid_minor,
                    COALESCE(SUM(CASE WHEN t.status='KAYITLI' THEN {effect} ELSE 0 END), 0) AS balance_minor
                FROM finance_transactions t
            """).fetchone()
            overdue = conn.execute(f"""
                WITH balances AS (
                    SELECT a.id,
                           COALESCE(SUM(CASE WHEN t.status='KAYITLI' THEN {effect} ELSE 0 END), 0) AS balance,
                           MAX(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' AND t.due_date<>'' AND t.due_date<? THEN 1 ELSE 0 END) AS has_overdue
                    FROM finance_accounts a LEFT JOIN finance_transactions t ON t.account_id=a.id
                    WHERE a.status='AKTİF' GROUP BY a.id
                )
                SELECT COALESCE(SUM(CASE WHEN has_overdue=1 AND balance>0 THEN balance ELSE 0 END), 0) FROM balances
            """, (today,)).fetchone()[0]
            return {**dict(summary), "overdue_minor": overdue}

    def list_finance_accounts(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        clauses = ["1=1"]
        params: list[Any] = []
        query = search_key(filters.get("query"))
        if query:
            clauses.append("a.search_text LIKE ?")
            params.append(f"%{query}%")
        status = normalize_text(filters.get("status"))
        if status:
            clauses.append("a.status=?")
            params.append(status)
        where = " AND ".join(clauses)
        effect = self._finance_effect_sql("t")
        today = datetime.now().date().isoformat()
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(f"""
                WITH tx AS (
                    SELECT t.account_id,
                           SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' THEN ABS(t.amount_minor) ELSE 0 END) AS invoiced_minor,
                           SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='ÖDEME' THEN ABS(t.amount_minor) ELSE 0 END) AS paid_minor,
                           SUM(CASE WHEN t.status='KAYITLI' THEN {effect} ELSE 0 END) AS balance_minor,
                           MAX(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' AND t.due_date<>'' AND t.due_date<? THEN 1 ELSE 0 END) AS has_overdue
                    FROM finance_transactions t GROUP BY t.account_id
                ), links AS (
                    SELECT account_id, COUNT(*) AS institution_count FROM finance_account_institutions GROUP BY account_id
                )
                SELECT a.*,
                       COALESCE(links.institution_count, 0) AS institution_count,
                       COALESCE(tx.invoiced_minor, 0) AS invoiced_minor,
                       COALESCE(tx.paid_minor, 0) AS paid_minor,
                       COALESCE(tx.balance_minor, 0) AS balance_minor,
                       COALESCE(tx.has_overdue, 0) AS has_overdue
                FROM finance_accounts a
                LEFT JOIN links ON links.account_id=a.id
                LEFT JOIN tx ON tx.account_id=a.id
                WHERE {where}
                ORDER BY a.status, a.name
            """, [today, *params])]
            return {"items": rows, "total": len(rows)}

    def get_finance_account(self, account_id: str) -> dict[str, Any] | None:
        effect = self._finance_effect_sql("t")
        with self.connect() as conn:
            account = self._row_to_dict(conn.execute(f"""
                SELECT a.*,
                       COALESCE(SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' THEN ABS(t.amount_minor) ELSE 0 END), 0) AS invoiced_minor,
                       COALESCE(SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='ÖDEME' THEN ABS(t.amount_minor) ELSE 0 END), 0) AS paid_minor,
                       COALESCE(SUM(CASE WHEN t.status='KAYITLI' THEN {effect} ELSE 0 END), 0) AS balance_minor
                FROM finance_accounts a LEFT JOIN finance_transactions t ON t.account_id=a.id
                WHERE a.id=? GROUP BY a.id
            """, (account_id,)).fetchone())
            if not account:
                return None
            account["institutions"] = [dict(row) for row in conn.execute("""
                SELECT i.id, i.name, i.city, i.district, i.portal_id, i.institution_code,
                       i.group_number, i.sequence_number
                FROM finance_account_institutions l JOIN institutions i ON i.id=l.institution_id
                WHERE l.account_id=? ORDER BY i.group_number, i.sequence_number
            """, (account_id,))]
            account["contracts"] = [dict(row) for row in conn.execute(
                "SELECT * FROM finance_contracts WHERE account_id=? ORDER BY start_date DESC, created_at DESC",
                (account_id,),
            )]
            account["transactions"] = [dict(row) for row in conn.execute("""
                SELECT t.*, i.name AS institution_name, c.contract_no
                FROM finance_transactions t
                LEFT JOIN institutions i ON i.id=t.institution_id
                LEFT JOIN finance_contracts c ON c.id=t.contract_id
                WHERE t.account_id=? ORDER BY t.transaction_date DESC, t.created_at DESC
            """, (account_id,))]
            return account

    def create_or_update_finance_account(self, data: dict[str, Any], account_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        name = normalize_text(data.get("name"))
        if not name:
            raise DatabaseError("Cari hesap adı zorunludur.")
        institution_ids = data.get("institution_ids", [])
        if not isinstance(institution_ids, list):
            raise DatabaseError("Bağlı kurum listesi geçersiz.")
        fields = {key: normalize_text(data.get(key)) for key in (
            "account_code", "name", "tax_id", "phone", "email", "billing_address", "sales_person", "status", "notes"
        )}
        fields["name"] = name
        fields["status"] = fields["status"] if fields["status"] in {"AKTİF", "PASİF"} else "AKTİF"
        now = utc_now()
        self.backup("finans_cari_kayit_oncesi")
        try:
            with self.transaction() as conn:
                if account_id:
                    current = conn.execute("SELECT row_version FROM finance_accounts WHERE id=?", (account_id,)).fetchone()
                    if not current:
                        raise DatabaseError("Cari hesap bulunamadı.")
                    expected = int(data.get("row_version", current[0]))
                    if not fields["account_code"]:
                        fields.pop("account_code")
                    assignments = ", ".join(f"{key}=?" for key in fields)
                    cursor = conn.execute(
                        f"UPDATE finance_accounts SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=? AND row_version=?",
                        [*fields.values(), now, account_id, expected],
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("Cari hesap başka bir işlem tarafından değiştirildi.")
                    action = "update"
                else:
                    account_id = new_id("cari")
                    if not fields["account_code"]:
                        number = conn.execute("SELECT COUNT(*)+1 FROM finance_accounts").fetchone()[0]
                        fields["account_code"] = f"CARI-{number:04d}"
                    columns = ["id", *fields.keys(), "created_at", "updated_at", "row_version"]
                    conn.execute(
                        f"INSERT INTO finance_accounts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [account_id, *fields.values(), now, now, 1],
                    )
                    action = "create"
                valid_ids = []
                for institution_id in dict.fromkeys(normalize_text(value) for value in institution_ids if normalize_text(value)):
                    if conn.execute("SELECT 1 FROM institutions WHERE id=? AND active=1", (institution_id,)).fetchone():
                        valid_ids.append(institution_id)
                conn.execute("DELETE FROM finance_account_institutions WHERE account_id=?", (account_id,))
                conn.executemany(
                    "INSERT INTO finance_account_institutions(account_id, institution_id, created_at) VALUES(?,?,?)",
                    [(account_id, institution_id, now) for institution_id in valid_ids],
                )
                self._rebuild_finance_account_search(conn, account_id)
                self._audit(conn, actor, action, "finance_account", account_id, {**fields, "institution_ids": valid_ids})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Cari hesap kodu daha önce kullanılmış.") from exc
        return self.get_finance_account(account_id) or {}

    def create_or_update_contract(self, data: dict[str, Any], contract_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        account_id = normalize_text(data.get("account_id"))
        if not account_id:
            raise DatabaseError("Sözleşme için cari hesap seçilmelidir.")
        try:
            vat_basis = int((Decimal(str(data.get("vat_rate", 0)).replace(",", ".")) * 100).quantize(Decimal("1")))
            commission_basis = int((Decimal(str(data.get("commission_rate", 0)).replace(",", ".")) * 100).quantize(Decimal("1")))
        except (InvalidOperation, ValueError) as exc:
            raise DatabaseError("KDV veya komisyon oranı geçersiz.") from exc
        fields = {
            "account_id": account_id,
            "contract_no": normalize_text(data.get("contract_no")),
            "start_date": validate_iso_date(data.get("start_date"), "Sözleşme başlangıcı"),
            "end_date": validate_iso_date(data.get("end_date"), "Sözleşme bitişi"),
            "billing_cycle": normalize_text(data.get("billing_cycle")) or "AYLIK",
            "base_amount_minor": money_to_minor(data.get("base_amount", 0)),
            "vat_rate_basis": vat_basis,
            "commission_rate_basis": commission_basis,
            "status": normalize_text(data.get("status")) or "AKTİF",
            "notes": normalize_text(data.get("notes")),
        }
        if fields["billing_cycle"] not in {"AYLIK", "YILLIK", "TEK SEFER"} or fields["status"] not in {"AKTİF", "TAMAMLANDI", "İPTAL"}:
            raise DatabaseError("Sözleşme durumu veya dönemi geçersiz.")
        if not 0 <= vat_basis <= 10000 or not 0 <= commission_basis <= 10000:
            raise DatabaseError("KDV ve komisyon oranları 0 ile 100 arasında olmalıdır.")
        if fields["start_date"] and fields["end_date"] and fields["end_date"] < fields["start_date"]:
            raise DatabaseError("Sözleşme bitiş tarihi başlangıçtan önce olamaz.")
        now = utc_now()
        self.backup("finans_sozlesme_kayit_oncesi")
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM finance_accounts WHERE id=?", (account_id,)).fetchone():
                raise DatabaseError("Cari hesap bulunamadı.")
            if contract_id:
                current = conn.execute("SELECT row_version FROM finance_contracts WHERE id=?", (contract_id,)).fetchone()
                if not current:
                    raise DatabaseError("Sözleşme bulunamadı.")
                expected = int(data.get("row_version", current[0]))
                assignments = ", ".join(f"{key}=?" for key in fields)
                cursor = conn.execute(
                    f"UPDATE finance_contracts SET {assignments}, updated_at=?, row_version=row_version+1 WHERE id=? AND row_version=?",
                    [*fields.values(), now, contract_id, expected],
                )
                if cursor.rowcount != 1:
                    raise ConflictError("Sözleşme başka bir işlem tarafından değiştirildi.")
                action = "update"
            else:
                contract_id = new_id("sozlesme")
                columns = ["id", *fields.keys(), "created_at", "updated_at", "row_version"]
                conn.execute(
                    f"INSERT INTO finance_contracts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [contract_id, *fields.values(), now, now, 1],
                )
                action = "create"
            self._audit(conn, actor, action, "finance_contract", contract_id, fields)
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM finance_contracts WHERE id=?", (contract_id,)).fetchone())

    def create_finance_transaction(self, data: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        account_id = normalize_text(data.get("account_id"))
        transaction_type = normalize_text(data.get("transaction_type"))
        transaction_date = validate_iso_date(data.get("transaction_date"), "İşlem tarihi", required=True)
        if transaction_type not in {"FATURA", "ÖDEME", "İNDİRİM", "DÜZELTME"}:
            raise DatabaseError("Finans işlem türü geçersiz.")
        if not account_id:
            raise DatabaseError("Cari hesap zorunludur.")
        amount_minor = money_to_minor(data.get("amount", 0))
        if transaction_type != "DÜZELTME":
            amount_minor = abs(amount_minor)
        if amount_minor == 0:
            raise DatabaseError("İşlem tutarı sıfır olamaz.")
        fields = {
            "account_id": account_id,
            "institution_id": normalize_text(data.get("institution_id")) or None,
            "contract_id": normalize_text(data.get("contract_id")) or None,
            "transaction_type": transaction_type,
            "document_no": normalize_text(data.get("document_no")),
            "transaction_date": transaction_date,
            "due_date": validate_iso_date(data.get("due_date"), "Vade tarihi"),
            "amount_minor": amount_minor,
            "currency": "TRY",
            "status": "KAYITLI",
            "description": normalize_text(data.get("description")),
        }
        now = utc_now()
        transaction_id = new_id("islem")
        self.backup("finans_islem_kayit_oncesi")
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM finance_accounts WHERE id=?", (account_id,)).fetchone():
                raise DatabaseError("Cari hesap bulunamadı.")
            if fields["institution_id"] and not conn.execute("""
                SELECT 1 FROM finance_account_institutions
                WHERE account_id=? AND institution_id=?
            """, (account_id, fields["institution_id"])).fetchone():
                raise DatabaseError("Kurum bu cari hesaba bağlı değil.")
            if fields["contract_id"] and not conn.execute("SELECT 1 FROM finance_contracts WHERE id=? AND account_id=?", (fields["contract_id"], account_id)).fetchone():
                raise DatabaseError("Sözleşme cari hesapla eşleşmiyor.")
            columns = ["id", *fields.keys(), "created_at", "updated_at", "row_version"]
            conn.execute(
                f"INSERT INTO finance_transactions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [transaction_id, *fields.values(), now, now, 1],
            )
            self._audit(conn, actor, "create", "finance_transaction", transaction_id, fields)
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM finance_transactions WHERE id=?", (transaction_id,)).fetchone())

    def reverse_finance_transaction(self, transaction_id: str, reason: str, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        reason = normalize_text(reason)
        if not reason:
            raise DatabaseError("İptal nedeni zorunludur.")
        self.backup("finans_islem_iptal_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute("""
                UPDATE finance_transactions SET status='İPTAL', reversal_reason=?, reversed_at=?, updated_at=?, row_version=row_version+1
                WHERE id=? AND status='KAYITLI'
            """, (reason, now, now, transaction_id))
            if cursor.rowcount != 1:
                raise DatabaseError("Finans işlemi bulunamadı veya daha önce iptal edildi.")
            self._audit(conn, actor, "reverse", "finance_transaction", transaction_id, {"reason": reason})
            return dict(conn.execute("SELECT * FROM finance_transactions WHERE id=?", (transaction_id,)).fetchone())

    def export_finance_rows(self) -> list[dict[str, Any]]:
        effect = self._finance_effect_sql("t")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(f"""
                WITH tx AS (
                    SELECT t.account_id,
                           SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='FATURA' THEN ABS(t.amount_minor) ELSE 0 END) AS invoiced_minor,
                           SUM(CASE WHEN t.status='KAYITLI' AND t.transaction_type='ÖDEME' THEN ABS(t.amount_minor) ELSE 0 END) AS paid_minor,
                           SUM(CASE WHEN t.status='KAYITLI' THEN {effect} ELSE 0 END) AS balance_minor
                    FROM finance_transactions t GROUP BY t.account_id
                ), links AS (
                    SELECT account_id, COUNT(*) AS institution_count FROM finance_account_institutions GROUP BY account_id
                )
                SELECT a.account_code, a.name, a.tax_id, a.sales_person, a.status,
                       COALESCE(links.institution_count, 0) AS institution_count,
                       COALESCE(tx.invoiced_minor, 0) AS invoiced_minor,
                       COALESCE(tx.paid_minor, 0) AS paid_minor,
                       COALESCE(tx.balance_minor, 0) AS balance_minor
                FROM finance_accounts a
                LEFT JOIN links ON links.account_id=a.id
                LEFT JOIN tx ON tx.account_id=a.id
                ORDER BY a.name
            """)]

    def export_rows(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT i.group_number, i.sequence_number, i.portal_id, i.institution_code, i.city, i.district, i.name,
                       i.school_type, i.customer_status, i.payment_status, i.sales_period,
                       i.sales_person, i.dealer, i.technical_person, i.accounting_person,
                       i.customer_person, i.health_status, i.notes,
                       COUNT(p.id) AS panel_count,
                       COALESCE(SUM(p.turnstile_count), 0) AS turnstile_count,
                       MAX(p.last_seen) AS last_seen
                FROM institutions i
                LEFT JOIN panels p ON p.institution_id=i.id AND p.active=1
                WHERE i.active=1
                GROUP BY i.id ORDER BY i.group_number, i.sequence_number
            """)]

    # --- Çatı kurum / grup yönetimi -------------------------------------------------
    def list_groups(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT g.*, COUNT(i.id) AS member_count,
                       COALESCE(SUM(CASE WHEN p.active=1 THEN 1 ELSE 0 END),0) AS panel_count
                FROM institution_groups g
                LEFT JOIN institutions i ON i.group_number=g.group_number AND i.active=1
                LEFT JOIN panels p ON p.institution_id=i.id
                WHERE g.active=1 GROUP BY g.id ORDER BY g.group_number
            """)]

    def create_or_update_group(self, data: dict[str, Any], group_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        name = normalize_text(data.get("name"))
        if not name:
            raise DatabaseError("Çatı kurum adı zorunludur.")
        institution_ids = _as_values(data.get("institution_ids"))
        self.backup("kurum_grubu_degisiklik_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            if group_id:
                current = conn.execute("SELECT * FROM institution_groups WHERE id=? AND active=1", (group_id,)).fetchone()
                if not current:
                    raise DatabaseError("Kurum grubu bulunamadı.")
                group_number = int(current["group_number"])
                expected = int(data.get("row_version", current["row_version"]))
                cursor = conn.execute("""
                    UPDATE institution_groups SET name=?, city=?, district=?, notes=?, updated_at=?, row_version=row_version+1
                    WHERE id=? AND row_version=?
                """, (name, normalize_text(data.get("city")), normalize_text(data.get("district")), normalize_text(data.get("notes")), now, group_id, expected))
                if cursor.rowcount != 1:
                    raise ConflictError("Kurum grubu başka bir işlem tarafından değiştirildi.")
                previous = [row[0] for row in conn.execute("SELECT id FROM institutions WHERE group_number=? AND active=1", (group_number,))]
                removed = [item for item in previous if item not in institution_ids]
                next_group = conn.execute("SELECT COALESCE(MAX(group_number),0)+1 FROM institution_groups").fetchone()[0]
                for institution_id in removed:
                    institution = conn.execute("SELECT name, city, district FROM institutions WHERE id=?", (institution_id,)).fetchone()
                    new_group_id = new_id("grup")
                    conn.execute("""
                        INSERT INTO institution_groups(id,group_number,name,city,district,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)
                    """, (new_group_id, next_group, institution["name"], institution["city"], institution["district"], now, now))
                    conn.execute("UPDATE institutions SET group_number=?, updated_at=?, row_version=row_version+1 WHERE id=?", (next_group, now, institution_id))
                    self._rebuild_search_text(conn, institution_id)
                    next_group += 1
                action = "update"
            else:
                group_number = int(data.get("group_number") or conn.execute("SELECT COALESCE(MAX(group_number),0)+1 FROM institution_groups").fetchone()[0])
                group_id = new_id("grup")
                conn.execute("""
                    INSERT INTO institution_groups(id,group_number,name,city,district,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (group_id, group_number, name, normalize_text(data.get("city")), normalize_text(data.get("district")), normalize_text(data.get("notes")), now, now))
                action = "create"
            valid_ids = [item for item in institution_ids if conn.execute("SELECT 1 FROM institutions WHERE id=? AND active=1", (item,)).fetchone()]
            source_groups = {
                int(row[0]) for row in conn.execute(
                    f"SELECT DISTINCT group_number FROM institutions WHERE id IN ({','.join('?' for _ in valid_ids)})" if valid_ids else "SELECT group_number FROM institutions WHERE 0",
                    valid_ids,
                ).fetchall() if row[0] is not None
            }
            if valid_ids:
                conn.executemany(
                    "UPDATE institutions SET group_number=?, updated_at=?, row_version=row_version+1 WHERE id=?",
                    [(group_number, now, item) for item in valid_ids],
                )
                for institution_id in valid_ids:
                    self._rebuild_search_text(conn, institution_id)
            for source_group in source_groups - {group_number}:
                if not conn.execute(
                    "SELECT 1 FROM institutions WHERE active=1 AND group_number=? LIMIT 1", (source_group,)
                ).fetchone():
                    conn.execute(
                        "UPDATE institution_groups SET active=0, updated_at=?, row_version=row_version+1 WHERE group_number=? AND active=1",
                        (now, source_group),
                    )
            self._audit(conn, actor, action, "institution_group", group_id, {"name": name, "institution_ids": valid_ids})
        return next(item for item in self.list_groups() if item["id"] == group_id)

    def archive_group(self, group_id: str, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        self.backup("kurum_grubu_silme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            group = conn.execute(
                "SELECT * FROM institution_groups WHERE id=? AND active=1", (group_id,)
            ).fetchone()
            if not group:
                raise DatabaseError("Kurum grubu bulunamadı.")
            members = conn.execute(
                "SELECT id, name, city, district FROM institutions WHERE active=1 AND group_number=? ORDER BY sequence_number",
                (group["group_number"],),
            ).fetchall()
            next_group = int(conn.execute("SELECT COALESCE(MAX(group_number),0)+1 FROM institution_groups").fetchone()[0])
            for member in members:
                conn.execute("""
                    INSERT INTO institution_groups(id,group_number,name,city,district,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                """, (new_id("grup"), next_group, member["name"], member["city"], member["district"], now, now))
                conn.execute(
                    "UPDATE institutions SET group_number=?, updated_at=?, row_version=row_version+1 WHERE id=?",
                    (next_group, now, member["id"]),
                )
                self._rebuild_search_text(conn, member["id"])
                next_group += 1
            conn.execute(
                "UPDATE institution_groups SET active=0, updated_at=?, row_version=row_version+1 WHERE id=?",
                (now, group_id),
            )
            self._audit(conn, actor, "archive", "institution_group", group_id, {
                "group_number": group["group_number"], "member_count": len(members)
            })
        return {"archived": True, "member_count": len(members)}

    # --- Dinamik finans alanları ----------------------------------------------------
    def list_finance_fields(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            result = []
            for row in conn.execute("SELECT * FROM finance_field_definitions WHERE active=1 ORDER BY sort_order,label"):
                item = dict(row)
                item["options"] = json.loads(item.pop("options_json") or "[]")
                result.append(item)
            return result

    def list_archived_finance_fields(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            result = []
            for row in conn.execute("SELECT * FROM finance_field_definitions WHERE active=0 ORDER BY updated_at DESC,label"):
                item = dict(row)
                item["options"] = json.loads(item.pop("options_json") or "[]")
                result.append(item)
            return result

    @staticmethod
    def _validate_finance_formula_graph(
        conn: sqlite3.Connection,
        candidate_key: str | None = None,
        candidate_formula: str = "",
        candidate_is_formula: bool = True,
    ) -> None:
        rows = conn.execute(
            "SELECT field_key,data_type,formula FROM finance_field_definitions WHERE active=1"
        ).fetchall()
        known = {row["field_key"] for row in rows}
        if candidate_key:
            known.add(candidate_key)
        formulas = {
            row["field_key"]: row["formula"]
            for row in rows if row["data_type"] == "formula" and row["field_key"] != candidate_key
        }
        if candidate_key and candidate_is_formula:
            formulas[candidate_key] = candidate_formula
        dependencies: dict[str, set[str]] = {}
        for key, expression in formulas.items():
            names = formula_names(expression)
            unknown = sorted(names - known)
            if unknown:
                raise DatabaseError("Formülde bulunmayan alan anahtarı var: " + ", ".join(unknown[:6]))
            dependencies[key] = names & set(formulas)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise DatabaseError("Dairesel finans formülü oluşturulamaz.")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies.get(key, set()):
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in dependencies:
            visit(key)

    def archive_finance_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Silinecek finans alanı seçilmedi.")
        self.backup("finans_alani_silme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            fields = conn.execute(
                f"SELECT id, field_key, label FROM finance_field_definitions WHERE active=1 AND id IN ({placeholders})",
                ids,
            ).fetchall()
            if not fields:
                raise DatabaseError("Silinecek aktif finans alanı bulunamadı.")
            selected_keys = {row["field_key"] for row in fields}
            dependencies: list[str] = []
            for formula in conn.execute(
                "SELECT id,label,formula FROM finance_field_definitions WHERE active=1 AND data_type='formula'"
            ):
                if formula["id"] in ids:
                    continue
                if any(re.search(rf"\b{re.escape(key)}\b", formula["formula"] or "") for key in selected_keys):
                    dependencies.append(f"{formula['label']} formülü")
            for rule in conn.execute("SELECT name,base_field_key,conditions_json FROM commission_rules WHERE active=1"):
                conditions = json.loads(rule["conditions_json"] or "[]")
                if rule["base_field_key"] in selected_keys or any(item.get("field") in selected_keys for item in conditions):
                    dependencies.append(f"{rule['name']} prim kuralı")
            if dependencies:
                raise DatabaseError("Alan önce bağlı formül/prim kurallarından çıkarılmalıdır: " + ", ".join(dependencies[:8]))
            valid_ids = [row["id"] for row in fields]
            valid_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(
                f"UPDATE finance_field_definitions SET active=0, visible=0, filterable=0, updated_at=? WHERE id IN ({valid_placeholders})",
                [now, *valid_ids],
            )
            self._audit(conn, actor, "archive", "finance_field", "bulk", {
                "field_ids": valid_ids, "labels": [row["label"] for row in fields]
            })
        return {"archived": len(valid_ids), "field_ids": valid_ids}

    def restore_finance_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Geri yüklenecek finans alanı seçilmedi.")
        self.backup("finans_alani_geri_yukleme_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id FROM finance_field_definitions WHERE active=0 AND id IN ({placeholders})", ids
            ).fetchall()
            if not rows:
                raise DatabaseError("Geri yüklenecek finans alanı bulunamadı.")
            valid_ids = [row["id"] for row in rows]
            valid_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(
                f"UPDATE finance_field_definitions SET active=1, updated_at=? WHERE id IN ({valid_placeholders})",
                [now, *valid_ids],
            )
            self._audit(conn, actor, "restore", "finance_field", "bulk", {"field_ids": valid_ids})
        return {"restored": len(valid_ids), "field_ids": valid_ids}

    def create_or_update_finance_field(self, data: dict[str, Any], field_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        label = normalize_text(data.get("label"))
        data_type = normalize_text(data.get("data_type"))
        if not label:
            raise DatabaseError("Finans alanı adı zorunludur.")
        if data_type not in {"text", "date", "number", "money", "list", "multiselect", "boolean", "formula"}:
            raise DatabaseError("Finans alanının veri türü geçersiz.")
        formula = normalize_text(data.get("formula"))
        if data_type == "formula":
            evaluate_formula(formula, {})
        options = data.get("options", [])
        if not isinstance(options, list):
            raise DatabaseError("Finans alanı seçenekleri liste biçiminde olmalıdır.")
        aggregate_type = normalize_text(data.get("aggregate_type")) or ("sum" if data_type in {"number", "money", "formula"} else "none")
        if aggregate_type not in {"none", "sum", "avg", "min", "max", "count"}:
            raise DatabaseError("Toplam işlemi geçersiz.")
        decimal_places = min(6, max(0, int(data.get("decimal_places", 2))))
        default_value = normalize_text(data.get("default_value"))
        now = utc_now()
        self.backup("finans_alani_degisiklik_oncesi")
        with self.transaction() as conn:
            if field_id:
                current = conn.execute("SELECT * FROM finance_field_definitions WHERE id=? AND active=1", (field_id,)).fetchone()
                if not current:
                    raise DatabaseError("Finans alanı bulunamadı.")
                field_key = current["field_key"]
                self._validate_finance_formula_graph(conn, field_key, formula, data_type == "formula")
                formula_changed = normalize_text(current["formula"]) != formula
                next_version = int(current["formula_version"] or 1) + (1 if formula_changed else 0)
                conn.execute("""
                    UPDATE finance_field_definitions
                    SET label=?,data_type=?,options_json=?,formula=?,default_value=?,required=?,decimal_places=?,aggregate_type=?,
                        filterable=?,visible=?,sort_order=?,active=?,formula_version=?,updated_at=?
                    WHERE id=?
                """, (
                    label, data_type, json.dumps(options, ensure_ascii=False), formula, default_value,
                    1 if data.get("required") else 0, decimal_places, aggregate_type,
                    1 if data.get("filterable", True) else 0, 1 if data.get("visible", True) else 0,
                    int(data.get("sort_order", current["sort_order"])), 1 if data.get("active", True) else 0,
                    next_version, now, field_id,
                ))
                if data_type == "formula" and formula_changed:
                    conn.execute("""
                        INSERT INTO finance_formula_versions(id,field_id,version,formula,created_at)
                        VALUES(?,?,?,?,?)
                    """, (new_id("ffver"), field_id, next_version, formula, now))
                action = "update"
            else:
                field_key = normalize_text(data.get("field_key")) or re.sub(r"[^a-z0-9]+", "_", search_key(label)).strip("_")
                if not field_key:
                    field_key = f"alan_{uuid.uuid4().hex[:8]}"
                self._validate_finance_formula_graph(conn, field_key, formula, data_type == "formula")
                field_id = new_id("ffin")
                sort_order = int(data.get("sort_order") or conn.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM finance_field_definitions").fetchone()[0])
                conn.execute("""
                    INSERT INTO finance_field_definitions(
                        id,field_key,label,data_type,options_json,formula,default_value,required,decimal_places,aggregate_type,
                        system_field,formula_version,filterable,visible,sort_order,active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,0,1,?,?,?,1,?,?)
                """, (
                    field_id, field_key, label, data_type, json.dumps(options, ensure_ascii=False), formula,
                    default_value, 1 if data.get("required") else 0, decimal_places, aggregate_type,
                    1 if data.get("filterable", True) else 0, 1 if data.get("visible", True) else 0,
                    sort_order, now, now,
                ))
                if data_type == "formula":
                    conn.execute("""
                        INSERT INTO finance_formula_versions(id,field_id,version,formula,created_at)
                        VALUES(?,?,?,?,?)
                    """, (new_id("ffver"), field_id, 1, formula, now))
                action = "create"
            self._audit(conn, actor, action, "finance_field", field_id, {"label": label, "data_type": data_type, "formula": formula})
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM finance_field_definitions WHERE id=?", (field_id,)).fetchone()
            item = dict(row)
            item["options"] = json.loads(item.pop("options_json") or "[]")
            return item

    def reorder_finance_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> list[dict[str, Any]]:
        ids = [normalize_text(item) for item in field_ids if normalize_text(item)]
        self.backup("finans_alani_siralama_oncesi")
        with self.transaction() as conn:
            for index, field_id in enumerate(ids, 1):
                conn.execute(
                    "UPDATE finance_field_definitions SET sort_order=?,updated_at=? WHERE id=? AND active=1",
                    (index * 10, utc_now(), field_id),
                )
            self._audit(conn, actor, "reorder", "finance_field", "bulk", {"field_ids": ids})
        return self.list_finance_fields()

    def purge_finance_fields(self, field_ids: list[str], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        ids = list(dict.fromkeys(normalize_text(item) for item in field_ids if normalize_text(item)))
        if not ids:
            raise DatabaseError("Kalıcı silinecek finans alanı seçilmedi.")
        self.backup("finans_alani_kalici_silme_oncesi")
        with self.transaction() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id,label,system_field FROM finance_field_definitions WHERE active=0 AND id IN ({placeholders})", ids
            ).fetchall()
            if not rows:
                raise DatabaseError("Kalıcı silinecek arşivlenmiş finans alanı bulunamadı.")
            if any(row["system_field"] for row in rows):
                raise DatabaseError("Programın hazır finans alanları kalıcı olarak silinemez; geri yüklenebilir veya gizli tutulabilir.")
            valid = [row["id"] for row in rows]
            marks = ",".join("?" for _ in valid)
            value_count = conn.execute(f"SELECT COUNT(*) FROM finance_values WHERE field_id IN ({marks})", valid).fetchone()[0]
            conn.execute(f"DELETE FROM finance_values WHERE field_id IN ({marks})", valid)
            conn.execute(f"DELETE FROM finance_field_definitions WHERE id IN ({marks})", valid)
            self._audit(conn, actor, "purge", "finance_field", "bulk", {"field_ids": valid, "value_count": value_count})
        return {"purged": len(valid), "value_count": value_count}

    def list_finance_formula_versions(self, field_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT version,formula,created_at FROM finance_formula_versions WHERE field_id=? ORDER BY version DESC",
                (field_id,),
            )]

    def set_finance_values(self, institution_id: str, values: dict[str, Any], actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        if not isinstance(values, dict):
            raise DatabaseError("Finans değerleri geçersiz.")
        self.backup("finans_degeri_degisiklik_oncesi")
        now = utc_now()
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM institutions WHERE id=? AND active=1", (institution_id,)).fetchone():
                raise DatabaseError("Kurum bulunamadı.")
            fields = {row["id"]: row for row in conn.execute("SELECT * FROM finance_field_definitions WHERE active=1")}
            for field_id, raw in values.items():
                field = fields.get(field_id)
                if not field or field["data_type"] == "formula":
                    continue
                if field["data_type"] == "multiselect":
                    selected = raw if isinstance(raw, list) else _as_values(raw)
                    text = json.dumps(selected, ensure_ascii=False)
                else:
                    text = normalize_text(raw)
                if field["required"] and not text:
                    raise DatabaseError(f"{field['label']} zorunludur.")
                if field["data_type"] in {"number", "money"} and text:
                    _strict_decimal(text, field["label"])
                if field["data_type"] == "date" and text:
                    validate_iso_date(text, field["label"])
                if field["data_type"] == "list" and text:
                    options = json.loads(field["options_json"] or "[]")
                    if options and text not in options:
                        raise DatabaseError(f"{field['label']} için tanımlı seçeneklerden birini seçin.")
                conn.execute("""
                    INSERT INTO finance_values(institution_id,field_id,value_text,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(institution_id,field_id) DO UPDATE SET value_text=excluded.value_text,updated_at=excluded.updated_at
                """, (institution_id, field_id, text, now))
            self._audit(conn, actor, "update", "finance_values", institution_id, {"field_ids": list(values)})
        return self.get_finance_institution(institution_id)

    def _finance_rows(self, filters: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        filters = filters or {}
        fields = self.list_finance_fields()
        institution_data = self.list_institutions(filters)
        rows = institution_data["items"]
        if not rows:
            return [], fields
        by_id = {row["id"]: row for row in rows}
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in rows)
            for value in conn.execute(f"""
                SELECT fv.institution_id, f.field_key, f.id AS field_id, fv.value_text
                FROM finance_values fv JOIN finance_field_definitions f ON f.id=fv.field_id
                WHERE fv.institution_id IN ({placeholders}) AND f.active=1
            """, list(by_id)):
                by_id[value["institution_id"]].setdefault("finance_values", {})[value["field_key"]] = value["value_text"]
            for tx in conn.execute(f"""
                SELECT institution_id,
                       SUM(CASE WHEN status='KAYITLI' AND transaction_type='FATURA' THEN ABS(amount_minor) ELSE 0 END) AS invoiced_minor,
                       SUM(CASE WHEN status='KAYITLI' AND transaction_type='ÖDEME' THEN ABS(amount_minor) ELSE 0 END) AS paid_minor
                FROM finance_transactions WHERE institution_id IN ({placeholders}) GROUP BY institution_id
            """, list(by_id)):
                by_id[tx["institution_id"]].update(dict(tx))
        formulas = [field for field in fields if field["data_type"] == "formula"]
        for row in rows:
            values = row.setdefault("finance_values", {})
            for field in fields:
                if field["data_type"] != "formula" and not normalize_text(values.get(field["field_key"])) and field.get("default_value"):
                    values[field["field_key"]] = field["default_value"]
            for _pass in range(len(formulas) + 1):
                changed = False
                for field in formulas:
                    places = min(6, max(0, int(field.get("decimal_places", 2))))
                    calculated = str(evaluate_formula(field["formula"], values).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))
                    if values.get(field["field_key"]) != calculated:
                        values[field["field_key"]] = calculated
                        changed = True
                if not changed:
                    break
            has_dynamic_revenue = any(normalize_text(values.get(key)) for key in (
                "sozlesme_kart_sayisi", "birinci_kart_fiyati", "basilan_ikinci_kart_sayisi", "ikinci_kart_fiyati"
            ))
            if not has_dynamic_revenue and int(row.get("invoiced_minor") or 0):
                values["toplam_ciro"] = str((Decimal(int(row["invoiced_minor"])) / 100).quantize(Decimal("0.01")))
            if not normalize_text(values.get("tahsilat")) and int(row.get("paid_minor") or 0):
                values["tahsilat"] = str((Decimal(int(row["paid_minor"])) / 100).quantize(Decimal("0.01")))
            values["bakiye"] = str((_decimal(values.get("toplam_ciro")) - _decimal(values.get("tahsilat"))).quantize(Decimal("0.01")))
        query_values = _as_values(filters.get("finance_query"))
        query = search_key(query_values[0]) if query_values else ""
        if query:
            rows = [row for row in rows if query in search_key(" ".join([row["name"], row["city"], row["district"], row["sales_person"], *row["finance_values"].values()]))]
        for key, raw in filters.items():
            if not key.startswith("finance."):
                continue
            field_key, selected = key[8:], _as_values(raw)
            if selected:
                def choices(row: dict[str, Any]) -> list[str]:
                    value = row["finance_values"].get(field_key, "")
                    try:
                        decoded = json.loads(value)
                        return [normalize_text(item) for item in decoded] if isinstance(decoded, list) else [normalize_text(value)]
                    except (json.JSONDecodeError, TypeError):
                        return [normalize_text(value)]
                rows = [row for row in rows if any(choice in choices(row) for choice in selected)]
        for key, raw in filters.items():
            if key.startswith("min.") or key.startswith("max."):
                boundary = _decimal(_as_values(raw)[0] if _as_values(raw) else 0)
                field_key = key[4:]
                if key.startswith("min."):
                    rows = [row for row in rows if _decimal(row["finance_values"].get(field_key)) >= boundary]
                else:
                    rows = [row for row in rows if _decimal(row["finance_values"].get(field_key)) <= boundary]
        for key, raw in filters.items():
            if key.startswith("from.") or key.startswith("to."):
                boundary_values = _as_values(raw)
                if not boundary_values:
                    continue
                boundary, field_key = boundary_values[0], key[5:] if key.startswith("from.") else key[3:]
                if key.startswith("from."):
                    rows = [row for row in rows if normalize_text(row["finance_values"].get(field_key)) >= boundary]
                else:
                    rows = [row for row in rows if normalize_text(row["finance_values"].get(field_key)) <= boundary]
        return rows, fields

    @staticmethod
    def _group_finance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        by_number: dict[int, dict[str, Any]] = {}
        for row in rows:
            number = int(row.get("group_number") or row.get("sequence_number") or 0)
            group = by_number.get(number)
            if not group:
                group = {"id": row.get("group_id") or f"grup_{number}", "group_number": number, "name": row.get("group_name") or row["name"], "children": []}
                by_number[number] = group
                groups.append(group)
            group["children"].append(row)
        return groups

    def list_finance_institutions(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        rows, fields = self._finance_rows(filters)
        groups = self._group_finance_rows(rows)
        return {
            "items": rows, "groups": groups, "total": len(groups), "record_total": len(rows), "fields": fields,
            "aggregates": self._finance_aggregates(rows, fields),
        }

    def get_finance_institution(self, institution_id: str) -> dict[str, Any]:
        data = self.list_finance_institutions({})
        item = next((row for row in data["items"] if row["id"] == institution_id), None)
        if not item:
            raise DatabaseError("Finans kurum kaydı bulunamadı.")
        return item

    def dynamic_finance_dashboard(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        rows, fields = self._finance_rows(filters)
        def total(key: str) -> int:
            return money_to_minor(sum((_decimal(row["finance_values"].get(key)) for row in rows), Decimal("0")))
        return {
            "institution_count": len({row.get("group_number") for row in rows}),
            "record_count": len(rows),
            "revenue_minor": total("toplam_ciro"),
            "paid_minor": total("tahsilat"),
            "balance_minor": total("bakiye"),
            "field_aggregates": self._finance_aggregates(rows, fields),
        }

    @staticmethod
    def _finance_aggregates(rows: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for field in fields:
            operation = field.get("aggregate_type", "none")
            if operation == "none":
                continue
            raw_values = [row.get("finance_values", {}).get(field["field_key"], "") for row in rows]
            if operation == "count":
                value = Decimal(sum(1 for raw in raw_values if normalize_text(raw)))
            else:
                values = [_decimal(raw) for raw in raw_values if normalize_text(raw)]
                if not values:
                    value = Decimal("0")
                elif operation == "sum":
                    value = sum(values, Decimal("0"))
                elif operation == "avg":
                    value = sum(values, Decimal("0")) / Decimal(len(values))
                elif operation == "min":
                    value = min(values)
                elif operation == "max":
                    value = max(values)
                else:
                    value = Decimal("0")
            places = min(6, max(0, int(field.get("decimal_places", 2))))
            result[field["field_key"]] = {
                "operation": operation,
                "value": str(value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)),
                "count": len(raw_values),
            }
        return result

    # --- Dinamik prim motoru --------------------------------------------------------
    def list_commission_rules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            result = []
            for row in conn.execute("SELECT * FROM commission_rules WHERE active=1 ORDER BY created_at,name"):
                item = dict(row)
                item["conditions"] = json.loads(item.pop("conditions_json") or "[]")
                result.append(item)
            return result

    def create_or_update_commission_rule(self, data: dict[str, Any], rule_id: str | None = None, actor: str = "Yerel Kullanıcı") -> dict[str, Any]:
        name = normalize_text(data.get("name"))
        base_field_key = normalize_text(data.get("base_field_key"))
        calculation_type = normalize_text(data.get("calculation_type"))
        conditions = data.get("conditions", [])
        if not name or not base_field_key:
            raise DatabaseError("Prim kuralı adı ve hesaplama alanı zorunludur.")
        if calculation_type not in {"percent", "fixed", "per_unit"}:
            raise DatabaseError("Prim hesaplama türü geçersiz.")
        if not isinstance(conditions, list):
            raise DatabaseError("Prim koşulları liste biçiminde olmalıdır.")
        rate_text = normalize_text(data.get("rate"))
        _strict_decimal(rate_text, "Prim oranı/tutarı")
        now = utc_now()
        self.backup("prim_kurali_degisiklik_oncesi")
        with self.transaction() as conn:
            if rule_id:
                current = conn.execute("SELECT row_version FROM commission_rules WHERE id=? AND active=1", (rule_id,)).fetchone()
                if not current:
                    raise DatabaseError("Prim kuralı bulunamadı.")
                expected = int(data.get("row_version", current[0]))
                cursor = conn.execute("""
                    UPDATE commission_rules SET name=?,sales_person=?,conditions_json=?,base_field_key=?,calculation_type=?,rate_text=?,updated_at=?,row_version=row_version+1
                    WHERE id=? AND row_version=?
                """, (name, normalize_text(data.get("sales_person")), json.dumps(conditions, ensure_ascii=False), base_field_key, calculation_type, rate_text, now, rule_id, expected))
                if cursor.rowcount != 1:
                    raise ConflictError("Prim kuralı başka bir işlem tarafından değiştirildi.")
                action = "update"
            else:
                rule_id = new_id("prim")
                conn.execute("""
                    INSERT INTO commission_rules(id,name,sales_person,conditions_json,base_field_key,calculation_type,rate_text,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (rule_id, name, normalize_text(data.get("sales_person")), json.dumps(conditions, ensure_ascii=False), base_field_key, calculation_type, rate_text, now, now))
                action = "create"
            self._audit(conn, actor, action, "commission_rule", rule_id, {"name": name})
        return next(item for item in self.list_commission_rules() if item["id"] == rule_id)

    @staticmethod
    def _condition_matches(actual: Any, operator: str, expected: Any) -> bool:
        a, e = normalize_text(actual), normalize_text(expected)
        if operator == "eq": return search_key(a) == search_key(e)
        if operator == "neq": return search_key(a) != search_key(e)
        if operator == "contains": return search_key(e) in search_key(a)
        if operator == "in": return search_key(a) in {search_key(item) for item in e.split(",")}
        if operator == "gt": return _decimal(a) > _decimal(e)
        if operator == "gte": return _decimal(a) >= _decimal(e)
        if operator == "lt": return _decimal(a) < _decimal(e)
        if operator == "lte": return _decimal(a) <= _decimal(e)
        return False

    def calculate_commissions(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        rows, _fields = self._finance_rows(filters)
        rules = self.list_commission_rules()
        details = []
        total = Decimal("0")
        for rule in rules:
            for row in rows:
                if rule["sales_person"] and search_key(row.get("sales_person")) != search_key(rule["sales_person"]):
                    continue
                context = {**row, **row.get("finance_values", {})}
                if not all(self._condition_matches(context.get(condition.get("field", ""), ""), normalize_text(condition.get("operator")) or "eq", condition.get("value", "")) for condition in rule["conditions"]):
                    continue
                base = _decimal(context.get(rule["base_field_key"], 0))
                rate = _decimal(rule["rate_text"])
                amount = rate if rule["calculation_type"] == "fixed" else base * rate if rule["calculation_type"] == "per_unit" else base * rate / Decimal("100")
                amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                total += amount
                details.append({
                    "rule_id": rule["id"], "rule_name": rule["name"], "institution_id": row["id"],
                    "institution_name": row["name"], "sales_person": row.get("sales_person", ""),
                    "base": str(base), "rate": str(rate), "amount_minor": money_to_minor(amount),
                })
        return {"items": details, "total_minor": money_to_minor(total), "rule_count": len(rules), "institution_count": len({item["institution_id"] for item in details})}

    # --- Filtrelenmiş dışa aktarma --------------------------------------------------
    def export_institution_rows(self, filters: dict[str, Any] | None = None, columns: list[str] | None = None) -> tuple[list[str], list[list[Any]]]:
        data = self.list_institutions(filters or {})
        custom = {field["id"]: field for field in self.list_custom_fields("institution")}
        labels = {
            "list_number": "Liste No", "group_number": "Grup", "sequence_number": "Kayıt Sıra", "institution": "Kurum", "identity": "Kimlik",
            "location": "Konum", "status": "Durum", "panel": "Panel", "turnstile": "Turnike",
            "last_seen": "Son Bağlantı", "installation_date": "Kurulum", "sales_person": "Satışı Yapan",
            "technical_person": "Teknik Servis", "accounting_person": "Muhasebe",
        }
        selected = [key for key in (columns or list(labels)) if key not in {"actions", "select"}]
        headers = [custom[key[7:]]["label"] if key.startswith("custom:") and key[7:] in custom else labels.get(key, key) for key in selected]
        result = []
        for group_index, group in enumerate(data["groups"], 1):
            for child_index, item in enumerate(group["children"], 1):
                list_number: int | str = group_index if len(group["children"]) == 1 else f"{group_index}.{child_index}"
                values = {
                    "list_number": list_number,
                    "group_number": item.get("group_number"), "sequence_number": item.get("sequence_number"), "institution": item.get("name"),
                    "identity": f"ID: {item.get('portal_id') or ''} / Kod: {item.get('institution_code') or ''}",
                    "location": " / ".join(value for value in [item.get("city"), item.get("district")] if value),
                    "status": item.get("customer_status") or item.get("health_status"), "panel": item.get("panel_count"),
                    "turnstile": item.get("turnstile_count"), "last_seen": item.get("last_seen"), "installation_date": item.get("installation_date"),
                    "sales_person": item.get("sales_person"), "technical_person": item.get("technical_person"), "accounting_person": item.get("accounting_person"),
                }
                result.append([(item.get("custom_values") or {}).get(key[7:], "") if key.startswith("custom:") else values.get(key, "") for key in selected])
        return headers, result

    def export_dynamic_finance_rows(self, filters: dict[str, Any] | None = None, columns: list[str] | None = None) -> tuple[list[str], list[list[Any]]]:
        rows, fields = self._finance_rows(filters)
        field_map = {field["field_key"]: field for field in fields}
        aggregates = self._finance_aggregates(rows, fields)
        default_columns = ["list_number", "group_number", "sequence_number", "institution", "city", "district", "sales_person", *[
            f"finance:{field['field_key']}" for field in fields if field["visible"]
        ]]
        selected = [key for key in (columns or default_columns) if key not in {"actions", "select"}]
        fixed_labels = {
            "list_number": "Liste No", "group_number": "Grup", "sequence_number": "Kayıt Sıra",
            "institution": "Kurum", "city": "Şehir", "district": "İlçe", "sales_person": "Satış Temsilcisi",
        }
        headers = [field_map[key[8:]]["label"] if key.startswith("finance:") and key[8:] in field_map else fixed_labels.get(key, key) for key in selected]
        output = []
        groups = self._group_finance_rows(rows)
        for group_index, group in enumerate(groups, 1):
            for child_index, row in enumerate(group["children"], 1):
                values: dict[str, Any] = {
                    "list_number": group_index if len(group["children"]) == 1 else f"{group_index}.{child_index}",
                    "group_number": row.get("group_number"), "sequence_number": row.get("sequence_number"),
                    "institution": row.get("name"), "city": row.get("city"), "district": row.get("district"),
                    "sales_person": row.get("sales_person"),
                }
                record = []
                for key in selected:
                    if key.startswith("finance:") and key[8:] in field_map:
                        field = field_map[key[8:]]
                        raw = row["finance_values"].get(field["field_key"], "")
                        record.append(_decimal(raw) if raw != "" and field["data_type"] in {"number", "money", "formula"} else raw)
                    else:
                        record.append(values.get(key, ""))
                output.append(record)
        if output:
            totals: list[Any] = []
            for index, key in enumerate(selected):
                if key == "institution":
                    totals.append("TOPLAM")
                elif key.startswith("finance:") and key[8:] in aggregates:
                    totals.append(_decimal(aggregates[key[8:]]["value"]))
                else:
                    totals.append("")
            output.append(totals)
        return headers, output
