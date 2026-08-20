from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .database import normalize_text


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200


class ImportErrorDetail(ValueError):
    pass


@dataclass
class ParsedWorkbook:
    records: list[dict[str, Any]]
    sha256: str
    warnings: list[str]


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + (ord(char) - 64)
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(name))
    result = []
    for item in root.findall(f"{{{MAIN_NS}}}si"):
        result.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))
    return result


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook_path = "xl/workbook.xml"
    rels_path = "xl/_rels/workbook.xml.rels"
    if workbook_path not in archive.namelist() or rels_path not in archive.namelist():
        if "xl/worksheets/sheet1.xml" in archive.namelist():
            return "xl/worksheets/sheet1.xml"
        raise ImportErrorDetail("Excel çalışma sayfası bulunamadı.")
    workbook = ET.fromstring(archive.read(workbook_path))
    first_sheet = workbook.find(f".//{{{MAIN_NS}}}sheet")
    if first_sheet is None:
        raise ImportErrorDetail("Excel dosyasında çalışma sayfası yok.")
    rel_id = first_sheet.attrib.get(f"{{{DOC_REL_NS}}}id")
    rels = ET.fromstring(archive.read(rels_path))
    target = None
    for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target")
            break
    if not target:
        raise ImportErrorDetail("Çalışma sayfası bağlantısı çözülemedi.")
    if target.startswith("/"):
        path = target.lstrip("/")
    else:
        path = posixpath.normpath(posixpath.join("xl", target))
    if path not in archive.namelist():
        raise ImportErrorDetail("Çalışma sayfası içeriği bulunamadı.")
    return path


