"""Auth Center 部署前環境檢查腳本。

逐步驗證所有必要元件是否正確設定：
  1. .env 檔案載入
  2. ODBC Driver 安裝
  3. MSSQL 連線 + staff 表查詢
  4. SQLite 建立/讀寫
  5. RSA 金鑰對
  6. apps.yaml 載入
  7. Super Admin 設定
  8. Teams Webhook（可選）

Usage:
    python scripts/preflight_check.py
    python scripts/preflight_check.py --test-user kane.beh   # 額外測試查詢特定員工
"""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

# 確保專案根目錄在 sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = PASS if ok else FAIL
    print(f"  {status} {name}")
    if detail:
        print(f"         {detail}")


def check_env() -> dict:
    """Step 1: 檢查 .env 檔案與必要環境變數。"""
    print(f"\n{'='*50}")
    print(f"Step 1: .env 環境變數")
    print(f"{'='*50}")

    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        record(".env 檔案存在", False, f"找不到 {env_file}，請從 .env.example 複製")
        return {}

    record(".env 檔案存在", True)

    from dotenv import load_dotenv
    load_dotenv(env_file, override=True)

    from app.config import Settings
    s = Settings()

    required = {
        "MSSQL_HOST": s.MSSQL_HOST,
        "MSSQL_PASSWORD": s.MSSQL_PASSWORD,
        "MSSQL_DATABASE": s.MSSQL_DATABASE,
        "ADMIN_PASSWORD": s.ADMIN_PASSWORD,
    }

    for var, val in required.items():
        if val and val not in ("", "your_mysql_password", "your_mssql_password", "your_secure_password"):
            record(f"{var} 已設定", True)
        else:
            record(f"{var} 已設定", False, "使用預設值或未設定")

    print(f"\n  {INFO} MSSQL 連線: {s.MSSQL_USER}@{s.MSSQL_HOST}:{s.MSSQL_PORT}/{s.MSSQL_DATABASE}")
    print(f"  {INFO} ODBC Driver: {s.MSSQL_DRIVER}")

    return {"settings": s}


def check_odbc_driver(settings) -> None:
    """Step 2: 檢查 ODBC Driver 是否安裝。"""
    print(f"\n{'='*50}")
    print(f"Step 2: ODBC Driver")
    print(f"{'='*50}")

    driver_name = settings.MSSQL_DRIVER if settings else "ODBC Driver 17 for SQL Server"

    try:
        result = subprocess.run(["odbcinst", "-q", "-d"], capture_output=True, text=True, timeout=5)
        drivers = result.stdout.strip()
        if driver_name in drivers:
            record(f"ODBC Driver 已安裝", True, driver_name)
        elif drivers:
            record(f"ODBC Driver 已安裝", False,
                   f"找到: {drivers}\n         但 .env 指定: {driver_name}")
        else:
            record(f"ODBC Driver 已安裝", False, "未偵測到任何 ODBC Driver")
    except FileNotFoundError:
        # Windows 或沒有 odbcinst
        try:
            import pyodbc
            drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
            if any(driver_name in d for d in drivers):
                record(f"ODBC Driver 已安裝", True, driver_name)
            elif drivers:
                record(f"ODBC Driver 已安裝", False,
                       f"找到: {drivers}\n         但 .env 指定: {driver_name}")
            else:
                record(f"ODBC Driver 已安裝", False, "未偵測到 SQL Server ODBC Driver")
        except ImportError:
            record(f"ODBC Driver 已安裝", False,
                   "無法檢測（odbcinst 不存在且 pyodbc 未安裝）")
    except Exception as e:
        record(f"ODBC Driver 已安裝", False, str(e))


