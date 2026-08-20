from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_MODELS = {
    "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash",
    "gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"
}
DEFAULT_MODEL = "gemini-2.0-flash"


@dataclass(frozen=True)
class AssistantConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    storage_secure: bool = False



class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


class AssistantConfigStore:
    """OpenAI anahtarını tarayıcıya geri vermeden sunucu tarafında saklar.

    Windows'ta DPAPI ile mevcut Windows kullanıcısına bağlı şifreleme kullanılır.
    Diğer sistemlerde geliştirme amacıyla yalnız kullanıcı izinli dosya kullanılır.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.data_dir / "assistant_settings.json"
        self.secret_path = self.data_dir / "assistant_secret.bin"

    @property
    def storage_secure(self) -> bool:
        return os.name == "nt"

    @staticmethod
    def _protect_windows(data: bytes) -> bytes:
        in_blob, in_buffer = _blob(data)
        out_blob = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptProtectData(ctypes.byref(in_blob), "LOGOS TECH OpenAI", None, None, None, 0, ctypes.byref(out_blob)):
            raise OSError("Windows DPAPI anahtarı koruyamadı.")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
            del in_buffer

    @staticmethod
    def _unprotect_windows(data: bytes) -> bytes:
        in_blob, in_buffer = _blob(data)
        out_blob = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            raise OSError("Windows DPAPI anahtarı çözemedi.")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
            del in_buffer

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def _read_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            value = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def load(self) -> AssistantConfig:
        meta = self._read_meta()
        model = str(meta.get("model", DEFAULT_MODEL))
        if model not in ALLOWED_MODELS:
            model = DEFAULT_MODEL
        key = ""
        if self.secret_path.exists():
            try:
                raw = self.secret_path.read_bytes()
                if os.name == "nt":
                    key = self._unprotect_windows(raw).decode("utf-8")
                else:
                    key = base64.b64decode(raw).decode("utf-8")
            except Exception:
                key = ""
        return AssistantConfig(api_key=key.strip(), model=model, storage_secure=self.storage_secure)

    def save(self, *, api_key: str | None = None, model: str | None = None, clear_key: bool = False) -> AssistantConfig:
        current = self.load()
        chosen_model = str(model or current.model or DEFAULT_MODEL).strip()
        if chosen_model not in ALLOWED_MODELS:
            raise ValueError("Desteklenmeyen OpenAI modeli seçildi.")
        self._atomic_write(self.meta_path, json.dumps({"model": chosen_model}, ensure_ascii=False, indent=2).encode("utf-8"))
        if clear_key:
            self.secret_path.unlink(missing_ok=True)
        elif api_key is not None and api_key.strip():
            clean = api_key.strip()
            if len(clean) < 10:
                raise ValueError("API anahtarı çok kısa veya geçersiz.")
            raw = clean.encode("utf-8")
            protected = self._protect_windows(raw) if os.name == "nt" else base64.b64encode(raw)
            self._atomic_write(self.secret_path, protected)

        return self.load()

    def public_status(self) -> dict[str, Any]:
        config = self.load()
        env_key = os.environ.get("OPENAI_API_KEY", "").strip()
        configured = bool(config.api_key or env_key)
        source = "saved" if config.api_key else ("environment" if env_key else "none")
        return {
            "configured": configured,
            "model": config.model,
            "storage_secure": config.storage_secure,
            "key_source": source,
            "key_hint": "••••" + (config.api_key[-4:] if config.api_key else env_key[-4:]) if configured else "",
            "allowed_models": ["gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"],
        }