def read_first_sheet(data: bytes) -> list[list[str]]:
    if not data:
        raise ImportErrorDetail("Excel dosyası boş.")
    if len(data) > MAX_FILE_SIZE:
        raise ImportErrorDetail("Excel dosyası 25 MB sınırını aşıyor.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportErrorDetail("Geçerli bir .xlsx dosyası seçilmelidir.") from exc
    with archive:
        suspicious = [name for name in archive.namelist() if ".." in PurePosixPath(name).parts]
        if suspicious:
            raise ImportErrorDetail("Excel dosyası güvenli olmayan yollar içeriyor.")
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        rows: list[list[str]] = []
        for _event, elem in ET.iterparse(archive.open(sheet_path), events=("end",)):
            if elem.tag != f"{{{MAIN_NS}}}row":
                continue
            row_values: dict[int, str] = {}
            for cell in elem.findall(f"{{{MAIN_NS}}}c"):
                col = _column_index(cell.attrib.get("r", "A1"))
                if col >= MAX_COLUMNS:
                    continue
                cell_type = cell.attrib.get("t", "")
                value = ""
                if cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
                else:
                    node = cell.find(f"{{{MAIN_NS}}}v")
                    raw = node.text if node is not None and node.text is not None else ""
                    if cell_type == "s" and raw:
                        try:
                            value = shared[int(raw)]
                        except (ValueError, IndexError):
                            value = raw
                    elif cell_type == "b":
                        value = "Evet" if raw == "1" else "Hayır"
                    else:
                        value = raw
                row_values[col] = normalize_text(value)
            width = (max(row_values) + 1) if row_values else 0
            rows.append([row_values.get(index, "") for index in range(width)])
            elem.clear()
            if len(rows) > MAX_ROWS:
                raise ImportErrorDetail("Excel dosyası 100.000 satır sınırını aşıyor.")
        return rows


def _cell(row: list[str], index: int) -> str:
    return normalize_text(row[index]) if index < len(row) else ""


def _detect_school_type(name: str) -> str:
    upper = name.upper()
    checks = [
        ("ANAOKUL", "Anaokulu"), ("ANASINIF", "Anasınıfı"),
        ("İLKOKUL", "İlkokul"), ("ORTAOKUL", "Ortaokul"),
        ("FEN LİSES", "Fen Lisesi"), ("ANADOLU LİSES", "Anadolu Lisesi"),
        ("MESLEKİ VE TEKNİK", "Mesleki ve Teknik Anadolu Lisesi"),
        ("LİSE", "Lise"), ("ÖĞRETİM KURSU", "Özel Öğretim Kursu"),
    ]
    return next((label for needle, label in checks if needle in upper), "")


def _status_value(text: str, choices: list[str]) -> str:
    return next((choice for choice in choices if choice in text), "")


def _prefixed(values: list[str], prefix: str) -> str:
    for value in values:
        if value.startswith(prefix):
            return normalize_text(value[len(prefix):])
    return ""


def _ipv4_values(text: str) -> list[str]:
    values = re.findall(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", text)
    result = []
    for value in values:
        try:
            if all(0 <= int(part) <= 255 for part in value.split(".")):
                result.append(value)
        except ValueError:
            continue
    return result


def _parse_camera(cells: list[str], direction: str) -> tuple[str, str, str]:
    direction_upper = direction.upper()
    for index, value in enumerate(cells):
        upper = value.upper()
        if direction_upper in upper and "KAMERA" in upper:
            status = "AKTİF" if "AKTİF" in upper else ("PASİF" if "PASİF" in upper else "")
            ip = ""
            rtsp = ""
            for following in cells[index + 1:index + 5]:
                if following.upper().startswith("IP:") and not ip:
                    ip = normalize_text(following.split(":", 1)[1])
                if following.upper().startswith("RTSP:") and not rtsp:
                    rtsp = normalize_text(following.split(":", 1)[1])
            return status, ip, rtsp
    combined = next((value for value in cells if "GİRİŞ / ÇIKIŞ KAMERASI" in value.upper()), "")
    if combined:
        status = "AKTİF" if "AKTİF" in combined.upper() else ("PASİF" if "PASİF" in combined.upper() else "")
        return status, "", ""
    return "", "", ""


def _panel_from_rows(segment: list[list[str]], start: int, end: int, ordinal: int) -> dict[str, Any]:
    detail = _cell(segment[start], 3)
    block_rows = segment[start:end]
    d_cells = [_cell(row, 3) for row in block_rows if _cell(row, 3)]
    e_cells = [_cell(row, 4) for row in block_rows if _cell(row, 4)]
    all_cells = d_cells + e_cells
    local_ips = _ipv4_values(detail)
    local_ip = local_ips[-1] if local_ips else ""
    before_product, _, tail = detail.partition("Kapı Kontrol")
    gate_name = normalize_text(before_product)
    tail_without_ip = tail
    if local_ip:
        tail_without_ip = tail_without_ip.rsplit(local_ip, 1)[0]
    panel_key_match = re.search(r"([0-9]+Kt[^\s]*)", tail_without_ip, re.IGNORECASE)
    panel_key = normalize_text(panel_key_match.group(1) if panel_key_match else tail_without_ip)
    if not panel_key:
        panel_key = f"panel_{ordinal}"
    panel_name_match = re.search(r"Kapı Kontrol\s*([0-9]+)?", detail, re.IGNORECASE)
    panel_name = "Kapı Kontrol" + (f" {panel_name_match.group(1)}" if panel_name_match and panel_name_match.group(1) else "")
    count_match = re.search(r"(\d+)\s*TURNİKELİ", detail.upper())
    if count_match:
        turnstile_count: int | None = int(count_match.group(1))
    elif "TEK TURNİKELİ" in detail.upper():
        turnstile_count = 1
    elif "TURNİKESİZ" in detail.upper():
        turnstile_count = 0
    else:
        turnstile_count = None
    date_match = next((re.search(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?", value) for value in all_cells if re.search(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?", value)), None)
    last_seen = date_match.group(0) if date_match else ""
    version_source = next((value for value in d_cells[1:] if re.search(r"\b\d+\.\d+\.\d+\.\d+\b", value)), "")
    versions = [value for value in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", version_source) if value not in _ipv4_values(version_source)]
    software_version = versions[0] if versions else ""
    database_version = versions[1] if len(versions) > 1 else ""
    external_ip = ""
    for value in e_cells:
        for candidate in _ipv4_values(value):
            if candidate != local_ip:
                external_ip = candidate
                break
        if external_ip:
            break
    network_cell = next((value for value in e_cells if any(term in value.upper() for term in ("KEENETIC", "VODAFONE", "AVEA", "TURKCELL", "KURUM İNTERNETİ"))), "")
    modem = "Keenetic" if "KEENETIC" in network_cell.upper() else ("Kurum İnterneti" if "KURUM İNTERNETİ" in network_cell.upper() else "")
    operator = next((label for label in ("Vodafone", "Avea", "Turkcell", "Türk Telekom") if label.upper() in network_cell.upper()), "")
    phone_match = re.search(r"(?<!\d)0?5\d{9}(?!\d)", network_cell.replace(" ", ""))
    phone = phone_match.group(0) if phone_match else ""
    entry_status, entry_ip, entry_rtsp = _parse_camera(d_cells, "GİRİŞ")
    exit_status, exit_ip, exit_rtsp = _parse_camera(d_cells, "ÇIKIŞ")
    physical_basis = re.sub(r"\s+", "", detail).lower()
    physical_system_key = hashlib.sha1(physical_basis.encode("utf-8")).hexdigest()[:20]
    return {
        "panel_key": panel_key,
        "physical_system_key": physical_system_key,
        "name": panel_name,
        "gate_name": gate_name,
        "product_name": "Kapı Kontrol",
        "turnstile_count": turnstile_count,
        "turnstile_label": gate_name,
        "local_ip": local_ip,
        "external_ip": external_ip,
        "software_version": software_version,
        "database_version": database_version,
        "last_seen": last_seen,
        "modem": modem,
        "operator": operator,
        "phone": phone,
        "entry_camera_status": entry_status,
        "entry_camera_ip": entry_ip,
        "entry_camera_rtsp": entry_rtsp,
        "exit_camera_status": exit_status,
        "exit_camera_ip": exit_ip,
        "exit_camera_rtsp": exit_rtsp,
        "status": "Kayıtlı",
        "raw_detail": json.dumps(all_cells, ensure_ascii=False),
    }


def parse_portal_export(rows: list[list[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    start_indexes = [
        index for index, row in enumerate(rows)
        if _cell(row, 0).isdigit() and " - " in _cell(row, 1)
    ]
    if not start_indexes:
        raise ImportErrorDetail("Kurum kayıtları bulunamadı. Okul Güvenliği kurumlar sayfasından alınan .xlsx dosyasını seçin.")
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for order, start in enumerate(start_indexes, 1):
        end = start_indexes[order] if order < len(start_indexes) else len(rows)
        segment = rows[start:end]
        values = [normalize_text(value) for row in segment for value in row if normalize_text(value)]
        full_name = _cell(segment[0], 1)
        raw_numbers = _cell(segment[0], 0)
        sequence_text = str(order)
        group_number = None
        if raw_numbers.endswith(sequence_text):
            group_text = raw_numbers[:-len(sequence_text)]
            if group_text.isdigit() and 1 <= int(group_text) <= order:
                group_number = int(group_text)
        location = full_name.split(" - ")
        city = location[0] if location else ""
        district = location[1] if len(location) > 1 else ""
        institution_name = " - ".join(location[2:]) if len(location) > 2 else full_name
        status_text = " ".join(_cell(row, 2) for row in segment if _cell(row, 2))
        portal_id = _prefixed(values, "ID:")
        institution_code = _prefixed(values, "KURUM KODU:")
        if not portal_id or not institution_code:
            warnings.append(f"Excel satır {start + 1}: Kurum ID veya kurum kodu eksik.")
        dealer = _prefixed(values, "BY:")
        customer_person = _prefixed(values, "MT:")
        technical_person = _prefixed(values, "TS:")
        accounting_person = _prefixed(values, "MH:")
        rating_text = next((value for value in values if "Puan" in value), "")
        rating_match = re.search(r"([0-5])\s*Puan", rating_text, re.IGNORECASE)
        panel_starts = [
            index for index, row in enumerate(segment)
            if "KAPI KONTROL" in _cell(row, 3).upper()
        ]
        panels = []
        used_panel_keys: dict[str, int] = {}
        for panel_order, panel_start in enumerate(panel_starts, 1):
            panel_end = panel_starts[panel_order] if panel_order < len(panel_starts) else len(segment)
            panel = _panel_from_rows(segment, panel_start, panel_end, panel_order)
            base_key = panel["panel_key"]
            used_panel_keys[base_key] = used_panel_keys.get(base_key, 0) + 1
            if used_panel_keys[base_key] > 1:
                panel["panel_key"] = f"{base_key}_{used_panel_keys[base_key]}"
            panels.append(panel)
        records.append({
            "portal_id": portal_id,
            "institution_code": institution_code,
            "sequence_number": order,
            "group_number": group_number,
            "name": institution_name,
            "city": city,
            "district": district,
            "school_type": _detect_school_type(institution_name),
            "sms_status": next((value for value in values if value.startswith("SMS ")), ""),
            "rental_status": "KİRALIK" if "KİRALIK" in status_text else ("SATILIK" if "SATILIK" in status_text else ""),
            "customer_status": _status_value(status_text, [
                "PAZARLAMA AŞAMASINDA İPTAL", "GEÇİCİ KULLANIM DIŞI", "KULLANIMI BIRAKANLAR",
                "RAKİBE GİDENLER", "ŞİRKET HESAPLARI", "PARA KAZANDIRMIYOR", "AKTİF",
            ]),
            "payment_status": _status_value(status_text, ["MUHASEBE İŞLEMLERİ BAŞLAMADI", "ÖDEMESİNİ YAPTI"]),
            "sales_period": _prefixed(values, "SD:"),
            "sales_person": _prefixed(values, "ST:"),
            "dealer": dealer,
            "technical_person": technical_person,
            "accounting_person": accounting_person,
            "customer_person": customer_person,
            "pilot": 1 if any("Pilot Kurum" in value for value in values) else 0,
            "health_status": next((value for value in values if value.startswith("Kurumda ")), ""),
            "rating": int(rating_match.group(1)) if rating_match else None,
            "source": "excel",
            "source_row": start + 1,
            "active": 1,
            "panels": panels,
        })
    portal_ids = [record["portal_id"] for record in records if record["portal_id"]]
    codes = [record["institution_code"] for record in records if record["institution_code"]]
    if len(set(portal_ids)) != len(portal_ids):
        raise ImportErrorDetail("Excel dosyasında tekrarlanan portal kurum ID kayıtları var. Veriler uygulanmadı.")
    if len(set(codes)) != len(codes):
        raise ImportErrorDetail("Excel dosyasında tekrarlanan kurum kodları var. Veriler uygulanmadı.")
    return records, warnings


def parse_xlsx(data: bytes) -> ParsedWorkbook:
    rows = read_first_sheet(data)
    records, warnings = parse_portal_export(rows)
    return ParsedWorkbook(records=records, sha256=hashlib.sha256(data).hexdigest(), warnings=warnings)