async def check_mssql(settings, test_user: str | None) -> None:
    """Step 3: 測試 MSSQL 連線與查詢。"""
    print(f"\n{'='*50}")
    print(f"Step 3: MSSQL 連線")
    print(f"{'='*50}")

    if not settings:
        record("MSSQL 連線", False, "缺少設定，跳過")
        return

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text

        engine = create_async_engine(settings.mssql_url, echo=False, pool_pre_ping=True)

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            result.fetchone()
        record("MSSQL 連線成功", True)

        # 測試 staff 表
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT TOP 1 nt_account, org_id, extension FROM staff")
            )
            row = result.fetchone()
            if row:
                record("staff 表可讀取", True, f"範例: nt_account={row[0]}, org_id={row[1]}")
            else:
                record("staff 表可讀取", True, "表存在但沒有資料")

        # 查詢特定使用者
        if test_user:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT nt_account, org_id, extension FROM staff WHERE nt_account = :ename"),
                    {"ename": test_user.lower().strip()},
                )
                row = result.fetchone()
                if row:
                    record(f"查詢使用者 '{test_user}'", True,
                           f"org_id={row[1]}, extension={row[2] or '(空)'}")
                else:
                    record(f"查詢使用者 '{test_user}'", False, "該使用者不存在於 staff 表")

        await engine.dispose()

    except Exception as e:
        error_msg = str(e)
        if "Login failed" in error_msg:
            record("MSSQL 連線成功", False, "帳號密碼錯誤，請檢查 MSSQL_USER / MSSQL_PASSWORD")
        elif "Could not open a connection" in error_msg or "Can't open lib" in error_msg:
            record("MSSQL 連線成功", False, "無法連線，請檢查 MSSQL_HOST / MSSQL_PORT / MSSQL_DRIVER")
        else:
            record("MSSQL 連線成功", False, error_msg[:200])


async def check_sqlite(settings) -> None:
    """Step 4: 測試 SQLite 讀寫。"""
    print(f"\n{'='*50}")
    print(f"Step 4: SQLite 本地資料庫")
    print(f"{'='*50}")

    if not settings:
        record("SQLite", False, "缺少設定，跳過")
        return

    db_path = Path(settings.SQLITE_PATH)
    record(f"SQLite 路徑", True, str(db_path))

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text

        engine = create_async_engine(settings.sqlite_url, echo=False)

        async with engine.connect() as conn:
            # 測試基本讀寫
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS _preflight_test (id INTEGER PRIMARY KEY)"
            ))
            await conn.execute(text("DROP TABLE IF EXISTS _preflight_test"))
            await conn.commit()
        record("SQLite 讀寫正常", True)

        # 檢查必要的表是否存在
        required_tables = ["user_accounts", "auth_codes", "registration_tokens",
                           "user_app_permissions", "app_admins"]
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))
            existing = {row[0] for row in result.fetchall()}

        missing = [t for t in required_tables if t not in existing]
        if missing:
            record("資料表已建立", False,
                   f"缺少: {', '.join(missing)}\n         請先啟動一次 Auth Center 讓 lifespan 自動建表")
        else:
            record("資料表已建立", True, f"共 {len(existing)} 張表")

        await engine.dispose()

    except Exception as e:
        record("SQLite 讀寫正常", False, str(e))


def check_rsa_keys(settings) -> None:
    """Step 5: 檢查 RSA 金鑰對。"""
    print(f"\n{'='*50}")
    print(f"Step 5: RSA 金鑰對")
    print(f"{'='*50}")

    if not settings:
        record("RSA 金鑰", False, "缺少設定，跳過")
        return

    priv_path = Path(settings.PRIVATE_KEY_PATH)
    pub_path = Path(settings.PUBLIC_KEY_PATH)

    if not priv_path.exists():
        record("private.pem 存在", False, f"找不到 {priv_path}\n         執行: python generate_keys.py")
        return
    record("private.pem 存在", True, str(priv_path))

    if not pub_path.exists():
        record("public.pem 存在", False, f"找不到 {pub_path}\n         執行: python generate_keys.py")
        return
    record("public.pem 存在", True, str(pub_path))

    # 測試簽發 + 驗證 JWT
    try:
        import jwt
        private_key = priv_path.read_text()
        public_key = pub_path.read_text()

        test_payload = {"sub": "preflight_test", "check": True}
        token = jwt.encode(test_payload, private_key, algorithm="RS256")
        decoded = jwt.decode(token, public_key, algorithms=["RS256"])

        if decoded["sub"] == "preflight_test":
            record("JWT 簽發/驗證", True, "RS256 金鑰對匹配")
        else:
            record("JWT 簽發/驗證", False, "解碼結果不符")
    except Exception as e:
        record("JWT 簽發/驗證", False, str(e))


