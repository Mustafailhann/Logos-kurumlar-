from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from okul_guvenligi.app import AppHandler, AppState
from okul_guvenligi.assistant.config import AssistantConfigStore
from okul_guvenligi.assistant.openai_vision import OpenAIVisionPlanner
from okul_guvenligi.core.health import HealthMonitor
from okul_guvenligi.core.recovery import RecoveryManager
from okul_guvenligi.core.models import ChangePlan, PlanOperation
from okul_guvenligi.core.safety import SafetyEngine, SafetyError
from okul_guvenligi.database import Database, DatabaseError
from okul_guvenligi.platform_engine import PlatformEngine


ONE_PIXEL_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="


class VisionPlannerContractTests(unittest.TestCase):
    def test_openai_payload_is_strict_and_carries_screen_image(self):
        planner = OpenAIVisionPlanner(api_key="test-key", model="gpt-5.6")
        payload = planner._payload(
            "Buraya Sil butonu ekle",
            ONE_PIXEL_PNG,
            {"current_view": "dynamicModule", "current_module": "Stok", "module_actions": ["Sil · row"]},
        )
        self.assertEqual("gpt-5.6", payload["model"])
        self.assertEqual({"type": "function", "name": "propose_logos_plan"}, payload["tool_choice"])
        self.assertFalse(payload["parallel_tool_calls"])
        tool = payload["tools"][0]
        self.assertTrue(tool["strict"])
        self.assertFalse(tool["parameters"]["additionalProperties"])
        content = payload["input"][0]["content"]
        self.assertEqual("input_text", content[0]["type"])
        self.assertEqual("input_image", content[1]["type"])
        self.assertEqual(ONE_PIXEL_PNG, content[1]["image_url"])

    def test_openai_function_call_becomes_semantic_plan(self):
        response = {
            "output": [{
                "type": "function_call",
                "name": "propose_logos_plan",
                "arguments": json.dumps({
                    "message": "Sil butonu sayfa üstüne taşınacak.",
                    "assistant_text": "Görselde üst araç alanı uygun.",
                    "operations": [{
                        "kind": "action_add_or_move", "module": "Stok", "target": "Sil", "label": "",
                        "data_type": "", "placement": "page_top", "direction": "", "value": None,
                        "reason": "Kullanıcı görselde üst alanı işaretledi.",
                    }],
                }, ensure_ascii=False),
            }]
        }
        plan = OpenAIVisionPlanner._parse_plan(response, vision_used=True)
        self.assertTrue(plan.vision_used)
        self.assertEqual("openai", plan.source)
        self.assertEqual("action_add_or_move", plan.operations[0].kind)
        self.assertEqual("Stok", plan.operations[0].module)


