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
  9. Exchange 自動寄信（可選）

Usage:
    python scripts/preflight_check.py
    python scripts/preflight_check.py --test-user kane.beh   # 額外測試查詢特定員工
    python scripts/preflight_check.py --test-mail kane.beh@company.com   # 額外寄一封測試信
    python scripts/preflight_check.py --test-webhook   # 額外發送一則 Teams 測試通知
"""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

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

        # 測試資料表
        table = settings.MSSQL_TABLE
        async with engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT TOP 1 nt_account, org_id, extension FROM {table}")
            )
            row = result.fetchone()
            if row:
                record(f"{table} 表可讀取", True, f"範例: nt_account={row[0]}, org_id={row[1]}")
            else:
                record(f"{table} 表可讀取", True, "表存在但沒有資料")

        # 查詢特定使用者
        if test_user:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(f"SELECT nt_account, org_id, extension FROM {table} WHERE nt_account = :ename"),
                    {"ename": test_user.lower().strip()},
                )
                row = result.fetchone()
                if row:
                    record(f"查詢使用者 '{test_user}'", True,
                           f"org_id={row[1]}, extension={row[2] or '(空)'}")
                else:
                    record(f"查詢使用者 '{test_user}'", False, f"該使用者不存在於 {table} 表")

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


# Teams Webhook 常見網域
TEAMS_WEBHOOK_DOMAINS = (
    "webhook.office.com",       # 舊版 Incoming Webhook（Office 365 Connector，Microsoft 已停用）
    "webhook.microsoft.com",
    "logic.azure.com",          # Teams Workflows（Power Automate）
    "api.powerplatform.com",    # Teams Workflows（Power Automate 新網址格式）
)


async def check_webhook(settings, test_webhook: bool = False) -> None:
    """Step 8: 檢查 Teams Webhook（可選）。"""
    print(f"\n{'='*50}")
    print(f"Step 8: Teams Webhook（可選）")
    print(f"{'='*50}")

    if not settings or not settings.TEAMS_WEBHOOK_URL:
        record("Teams Webhook", True, "未設定，跳過（非必要）")
        return

    url = settings.TEAMS_WEBHOOK_URL
    record("Webhook URL 已設定", True, url[:60] + "...")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        record("Webhook URL 格式", False, "必須是 https:// 開頭的完整網址")
        return
    if any(host == d or host.endswith("." + d) for d in TEAMS_WEBHOOK_DOMAINS):
        record("Webhook URL 格式", True, host)
    else:
        # 只比對網域，無法確定是否有效，不列為失敗
        print(f"  {WARN} {host} 不是常見的 Teams Webhook 網域，請加上 --test-webhook 實際發送確認")

    if test_webhook:
        from app.webhook.teams import send_test_notification

        ok = await send_test_notification()
        record("發送 Teams 測試通知", ok, "" if ok else "發送失敗，請檢查 URL 與 HTTP_PROXY（錯誤訊息見上方 log）")
    else:
        print(f"  {INFO} 加上 --test-webhook 可實際發送一則測試通知")


async def check_mail(settings, test_mail: str | None) -> None:
    """Step 9: 檢查 Exchange 自動寄信（可選）。"""
    print(f"\n{'='*50}")
    print(f"Step 9: Exchange 自動寄信（可選）")
    print(f"{'='*50}")

    exchange = {
        "EXCHANGE_SERVER": getattr(settings, "EXCHANGE_SERVER", ""),
        "EXCHANGE_USER": getattr(settings, "EXCHANGE_USER", ""),
        "EXCHANGE_PASSWORD": getattr(settings, "EXCHANGE_PASSWORD", ""),
        "EXCHANGE_EMAIL": getattr(settings, "EXCHANGE_EMAIL", ""),
    }
    if not any(exchange.values()):
        record("Exchange 寄信", True, "未設定，跳過（註冊／忘記密碼由管理員手動產生連結）")
        return

    required = {**exchange, "COMPANY_EMAIL_DOMAIN": settings.COMPANY_EMAIL_DOMAIN}
    missing = [k for k, v in required.items() if not v]
    if missing:
        record("Exchange 設定完整", False, f"缺少 {', '.join(missing)}，設定不完整時不會自動寄信")
        return
    record(
        "Exchange 設定完整", True,
        f"{settings.EXCHANGE_USER} → 寄件信箱 {settings.EXCHANGE_EMAIL}（{settings.EXCHANGE_SERVER}）",
    )

    from app.mail.exchange import _get_account, send_mail

    try:
        build = await asyncio.to_thread(lambda: _get_account().version.build)  # 實際連線並通過 NTLM 驗證
        record("Exchange 連線與登入", True, f"Exchange build {build}")
    except Exception as e:
        lines = str(e).strip().splitlines()
        record("Exchange 連線與登入", False, f"{type(e).__name__}: {(lines[0] if lines else '')[:150]}")
        return

    if test_mail:
        ok, error = await send_mail(
            test_mail, "【AuthCenter】寄信測試",
            "<p>這是 AuthCenter preflight_check 寄出的測試信，收到代表自動寄信設定正確。</p>",
        )
        record(f"寄送測試信至 {test_mail}", ok, error)
    else:
        print(f"  {INFO} 加上 --test-mail <信箱> 可實際寄一封測試信")


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
    parser.add_argument("--test-mail", help="透過 Exchange 寄一封測試信到指定信箱")
    parser.add_argument("--test-webhook", action="store_true", help="實際發送一則 Teams 測試通知")
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
    await check_webhook(settings, args.test_webhook)
    await check_mail(settings, args.test_mail)

    print_summary()

    # Exit code
    failed = sum(1 for _, ok, _ in results if not ok)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