def check_apps_yaml() -> None:
    """Step 6: 檢查 apps.yaml。"""
    print(f"\n{'='*50}")
    print(f"Step 6: apps.yaml App 註冊")
    print(f"{'='*50}")

    apps_file = BASE_DIR / "config" / "apps.yaml"
    if not apps_file.exists():
        record("apps.yaml 存在", False, f"找不到 {apps_file}")
        return
    record("apps.yaml 存在", True)

    try:
        from app.config import load_registered_apps
        apps = load_registered_apps()
        if apps:
            record(f"已註冊 {len(apps)} 個 App", True)
            for app_id, info in apps.items():
                name = info.get("name", app_id)
                has_secret = bool(info.get("client_secret"))
                has_redirect = bool(info.get("redirect_uri"))
                status = PASS if (has_secret and has_redirect) else WARN
                print(f"    {status} {app_id} ({name})"
                      f"{'  ⚠ 缺少 client_secret' if not has_secret else ''}"
                      f"{'  ⚠ 缺少 redirect_uri' if not has_redirect else ''}")
        else:
            record("已註冊 App", False, "apps.yaml 為空，尚未註冊任何 App")
    except Exception as e:
        record("apps.yaml 解析", False, str(e))


def check_admin(settings) -> None:
    """Step 7: 檢查 Super Admin 設定。"""
    print(f"\n{'='*50}")
    print(f"Step 7: Super Admin")
    print(f"{'='*50}")

    if not settings:
        record("Super Admin", False, "缺少設定，跳過")
        return

    if settings.ADMIN_USERNAME and settings.ADMIN_PASSWORD:
        record("Admin 帳密已設定", True, f"帳號: {settings.ADMIN_USERNAME}")
    else:
        record("Admin 帳密已設定", False, "請在 .env 設定 ADMIN_USERNAME 和 ADMIN_PASSWORD")


async def check_webhook(settings) -> None:
    """Step 8: 測試 Teams Webhook（可選）。"""
    print(f"\n{'='*50}")
    print(f"Step 8: Teams Webhook（可選）")
    print(f"{'='*50}")

    if not settings or not settings.TEAMS_WEBHOOK_URL:
        record("Teams Webhook", True, "未設定，跳過（非必要）")
        return

    url = settings.TEAMS_WEBHOOK_URL
    record("Webhook URL 已設定", True, url[:60] + "...")

    # 只檢查 URL 格式，不實際發送
    if "webhook.office.com" in url or "webhook.microsoft.com" in url:
        record("URL 格式正確", True)
    else:
        record("URL 格式正確", False, "不像是有效的 Teams Webhook URL")


def print_summary() -> None:
    """印出最終摘要。"""
    print(f"\n{'='*50}")
    print(f"檢查結果摘要")
    print(f"{'='*50}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)

    if failed == 0:
        print(f"\n  {PASS} 全部通過！({passed}/{passed})")
        print(f"\n  可以啟動 Auth Center：")
        print(f"    fastapi dev app/main.py")
    else:
        print(f"\n  通過: {passed}  失敗: {failed}")
        print(f"\n  需要修正的項目：")
        for name, ok, detail in results:
            if not ok:
                print(f"    {FAIL} {name}")
                if detail:
                    print(f"           {detail}")


async def main():
    parser = argparse.ArgumentParser(description="Auth Center 部署前環境檢查")
    parser.add_argument("--test-user", help="測試查詢特定員工（e.g. kane.beh）")
    args = parser.parse_args()

    print("\n  Auth Center 環境檢查")
    print(f"  {'─'*30}")

    ctx = check_env()
    settings = ctx.get("settings")

    check_odbc_driver(settings)
    await check_mssql(settings, args.test_user)
    await check_sqlite(settings)
    check_rsa_keys(settings)
    check_apps_yaml()
    check_admin(settings)
    await check_webhook(settings)

    print_summary()

    # Exit code
    failed = sum(1 for _, ok, _ in results if not ok)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
