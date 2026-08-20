from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import io
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from . import APP_VERSION
from .database import (
    ConflictError, Database, DatabaseError, DEFAULT_FINANCE_SUMMARY_CARDS,
    DEFAULT_INSTITUTION_FORM_FIELDS, DEFAULT_NAVIGATION_PREFERENCES, DEFAULT_THEME_PREFERENCES,
)
from .xlsx_import import ImportErrorDetail, parse_xlsx
from .xlsx_export import build_xlsx
from .platform_engine import PlatformEngine
from .core import HealthMonitor, RecoveryManager
from .assistant import AssistantConfigStore, LogosAssistantService, OpenAIVisionPlanner


MAX_REQUEST_BYTES = 25 * 1024 * 1024
DEFAULT_PORT = 8765

SETTING_ENDPOINTS = {
    "/api/settings/table-columns": ("table_columns", []),
    "/api/settings/finance-table-columns": ("finance_table_columns", []),
    "/api/settings/filters": ("dynamic_filters", ["city", "district", "health", "status", "sales_person"]),
    "/api/settings/finance-filters": ("finance_filters", []),
    "/api/settings/finance-saved-views": ("finance_saved_views", []),
    "/api/settings/finance-summary-cards": ("finance_summary_cards", DEFAULT_FINANCE_SUMMARY_CARDS),
    "/api/settings/general": ("general_preferences", {}),
    "/api/settings/institution-form": ("institution_form_fields", DEFAULT_INSTITUTION_FORM_FIELDS),
    "/api/settings/navigation": ("navigation_preferences", DEFAULT_NAVIGATION_PREFERENCES),
    "/api/settings/theme": ("theme_preferences", DEFAULT_THEME_PREFERENCES),
}


