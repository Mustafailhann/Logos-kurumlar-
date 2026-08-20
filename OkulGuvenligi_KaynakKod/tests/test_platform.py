from __future__ import annotations

import tempfile
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from okul_guvenligi.app import AppHandler, AppState
from okul_guvenligi.database import Database, DatabaseError
from okul_guvenligi.platform_engine import PLATFORM_SCHEMA_VERSION, PlatformEngine


class PlatformEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name))
        self.engine = PlatformEngine(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def module(self, key):
        return next(item for item in self.engine.list_modules() if item["module_key"] == key)

    def test_platform_schema_and_default_modules(self):
        modules = self.engine.list_modules()
        self.assertEqual(["servis", "sozlesmeler", "stok"], [item["module_key"] for item in modules])
        self.assertGreaterEqual(len(self.module("stok")["fields"]), 12)
        with self.db.connect() as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertEqual(PLATFORM_SCHEMA_VERSION, version)
        self.assertEqual("ok", self.db.integrity_check())

    def test_formulas_apply_to_every_existing_and_new_record(self):
        stock = self.module("stok")
        first = self.engine.save_record(stock["id"], {"values": {
            "urun_kodu": "T-1", "urun_adi": "Turnike", "giris": "10", "cikis": "3",
            "kritik_seviye": "5", "alis_fiyati": "1250",
        }})
        self.assertEqual("7", first["values"]["mevcut_stok"])
        self.assertEqual("8750.00", first["values"]["stok_degeri"])

        preview = self.engine.preview_change("field.save", {
            "module_id": stock["id"], "label": "KDV Dahil Değer", "data_type": "formula",
            "formula": "stok_degeri * 1.20", "aggregate_type": "sum",
        })
        field = self.engine.apply_change(preview)
        listed = self.engine.list_records(stock["id"])
        self.assertEqual("10500.00", listed["items"][0]["values"][field["field_key"]])
        self.assertEqual(0, self.engine.list_records(stock["id"], {"min.mevcut_stok": ["8"]})["total"])
        self.assertEqual(1, self.engine.list_records(stock["id"], {"max.mevcut_stok": ["7"]})["total"])
        label, headers, rows = self.engine.export_rows(stock["id"], {}, ["urun_adi", "mevcut_stok"])
        self.assertEqual("Stok", label)
        self.assertEqual(["Ürün Adı", "Mevcut Stok"], headers)
        self.assertGreaterEqual(len(rows), 2)  # kayıt ve genel toplam

    def test_formula_cycle_and_unknown_field_are_blocked(self):
        stock = self.module("stok")
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("field.save", {
                "module_id": stock["id"], "label": "Hatalı", "data_type": "formula",
                "formula": "olmayan_alan + 1",
            })
        first = self.engine.apply_change(self.engine.preview_change("field.save", {
            "module_id": stock["id"], "label": "Birinci", "field_key": "birinci",
            "data_type": "formula", "formula": "mevcut_stok + 1",
        }))
        second = self.engine.apply_change(self.engine.preview_change("field.save", {
            "module_id": stock["id"], "label": "İkinci", "field_key": "ikinci",
            "data_type": "formula", "formula": "birinci + 1",
        }))
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("field.save", {
                **first, "formula": f"{second['field_key']} + 1",
            })

    def test_business_rule_runs_on_save(self):
        service = self.module("servis")
        institution = self.db.create_institution({"name": "Test Kurumu"})
        self.engine.apply_change(self.engine.preview_change("rule.save", {
            "module_id": service["id"], "name": "Acil kaydı işleme al",
            "conditions": [{"field": "oncelik", "operator": "eq", "value": "ACİL"}],
            "actions": [{"type": "set", "field": "durum", "value": "İŞLEMDE"}],
        }))
        record = self.engine.save_record(service["id"], {"values": {
            "kayit_no": "SRV-1", "kurum": institution["id"], "acilis_tarihi": "2026-08-16",
            "sorun": "Test", "oncelik": "ACİL",
        }})
        self.assertEqual("İŞLEMDE", record["values"]["durum"])
        self.assertEqual(["Acil kaydı işleme al"], record["applied_rules"])

    def test_visible_name_changes_but_technical_key_is_protected(self):
        stock = self.module("stok")
        field = next(item for item in stock["fields"] if item["field_key"] == "urun_adi")
        updated = self.engine.apply_change(self.engine.preview_change("field.save", {
            **field, "label": "Malzeme Adı", "field_key": "bozulacak_anahtar",
        }))
        self.assertEqual("Malzeme Adı", updated["label"])
        self.assertEqual("urun_adi", updated["field_key"])
        history = next(item for item in self.engine.list_history() if item["entity_id"] == field["id"] and item["action"] == "update")
        self.engine.restore_history(history["id"])
        restored = next(item for item in self.engine.get_module(stock["id"])["fields"] if item["id"] == field["id"])
        self.assertEqual("Ürün Adı", restored["label"])

    def test_field_and_module_archive_restore_preserve_content(self):
        stock = self.module("stok")
        field = next(item for item in stock["fields"] if item["field_key"] == "depo")
        self.engine.apply_change(self.engine.preview_change("field.archive", {"id": field["id"]}))
        archived = self.engine.get_module(stock["id"])
        self.assertIn(field["id"], [item["id"] for item in archived["archived_fields"]])
        self.engine.apply_change(self.engine.preview_change("field.restore", {"id": field["id"]}))
        self.assertIn(field["id"], [item["id"] for item in self.engine.get_module(stock["id"])["fields"]])

        self.engine.apply_change(self.engine.preview_change("module.archive", {"id": stock["id"]}))
        self.assertNotIn(stock["id"], [item["id"] for item in self.engine.list_modules()])
        self.engine.apply_change(self.engine.preview_change("module.restore", {"id": stock["id"]}))
        self.assertIn(stock["id"], [item["id"] for item in self.engine.list_modules()])


    def test_default_actions_exist_and_can_move_safely(self):
        stock = self.module("stok")
        action_types = {item["action_type"] for item in stock["actions"]}
        self.assertTrue({"new_record", "edit_record", "save_record", "archive_record", "duplicate_record", "export_xlsx", "cancel_form"}.issubset(action_types))
        delete_action = next(item for item in stock["actions"] if item["action_type"] == "archive_record")
        moved = self.engine.apply_change(self.engine.preview_change("action.save", {**delete_action, "module_id": stock["id"], "placement": "page_top"}))
        self.assertEqual("page_top", moved["placement"])
        save_action = next(item for item in self.engine.get_module(stock["id"])["actions"] if item["action_type"] == "save_record")
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("action.save", {**save_action, "module_id": stock["id"], "placement": "row"})

    def test_action_archive_restore_and_history(self):
        stock = self.module("stok")
        action = next(item for item in stock["actions"] if item["action_type"] == "duplicate_record")
        self.engine.apply_change(self.engine.preview_change("action.archive", {"id": action["id"]}))
        module = self.engine.get_module(stock["id"])
        self.assertIn(action["id"], [item["id"] for item in module["archived_actions"]])
        self.engine.apply_change(self.engine.preview_change("action.restore", {"id": action["id"]}))
        self.assertIn(action["id"], [item["id"] for item in self.engine.get_module(stock["id"])["actions"]])

    def test_invalid_numeric_input_is_rejected_before_save(self):
        stock = self.module("stok")
        with self.assertRaises(DatabaseError) as caught:
            self.engine.save_record(stock["id"], {"values": {
                "urun_kodu": "X", "urun_adi": "Hatalı", "giris": "abc", "cikis": "1"
            }})
        self.assertIn("geçerli bir sayı", str(caught.exception))

    def test_rule_cannot_bypass_numeric_validation(self):
        stock = self.module("stok")
        self.engine.apply_change(self.engine.preview_change("rule.save", {
            "module_id": stock["id"], "name": "Yanlış sayı üret",
            "conditions": [{"field": "urun_kodu", "operator": "eq", "value": "BAD"}],
            "actions": [{"type": "set", "field": "giris", "value": "ABC"}],
        }))
        with self.assertRaises(DatabaseError):
            self.engine.save_record(stock["id"], {"values": {
                "urun_kodu": "BAD", "urun_adi": "Test", "giris": "5", "cikis": "1"
            }})

    def test_assistant_turns_turkish_commands_into_safe_previews(self):
        preview = self.engine.preview_command("Stok bölümünde Sil butonunu sayfa üstünde göster")
        self.assertEqual("action.save", preview["operation"])
        self.assertEqual("page_top", preview["payload"]["placement"])
        field_preview = self.engine.preview_command("Stok bölümüne Raf No metin alanı ekle")
        self.assertEqual("field.save", field_preview["operation"])
        self.assertEqual("Raf No", field_preview["payload"]["label"])


    def test_archived_module_can_be_permanently_deleted_only_with_safe_confirmation(self):
        created = self.engine.apply_change(self.engine.preview_change("module.save", {
            "label": "Geçici Test Bölümü", "singular_label": "Test Kaydı", "icon": "T"
        }))
        self.engine.apply_change(self.engine.preview_change("field.save", {
            "module_id": created["id"], "label": "Ad", "data_type": "text", "required": True, "is_title": True
        }))
        self.engine.save_record(created["id"], {"values": {"ad": "Silinecek"}})
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("module.delete_permanent", {"id": created["id"], "confirmation": "SIL"})
        self.engine.apply_change(self.engine.preview_change("module.archive", {"id": created["id"]}))
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("module.delete_permanent", {"id": created["id"], "confirmation": "EVET"})
        preview = self.engine.preview_change("module.delete_permanent", {"id": created["id"], "confirmation": "SIL"})
        self.assertGreater(preview["impact"], 1)
        result = self.engine.apply_change(preview)
        self.assertTrue(result["purged"])
        self.assertNotIn(created["id"], [item["id"] for item in self.engine.list_modules(True)])
        purge_history = next(item for item in self.engine.list_history() if item["entity_id"] == created["id"] and item["action"] == "purge")
        with self.assertRaises(DatabaseError):
            self.engine.preview_change("history.restore", {"id": purge_history["id"]})
        self.assertEqual("ok", self.db.integrity_check())

    def test_assistant_is_tolerant_and_always_returns_a_safe_answer(self):
        variants = [
            "STOK BÖLÜMÜNDE SİL BUTONUNU SAYFA ÜSTÜNDE GÖSTER",
            "stokta sili yukarı taşı",
            "Stok bölümünde sil butonunu sayfa üstüne al.",
            "stok sil üstte",
        ]
        for command in variants:
            preview = self.engine.preview_command(command)
            self.assertEqual("action.save", preview["operation"], command)
            self.assertEqual("page_top", preview["payload"]["placement"], command)
        help_reply = self.engine.preview_command("yardım")
        self.assertTrue(help_reply["assistant_only"])
        self.assertTrue(help_reply["suggestions"])
        count_reply = self.engine.preview_command("Kaç kurum var?")
        self.assertTrue(count_reply["assistant_only"])
        self.assertIn("kurum", count_reply["summary"].lower())
        module_reply = self.engine.preview_command("Hangi bölümler var?")
        self.assertIn("Stok", module_reply["summary"])
        unknown = self.engine.preview_command("Bugün bana güzel bir şey söyle")
        self.assertTrue(unknown["assistant_only"])
        self.assertFalse(unknown["executable"])
        self.assertTrue(unknown["summary"])
        self.assertTrue(unknown["suggestions"])
        protected = self.engine.preview_command("Kurumlar bölümünü kalıcı sil")
        self.assertTrue(protected["assistant_only"])
        self.assertIn("korunan", protected["summary"].lower())

    def test_database_context_closes_connection(self):
        with self.db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        with self.assertRaises(Exception):
            conn.execute("SELECT 1")

    def test_saved_view_and_history(self):
        stock = self.module("stok")
        view = self.engine.save_view(stock["id"], {
            "name": "Kritik stoklar", "filters": {"selections": {"kritik": "1"}},
            "columns": ["urun_kodu", "urun_adi", "mevcut_stok"], "is_default": True,
        })
        self.assertTrue(view["is_default"])
        self.assertGreater(len(self.engine.list_history()), 0)


class FrontendContractTests(unittest.TestCase):
    def test_bulk_fix_ui_controls_are_present_in_packaged_resources(self):
        package = resources.files("okul_guvenligi")
        html = package.joinpath("web/index.html").read_text(encoding="utf-8")
        js = package.joinpath("web/app.js").read_text(encoding="utf-8")
        self.assertIn('data-scroll-sync="#institutionsScroll"', html)
        self.assertIn('data-scroll-sync="#financeScroll"', html)
        self.assertIn('data-scroll-sync="#dynamicModuleScroll"', html)
        self.assertIn('id="institutionPhotoInput"', html)
        self.assertIn('id="logosAssistantReply"', html)
        self.assertIn('data-purge-studio-module', js)
        self.assertIn('function refreshTopScrollbars()', js)
        self.assertIn('filter_count??item.usage_count', js)


class PlatformHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = AppState(Path(self.temp.name))
        handler = type("PlatformTestHandler", (AppHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.state.server = self.server
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["X-App-Token"] = self.state.token
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_platform_preview_commit_and_record_endpoints(self):
        bootstrap = self.request("/api/platform/bootstrap")["data"]
        self.assertEqual(3, len(bootstrap["modules"]))

        preview = self.request("/api/platform/change/preview", "POST", {
            "operation": "module.save", "payload": {
                "label": "Teklifler", "singular_label": "Teklif", "icon": "₺", "color": "#2469d8",
            },
        })["data"]
        created = self.request("/api/platform/change/commit", "POST", {"token": preview["token"]})["data"]
        field_preview = self.request("/api/platform/change/preview", "POST", {
            "operation": "field.save", "payload": {
                "module_id": created["id"], "label": "Teklif No", "data_type": "text",
                "required": True, "is_title": True,
            },
        })["data"]
        self.request("/api/platform/change/commit", "POST", {"token": field_preview["token"]})
        record = self.request(f"/api/platform/modules/{created['id']}/records", "POST", {
            "values": {"teklif_no": "TKL-001"},
        })["data"]
        self.assertEqual("TKL-001", record["title"])
        listing = self.request(f"/api/platform/modules/{created['id']}/records")["data"]
        self.assertEqual(1, listing["total"])

    def test_assistant_preview_endpoint_and_bulk_archive(self):
        bootstrap = self.request("/api/platform/bootstrap")["data"]
        stock = next(item for item in bootstrap["modules"] if item["module_key"] == "stok")
        assistant = self.request("/api/platform/assistant/preview", "POST", {
            "command": "Stok bölümünde Sil butonunu sayfa üstünde göster"
        })["data"]
        self.assertTrue(assistant["assistant"])
        self.assertEqual("action.save", assistant["operation"])
        self.request("/api/platform/change/commit", "POST", {"token": assistant["token"]})
        one = self.request(f"/api/platform/modules/{stock['id']}/records", "POST", {
            "values": {"urun_kodu": "A", "urun_adi": "A", "giris": "2", "cikis": "0"}
        })["data"]
        two = self.request(f"/api/platform/modules/{stock['id']}/records", "POST", {
            "values": {"urun_kodu": "B", "urun_adi": "B", "giris": "3", "cikis": "0"}
        })["data"]
        result = self.request(f"/api/platform/modules/{stock['id']}/records/bulk-archive", "POST", {"ids": [one["id"], two["id"]]})["data"]
        self.assertEqual(2, result["count"])



    def test_assistant_unknown_command_returns_help_without_change_token(self):
        result = self.request("/api/platform/assistant/preview", "POST", {
            "command": "Bunu daha güzel bir ekrana çevir"
        })["data"]
        self.assertTrue(result["assistant"])
        self.assertTrue(result["assistant_only"])
        self.assertEqual("", result["token"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["suggestions"])

    def test_permanent_module_delete_http_requires_archive_and_confirmation(self):
        created_preview = self.request("/api/platform/change/preview", "POST", {
            "operation": "module.save", "payload": {"label": "HTTP Geçici", "singular_label": "Kayıt"}
        })["data"]
        created = self.request("/api/platform/change/commit", "POST", {"token": created_preview["token"]})["data"]
        archived_preview = self.request("/api/platform/change/preview", "POST", {
            "operation": "module.archive", "payload": {"id": created["id"]}
        })["data"]
        self.request("/api/platform/change/commit", "POST", {"token": archived_preview["token"]})
        purge_preview = self.request("/api/platform/change/preview", "POST", {
            "operation": "module.delete_permanent", "payload": {"id": created["id"], "confirmation": "SIL"}
        })["data"]
        purged = self.request("/api/platform/change/commit", "POST", {"token": purge_preview["token"]})["data"]
        self.assertTrue(purged["purged"])
        bootstrap = self.request("/api/platform/bootstrap")["data"]
        self.assertNotIn(created["id"], {item["id"] for item in bootstrap["modules"] + bootstrap["archived_modules"]})


if __name__ == "__main__":
    unittest.main()
