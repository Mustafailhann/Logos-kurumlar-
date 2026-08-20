from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


class RecoveryError(RuntimeError):
    pass


class RecoveryManager:
    """Beklenmedik kapanmayı algılar ve bir sonraki açılışta güvenli kurtarma noktası üretir."""

    def __init__(self, db, *, version: str = ""):
        self.db = db
        self.version = version
        self.marker = self.db.data_dir / ".logos_session_active.json"
        self.last_status: dict[str, Any] = {}

    def _write_marker(self) -> None:
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "version": self.version,
        }
        self.db.data_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".logos_session_", suffix=".tmp", dir=self.db.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.marker)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def start(self) -> dict[str, Any]:
        unclean = self.marker.exists()
        integrity = self.db.integrity_check()
        if integrity != "ok":
            raise RecoveryError(
                "Güvenli açılış durduruldu: veritabanı bütünlük kontrolü başarısız. "
                "Program veriye yazmayacak; doğrulanmış bir yedekten kurtarma gerekir."
            )
        backup_name = ""
        if unclean:
            backup_name = self.db.backup("beklenmeyen_kapanma_sonrasi", retain=50).name
        self._write_marker()
        self.last_status = {
            "ok": True,
            "previous_unclean_shutdown": unclean,
            "recovery_backup": backup_name,
            "integrity": integrity,
        }
        return dict(self.last_status)

    def mark_clean_shutdown(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.last_status = {**self.last_status, "clean_shutdown": True}
