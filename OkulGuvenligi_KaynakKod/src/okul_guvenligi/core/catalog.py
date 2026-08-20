from __future__ import annotations

from dataclasses import dataclass


def key(text: object) -> str:
    value = str(text or "").strip().casefold()
    table = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "İ": "i"})
    return " ".join(value.translate(table).replace("_", " ").split())


@dataclass(frozen=True)
class StandardAction:
    action_key: str
    label: str
    action_type: str
    icon: str
    style: str
    placement: str
    confirmation_text: str = ""


STANDARD_ACTIONS = {
    "new_record": StandardAction("new_record", "Yeni Kayıt", "new_record", "＋", "primary", "page_top"),
    "export_xlsx": StandardAction("export_xlsx", "Excel’e İndir", "export_xlsx", "⇩", "secondary", "page_top"),
    "edit_record": StandardAction("edit_record", "Düzenle", "edit_record", "✎", "secondary", "row"),
    "duplicate_record": StandardAction("duplicate_record", "Kopyala", "duplicate_record", "⧉", "secondary", "row"),
    "archive_record": StandardAction(
        "archive_record", "Sil", "archive_record", "⌫", "danger", "row",
        "Bu kayıt arşive alınsın mı? Kayıt geri yüklenebilir ve geçmiş korunur.",
    ),
    "save_record": StandardAction("save_record", "Kaydet", "save_record", "✓", "primary", "form_footer"),
    "cancel_form": StandardAction("cancel_form", "Vazgeç", "cancel_form", "×", "ghost", "form_footer"),
}

ACTION_ALIASES = {
    "yeni": "new_record", "yeni kayit": "new_record", "ekle": "new_record",
    "excel": "export_xlsx", "excele indir": "export_xlsx", "indir": "export_xlsx",
    "duzenle": "edit_record", "degistir": "edit_record", "edit": "edit_record",
    "kopyala": "duplicate_record", "cogalt": "duplicate_record",
    "sil": "archive_record", "silme": "archive_record", "arsivle": "archive_record",
    "kaydet": "save_record", "save": "save_record",
    "vazgec": "cancel_form", "iptal": "cancel_form",
}

PLACEMENT_ALIASES = {
    "sayfa ustu": "page_top", "ust": "page_top", "toolbar": "page_top", "arac cubugu": "page_top",
    "satir": "row", "kayit satiri": "row", "sag": "row",
    "form ustu": "form_header", "form basi": "form_header",
    "form alti": "form_footer", "form sonu": "form_footer", "alt": "form_footer",
}

FIELD_TYPES = {"text", "longtext", "number", "money", "date", "boolean", "select", "relation", "formula"}


def resolve_action_name(name: str) -> StandardAction | None:
    wanted = key(name)
    action_key = ACTION_ALIASES.get(wanted)
    if not action_key:
        for alias, mapped in ACTION_ALIASES.items():
            if alias and alias in wanted:
                action_key = mapped
                break
    return STANDARD_ACTIONS.get(action_key or "")


def resolve_placement(name: str) -> str:
    raw = str(name or "").strip()
    if raw in {"page_top", "row", "form_header", "form_footer"}:
        return raw
    wanted = key(raw)
    normalized_enum = wanted.replace(" ", "_")
    if normalized_enum in {"page_top", "row", "form_header", "form_footer"}:
        return normalized_enum
    for alias, mapped in PLACEMENT_ALIASES.items():
        if alias in wanted:
            return mapped
    return ""
