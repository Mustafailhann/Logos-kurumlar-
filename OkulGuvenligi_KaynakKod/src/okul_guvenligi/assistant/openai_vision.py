from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from ..core.models import ChangePlan
from .schema import PLAN_TOOL, SYSTEM_INSTRUCTIONS


class OpenAIUnavailable(RuntimeError):
    pass


class OpenAIVisionPlanner:
    """OpenAI & Google Gemini API client with fallback to local safe core."""

    OPENAI_API_URL = "https://api.openai.com/v1/responses"
    MAX_IMAGE_DATA_URL_CHARS = 10 * 1024 * 1024

    def __init__(self, *, api_key: str | None = None, model: str | None = None, timeout: int = 45):
        env_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        self.api_key = (api_key if api_key is not None else env_key).strip()
        default_model = "gemini-2.0-flash" if self.api_key.startswith("AIza") else "gpt-5.6"
        self.model = (model if model is not None else os.environ.get("LOGOS_OPENAI_MODEL", default_model)).strip() or default_model
        self.timeout = max(10, min(int(timeout), 120))

    @property
    def is_gemini(self) -> bool:
        return self.model.startswith("gemini") or self.api_key.startswith("AIza")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        mode_label = "gemini" if self.is_gemini else ("openai" if self.configured else "local-fallback")
        return {"configured": self.configured, "model": self.model, "vision": True, "mode": mode_label}

    @staticmethod
    def _screen_context_text(screen_context: dict[str, Any] | None) -> str:
        if not screen_context:
            return "Ekran bağlamı verilmedi."
        safe = {
            "current_view": str(screen_context.get("current_view", ""))[:80],
            "current_module": str(screen_context.get("current_module", ""))[:120],
            "module_fields": [str(x)[:80] for x in (screen_context.get("module_fields") or [])[:60]],
            "module_actions": [str(x)[:80] for x in (screen_context.get("module_actions") or [])[:30]],
            "visible_columns": [str(x)[:80] for x in (screen_context.get("visible_columns") or [])[:80]],
        }
        return "LOGOS ekran bağlamı: " + json.dumps(safe, ensure_ascii=False)

    def _payload(self, message: str, image_data_url: str = "", screen_context: dict[str, Any] | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": self._screen_context_text(screen_context) + "\n\nKullanıcı isteği: " + message.strip()}
        ]
        if image_data_url:
            if len(image_data_url) > self.MAX_IMAGE_DATA_URL_CHARS:
                raise ValueError("Asistan görseli çok büyük. Daha küçük bir ekran görüntüsü yükleyin.")
            if not image_data_url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
                raise ValueError("Asistan görseli PNG, JPG veya WEBP olmalıdır.")
            content.append({"type": "input_image", "image_url": image_data_url, "detail": "auto"})
        return {
            "model": self.model,
            "instructions": SYSTEM_INSTRUCTIONS,
            "input": [{"role": "user", "content": content}],
            "tools": [PLAN_TOOL],
            "tool_choice": {"type": "function", "name": "propose_logos_plan"},
            "parallel_tool_calls": False,
        }

    def _plan_gemini(self, message: str, image_data_url: str = "", screen_context: dict[str, Any] | None = None) -> ChangePlan:
        models_to_try = [self.model, "gemini-flash-latest", "gemini-1.5-flash", "gemini-2.5-flash"]
        last_error = None

        parts: list[dict[str, Any]] = [
            {"text": f"Sistem Talimatı: {SYSTEM_INSTRUCTIONS}\n\n{self._screen_context_text(screen_context)}\n\nKullanıcı İsteği: {message}"}
        ]

        if image_data_url and "," in image_data_url:
            header, base64_data = image_data_url.split(",", 1)
            mime_type = "image/png"
            if "jpeg" in header or "jpg" in header:
                mime_type = "image/jpeg"
            elif "webp" in header:
                mime_type = "image/webp"

            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64_data
                }
            })

        payload = json.dumps({"contents": [{"parts": parts}]}).encode("utf-8")

        for m in models_to_try:
            if not m:
                continue
            clean_m = m.replace("models/", "")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_m}:generateContent?key={self.api_key}"
            req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                    candidates = data.get("candidates", [])
                    if candidates:
                        text_parts = candidates[0].get("content", {}).get("parts", [])
                        if text_parts:
                            reply_text = text_parts[0].get("text", "")
                            return ChangePlan(message=reply_text, assistant_text=reply_text, source="gemini", vision_used=bool(image_data_url))
            except Exception as exc:
                last_error = exc
                continue

        raise OpenAIUnavailable(f"Gemini API Hatası: {last_error or 'Yanıt alınamadı'}")


    def plan(self, message: str, *, image_data_url: str = "", screen_context: dict[str, Any] | None = None) -> ChangePlan:
        if not self.configured:
            raise OpenAIUnavailable("AI API bağlantısı (Gemini/OpenAI) yapılandırılmamış.")

        if self.is_gemini:
            return self._plan_gemini(message, image_data_url, screen_context)

        payload = json.dumps(self._payload(message, image_data_url, screen_context), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.OPENAI_API_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "LOGOS-TECH-Vision/5.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise OpenAIUnavailable(f"OpenAI isteği başarısız ({exc.code}). {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise OpenAIUnavailable("AI bağlantısına ulaşılamadı. Yerel güvenli asistan kullanılabilir.") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAIUnavailable("AI yanıtı okunamadı.") from exc
        return self._parse_plan(parsed, vision_used=bool(image_data_url))
