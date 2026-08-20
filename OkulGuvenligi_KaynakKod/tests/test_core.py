from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from okul_guvenligi.app import AppHandler, AppState, open_existing_instance
from okul_guvenligi.database import Database, DatabaseError, DEFAULT_INSTITUTION_FORM_FIELDS
from okul_guvenligi.xlsx_import import parse_xlsx


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get("OKUL_GUVENLIGI_TEST_XLSX")
        cls.xlsx_path = Path(source) if source else Path(__file__).parent / "fixtures" / "portal_export_anon.xlsx"
        if not cls.xlsx_path.exists():
            raise unittest.SkipTest("Anonim test Excel dosyası bulunamadı")
        cls.xlsx_bytes = cls.xlsx_path.read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_and_database_roundtrip(self):
        parsed = parse_xlsx(self.xlsx_bytes)
        self.assertEqual(276, len(parsed.records))
        self.assertGreater(sum(len(item["panels"]) for item in parsed.records), 250)
        db = Database(self.data_dir)
        result = db.import_records(parsed.records, self.xlsx_path.name, parsed.sha256)
        self.assertEqual(276, result["total"])
        self.assertEqual("ok", db.integrity_check())
        dashboard = db.dashboard()
        self.assertEqual(238, dashboard["institution_count"])
        self.assertEqual(278, dashboard["panel_count"])
        self.assertNotIn("physical_system_count", dashboard)
        self.assertTrue(db.list_backups())

    def test_custom_field_and_optimistic_update(self):
        db = Database(self.data_dir)
        field = db.add_custom_field({
            "entity_type": "institution", "label": "Kurulum Sözleşme Tarihi", "data_type": "date"
        })
        created = db.create_institution({
            "name": "Test Okulu", "portal_id": "TEST-1", "institution_code": "TEST-KOD",
            "custom_values": {field["id"]: "2026-08-14"},
        })
        updated = db.update_institution(created["id"], {
            "row_version": created["row_version"], "sales_person": "Test Kullanıcı"
        })
        self.assertEqual("Test Kullanıcı", updated["sales_person"])
        self.assertEqual("ok", db.integrity_check())

    def test_dynamic_search_group_numbers_columns_and_finance(self):
        parsed = parse_xlsx(self.xlsx_bytes)
        db = Database(self.data_dir)
        db.import_records(parsed.records, self.xlsx_path.name, parsed.sha256)

        all_items = db.list_institutions({})
        self.assertEqual(238, all_items["total"])
        self.assertEqual(276, all_items["record_total"])
        self.assertEqual(276, len(all_items["items"]))
        self.assertTrue(all_items["all_shown"])
        self.assertEqual(6, db.list_institutions({"query": "ibrahim"})["record_total"])
        self.assertEqual(6, db.list_institutions({"query": "İBRAHİM"})["record_total"])
        self.assertGreater(db.list_institutions({"query": "sehitkamil"})["total"], 0)
        self.assertGreater(db.list_institutions({"query": "192.168.1.64"})["total"], 0)
        self.assertEqual(163, db.dashboard({"status": "AKTİF"})["institution_count"])
        with db.connect() as conn:
            groups, max_sequence, max_group = conn.execute(
                "SELECT COUNT(DISTINCT group_number), MAX(sequence_number), MAX(group_number) FROM institutions"
            ).fetchone()
        self.assertEqual((238, 276, 238), (groups, max_sequence, max_group))
        self.assertEqual(238, next(item for item in all_items["items"] if item["sequence_number"] == 276)["group_number"])

        columns = [{"key": "institution", "visible": True, "width": 360}]
        self.assertEqual(columns, db.set_setting("table_columns", columns))
        self.assertEqual(columns, db.get_setting("table_columns"))

        institution_ids = [item["id"] for item in all_items["items"][:3]]
        account = db.create_or_update_finance_account({
            "name": "Test Kampüsü Cari", "institution_ids": institution_ids,
            "sales_person": "İbrahim Test",
        })
        self.assertEqual(3, len(account["institutions"]))
        self.assertEqual(1, db.list_finance_accounts({"query": "ibrahim"})["total"])
        contract = db.create_or_update_contract({
            "account_id": account["id"], "contract_no": "S-001", "billing_cycle": "AYLIK",
            "base_amount": "1000,00", "vat_rate": "20", "commission_rate": "2,5",
            "status": "AKTİF",
        })
        invoice = db.create_finance_transaction({
            "account_id": account["id"], "institution_id": institution_ids[0],
            "contract_id": contract["id"], "transaction_type": "FATURA", "amount": "1000,00",
            "transaction_date": date.today().isoformat(),
            "due_date": (date.today() - timedelta(days=10)).isoformat(),
        })
        payment = db.create_finance_transaction({
            "account_id": account["id"], "transaction_type": "ÖDEME", "amount": "400,00",
            "transaction_date": date.today().isoformat(),
        })
        listed = db.list_finance_accounts({})["items"][0]
        self.assertEqual(100000, listed["invoiced_minor"])
        self.assertEqual(40000, listed["paid_minor"])
        self.assertEqual(60000, listed["balance_minor"])
        self.assertEqual(60000, db.finance_dashboard()["overdue_minor"])
        db.reverse_finance_transaction(payment["id"], "Hatalı ödeme kaydı")
        self.assertEqual(100000, db.get_finance_account(account["id"])["balance_minor"])
        self.assertEqual("ok", db.integrity_check())

    def test_grouped_finance_formula_commission_and_xlsx(self):
        from okul_guvenligi.xlsx_export import build_xlsx
        import io
        import zipfile

        parsed = parse_xlsx(self.xlsx_bytes)
        db = Database(self.data_dir)
        db.import_records(parsed.records, self.xlsx_path.name, parsed.sha256)
        malatya = db.list_institutions({"city": ["MALATYA"]})
        self.assertEqual(1, malatya["total"])
        self.assertEqual(2, malatya["record_total"])
        group = malatya["groups"][0]
        self.assertEqual("DOĞA KOLEJİ MALATYA KAMPÜSÜ", group["name"])
        self.assertEqual(2, group["panel_count"])

        finance = db.list_finance_institutions({})
        self.assertEqual(238, finance["total"])
        self.assertEqual(276, finance["record_total"])
        field_ids = {field["field_key"]: field["id"] for field in finance["fields"]}
        institution = finance["items"][0]
        updated = db.set_finance_values(institution["id"], {
            field_ids["sozlesme_kart_sayisi"]: "100",
            field_ids["birinci_kart_fiyati"]: "12,50",
            field_ids["basilan_ikinci_kart_sayisi"]: "10",
            field_ids["ikinci_kart_fiyati"]: "5",
            field_ids["tahsilat"]: "1000",
        })
        self.assertEqual("1300.00", updated["finance_values"]["toplam_ciro"])
        self.assertEqual("300.00", updated["finance_values"]["bakiye"])
        db.create_or_update_commission_rule({
            "name": "Ciro primi", "base_field_key": "toplam_ciro",
            "calculation_type": "percent", "rate": "10",
            "conditions": [{"field": "toplam_ciro", "operator": "gte", "value": "100"}],
        })
        self.assertEqual(13000, db.calculate_commissions({})["total_minor"])
        headers, rows = db.export_dynamic_finance_rows({}, None)
        workbook = build_xlsx(headers, rows, "Finans")
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())

    def test_safe_group_merge_archive_restore_and_dynamic_exports(self):
        db = Database(self.data_dir)
        first = db.create_institution({"name": "Birinci Kampüs", "city": "GAZİANTEP"})
        second = db.create_institution({"name": "İkinci Okul", "city": "GAZİANTEP"})
        third = db.create_institution({"name": "Üçüncü Okul", "city": "KİLİS"})
        groups = db.list_groups()
        target = next(item for item in groups if item["group_number"] == first["group_number"])
        source = next(item for item in groups if item["group_number"] == second["group_number"])
        merged = db.create_or_update_group({
            "name": "Birinci Kampüs", "row_version": target["row_version"],
            "institution_ids": [first["id"], second["id"]],
        }, target["id"])
        self.assertEqual(2, merged["member_count"])
        self.assertNotIn(source["id"], {item["id"] for item in db.list_groups()})
        self.assertEqual(first["group_number"], db.get_institution(second["id"])["group_number"])

        dissolved = db.archive_group(target["id"])
        self.assertEqual(2, dissolved["member_count"])
        self.assertTrue(db.get_institution(first["id"])["active"])
        self.assertNotEqual(db.get_institution(first["id"])["group_number"], db.get_institution(second["id"])["group_number"])

        archived = db.archive_institutions([first["id"], third["id"]])
        self.assertEqual(2, archived["archived"])
        self.assertEqual({first["id"], third["id"]}, {item["id"] for item in db.list_archived_institutions()})
        restored = db.restore_institutions([first["id"], third["id"]])
        self.assertEqual(2, restored["restored"])

        custom_field = db.create_or_update_finance_field({
            "label": "Geçici Finans Alanı", "field_key": "gecici_finans", "data_type": "money"
        })
        self.assertEqual(1, db.archive_finance_fields([custom_field["id"]])["archived"])
        self.assertIn(custom_field["id"], {item["id"] for item in db.list_archived_finance_fields()})
        self.assertEqual(1, db.restore_finance_fields([custom_field["id"]])["restored"])

        headers, rows = db.export_institution_rows({}, ["list_number", "institution", "sequence_number"])
        self.assertEqual(["Liste No", "Kurum", "Kayıt Sıra"], headers)
        self.assertEqual(1, rows[0][0])
        self.assertEqual(db.list_institutions({})["record_total"], len(rows))
        self.assertTrue(all(row[0] not in {None, ""} for row in rows))
        finance_headers, finance_rows = db.export_dynamic_finance_rows({}, ["list_number", "institution", "finance:gecici_finans"])
        self.assertEqual(["Liste No", "Kurum", "Geçici Finans Alanı"], finance_headers)
        self.assertEqual("TOPLAM", finance_rows[-1][1])
        self.assertEqual("ok", db.integrity_check())

    def test_dynamic_settings_formula_versions_and_permanent_trash(self):
        db = Database(self.data_dir)
        created = db.create_institution({"name": "Kalıcı Silme Testi", "city": "GAZİANTEP"})
        db.create_or_update_panel({
            "institution_id": created["id"], "panel_key": "PURGE-PANEL", "name": "Test Paneli"
        })
        field = db.create_or_update_finance_field({
            "label": "Dinamik Tutar", "field_key": "dinamik_tutar", "data_type": "money",
            "aggregate_type": "sum", "decimal_places": 2,
        })
        formula = db.create_or_update_finance_field({
            "label": "Dinamik İki Kat", "field_key": "dinamik_iki_kat", "data_type": "formula",
            "formula": "dinamik_tutar * 2", "aggregate_type": "sum",
        })
        db.set_finance_values(created["id"], {field["id"]: "125.50"})
        finance = db.list_finance_institutions({})
        self.assertEqual("251.00", finance["aggregates"]["dinamik_iki_kat"]["value"])
        updated_formula = db.create_or_update_finance_field({
            **formula, "label": formula["label"], "data_type": "formula",
            "formula": "YUVARLA(dinamik_tutar * 3, 2)", "options": [],
        }, formula["id"])
        self.assertEqual(2, updated_formula["formula_version"])
        self.assertEqual(2, len(db.list_finance_formula_versions(formula["id"])))
        with self.assertRaises(Exception):
            db.create_or_update_finance_field({
                **updated_formula, "label": updated_formula["label"], "data_type": "formula",
                "formula": "dinamik_iki_kat + 1", "options": [],
            }, formula["id"])

        custom = db.add_custom_field({"entity_type": "institution", "label": "Eski Ad", "data_type": "text"})
        renamed = db.update_custom_field(custom["id"], {
            "label": "Yeni Ad", "data_type": "text", "options": [], "required": False,
        })
        self.assertEqual("Yeni Ad", renamed["label"])
        db.archive_custom_fields([custom["id"]])
        self.assertEqual(1, db.purge_custom_fields([custom["id"]])["purged"])

        settings = [{"key": "city", "label": "Şehirler", "visible": True, "width": 190}]
        db.set_setting("dynamic_filters", settings)
        db.set_setting("dynamic_filters", [{**settings[0], "label": "İller"}])
        self.assertEqual("Şehirler", db.list_setting_history("dynamic_filters")[0]["value"][0]["label"])

        db.archive_institutions([created["id"]])
        trash = db.list_archived_institutions()
        self.assertEqual(1, trash[0]["panel_count"])
        purged = db.purge_institutions([created["id"]])
        self.assertEqual(1, purged["purged"])
        self.assertIsNone(db.get_institution(created["id"]))
        self.assertEqual("ok", db.integrity_check())

    def test_single_source_lookups_dynamic_form_cards_theme_and_history(self):
        db = Database(self.data_dir)
        categories = db.list_lookup_categories()
        statuses = next(item for item in categories if item["category_key"] == "customer_status")
        self.assertEqual(7, len([item for item in statuses["items"] if item["active"]]))
        active = next(item for item in statuses["items"] if item["item_key"] == "AKTİF")
        renamed = db.create_or_update_lookup_item({
            **active, "category_key": "customer_status", "label": "AKTİF MÜŞTERİ", "active": True,
        }, active["id"])
        self.assertEqual("AKTİF MÜŞTERİ", renamed["label"])
        created = db.create_institution({"name": "Dinamik Kurum", "customer_status": "AKTİF"})
        lookup_filter = db.list_filters()["lookups"]["customer_status"]
        self.assertEqual("AKTİF MÜŞTERİ", next(item for item in lookup_filter if item["item_key"] == "AKTİF")["label"])
        self.assertGreaterEqual(db.list_institutions({"status": ["AKTİF"]})["record_total"], 1)

        new_status = db.create_or_update_lookup_item({
            "category_key": "customer_status", "label": "YENİ DURUM", "color": "#2469d8",
        })
        db.update_institution(created["id"], {
            "row_version": created["row_version"], "customer_status": new_status["item_key"],
        })
        result = db.merge_lookup_items(new_status["id"], active["id"])
        self.assertEqual(1, result["changed_records"])
        self.assertEqual("AKTİF", db.get_institution(created["id"])["customer_status"])

        form_fields = [{**item, "label": "Kurum / Müşteri" if item["key"] == "name" else item["label"]}
                       for item in DEFAULT_INSTITUTION_FORM_FIELDS]
        saved_form = db.set_setting("institution_form_fields", form_fields)
        self.assertEqual("Kurum / Müşteri", saved_form[0]["label"])
        cards = [{"id": "borc", "label": "Borç", "metric": "field:bakiye", "format": "money",
                  "subtitle": "Filtre sonucu", "color": "danger", "visible": True}]
        self.assertEqual(cards, db.set_setting("finance_summary_cards", cards))
        theme = db.set_setting("theme_preferences", {"primary": "#123456", "font_scale": 110})
        self.assertEqual("#123456", theme["primary"])
        self.assertEqual(110, theme["font_scale"])
        db.set_setting("theme_preferences", {"primary": "#654321"})
        history = db.list_setting_history("theme_preferences")
        restored = db.restore_setting_history(history[0]["id"])
        self.assertEqual("#123456", restored["primary"])
        self.assertEqual("ok", db.integrity_check())


    def test_lookup_filter_counts_match_unique_institution_kpi(self):
        parsed = parse_xlsx(self.xlsx_bytes)
        db = Database(self.data_dir)
        db.import_records(parsed.records, self.xlsx_path.name, parsed.sha256)
        statuses = next(item for item in db.list_lookup_categories() if item["category_key"] == "customer_status")
        active = next(item for item in statuses["items"] if item["item_key"] == "AKTİF")
        dashboard_count = db.dashboard({"status": ["AKTİF"]})["institution_count"]
        self.assertEqual(dashboard_count, active["filter_count"])
        self.assertGreaterEqual(active["usage_count"], active["filter_count"])

    def test_institution_photo_media_roundtrip_and_safe_removal(self):
        db = Database(self.data_dir)
        institution = db.create_institution({"name": "Fotoğraflı Kurum", "city": "GAZİANTEP"})
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        saved = db.save_media_asset("institution", institution["id"], "primary", "kurum.png", "image/png", png)
        self.assertEqual("image/png", saved["mime_type"])
        detail = db.get_institution(institution["id"])
        self.assertIsNotNone(detail["photo"])
        listed = db.list_institutions({})["items"]
        self.assertTrue(next(item for item in listed if item["id"] == institution["id"])["has_photo"])
        read, mime = db.read_media_asset("institution", institution["id"], "primary")
        self.assertEqual(png, read)
        self.assertEqual("image/png", mime)
        with self.assertRaises(DatabaseError):
            db.save_media_asset("institution", institution["id"], "primary", "sahte.png", "image/png", b"not-an-image")
        removed = db.delete_media_asset("institution", institution["id"], "primary")
        self.assertTrue(removed["removed"])
        self.assertIsNone(db.get_institution(institution["id"])["photo"])
        self.assertIsNone(db.read_media_asset("institution", institution["id"], "primary"))


