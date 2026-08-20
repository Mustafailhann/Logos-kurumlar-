from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile
from pathlib import Path


def is_valid_db_with_data(db_path: Path) -> bool:
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        cnt = cur.execute("SELECT COUNT(*) FROM institutions").fetchone()[0]
        conn.close()
        return cnt > 0
    except Exception:
        return False


def extract_seed_from_pyz(pyz_path: Path, target_db: Path) -> bool:
    if not pyz_path.is_file():
        return False
    try:
        with zipfile.ZipFile(pyz_path, "r") as z:
            seed_name = "okul_guvenligi/seed/okul_guvenligi.db"
            if seed_name in z.namelist():
                data = z.read(seed_name)
                temp = target_db.parent / ".seed_extract.tmp"
                temp.write_bytes(data)
                os.replace(temp, target_db)
                return True
    except Exception as e:
        print("Seed çıkarma uyarısı:", e)
    return False


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    prod = local / "Sekizdesekiz" / "OkulGuvenligi"
    test = local / "Sekizdesekiz" / "OkulGuvenligi_5_0_VISION_ALPHA1_TEST"
    test.mkdir(parents=True, exist_ok=True)

    src_db = prod / "okul_guvenligi.db"
    dst_db = test / "okul_guvenligi.db"
    bundled_db = base_dir / "okul_guvenligi.db"
    pyz_path = base_dir / "OkulGuvenligi.pyz"

    # Check if test DB already exists AND has valid institution data
    if is_valid_db_with_data(dst_db):
        print(f"VISION 5.0 test veritabanı hazır (kurum verileri mevcut): {test}")
        return 0

    print("Test veritabanı bulunamadı veya boş. Kurum verileri yükleniyor...")

    # Strategy 1: Copy from bundled DB in folder if present and valid
    if is_valid_db_with_data(bundled_db):
        print(f"Klasördeki paket veritabanı yüklendi: {bundled_db}")
        shutil.copy2(bundled_db, dst_db)

    # Strategy 2: Copy from prod DB if valid
    elif is_valid_db_with_data(src_db):
        print(f"Mevcut veritabanından yedek alındı: {src_db}")
        source = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(dst_db, timeout=30)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    # Strategy 3: Extract seed DB from OkulGuvenligi.pyz
    elif extract_seed_from_pyz(pyz_path, dst_db):
        print(f"OkulGuvenligi.pyz içindeki seed veritabanı kuruldu.")

    else:
        print("Uyarı: Önceden yüklenmiş veri bulunamadı.")
        return 0

    # Verify integrity
    try:
        conn = sqlite3.connect(dst_db, timeout=10)
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        cnt = conn.execute("SELECT COUNT(*) FROM institutions").fetchone()[0]
        conn.close()
        if res == "ok":
            print(f"Veritabanı başarıyla doğrulandı: {cnt} kurum kaydı hazır.")
        else:
            print(f"Bütünlük uyarısı: {res}")
    except Exception as e:
        print("Veritabanı doğrulama hatası:", e)

    src_media = prod / "medya"
    dst_media = test / "medya"
    if src_media.is_dir():
        shutil.copytree(src_media, dst_media, dirs_exist_ok=True)

    # Sync prod_db so future runs have prod_db populated
    if not is_valid_db_with_data(src_db) and is_valid_db_with_data(dst_db):
        prod.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst_db, src_db)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