class SafetyEngine5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name))
        self.platform = PlatformEngine(self.db)
        self.health = HealthMonitor(self.db)
        self.safety = SafetyEngine(self.db, self.platform, self.health)

    def tearDown(self):
        self.temp.cleanup()

    def test_dependent_multi_step_plan_is_tested_in_shadow_db_then_committed(self):
        plan = ChangePlan(
            message="Teklifler bölümü ve temel alanı hazırlanacak.", source="openai", vision_used=True,
            operations=[
                PlanOperation(kind="module_create", label="Teklifler", reason="Yeni iş alanı"),
                PlanOperation(kind="field_add", module="Teklifler", label="Teklif No", data_type="text", reason="Temel kimlik alanı"),
                PlanOperation(kind="action_add_or_move", module="Teklifler", target="Sil", placement="page_top", reason="Toplu işlem için"),
            ],
        )
        preview = self.safety.preview_plan(plan)
        self.assertTrue(preview["executable"])
        self.assertEqual(3, len(preview["operations"]))
        # Ön izleme gerçek DB'yi değiştirmemelidir.
        self.assertNotIn("Teklifler", [m["label"] for m in self.platform.list_modules(True)])
        result = self.safety.commit_plan(preview)
        self.assertEqual("ok", result["integrity"])
        module = next(m for m in self.platform.list_modules() if m["label"] == "Teklifler")
        self.assertIn("Teklif No", [f["label"] for f in module["fields"]])
        delete_action = next(a for a in module["actions"] if a["action_type"] == "archive_record")
        self.assertEqual("page_top", delete_action["placement"])
        # Yeni bölüm kod yazmadan temel eylemlerle doğar.
        self.assertGreaterEqual(len(module["actions"]), 7)

    def test_ai_cannot_modify_protected_core_or_request_permanent_delete(self):
        with self.assertRaises(SafetyError):
            self.safety.preview_plan(ChangePlan(
                message="Kurumlara alan ekle",
                operations=[PlanOperation(kind="field_add", module="Kurumlar", label="Riskli", data_type="text")],
            ))
        with self.assertRaises(SafetyError):
            self.safety.preview_plan(ChangePlan(
                message="Kalıcı sil",
                operations=[PlanOperation(kind="module_delete_permanent", module="Stok")],
            ))

    def test_failed_commit_restores_preplan_backup(self):
        plan = ChangePlan(
            message="İki alan eklenecek",
            operations=[
                PlanOperation(kind="field_add", module="Stok", label="Rollback A", data_type="text"),
                PlanOperation(kind="field_add", module="Stok", label="Rollback B", data_type="text"),
            ],
        )
        preview = self.safety.preview_plan(plan)
        original_apply = self.platform.apply_change
        counter = {"n": 0}

        def fail_second(item):
            counter["n"] += 1
            if counter["n"] == 2:
                raise RuntimeError("test failure")
            return original_apply(item)

        self.platform.apply_change = fail_second
        with self.assertRaises(SafetyError) as caught:
            self.safety.commit_plan(preview)
        self.assertIn("otomatik", str(caught.exception).lower())
        module = self.platform.get_module("stok")
        labels = {field["label"] for field in module["fields"]}
        self.assertNotIn("Rollback A", labels)
        self.assertNotIn("Rollback B", labels)
        self.assertEqual("ok", self.db.integrity_check())




class CrashSafetyTests(unittest.TestCase):
    def test_unclean_shutdown_creates_verified_recovery_backup(self):
        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name)
            db = Database(data_dir)
            first = RecoveryManager(db, version="5-test")
            self.assertFalse(first.start()["previous_unclean_shutdown"])
            # Temiz kapanış işareti verilmeden aynı veri alanı yeniden açılırsa önceki oturum beklenmedik kapanmış sayılır.
            second_db = Database(data_dir)
            second = RecoveryManager(second_db, version="5-test")
            status = second.start()
            self.assertTrue(status["previous_unclean_shutdown"])
            self.assertTrue(status["recovery_backup"])
            backup = data_dir / "yedekler" / status["recovery_backup"]
            self.assertTrue(backup.exists())
            self.assertEqual("ok", second_db.integrity_check(backup))
            second.mark_clean_shutdown()
            self.assertFalse(second.marker.exists())

    def test_corrupt_existing_database_fails_closed_before_migration(self):
        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name)
            (data_dir / "okul_guvenligi.db").write_bytes(b"this-is-not-sqlite")
            with self.assertRaises(DatabaseError):
                Database(data_dir)

    def test_assistant_config_never_returns_full_secret(self):
        with tempfile.TemporaryDirectory() as temp_name:
            store = AssistantConfigStore(Path(temp_name))
            config = store.save(api_key="sk-test-abcdefghijklmnopqrstuvwxyz", model="gpt-5.6-terra")
            self.assertEqual("gpt-5.6-terra", config.model)
            self.assertTrue(config.api_key.startswith("sk-test-"))
            public = store.public_status()
            self.assertTrue(public["configured"])
            self.assertNotIn(config.api_key, json.dumps(public))
            self.assertTrue(public["key_hint"].endswith(config.api_key[-4:]))
            store.save(clear_key=True)
            self.assertFalse(store.public_status()["configured"])


