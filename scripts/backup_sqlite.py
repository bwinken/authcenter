"""備份 Auth Center 的 SQLite 資料庫（可在服務運行中執行，不需停機）。

使用 SQLite Online Backup API 取得一致的快照：服務照常讀寫，
WAL 檔（*.db-wal）中尚未寫回主檔的資料也會一併備份。
直接 cp .db 檔可能漏掉 WAL 裡的資料，或複製到寫入一半的內容，請勿這樣備份。

備份流程：
  1. 以唯讀模式開啟來源，複製到暫存檔
  2. 備份檔轉成單一檔案（journal_mode=DELETE，不帶 -wal / -shm）
  3. PRAGMA integrity_check 驗證通過後才改成正式檔名
  4. 權限設為 600（內含密碼雜湊與 token）

Usage:
    python scripts/backup_sqlite.py                              # 備份 .env 的 SQLITE_PATH 到 backups/
    python scripts/backup_sqlite.py --keep 14                    # 備份後只保留最新 14 份
    python scripts/backup_sqlite.py --db data/auth_local.db      # 指定來源（如 Docker 部署）
    python scripts/backup_sqlite.py --output /mnt/backup/        # 指定輸出目錄（檔名自動加時間戳記）
    python scripts/backup_sqlite.py --output before-upgrade.db   # 指定輸出檔名

還原（必須先停止服務）：
    systemctl --user stop authcenter
    cp backups/auth_local-YYYYmmdd-HHMMSS.db auth_local.db
    rm -f auth_local.db-wal auth_local.db-shm   # 一定要刪，否則舊的 WAL 會套用到還原的檔案上
    systemctl --user start authcenter
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TIMESTAMP_GLOB = "????????-??????"  # 對應 %Y%m%d-%H%M%S

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # 未安裝 python-dotenv 時，改用環境變數或 --db
    pass


def default_db_path() -> Path:
    """與 Auth Center 相同：SQLITE_PATH 的相對路徑以專案根目錄為基準。"""
    path = Path(os.getenv("SQLITE_PATH", "auth_local.db"))
    return path if path.is_absolute() else BASE_DIR / path


def resolve_output(output: str | None, db: Path) -> tuple[Path, bool]:
    """回傳 (備份檔路徑, 是否為自動命名)。"""
    auto_name = f"{db.stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
    if output is None:
        return BASE_DIR / "backups" / auto_name, True
    out = Path(output).expanduser()
    if out.is_dir() or output.endswith(("/", os.sep)):
        return out / auto_name, True
    return out, False


def backup(db: Path, dest: Path) -> dict[str, int]:
    """備份 *db* 到 *dest*，回傳各資料表筆數。失敗時不留下任何檔案。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        src = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)  # 單一步驟複製整個資料庫，取得一致的快照
                dst.execute("PRAGMA journal_mode=DELETE")
                result = dst.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"integrity_check 失敗：{result}")
                tables = [r[0] for r in dst.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )]
                counts = {t: dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            finally:
                dst.close()
        finally:
            src.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return counts


def prune(directory: Path, stem: str, keep: int) -> list[Path]:
    """只保留最新 *keep* 份自動命名的備份，回傳被刪除的檔案。"""
    backups = sorted(directory.glob(f"{stem}-{TIMESTAMP_GLOB}.db"))  # 時間戳記檔名，字典序即時間序
    old = backups[:-keep]
    for path in old:
        path.unlink()
    return old


def main() -> None:
    parser = argparse.ArgumentParser(description="備份 SQLite 資料庫（可在服務運行中執行，不需停機）")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"來源資料庫路徑（預設：.env 的 SQLITE_PATH，目前為 {default_db_path()}）",
    )
    parser.add_argument(
        "-o", "--output",
        help="備份檔路徑或目錄（預設：<專案>/backups/，目錄時檔名自動加時間戳記）",
    )
    parser.add_argument(
        "--keep", type=int, default=0,
        help="備份後只保留最新 N 份自動命名的備份（預設 0 = 全部保留）",
    )
    args = parser.parse_args()

    if args.keep < 0:
        parser.error("--keep 不能是負數")
    db = (args.db or default_db_path()).expanduser()
    if not db.is_file():
        sys.exit(f"[ERROR] 找不到資料庫：{db}")
    dest, auto_named = resolve_output(args.output, db)
    if dest.resolve() == db.resolve():
        sys.exit("[ERROR] 備份檔不能與來源資料庫相同")
    if dest.exists():
        sys.exit(f"[ERROR] 備份檔已存在：{dest}")

    start = time.monotonic()
    try:
        counts = backup(db, dest)
    except (sqlite3.Error, RuntimeError, OSError) as e:
        sys.exit(f"[ERROR] 備份失敗：{e}")

    print(f"[OK] {db} → {dest}")
    print(f"     大小 {dest.stat().st_size / 1024:.1f} KB，耗時 {time.monotonic() - start:.2f} 秒，integrity_check: ok")
    for table, n in counts.items():
        print(f"     {table:<24} {n:>8} 筆")

    if args.keep and auto_named:
        removed = prune(dest.parent, db.stem, args.keep)
        if removed:
            print(f"     已刪除 {len(removed)} 份舊備份（保留最新 {args.keep} 份）")


if __name__ == "__main__":
    main()