def default_data_dir() -> Path:
    override = os.environ.get("OKUL_GUVENLIGI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Sekizdesekiz" / "OkulGuvenligi"


def setup_logging(data_dir: Path) -> logging.Logger:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("okul_guvenligi")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "uygulama.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class PreviewStore:
    def __init__(self):
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, item: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._cleanup()
            self._items[token] = {**item, "created": time.time()}
        return token

    def pop(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup()
            return self._items.pop(token, None)

    def _cleanup(self):
        expiry = time.time() - 30 * 60
        for token in [key for key, value in self._items.items() if value["created"] < expiry]:
            self._items.pop(token, None)


class AppState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._install_seed_if_needed()
        self.db = Database(data_dir)
        self.platform = PlatformEngine(self.db)
        self.health = HealthMonitor(self.db)
        self.recovery = RecoveryManager(self.db, version=APP_VERSION)
        self.recovery_status = self.recovery.start()
        self.assistant_config = AssistantConfigStore(data_dir)
        self.assistant = self._build_assistant()
        self.token = secrets.token_urlsafe(32)
        self.previews = PreviewStore()
        self.mutation_lock = threading.RLock()
        self.logger = setup_logging(data_dir)
        from .portal_sync import PortalAutoSyncManager, PortalLoginSessionStore
        self.portal_autosync = PortalAutoSyncManager(self.db)
        self.portal_autosync.start()
        self.portal_sessions = PortalLoginSessionStore()
        self.server: ThreadingHTTPServer | None = None



    def _build_assistant(self) -> LogosAssistantService:
        config = self.assistant_config.load()
        # Kaydedilmiş anahtar yoksa OpenAIVisionPlanner ortam değişkenine güvenli biçimde geri döner.
        planner = OpenAIVisionPlanner(api_key=config.api_key or None, model=config.model)
        return LogosAssistantService(self.db, self.platform, self.health, planner=planner)

    def reload_assistant(self) -> None:
        self.assistant = self._build_assistant()

    def _install_seed_if_needed(self) -> None:
        target = self.data_dir / "okul_guvenligi.db"
        if target.exists():
            return
        try:
            package_root = resources.files("okul_guvenligi")
            seed = package_root.joinpath("seed", "okul_guvenligi.db")
            digest_file = package_root.joinpath("seed", "okul_guvenligi.db.sha256")
            if not seed.is_file() or not digest_file.is_file():
                return
            data = seed.read_bytes()
            expected = digest_file.read_text(encoding="utf-8").strip()
            actual = hashlib.sha256(data).hexdigest()
            if not secrets.compare_digest(actual, expected):
                raise DatabaseError("Başlangıç verisinin doğrulama değeri eşleşmedi.")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.data_dir / ".ilk_kurulum.tmp"
            with temp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except FileNotFoundError:
            return


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LogosTechPlatform/5.0"
    state: AppState

    def log_message(self, format_string: str, *args: Any) -> None:
        self.state.logger.info("%s - %s", self.address_string(), format_string % args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if filename:
            quoted = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DatabaseError("Geçersiz istek boyutu.") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise DatabaseError("İstek 25 MB sınırını aşıyor.")
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body()
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DatabaseError("Geçersiz JSON isteği.") from exc
        if not isinstance(value, dict):
            raise DatabaseError("İstek nesne biçiminde olmalıdır.")
        return value

    def _require_token(self) -> None:
        supplied = self.headers.get("X-App-Token", "")
        if not secrets.compare_digest(supplied, self.state.token):
            raise PermissionError("Geçersiz uygulama oturumu.")

    @staticmethod
    def _post_changes_data(path: str) -> bool:
        # Ön izleme, asistan düşünme, hesaplama ve kapatma veritabanı mutasyonu değildir.
        read_like = {
            "/api/import/preview", "/api/platform/change/preview",
            "/api/platform/assistant/preview", "/api/assistant/preview",
            "/api/commissions/calculate", "/api/shutdown",
        }
        return path not in read_like

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(exc, ConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, (DatabaseError, ImportErrorDetail, ValueError)):
            status = HTTPStatus.BAD_REQUEST
        elif isinstance(exc, FileNotFoundError):
            status = HTTPStatus.NOT_FOUND
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            self.state.logger.exception("Beklenmeyen sunucu hatası")
        self._send_json({"ok": False, "error": str(exc) or "Beklenmeyen hata"}, int(status))

    def do_GET(self) -> None:
        try:
            self._get()
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        try:
            self._require_token()
            path, _query = self._route()
            if self._post_changes_data(path):
                with self.state.mutation_lock:
                    self.state.health.require_writable()
                    self._post()
            else:
                self._post()
        except Exception as exc:
            self._handle_error(exc)

    def do_PUT(self) -> None:
        try:
            self._require_token()
            with self.state.mutation_lock:
                self.state.health.require_writable()
                self._put()
        except Exception as exc:
            self._handle_error(exc)

    def do_DELETE(self) -> None:
        try:
            self._require_token()
            with self.state.mutation_lock:
                self.state.health.require_writable()
                self._delete()
        except Exception as exc:
            self._handle_error(exc)

    def _get(self) -> None:
        path, query = self._route()
        if path in {"/", "/index.html"}:
            return self._serve_resource("web/index.html", "text/html; charset=utf-8")
        if path == "/assets/styles.css":
            return self._serve_resource("web/styles.css", "text/css; charset=utf-8")
        if path == "/assets/app.js":
            return self._serve_resource("web/app.js", "text/javascript; charset=utf-8")
        if path == "/assets/logos-tech.png":
            return self._serve_resource("web/logos-tech.png", "image/png")
        if path == "/api/session":
            return self._send_json({"ok": True, "token": self.state.token, "version": APP_VERSION, "assistant": {**self.state.assistant.status(), **self.state.assistant_config.public_status()}})
        if path == "/api/health":
            return self._send_json({"ok": True, "data": {**self.state.health.check(), "recovery": self.state.recovery_status}})
        if path == "/api/assistant/status":
            return self._send_json({"ok": True, "data": {**self.state.assistant.status(), **self.state.assistant_config.public_status()}})
        if path == "/api/assistant/settings":
            return self._send_json({"ok": True, "data": self.state.assistant_config.public_status()})
        if path == "/api/portal/autosync/status":
            return self._send_json({"ok": True, "data": self.state.portal_autosync.status()})

        if path == "/api/dashboard":
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.db.dashboard(filters)})
        if path == "/api/filters":
            return self._send_json({"ok": True, "data": self.state.db.list_filters()})
        if path == "/api/assistant/robot-status":
            autosync_info = self.state.portal_autosync.status()
            faulty_list = self.state.db.get_faulty_institutions_log()
            return self._send_json({
                "ok": True,
                "data": {
                    "autosync": autosync_info,
                    "faulty_institutions": faulty_list,
                    "faulty_count": len(faulty_list)
                }
            })

        if path == "/api/lookups":
            return self._send_json({"ok": True, "data": self.state.db.list_lookup_categories()})
        if path == "/api/platform/bootstrap":
            return self._send_json({"ok": True, "data": self.state.platform.bootstrap()})
        if path == "/api/platform/history":
            limit = int(query.get("limit", ["100"])[0])
            return self._send_json({"ok": True, "data": self.state.platform.list_history(limit)})
        platform_export_match = re_fullmatch(r"/api/platform/modules/([^/]+)/export/xlsx", path)
        if platform_export_match:
            columns = query.get("columns", [""])[0].split(",") if query.get("columns") else None
            filters = {key: values for key, values in query.items() if key != "columns"}
            label, headers, rows = self.state.platform.export_rows(platform_export_match.group(1), filters, columns)
            data = build_xlsx(headers, rows, label[:31] or "Dinamik Modül")
            filename = f"LOGOS_TECH_{platform_export_match.group(1)}.xlsx"
            return self._send_bytes(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename)
        platform_module_match = re_fullmatch(r"/api/platform/modules/([^/]+)", path)
        if platform_module_match:
            return self._send_json({"ok": True, "data": self.state.platform.get_module(platform_module_match.group(1))})
        platform_records_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records", path)
        if platform_records_match:
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.platform.list_records(platform_records_match.group(1), filters)})
        platform_record_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records/([^/]+)", path)
        if platform_record_match:
            return self._send_json({"ok": True, "data": self.state.platform.get_record(platform_record_match.group(1), platform_record_match.group(2))})
        platform_views_match = re_fullmatch(r"/api/platform/modules/([^/]+)/views", path)
        if platform_views_match:
            return self._send_json({"ok": True, "data": self.state.platform.list_views(platform_views_match.group(1))})
        if path == "/api/custom-fields":
            entity = query.get("entity_type", [None])[0]
            return self._send_json({"ok": True, "data": self.state.db.list_custom_fields(entity)})
        if path == "/api/custom-fields/trash":
            entity = query.get("entity_type", [None])[0]
            return self._send_json({"ok": True, "data": self.state.db.list_archived_custom_fields(entity)})
        if path == "/api/institutions":
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.db.list_institutions(filters)})
        if path == "/api/institutions/trash":
            return self._send_json({"ok": True, "data": self.state.db.list_archived_institutions()})
        if path in SETTING_ENDPOINTS:
            key, default = SETTING_ENDPOINTS[path]
            return self._send_json({"ok": True, "data": self.state.db.get_setting(key, default)})
        if path == "/api/settings/history":
            key = query.get("key", [""])[0]
            if key not in {item[0] for item in SETTING_ENDPOINTS.values()}:
                raise DatabaseError("Ayar geçmişi anahtarı geçersiz.")
            return self._send_json({"ok": True, "data": self.state.db.list_setting_history(key)})
        if path == "/api/groups":
            return self._send_json({"ok": True, "data": self.state.db.list_groups()})
        if path == "/api/finance/fields":
            return self._send_json({"ok": True, "data": self.state.db.list_finance_fields()})
        if path == "/api/finance/fields/trash":
            return self._send_json({"ok": True, "data": self.state.db.list_archived_finance_fields()})
        formula_versions_match = re_fullmatch(r"/api/finance/fields/([^/]+)/versions", path)
        if formula_versions_match:
            return self._send_json({"ok": True, "data": self.state.db.list_finance_formula_versions(formula_versions_match.group(1))})
        if path == "/api/finance/dashboard":
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.db.dynamic_finance_dashboard(filters)})
        if path == "/api/finance/institutions":
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.db.list_finance_institutions(filters)})
        finance_institution_match = re_fullmatch(r"/api/finance/institutions/([^/]+)", path)
        if finance_institution_match:
            return self._send_json({"ok": True, "data": self.state.db.get_finance_institution(finance_institution_match.group(1))})
        if path == "/api/commissions/rules":
            return self._send_json({"ok": True, "data": self.state.db.list_commission_rules()})
        if path == "/api/finance/accounts":
            filters = {key: values for key, values in query.items()}
            return self._send_json({"ok": True, "data": self.state.db.list_finance_accounts(filters)})
        finance_match = re_fullmatch(r"/api/finance/accounts/([^/]+)", path)
        if finance_match:
            item = self.state.db.get_finance_account(finance_match.group(1))
            if not item:
                raise FileNotFoundError("Cari hesap bulunamadı.")
            return self._send_json({"ok": True, "data": item})
        institution_photo_match = re_fullmatch(r"/api/institutions/([^/]+)/photo", path)
        if institution_photo_match:
            media = self.state.db.read_media_asset("institution", institution_photo_match.group(1), "primary")
            if not media:
                raise FileNotFoundError("Kurum fotoğrafı bulunamadı.")
            data, mime_type = media
            return self._send_bytes(data, mime_type)
        match = re_fullmatch(r"/api/institutions/([^/]+)", path)
        if match:
            item = self.state.db.get_institution(match.group(1))
            if not item:
                raise FileNotFoundError("Kurum bulunamadı.")
            return self._send_json({"ok": True, "data": item})
        if path == "/api/audit":
            limit = int(query.get("limit", ["100"])[0])
            return self._send_json({"ok": True, "data": self.state.db.list_audit(limit)})
        if path == "/api/backups":
            return self._send_json({"ok": True, "data": self.state.db.list_backups()})
        backup_match = re_fullmatch(r"/api/backups/([^/]+)", path)
        if backup_match:
            name = urllib.parse.unquote(backup_match.group(1))
            if Path(name).name != name:
                raise FileNotFoundError("Yedek bulunamadı.")
            target = self.state.db.backup_dir / name
            if not target.exists():
                raise FileNotFoundError("Yedek bulunamadı.")
            return self._send_bytes(target.read_bytes(), "application/octet-stream", name)
        if path in {"/api/export/xlsx", "/api/export/csv"}:
            return self._export_xlsx(query)
        if path in {"/api/finance/export/xlsx", "/api/finance/export/csv"}:
            return self._export_finance_xlsx(query)
        raise FileNotFoundError("Sayfa bulunamadı.")

    def _post(self) -> None:
        path, _query = self._route()
        if path == "/api/import/preview":
            data = self._read_body()
            file_name = urllib.parse.unquote(self.headers.get("X-File-Name", "kurumlar.xlsx"))
            if not file_name.lower().endswith(".xlsx"):
                raise ImportErrorDetail("Yalnızca .xlsx dosyası seçilebilir.")
            parsed = parse_xlsx(data)
            comparison = self.state.db.compare_import(parsed.records)
            token = self.state.previews.put({
                "records": parsed.records,
                "file_name": Path(file_name).name,
                "sha256": parsed.sha256,
                "warnings": parsed.warnings,
            })
            return self._send_json({
                "ok": True,
                "data": {
                    "token": token,
                    "total": len(parsed.records),
                    "panel_count": sum(len(item.get("panels", [])) for item in parsed.records),
                    "warnings": parsed.warnings,
                    **comparison,
                },
            })
        if path == "/api/assistant/settings":
            payload = self._read_json()
            config = self.state.assistant_config.save(
                api_key=str(payload.get("api_key", "")) if "api_key" in payload else None,
                model=str(payload.get("model", "")) if payload.get("model") else None,
                clear_key=bool(payload.get("clear_key", False)),
            )
            self.state.reload_assistant()
            return self._send_json({"ok": True, "data": {**self.state.assistant.status(), **self.state.assistant_config.public_status()}})
        if path == "/api/platform/change/preview":
            payload = self._read_json()
            preview = self.state.platform.preview_change(str(payload.get("operation", "")), payload.get("payload", {}))
            token = self.state.previews.put({"platform_change": preview})
            return self._send_json({"ok": True, "data": {**preview, "token": token}})
        if path == "/api/platform/change/commit":
            payload = self._read_json()
            stored = self.state.previews.pop(str(payload.get("token", "")))
            if not stored:
                raise DatabaseError("Değişiklik ön izlemesinin süresi doldu. Yeniden ön izleyin.")
            if "platform_change" in stored:
                result = self.state.platform.apply_change(stored["platform_change"])
            elif "assistant_plan" in stored:
                # 4.5 istemcileriyle geriye dönük uyumluluk: eski commit adresi asistan planını da güvenli katmandan geçirir.
                result = self.state.assistant.commit(stored["assistant_plan"])
            else:
                raise DatabaseError("Değişiklik ön izlemesinin türü geçersiz.")
            return self._send_json({"ok": True, "data": result})
        if path in {"/api/platform/assistant/preview", "/api/assistant/preview"}:
            payload = self._read_json()
            preview = self.state.assistant.preview(
                str(payload.get("command", payload.get("message", ""))),
                image_data_url=str(payload.get("image_data_url", "") or ""),
                screen_context=payload.get("screen_context") if isinstance(payload.get("screen_context"), dict) else {},
            )
            if preview.get("assistant_only") or not preview.get("executable", True):
                return self._send_json({"ok": True, "data": {**preview, "token": "", "assistant": True}})
            token = self.state.previews.put({"assistant_plan": preview})
            return self._send_json({"ok": True, "data": {**preview, "token": token, "assistant": True}})
        if path == "/api/assistant/commit":
            payload = self._read_json()
            stored = self.state.previews.pop(str(payload.get("token", "")))
            if not stored or "assistant_plan" not in stored:
                raise DatabaseError("Asistan ön izlemesinin süresi doldu. Yeniden inceleyin.")
            result = self.state.assistant.commit(stored["assistant_plan"])
            return self._send_json({"ok": True, "data": result})
        institution_photo_match = re_fullmatch(r"/api/institutions/([^/]+)/photo", path)
        if institution_photo_match:
            payload = self._read_json()
            data_url = str(payload.get("data_url", ""))
            image_match = re.fullmatch(r"data:([^;,]+);base64,(.+)", data_url, re.IGNORECASE | re.DOTALL)
            if not image_match:
                raise DatabaseError("Fotoğraf verisi okunamadı.")
            try:
                image_bytes = base64.b64decode(image_match.group(2), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise DatabaseError("Fotoğraf Base64 verisi geçersiz.") from exc
            item = self.state.db.save_media_asset(
                "institution", institution_photo_match.group(1), "primary",
                str(payload.get("name", "kurum-fotografi")), image_match.group(1), image_bytes,
            )
            return self._send_json({"ok": True, "data": item}, 201)
        platform_bulk_archive_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records/bulk-archive", path)
        if platform_bulk_archive_match:
            payload = self._read_json()
            result = self.state.platform.archive_records(platform_bulk_archive_match.group(1), payload.get("ids", []))
            return self._send_json({"ok": True, "data": result})
        platform_records_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records", path)
        if platform_records_match:
            item = self.state.platform.save_record(platform_records_match.group(1), self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        platform_views_match = re_fullmatch(r"/api/platform/modules/([^/]+)/views", path)
        if platform_views_match:
            item = self.state.platform.save_view(platform_views_match.group(1), self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/import/commit":
            payload = self._read_json()
            preview = self.state.previews.pop(str(payload.get("token", "")))
            if not preview:
                raise DatabaseError("İçe aktarma ön izlemesi süresi doldu. Dosyayı yeniden seçin.")
            result = self.state.db.import_records(
                preview["records"], preview["file_name"], preview["sha256"]
            )
            return self._send_json({"ok": True, "data": result})
        if path == "/api/portal/sync/text":
            payload = self._read_json()
            content = str(payload.get("content", ""))
            from .portal_sync import parse_portal_html_or_text
            records = parse_portal_html_or_text(content)
            if not records:
                raise DatabaseError("Portal metninden geçerli kurum kaydı çıkarılamadı. Kopyalanan sayfa içeriğini kontrol edin.")
            sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            result = self.state.db.import_records(records, "okulguvenligi.com Canlı Web Metin", sha256)
            return self._send_json({"ok": True, "data": result})
        if path == "/api/portal/sync/json":
            # Browser ds._data based sync — direct JSON, no HTML parsing needed
            payload = self._read_json()
            institutions = payload.get("institutions", [])
            if not institutions or not isinstance(institutions, list):
                raise DatabaseError("Geçerli kurum verisi bulunamadı. 'institutions' listesi boş.")
            # Map portal JSON fields to our record format
            records = []
            for item in institutions:
                if not isinstance(item, dict):
                    continue
                portal_id = str(item.get("portal_id") or "").strip()
                institution_code = str(item.get("institution_code") or "").strip()
                name = str(item.get("name") or "").strip()
                city = str(item.get("city") or "").strip()
                district = str(item.get("district") or "").strip()
                if not name:
                    continue
                # Skip anaokulu
                name_lower = name.lower()
                if any(kw in name_lower for kw in ["anaokul", "anasınıf", "anasinif", "kreş", "kres", "yuva", "montessori", "kindergarten"]):
                    continue
                # Skip non-LOGOS dealers
                dealer = str(item.get("dealer") or "LOGOS")
                if dealer and "LOGOS" not in dealer.upper():
                    continue
                notes = str(item.get("notes") or "").strip()
                health_status = "Kurumda Hatalar Var" if notes else "Kurumda sorun yok"
                customer_status = "AKTİF" if item.get("active") else "PASİF"
                records.append({
                    "portal_id": portal_id,
                    "institution_code": institution_code,
                    "name": name,
                    "city": city,
                    "district": district,
                    "health_status": health_status,
                    "customer_status": customer_status,
                    "notes": notes,
                    "sales_period": str(item.get("sales_period") or ""),
                    "sales_person": str(item.get("sales_person") or ""),
                    "dealer": dealer,
                    "marketing_person": str(item.get("marketing_person") or ""),
                    "customer_person": str(item.get("customer_person") or ""),
                    "technical_person": str(item.get("technical_person") or ""),
                    "accounting_person": str(item.get("accounting_person") or ""),
                    "panels": [],
                })
            if not records:
                raise DatabaseError("Filtrelerden geçen geçerli LOGOS kurumu bulunamadı.")
            sha256 = hashlib.sha256(f"json_{len(records)}".encode("utf-8")).hexdigest()
            result = self.state.db.import_records(records, "okulguvenligi.com Browser ds._data JSON", sha256)
            import datetime
            self.state.portal_autosync.last_sync_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return self._send_json({"ok": True, "data": result})

        if path == "/api/portal/sync/live":
            payload = self._read_json()
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            from .portal_sync import fetch_portal_live
            records = fetch_portal_live(username, password)
            if not records:
                raise DatabaseError("okulguvenligi.com sitesinden veri çekilemedi. Giriş bilgilerini kontrol edin.")
            sha256 = hashlib.sha256(f"{username}_{len(records)}".encode("utf-8")).hexdigest()
            result = self.state.db.import_records(records, "okulguvenligi.com Canlı Web Bağlantı", sha256)
            import datetime
            self.state.portal_autosync.last_sync_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return self._send_json({"ok": True, "data": result})
        if path == "/api/portal/login/start":
            payload = self._read_json()
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            result = self.state.portal_sessions.start_login(username, password)
            return self._send_json({"ok": True, "data": result})
        if path == "/api/portal/login/verify-sms":
            payload = self._read_json()
            session_id = str(payload.get("session_id", ""))
            sms_code = str(payload.get("sms_code", "")).strip()
            interval_minutes = int(payload.get("interval_minutes", 30))
            verify_res = self.state.portal_sessions.verify_sms(session_id, sms_code)
            cookie_string = verify_res["cookie_string"]
            records = verify_res.get("records", [])

            # Automatically save cookie & start background auto-sync task!
            if cookie_string:
                self.state.portal_autosync.save_config(
                    enabled=True,
                    mode="cookie",
                    cookie_string=cookie_string,
                    interval_minutes=interval_minutes,
                    trigger_now=True
                )

            import_res = {}
            import datetime
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.state.portal_autosync.last_sync_at = now_str
            if records:
                sha256 = hashlib.sha256(f"{cookie_string[:10]}_{len(records)}".encode("utf-8")).hexdigest()
                import_res = self.state.db.import_records(records, "okulguvenligi.com SMS Doğrulamalı Canlı Aktarım", sha256)
                self.state.db.recalculate_health_statuses()

            return self._send_json({"ok": True, "data": {"verify": verify_res, "import": import_res}})




        if path == "/api/portal/autosync/trigger":
            result = self.state.portal_autosync.trigger_sync()
            return self._send_json({"ok": True, "data": result})


        if path == "/api/institutions":
            item = self.state.db.create_institution(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/institutions/bulk-delete":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.archive_institutions(payload.get("institution_ids", []))})
        if path == "/api/institutions/restore":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.restore_institutions(payload.get("institution_ids", []))})
        if path == "/api/institutions/purge":
            payload = self._read_json()
            if payload.get("confirmation") != "KALICI SİL":
                raise DatabaseError("Kalıcı silme onayı eksik.")
            return self._send_json({"ok": True, "data": self.state.db.purge_institutions(payload.get("institution_ids", []))})
        if path == "/api/panels":
            item = self.state.db.create_or_update_panel(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/custom-fields":
            item = self.state.db.add_custom_field(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/lookups/items":
            item = self.state.db.create_or_update_lookup_item(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/lookups/items/reorder":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.reorder_lookup_items(
                str(payload.get("category_key", "")).strip(), payload.get("item_ids", [])
            )})
        if path == "/api/lookups/items/restore":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.restore_lookup_items(payload.get("item_ids", []))})
        if path == "/api/lookups/items/merge":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.merge_lookup_items(
                str(payload.get("source_id", "")).strip(), str(payload.get("target_id", "")).strip()
            )})
        if path == "/api/settings/history/restore":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.restore_setting_history(int(payload.get("history_id", 0)))})
        if path == "/api/custom-fields/bulk-delete":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.archive_custom_fields(payload.get("field_ids", []))})
        if path == "/api/custom-fields/restore":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.restore_custom_fields(payload.get("field_ids", []))})
        if path == "/api/custom-fields/purge":
            payload = self._read_json()
            if payload.get("confirmation") != "KALICI SİL":
                raise DatabaseError("Kalıcı silme onayı eksik.")
            return self._send_json({"ok": True, "data": self.state.db.purge_custom_fields(payload.get("field_ids", []))})
        if path == "/api/custom-fields/reorder":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.reorder_custom_fields(payload.get("field_ids", []))})
        if path == "/api/groups":
            item = self.state.db.create_or_update_group(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/finance/fields":
            item = self.state.db.create_or_update_finance_field(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/finance/fields/bulk-delete":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.archive_finance_fields(payload.get("field_ids", []))})
        if path == "/api/finance/fields/restore":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.restore_finance_fields(payload.get("field_ids", []))})
        if path == "/api/finance/fields/purge":
            payload = self._read_json()
            if payload.get("confirmation") != "KALICI SİL":
                raise DatabaseError("Kalıcı silme onayı eksik.")
            return self._send_json({"ok": True, "data": self.state.db.purge_finance_fields(payload.get("field_ids", []))})
        if path == "/api/finance/fields/reorder":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.reorder_finance_fields(payload.get("field_ids", []))})
        finance_value_match = re_fullmatch(r"/api/finance/institutions/([^/]+)/values", path)
        if finance_value_match:
            payload = self._read_json()
            item = self.state.db.set_finance_values(finance_value_match.group(1), payload.get("values", {}))
            return self._send_json({"ok": True, "data": item})
        if path == "/api/commissions/rules":
            item = self.state.db.create_or_update_commission_rule(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/commissions/calculate":
            payload = self._read_json()
            return self._send_json({"ok": True, "data": self.state.db.calculate_commissions(payload.get("filters", {}))})
        if path == "/api/finance/accounts":
            item = self.state.db.create_or_update_finance_account(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/finance/contracts":
            item = self.state.db.create_or_update_contract(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        if path == "/api/finance/transactions":
            item = self.state.db.create_finance_transaction(self._read_json())
            return self._send_json({"ok": True, "data": item}, 201)
        reverse_match = re_fullmatch(r"/api/finance/transactions/([^/]+)/reverse", path)
        if reverse_match:
            payload = self._read_json()
            item = self.state.db.reverse_finance_transaction(
                reverse_match.group(1), str(payload.get("reason", ""))
            )
            return self._send_json({"ok": True, "data": item})
        if path == "/api/backups":
            target = self.state.db.backup("manuel")
            return self._send_json({"ok": True, "data": {"name": target.name}})
        if path == "/api/backups/restore":
            payload = self._read_json()
            if payload.get("confirmation") != "GERİ YÜKLE":
                raise DatabaseError("Geri yükleme için onay metni eksik.")
            self.state.db.restore_backup(str(payload.get("name", "")))
            return self._send_json({"ok": True})
        if path == "/api/shutdown":
            self._send_json({"ok": True})
            if self.state.server:
                threading.Thread(target=self.state.server.shutdown, daemon=True).start()
            return
        raise FileNotFoundError("İşlem bulunamadı.")

    def _put(self) -> None:
        path, _query = self._route()
        platform_record_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records/([^/]+)", path)
        if platform_record_match:
            item = self.state.platform.save_record(platform_record_match.group(1), self._read_json(), platform_record_match.group(2))
            return self._send_json({"ok": True, "data": item})
        institution_match = re_fullmatch(r"/api/institutions/([^/]+)", path)
        if institution_match:
            item = self.state.db.update_institution(institution_match.group(1), self._read_json())
            return self._send_json({"ok": True, "data": item})
        panel_match = re_fullmatch(r"/api/panels/([^/]+)", path)
        if panel_match:
            payload = self._read_json()
            item = self.state.db.create_or_update_panel(payload, panel_match.group(1))
            return self._send_json({"ok": True, "data": item})
        if path in SETTING_ENDPOINTS:
            payload = self._read_json()
            key, default = SETTING_ENDPOINTS[path]
            property_name = {
                "table_columns": "columns", "finance_table_columns": "columns", "dynamic_filters": "filters",
                "finance_filters": "filters", "finance_saved_views": "views", "finance_summary_cards": "cards",
                "general_preferences": "preferences", "institution_form_fields": "fields",
                "navigation_preferences": "navigation", "theme_preferences": "theme",
            }[key]
            value = payload.get(property_name)
            if not isinstance(value, type(default)):
                raise DatabaseError("Ayar verisi beklenen biçimde değildir.")
            return self._send_json({"ok": True, "data": self.state.db.set_setting(key, value)})
        custom_field_match = re_fullmatch(r"/api/custom-fields/([^/]+)", path)
        if custom_field_match:
            item = self.state.db.update_custom_field(custom_field_match.group(1), self._read_json())
            return self._send_json({"ok": True, "data": item})
        lookup_item_match = re_fullmatch(r"/api/lookups/items/([^/]+)", path)
        if lookup_item_match:
            item = self.state.db.create_or_update_lookup_item(self._read_json(), lookup_item_match.group(1))
            return self._send_json({"ok": True, "data": item})
        lookup_category_match = re_fullmatch(r"/api/lookups/categories/([^/]+)", path)
        if lookup_category_match:
            item = self.state.db.update_lookup_category(lookup_category_match.group(1), self._read_json())
            return self._send_json({"ok": True, "data": item})
        group_match = re_fullmatch(r"/api/groups/([^/]+)", path)
        if group_match:
            item = self.state.db.create_or_update_group(self._read_json(), group_match.group(1))
            return self._send_json({"ok": True, "data": item})
        finance_field_match = re_fullmatch(r"/api/finance/fields/([^/]+)", path)
        if finance_field_match:
            item = self.state.db.create_or_update_finance_field(self._read_json(), finance_field_match.group(1))
            return self._send_json({"ok": True, "data": item})
        commission_match = re_fullmatch(r"/api/commissions/rules/([^/]+)", path)
        if commission_match:
            item = self.state.db.create_or_update_commission_rule(self._read_json(), commission_match.group(1))
            return self._send_json({"ok": True, "data": item})
        finance_match = re_fullmatch(r"/api/finance/accounts/([^/]+)", path)
        if finance_match:
            item = self.state.db.create_or_update_finance_account(
                self._read_json(), finance_match.group(1)
            )
            return self._send_json({"ok": True, "data": item})
        contract_match = re_fullmatch(r"/api/finance/contracts/([^/]+)", path)
        if contract_match:
            item = self.state.db.create_or_update_contract(
                self._read_json(), contract_match.group(1)
            )
            return self._send_json({"ok": True, "data": item})
        raise FileNotFoundError("İşlem bulunamadı.")

    def _delete(self) -> None:
        path, _query = self._route()
        institution_photo_match = re_fullmatch(r"/api/institutions/([^/]+)/photo", path)
        if institution_photo_match:
            result = self.state.db.delete_media_asset("institution", institution_photo_match.group(1), "primary")
            return self._send_json({"ok": True, "data": result})
        platform_record_match = re_fullmatch(r"/api/platform/modules/([^/]+)/records/([^/]+)", path)
        if platform_record_match:
            result = self.state.platform.archive_record(platform_record_match.group(1), platform_record_match.group(2))
            return self._send_json({"ok": True, "data": result})
        institution_match = re_fullmatch(r"/api/institutions/([^/]+)", path)
        if institution_match:
            result = self.state.db.archive_institutions([institution_match.group(1)])
            return self._send_json({"ok": True, "data": result})
        lookup_item_match = re_fullmatch(r"/api/lookups/items/([^/]+)", path)
        if lookup_item_match:
            result = self.state.db.archive_lookup_items([lookup_item_match.group(1)])
            return self._send_json({"ok": True, "data": result})
        group_match = re_fullmatch(r"/api/groups/([^/]+)", path)
        if group_match:
            result = self.state.db.archive_group(group_match.group(1))
            return self._send_json({"ok": True, "data": result})
        finance_field_match = re_fullmatch(r"/api/finance/fields/([^/]+)", path)
        if finance_field_match:
            result = self.state.db.archive_finance_fields([finance_field_match.group(1)])
            return self._send_json({"ok": True, "data": result})
        raise FileNotFoundError("Silme işlemi bulunamadı.")

    def _serve_resource(self, resource_name: str, content_type: str) -> None:
        root = resources.files("okul_guvenligi")
        target = root.joinpath(*resource_name.split("/"))
        self._send_bytes(target.read_bytes(), content_type)

    @staticmethod
    def _query_filters(query: dict[str, list[str]]) -> dict[str, Any]:
        return {key: values for key, values in query.items() if key not in {"columns", "fields"}}

    def _export_xlsx(self, query: dict[str, list[str]]) -> None:
        columns = query.get("columns", [""])[0].split(",") if query.get("columns") else None
        headers, rows = self.state.db.export_institution_rows(self._query_filters(query), columns)
        data = build_xlsx(headers, rows, "Kurum ve Paneller")
        self._send_bytes(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "LOGOS_TECH_filtrelenmis_kurumlar.xlsx")

    def _export_finance_xlsx(self, query: dict[str, list[str]]) -> None:
        columns = query.get("columns", [""])[0].split(",") if query.get("columns") else None
        if columns is None and query.get("fields"):
            columns = ["group_number", "sequence_number", "city", "district", "institution", "sales_person", *[
                f"finance:{field}" for field in query.get("fields", [""])[0].split(",") if field
            ]]
        headers, rows = self.state.db.export_dynamic_finance_rows(self._query_filters(query), columns)
        data = build_xlsx(headers, rows, "Finans Takibi")
        self._send_bytes(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "LOGOS_TECH_filtrelenmis_finans.xlsx")


def re_fullmatch(pattern: str, value: str):
    import re
    return re.fullmatch(pattern, value)


def find_port(preferred: int = DEFAULT_PORT) -> int:
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Uygulama için boş yerel bağlantı noktası bulunamadı.")


def run_server(data_dir: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    state = AppState(data_dir)
    chosen_port = find_port(port)
    handler = type("BoundAppHandler", (AppHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", chosen_port), handler)
    server.daemon_threads = True
    state.server = server
    (data_dir / "port.txt").write_text(str(chosen_port), encoding="utf-8")
    url = f"http://127.0.0.1:{chosen_port}/"
    state.logger.info("Uygulama başlatıldı: %s sürüm %s", url, APP_VERSION)
    print(f"LOGOS TECH Dinamik İşletme Platformu çalışıyor: {url}")
    print("Bu pencere açık kaldığı sürece program çalışır.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.recovery.mark_clean_shutdown()
        state.logger.info("Uygulama kapatıldı")


def open_existing_instance(data_dir: Path) -> bool:
    port_file = data_dir / "port.txt"
    if not port_file.exists():
        return False
    try:
        port = int(port_file.read_text(encoding="utf-8").strip())
        if not 1 <= port <= 65535:
            return False
        url = f"http://127.0.0.1:{port}/api/session"
        with urllib.request.urlopen(url, timeout=0.6) as response:
            if response.status != 200:
                return False
            server_header = response.headers.get("Server", "")
            session = json.loads(response.read().decode("utf-8"))
        if session.get("version") == APP_VERSION:
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return True
        if server_header.startswith(("OkulGuvenligi/", "LogosTechTakip/", "LogosTechPlatform/")) and session.get("token"):
            shutdown_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/shutdown",
                data=b"{}",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-App-Token": str(session["token"]),
                },
            )
            with urllib.request.urlopen(shutdown_request, timeout=1.5) as response:
                if response.status != 200:
                    raise RuntimeError("Eski Okul Güvenliği işlemi kapatılamadı.")
            for _attempt in range(20):
                try:
                    urllib.request.urlopen(url, timeout=0.15)
                except OSError:
                    break
                time.sleep(0.1)
        return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def import_file(data_dir: Path, file_path: Path) -> dict[str, Any]:
    data = file_path.read_bytes()
    parsed = parse_xlsx(data)
    db = Database(data_dir)
    result = db.import_records(parsed.records, file_path.name, parsed.sha256, actor="Komut Satırı İçe Aktarma")
    return {**result, "warnings": parsed.warnings}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LOGOS TECH çevrimdışı dinamik işletme platformu")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--import-xlsx", type=Path)
    args = parser.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.import_xlsx:
        result = import_file(args.data_dir, args.import_xlsx)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if open_existing_instance(args.data_dir):
        print("LOGOS TECH takip programı zaten çalışıyor. Mevcut pencere açıldı.")
        return
    run_server(args.data_dir, args.port, not args.no_browser)
