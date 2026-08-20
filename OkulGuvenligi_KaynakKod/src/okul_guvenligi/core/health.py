from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


class HealthMonitor:
    """Başlangıç/çalışma sağlığı. Hata halinde yazmayı durdurabilecek sinyal üretir."""

    def __init__(self, db):
        self.db = db
        self.last_check: dict[str, Any] = {}

    def check(self) -> dict[str, Any]:
        integrity = self.db.integrity_check()
        free_bytes = shutil.disk_usage(self.db.data_dir).free
        db_size = self.db.path.stat().st_size if self.db.path.exists() else 0
        writable = os.access(self.db.data_dir, os.W_OK)
        warnings: list[str] = []
        if integrity != "ok":
            warnings.append(f"Veritabanı bütünlük kontrolü başarısız: {integrity}")
        if not writable:
            warnings.append("Veri klasörü yazılabilir değil.")
        if free_bytes < max(250 * 1024 * 1024, db_size * 5):
            warnings.append("Diskte güvenli yedekleme için boş alan az.")
        result = {
            "ok": integrity == "ok" and writable and not warnings,
            "integrity": integrity,
            "writable": writable,
            "free_bytes": free_bytes,
            "db_size": db_size,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "warnings": warnings,
        }
        self.last_check = result
        return result

    def require_writable(self) -> None:
        result = self.check()
        if result["integrity"] != "ok":
            raise RuntimeError("Güvenli mod: veritabanı bütünlüğü doğrulanmadığı için değişiklik engellendi.")
        if not result["writable"]:
            raise RuntimeError("Güvenli mod: veri klasörü yazılabilir değil.")
        required_free = max(250 * 1024 * 1024, int(result["db_size"]) * 5)
        if int(result["free_bytes"]) < required_free:
            raise RuntimeError("Güvenli mod: işlem öncesi yedek için yeterli boş disk alanı yok.")