class FakeVisionPlanner:
    configured = True

    def status(self):
        return {"configured": True, "model": "fake-vision", "vision": True, "mode": "openai"}

    def plan(self, message, *, image_data_url="", screen_context=None):
        assert image_data_url.startswith("data:image/")
        return ChangePlan(
            message="Görseldeki isteğe göre Sil butonu sayfa üstüne alınacak.",
            assistant_text="Kayıt verisi değişmeyecek; yalnız eylem yerleşimi değişecek.",
            source="openai", vision_used=True,
            operations=[PlanOperation(
                kind="action_add_or_move", module="Stok", target="Sil", placement="page_top",
                reason="Görselde üst araç alanı hedeflendi.",
            )],
        )


class VisionHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = AppState(Path(self.temp.name))
        handler = type("Vision5TestHandler", (AppHandler,), {"state": self.state})
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
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_and_assistant_status_endpoints(self):
        health = self.request("/api/health")["data"]
        self.assertEqual("ok", health["integrity"])
        status = self.request("/api/assistant/status")["data"]
        self.assertTrue(status["safe_core"])
        self.assertFalse(status["direct_code_access"])
        self.assertFalse(status["direct_sql_access"])

    def test_assistant_settings_are_no_code_and_secret_is_not_echoed(self):
        saved = self.request("/api/assistant/settings", "POST", {
            "api_key": "sk-test-abcdefghijklmnopqrstuvwxyz",
            "model": "gpt-5.6-luna",
        })["data"]
        self.assertTrue(saved["configured"])
        self.assertEqual("gpt-5.6-luna", saved["model"])
        self.assertNotIn("sk-test-abcdefghijklmnopqrstuvwxyz", json.dumps(saved))
        status = self.request("/api/assistant/settings")["data"]
        self.assertTrue(status["key_hint"].endswith("wxyz"))
        cleared = self.request("/api/assistant/settings", "POST", {"clear_key": True, "model": "gpt-5.6"})["data"]
        self.assertFalse(cleared["configured"])

    def test_image_without_openai_never_changes_program(self):
        self.state.assistant.planner = OpenAIVisionPlanner(api_key="")
        result = self.request("/api/assistant/preview", "POST", {
            "message": "Bu görselde buraya Sil ekle",
            "image_data_url": ONE_PIXEL_PNG,
            "screen_context": {"current_module": "Stok"},
        })["data"]
        self.assertTrue(result["assistant_only"])
        self.assertEqual("", result["token"])
        self.assertFalse(result["vision_used"])

    def test_fake_vision_plan_gets_preview_token_and_safe_commit(self):
        self.state.assistant.planner = FakeVisionPlanner()
        result = self.request("/api/assistant/preview", "POST", {
            "message": "Buraya Sil butonu ekle",
            "image_data_url": ONE_PIXEL_PNG,
            "screen_context": {"current_view": "dynamicModule", "current_module": "Stok"},
        })["data"]
        self.assertTrue(result["vision_used"])
        self.assertTrue(result["token"])
        self.assertEqual("low", result["risk"])
        committed = self.request("/api/assistant/commit", "POST", {"token": result["token"]})["data"]
        self.assertEqual("ok", committed["integrity"])
        stock = self.state.platform.get_module("stok")
        delete_action = next(a for a in stock["actions"] if a["action_type"] == "archive_record")
        self.assertEqual("page_top", delete_action["placement"])


class VisionFrontendContractTests(unittest.TestCase):
    def test_vision_ui_and_fail_safe_controls_are_present(self):
        root = Path(__file__).parents[1] / "src" / "okul_guvenligi" / "web"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="logosAssistantImage"', html)
        self.assertIn('LOGOS ASİSTAN VISION 5.0', html)
        self.assertIn('id="platformChangeRisk"', html)
        self.assertIn("assistantScreenContext()", js)
        self.assertIn("/api/assistant/preview", js)
        self.assertIn("/api/assistant/commit", js)
        self.assertIn("state.assistantImageData", js)
        self.assertIn('id="assistantSettingsDialog"', html)
        self.assertIn('id="settingsAssistant"', html)
        self.assertIn("/api/assistant/settings", js)


if __name__ == "__main__":
    unittest.main()