class HttpTests(CoreTests):
    def setUp(self):
        super().setUp()
        self.state = AppState(self.data_dir)
        handler = type("TestHandler", (AppHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.state.server = self.server
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path: str, method: str = "GET", body=None, headers=None):
        request_headers = dict(headers or {})
        data = body
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=request_headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8")) if "json" in response.headers.get_content_type() else response.read()

    def test_import_preview_commit_and_api(self):
        session = self.request("/api/session")
        token = session["token"]
        headers = {"X-App-Token": token, "Content-Type": "application/octet-stream", "X-File-Name": urllib.parse.quote(self.xlsx_path.name)}
        preview = self.request("/api/import/preview", "POST", self.xlsx_bytes, headers)
        self.assertEqual(276, preview["data"]["total"])
        result = self.request("/api/import/commit", "POST", {"token": preview["data"]["token"]}, {"X-App-Token": token})
        self.assertEqual(276, result["data"]["total"])
        dashboard = self.request("/api/dashboard")["data"]
        self.assertEqual(238, dashboard["institution_count"])
        institutions = self.request("/api/institutions?page=1&page_size=25")["data"]
        self.assertEqual(238, institutions["total"])
        self.assertEqual(276, institutions["record_total"])
        first = self.request(f"/api/institutions/{institutions['items'][0]['id']}")["data"]
        self.assertIn("panels", first)
        self.assertEqual("ok", self.state.db.integrity_check())

    def test_mutation_requires_session_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/backups", "POST", {})
        self.assertEqual(403, ctx.exception.code)

    def test_dynamic_and_finance_http_endpoints(self):
        token = self.request("/api/session")["token"]
        headers = {"X-App-Token": token, "Content-Type": "application/octet-stream", "X-File-Name": urllib.parse.quote(self.xlsx_path.name)}
        preview = self.request("/api/import/preview", "POST", self.xlsx_bytes, headers)
        self.request("/api/import/commit", "POST", {"token": preview["data"]["token"]}, {"X-App-Token": token})

        search = self.request("/api/institutions?query=ibrahim")["data"]
        self.assertEqual(6, search["record_total"])
        filtered = self.request("/api/dashboard?status=" + urllib.parse.quote("AKTİF"))["data"]
        self.assertEqual(163, filtered["institution_count"])

        columns = [{"key": "group_number", "visible": True, "width": 80}]
        saved = self.request("/api/settings/table-columns", "PUT", {"columns": columns}, {"X-App-Token": token})
        self.assertEqual(columns, saved["data"])
        self.assertEqual(columns, self.request("/api/settings/table-columns")["data"])

        institutions = self.request("/api/institutions")["data"]["items"]
        account = self.request("/api/finance/accounts", "POST", {
            "name": "HTTP Test Cari", "institution_ids": [institutions[0]["id"], institutions[1]["id"]]
        }, {"X-App-Token": token})["data"]
        self.request("/api/finance/transactions", "POST", {
            "account_id": account["id"], "transaction_type": "FATURA", "amount": "250.50",
            "transaction_date": date.today().isoformat(),
        }, {"X-App-Token": token})
        detail = self.request(f"/api/finance/accounts/{account['id']}")["data"]
        self.assertEqual(25050, detail["balance_minor"])
        self.assertEqual(2, len(detail["institutions"]))

        finance = self.request("/api/finance/institutions")["data"]
        self.assertEqual(238, finance["total"])
        field_ids = {field["field_key"]: field["id"] for field in finance["fields"]}
        first_id = finance["items"][0]["id"]
        values = self.request(f"/api/finance/institutions/{first_id}/values", "POST", {
            "values": {
                field_ids["sozlesme_kart_sayisi"]: "25",
                field_ids["birinci_kart_fiyati"]: "20",
                field_ids["tahsilat"]: "100",
            }
        }, {"X-App-Token": token})["data"]
        self.assertEqual("500.00", values["finance_values"]["toplam_ciro"])
        workbook = self.request("/api/finance/export/xlsx?city=" + urllib.parse.quote(finance["items"][0]["city"]))
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            self.assertIsNone(archive.testzip())

    def test_safe_delete_restore_and_finance_column_settings_http(self):
        token = self.request("/api/session")["token"]
        auth = {"X-App-Token": token}
        created = self.request("/api/institutions", "POST", {
            "name": "Silme HTTP Test Kurumu", "city": "GAZİANTEP"
        }, auth)["data"]
        deleted = self.request(f"/api/institutions/{created['id']}", "DELETE", None, auth)["data"]
        self.assertEqual(1, deleted["archived"])
        trash = self.request("/api/institutions/trash")["data"]
        self.assertIn(created["id"], {item["id"] for item in trash})
        restored = self.request("/api/institutions/restore", "POST", {
            "institution_ids": [created["id"]]
        }, auth)["data"]
        self.assertEqual(1, restored["restored"])

        columns = [{"key": "institution", "visible": True, "width": 330, "pinned": True}]
        saved = self.request("/api/settings/finance-table-columns", "PUT", {"columns": columns}, auth)
        self.assertEqual(columns, saved["data"])
        self.assertEqual(columns, self.request("/api/settings/finance-table-columns")["data"])

        field = self.request("/api/finance/fields", "POST", {
            "label": "HTTP Silinebilir Alan", "field_key": "http_silinebilir", "data_type": "text"
        }, auth)["data"]
        archived = self.request(f"/api/finance/fields/{field['id']}", "DELETE", None, auth)["data"]
        self.assertEqual(1, archived["archived"])
        self.assertIn(field["id"], {item["id"] for item in self.request("/api/finance/fields/trash")["data"]})

    def test_settings_center_and_permanent_delete_http(self):
        token = self.request("/api/session")["token"]
        auth = {"X-App-Token": token}
        created = self.request("/api/institutions", "POST", {"name": "HTTP Kalıcı Silme"}, auth)["data"]
        self.request(f"/api/institutions/{created['id']}", "DELETE", None, auth)
        result = self.request("/api/institutions/purge", "POST", {
            "institution_ids": [created["id"]], "confirmation": "KALICI SİL"
        }, auth)["data"]
        self.assertEqual(1, result["purged"])

        filters = [{"key": "city", "label": "İller", "visible": True, "width": 200, "pinned": True}]
        saved = self.request("/api/settings/finance-filters", "PUT", {"filters": filters}, auth)["data"]
        self.assertEqual(filters, saved)
        self.assertEqual(filters, self.request("/api/settings/finance-filters")["data"])

        custom = self.request("/api/custom-fields", "POST", {
            "entity_type": "institution", "label": "HTTP Alan", "data_type": "text", "options": []
        }, auth)["data"]
        renamed = self.request(f"/api/custom-fields/{custom['id']}", "PUT", {
            "label": "HTTP Yeni Alan", "data_type": "text", "options": [], "required": False
        }, auth)["data"]
        self.assertEqual("HTTP Yeni Alan", renamed["label"])

    def test_dynamic_management_http_endpoints(self):
        token = self.request("/api/session")["token"]
        auth = {"X-App-Token": token}
        lookups = self.request("/api/lookups")["data"]
        statuses = next(item for item in lookups if item["category_key"] == "customer_status")
        self.assertEqual(7, len([item for item in statuses["items"] if item["active"]]))
        added = self.request("/api/lookups/items", "POST", {
            "category_key": "customer_status", "label": "HTTP DURUM", "color": "#2469d8",
        }, auth)["data"]
        renamed = self.request(f"/api/lookups/items/{added['id']}", "PUT", {
            "category_key": "customer_status", "label": "HTTP YENİ DURUM", "color": "#15865d", "active": True,
        }, auth)["data"]
        self.assertEqual("HTTP YENİ DURUM", renamed["label"])
        archived = self.request(f"/api/lookups/items/{added['id']}", "DELETE", None, auth)["data"]
        self.assertEqual(1, archived["archived"])
        restored = self.request("/api/lookups/items/restore", "POST", {"item_ids": [added["id"]]}, auth)["data"]
        self.assertEqual(1, restored["restored"])

        fields = self.request("/api/settings/institution-form")["data"]
        fields[0]["label"] = "HTTP Kurum Adı"
        saved_fields = self.request("/api/settings/institution-form", "PUT", {"fields": fields}, auth)["data"]
        self.assertEqual("HTTP Kurum Adı", saved_fields[0]["label"])
        cards = [{"id": "http_card", "label": "HTTP Bakiye", "metric": "field:bakiye", "format": "money",
                  "subtitle": "Test", "color": "danger", "visible": True}]
        self.assertEqual(cards, self.request("/api/settings/finance-summary-cards", "PUT", {"cards": cards}, auth)["data"])
        theme = self.request("/api/settings/theme", "PUT", {"theme": {"primary": "#112233", "font_scale": 105}}, auth)["data"]
        self.assertEqual("#112233", theme["primary"])
        navigation = self.request("/api/settings/navigation")["data"]
        navigation[0]["label"] = "Müşteriler"
        saved_navigation = self.request("/api/settings/navigation", "PUT", {"navigation": navigation}, auth)["data"]
        self.assertEqual("Müşteriler", saved_navigation[0]["label"])

    def test_launcher_stops_an_old_running_version(self):
        token = "eski-surum-token"

        class OldVersionHandler(BaseHTTPRequestHandler):
            server_version = "OkulGuvenligi/1.0"

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                payload = json.dumps({"ok": True, "token": token, "version": "1.0.0"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                if self.path != "/api/shutdown" or self.headers.get("X-App-Token") != token:
                    self.send_error(403)
                    return
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        old_server = ThreadingHTTPServer(("127.0.0.1", 0), OldVersionHandler)
        old_server.daemon_threads = True
        old_thread = threading.Thread(target=old_server.serve_forever, daemon=True)
        old_thread.start()
        (self.data_dir / "port.txt").write_text(str(old_server.server_port), encoding="utf-8")
        self.assertFalse(open_existing_instance(self.data_dir))
        old_thread.join(timeout=3)
        old_server.server_close()
        self.assertFalse(old_thread.is_alive())


    def test_institution_photo_http_upload_read_and_remove(self):
        token = self.request("/api/session")["token"]
        auth = {"X-App-Token": token}
        created = self.request("/api/institutions", "POST", {"name": "HTTP Fotoğraf Kurumu"}, auth)["data"]
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        uploaded = self.request(f"/api/institutions/{created['id']}/photo", "POST", {
            "data_url": data_url, "name": "kurum.png"
        }, auth)["data"]
        self.assertEqual("image/png", uploaded["mime_type"])
        self.assertEqual(png, self.request(f"/api/institutions/{created['id']}/photo"))
        detail = self.request(f"/api/institutions/{created['id']}")["data"]
        self.assertIsNotNone(detail["photo"])
        removed = self.request(f"/api/institutions/{created['id']}/photo", "DELETE", None, auth)["data"]
        self.assertTrue(removed["removed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
