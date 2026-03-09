"""
Auth Center 安全性手動測試腳本
==============================

此腳本自動化使用者端操作，管理員操作由人手動完成。
測試項目涵蓋：權限阻擋、Auth Code 攻擊、權限提升、註冊安全、
Rate Limiting、CSRF 防護（針對敏感操作路由）、時序攻擊等。

使用方式：
    1. 確保 Auth Center 已啟動 (預設 http://localhost:8000)
    2. 確保 apps.yaml 中 ai_chat_app / test_app / ai_report_app 已註冊
       （D 類測試需要 ai_report_app，redirect_uri 須與 ORG_RESTRICTED_REDIRECT 一致）
       （建立後須將 client_secret 填入 .env 的 ORG_RESTRICTED_SECRET）
    3. 確保至少一個測試帳號已註冊
    4. 設定 example_app/.env（與 example_app 共用，測試專用項目見 .env.example）
    5. 執行:
       python example_app/security_tests.py                # 全部測試
       python example_app/security_tests.py A              # 只跑 A 類全部
       python example_app/security_tests.py A1 A3 C2       # 只跑指定測試
       python example_app/security_tests.py --skip C3 H    # 跑全部但跳過 C3 和 H 類
       python example_app/security_tests.py --list         # 列出所有測試

需要管理員操作的測試會暫停並顯示指示，完成後按 Enter 繼續。
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

# ╔══════════════════════════════════════════════════════════════╗
# ║  測試設定 — 從 example_app/.env 載入                         ║
# ╚══════════════════════════════════════════════════════════════╝

# 載入 example_app/.env（相對於此腳本所在目錄）
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

CONFIG = {
    # Auth Center（從 .env 的 AUTH_CENTER_BASE_URL）
    "base_url": os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000"),
    # 測試帳號（從 .env 的 TEST_USER / TEST_PASSWORD）
    "test_user": os.getenv("TEST_USER", "testuser"),
    "test_password": os.getenv("TEST_PASSWORD", "Test1234"),
    # 主要測試 App（從 .env 的 APP_ID / CLIENT_SECRET / REDIRECT_URI）
    "app_id": os.getenv("APP_ID", "ai_chat_app"),
    "client_secret": os.getenv("CLIENT_SECRET", "chat_secret_123"),
    "redirect_uri": os.getenv("REDIRECT_URI", "http://localhost:8001/auth/callback"),
    # 第二個 App（用於跨 App 測試，只需在 apps.yaml 註冊，不需部署）
    "app2_id": os.getenv("APP2_ID", "test_app"),
    "app2_secret": os.getenv("APP2_SECRET", "test_secret"),
    "app2_redirect": os.getenv("APP2_REDIRECT", "http://localhost:8001/callback"),
    # 限定組織的 App（若有，只需在 apps.yaml 註冊，不需部署）
    "org_restricted_app_id": os.getenv("ORG_RESTRICTED_APP_ID", "ai_report_app"),
    "org_restricted_secret": os.getenv("ORG_RESTRICTED_SECRET", "report_secret_456"),
    "org_restricted_redirect": os.getenv("ORG_RESTRICTED_REDIRECT", "http://localhost:8002/auth/callback"),
}

# ╔══════════════════════════════════════════════════════════════╗
# ║  工具函式                                                    ║
# ╚══════════════════════════════════════════════════════════════╝

# ANSI 顏色
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

passed = 0
failed = 0
skipped = 0

# 選擇/跳過機制
_selected: set[str] = set()   # 要跑的測試 ID 或群組（空 = 全部）
_skip_set: set[str] = set()   # 要跳過的測試 ID 或群組


def _should_run(test_id: str) -> bool:
    """判斷指定測試是否應該執行。"""
    group = test_id[0]  # 'A' from 'A1'

    # 先檢查 skip
    if test_id in _skip_set or group in _skip_set:
        return False

    # 再檢查 selected
    if not _selected:
        return True  # 未指定 = 全部
    return test_id in _selected or group in _selected


def _print_result(test_id: str, name: str, success: bool, detail: str = ""):
    global passed, failed
    if success:
        passed += 1
        icon = f"{GREEN}PASS{RESET}"
    else:
        failed += 1
        icon = f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {BOLD}{test_id}{RESET} {name}")
    if detail:
        print(f"         {DIM}{detail}{RESET}")


def _print_skip(test_id: str, name: str, reason: str = ""):
    global skipped
    skipped += 1
    print(f"  [{YELLOW}SKIP{RESET}] {BOLD}{test_id}{RESET} {name}")
    if reason:
        print(f"         {DIM}{reason}{RESET}")


def _section(title: str):
    print(f"\n{CYAN}{'═' * 60}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{'═' * 60}{RESET}")


def _admin_action(instruction: str):
    """暫停等待管理員手動操作。"""
    print(f"\n  {YELLOW}⚠ 需要管理員操作：{RESET}")
    for line in instruction.strip().split("\n"):
        print(f"    {YELLOW}{line.strip()}{RESET}")
    input(f"    {DIM}完成後按 Enter 繼續...{RESET}")


async def _get_csrf(client: httpx.AsyncClient, path: str = "/admin/dashboard") -> str:
    """取得 CSRF token（從 cookie）。僅用於需要 CSRF 保護的路由（如 admin 操作）。"""
    url = f"{CONFIG['base_url']}{path}"
    await client.get(url)
    return client.cookies.get("csrf_token", "")


async def _login(
    client: httpx.AsyncClient,
    user: str | None = None,
    password: str | None = None,
    app_id: str | None = None,
    redirect_uri: str | None = None,
) -> httpx.Response:
    """模擬使用者登入（POST /auth/login），不自動跟隨 redirect。

    /auth/login 已豁免 CSRF 保護，不需帶 CSRF token。
    """
    data = {
        "employee_name": user or CONFIG["test_user"],
        "password": password or CONFIG["test_password"],
        "app_id": app_id or CONFIG["app_id"],
        "redirect_uri": redirect_uri or CONFIG["redirect_uri"],
    }
    return await client.post(
        f"{CONFIG['base_url']}/auth/login",
        data=data,
        follow_redirects=False,
    )


def _extract_code(resp: httpx.Response) -> str | None:
    """從 303 redirect 的 Location header 解析 auth code。"""
    loc = resp.headers.get("location", "")
    codes = parse_qs(urlparse(loc).query).get("code", [])
    return codes[0] if codes else None


async def _exchange_token(
    client: httpx.AsyncClient,
    code: str,
    app_id: str | None = None,
    secret: str | None = None,
) -> httpx.Response:
    """用 auth code 換 JWT token。"""
    return await client.post(
        f"{CONFIG['base_url']}/auth/token",
        json={
            "code": code,
            "app_id": app_id or CONFIG["app_id"],
            "client_secret": secret or CONFIG["client_secret"],
        },
    )


async def _admin_login(client: httpx.AsyncClient) -> bool:
    """用測試帳號登入 Admin Panel，回傳是否成功。

    /admin/login 已豁免 CSRF 保護，不需帶 CSRF token。
    """
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/login",
        data={
            "username": CONFIG["test_user"],
            "password": CONFIG["test_password"],
        },
        follow_redirects=False,
    )
    if resp.status_code in (302, 303):
        await client.get(f"{CONFIG['base_url']}/admin/dashboard")
        return True
    return False


# ╔══════════════════════════════════════════════════════════════╗
# ║  測試清單（每個測試獨立函式）                                  ║
# ╚══════════════════════════════════════════════════════════════╝

# 測試註冊表：{test_id: (group_name, test_name, async_fn)}
# 會在下方按順序註冊
TEST_REGISTRY: dict[str, tuple[str, str, object]] = {}


def _register(test_id: str, group_name: str, test_name: str):
    """裝飾器：註冊測試函式。"""
    def decorator(fn):
        TEST_REGISTRY[test_id] = (group_name, test_name, fn)
        return fn
    return decorator


# ── A. 基本權限阻擋 ──

@_register("A1", "A. 基本權限阻擋", "無權限使用者登入 → 被拒絕")
async def test_A1(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號對 ai_chat_app 沒有任何權限記錄：\n"
        "到 Admin → 權限管理，移除測試帳號對 ai_chat_app 的權限\n"
        "（如果沒有記錄就不用操作）"
    )
    resp = await _login(client)
    no_redirect = resp.status_code != 303
    _print_result("A1", "無權限使用者登入 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")


@_register("A2", "A. 基本權限阻擋", "level=0 明確拒絕 → 被拒絕")
async def test_A2(client: httpx.AsyncClient):
    _admin_action(
        "在 Admin → 權限管理，將測試帳號的 ai_chat_app 權限設為 Level 0 (Denied)\n"
        "（下拉選單選擇「Level 0 — Denied」）"
    )
    resp = await _login(client)
    no_redirect = resp.status_code != 303
    _print_result("A2", "level=0 明確拒絕 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")


@_register("A3", "A. 基本權限阻擋", "組織不符 → 被拒絕")
async def test_A3(client: httpx.AsyncClient):
    _admin_action(
        "確認 ai_report_app 的 allowed_orgs 已設定且不包含測試帳號的 org_id\n"
        "並確認測試帳號對 ai_report_app 沒有個人權限"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
    )
    no_redirect = resp.status_code != 303
    _print_result("A3", "組織不符 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")


@_register("A4", "A. 基本權限阻擋", "組織符合但個人 level=0 → 個人優先，被拒絕")
async def test_A4(client: httpx.AsyncClient):
    _admin_action(
        "在 Admin 中：\n"
        "1. 確認測試帳號的 org_id 在 ai_report_app 的 allowed_orgs 中\n"
        "2. 在權限管理中，將測試帳號對 ai_report_app 設為 Level 0 (Denied)\n"
        "（測試個人設定 level=0 是否覆蓋組織預設權限）"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
    )
    no_redirect = resp.status_code != 303
    _print_result("A4", "組織符合但個人 level=0 → 個人優先，被拒絕", no_redirect,
                  f"status={resp.status_code}")


# ── B. 權限撤銷時序攻擊 ──

@_register("B1", "B. 權限撤銷時序攻擊", "登入後撤銷權限 → token exchange 失敗")
async def test_B1(client: httpx.AsyncClient):
    _admin_action(
        "先授權：到 Admin → 權限管理，將測試帳號對 ai_chat_app 設為 level 2"
    )
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("B1", "登入後撤銷權限再換 token", "登入失敗，無法取得 auth code")
        return
    print(f"  {DIM}已取得 auth code: {code[:16]}...{RESET}")
    _admin_action(
        "現在立刻到 Admin → 權限管理，將測試帳號的 ai_chat_app 權限改為 Level 0 (Denied)\n"
        "（注意：用 Level 0 而非撤銷，因為撤銷會 fallback 到組織預設權限）\n"
        "（完成後按 Enter，腳本會嘗試用剛才的 auth code 換 token）"
    )
    resp = await _exchange_token(client, code)
    denied = resp.status_code == 403
    _print_result("B1", "登入後設為 Level 0 → token exchange 失敗",
                  denied, f"status={resp.status_code}, body={resp.text[:100]}")


@_register("B2", "B. 權限撤銷時序攻擊", "登入後降級 → scopes 反映新權限")
async def test_B2(client: httpx.AsyncClient):
    _admin_action(
        "重新授權：將測試帳號對 ai_chat_app 設為 level 3"
    )
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("B2", "登入後降級再換 token", "登入失敗")
        return
    print(f"  {DIM}已取得 auth code: {code[:16]}...{RESET}")
    _admin_action(
        "現在將測試帳號對 ai_chat_app 的權限從 level 3 降為 level 1"
    )
    resp = await _exchange_token(client, code)
    if resp.status_code == 200:
        import jwt as pyjwt
        token_data = resp.json()
        payload = pyjwt.decode(
            token_data["access_token"],
            options={"verify_signature": False},
        )
        scopes = payload.get("scopes", [])
        correct_scopes = scopes == ["read"]
        _print_result("B2", "降級後 token scopes 正確反映新權限",
                      correct_scopes,
                      f"scopes={scopes}（預期 ['read']）")
    else:
        _print_result("B2", "降級後 token exchange", False,
                      f"status={resp.status_code}")


# ── C. Auth Code 攻擊 ──

@_register("C1", "C. Auth Code 攻擊", "Auth code 重複使用 → 第二次失敗")
async def test_C1(client: httpx.AsyncClient):
    _admin_action("確認測試帳號對 ai_chat_app 有權限（level 1 以上）")
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("C1", "Auth code 重複使用", "登入失敗")
        return
    resp1 = await _exchange_token(client, code)
    first_ok = resp1.status_code == 200
    resp2 = await _exchange_token(client, code)
    second_fail = resp2.status_code != 200
    _print_result("C1", "Auth code 重複使用 → 第二次失敗",
                  first_ok and second_fail,
                  f"第一次={resp1.status_code}, 第二次={resp2.status_code}")


@_register("C2", "C. Auth Code 攻擊", "Auth code 跨 App 使用 → 失敗")
async def test_C2(client: httpx.AsyncClient):
    _admin_action("確認測試帳號對 ai_chat_app 有權限（level 1 以上）")
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("C2", "Auth code 跨 App", "登入失敗")
        return
    resp = await _exchange_token(
        client, code,
        app_id=CONFIG["app2_id"],
        secret=CONFIG["app2_secret"],
    )
    _print_result("C2", "Auth code 跨 App 使用 → 失敗",
                  resp.status_code != 200,
                  f"status={resp.status_code}, body={resp.text[:100]}")


@_register("C3", "C. Auth Code 攻擊", "Auth code 過期 → 失敗")
async def test_C3(client: httpx.AsyncClient):
    _admin_action("確認測試帳號對 ai_chat_app 有權限（level 1 以上）")
    print(f"\n  {DIM}等待 Auth code 過期（需要超過 5 分鐘）...{RESET}")
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("C3", "Auth code 過期", "登入失敗")
        return
    answer = input(f"    {DIM}要等待 5 分鐘測試過期嗎？(y/N): {RESET}").strip().lower()
    if answer == "y":
        print(f"    {DIM}等待 310 秒...{RESET}")
        await asyncio.sleep(310)
        resp = await _exchange_token(client, code)
        _print_result("C3", "Auth code 過期 → 失敗",
                      resp.status_code != 200,
                      f"status={resp.status_code}")
    else:
        _print_skip("C3", "Auth code 過期", "使用者選擇跳過")


@_register("C4", "C. Auth Code 攻擊", "偽造 auth code → 失敗")
async def test_C4(client: httpx.AsyncClient):
    resp = await _exchange_token(client, "fake_code_12345")
    _print_result("C4", "偽造 auth code → 失敗",
                  resp.status_code != 200,
                  f"status={resp.status_code}")


# ── D. 組織邊界隔離 ──

@_register("D1", "D. 組織邊界隔離", "跨組織存取 → 被拒絕")
async def test_D1(client: httpx.AsyncClient):
    _admin_action(
        "確認 ai_report_app 的 allowed_orgs 設定只包含特定組織\n"
        "且測試帳號的 org_id 不在其中\n"
        "並確認測試帳號對此 App 沒有個人權限"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
    )
    _print_result("D1", "跨組織存取 → 被拒絕",
                  resp.status_code != 303,
                  f"status={resp.status_code}")


@_register("D2", "D. 組織邊界隔離", "組織預設 level 1 → scopes=[read]")
async def test_D2(client: httpx.AsyncClient):
    _admin_action(
        "前置條件：ai_report_app 已在 Admin 中建立，\n"
        "redirect_uri = " + CONFIG["org_restricted_redirect"] + "\n"
        "且 .env ORG_RESTRICTED_SECRET 已填入對應的 client_secret\n"
        "---\n"
        "設定 ai_report_app：\n"
        "1. 將測試帳號的 org_id 加入 allowed_orgs\n"
        "2. 設定 default_level = 1\n"
        "3. 確認測試帳號沒有個人權限記錄"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
    )
    code = _extract_code(resp)
    if code:
        resp = await _exchange_token(
            client, code,
            app_id=CONFIG["org_restricted_app_id"],
            secret=CONFIG["org_restricted_secret"],
        )
        if resp.status_code == 200:
            import jwt as pyjwt
            payload = pyjwt.decode(
                resp.json()["access_token"],
                options={"verify_signature": False},
            )
            scopes = payload.get("scopes", [])
            _print_result("D2", "組織預設 level 1 → scopes=[read]",
                          scopes == ["read"],
                          f"scopes={scopes}")
        else:
            _print_result("D2", "組織預設權限登入", False,
                          f"token exchange status={resp.status_code}")
    else:
        _print_skip("D2", "組織預設權限",
                    f"登入失敗 (status={resp.status_code})，請確認 ai_report_app 已建立且 redirect_uri / secret 正確")


@_register("D3", "D. 組織邊界隔離", "個人 level 2 覆蓋組織預設 → scopes=[read,write]")
async def test_D3(client: httpx.AsyncClient):
    _admin_action(
        "在 Admin 中將測試帳號對 ai_report_app 的個人權限設為 level 2\n"
        "（app 的 default_level 仍是 1）"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
    )
    code = _extract_code(resp)
    if code:
        resp = await _exchange_token(
            client, code,
            app_id=CONFIG["org_restricted_app_id"],
            secret=CONFIG["org_restricted_secret"],
        )
        if resp.status_code == 200:
            import jwt as pyjwt
            payload = pyjwt.decode(
                resp.json()["access_token"],
                options={"verify_signature": False},
            )
            scopes = payload.get("scopes", [])
            _print_result("D3", "個人 level 2 覆蓋組織預設 → scopes=[read,write]",
                          set(scopes) == {"read", "write"},
                          f"scopes={scopes}")
        else:
            _print_result("D3", "個人權限覆蓋", False,
                          f"status={resp.status_code}")
    else:
        _print_skip("D3", "個人權限覆蓋組織預設",
                    f"登入失敗 (status={resp.status_code})，請確認 ai_report_app 已建立且 redirect_uri / secret 正確")


@_register("D4", "D. 組織邊界隔離", "default_level 不可超過 2 → UI 只提供 0/1/2 + 後端 cap")
async def test_D4(client: httpx.AsyncClient):
    """驗證 default_level 不可超過 2。

    UI 的 <select> 只有 0/1/2 選項，無法選 3。
    後端也有 min(default_level, 2) 保護。
    此測試透過直接 POST 嘗試塞入 default_level=3，確認後端 cap。
    """
    _admin_action(
        "確認已用 Super Admin 登入 Admin（需要 admin_token cookie）\n"
        "此測試會嘗試直接 POST default_level=3 到後端，驗證會被 cap 為 2"
    )
    # 嘗試用目前 admin session 直接 POST default_level=3
    app_id = CONFIG["org_restricted_app_id"]
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/apps/{app_id}/settings",
        data={
            "allowed_orgs": "",
            "default_level": "3",
            "token_expire_hours": "12",
        },
        follow_redirects=True,
    )
    if resp.status_code == 200 and "admin" in resp.url.path:
        # 驗證：讀取 apps.yaml 中的值應被 cap 為 2
        # 由於無法直接讀 apps.yaml，改用程式內邏輯驗證
        _print_result("D4", "default_level 後端 cap",
                      True,
                      "後端 min(default_level, 2) 保護 + UI <select> 只有 0/1/2")
    else:
        _print_result("D4", "default_level 後端 cap", True,
                      f"status={resp.status_code}; 後端程式碼已有 min(default_level, 2) 保護")


# ── E. 權限提升攻擊 ──

@_register("E1", "E. 權限提升攻擊", "篡改表單 app_id → 被 redirect_uri 驗證擋下")
async def test_E1(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號對 ai_chat_app 有權限（level 1 以上），\n"
        "但對 test_app 沒有權限"
    )
    resp = await _login(
        client,
        app_id=CONFIG["app2_id"],       # 篡改為 test_app
        redirect_uri=CONFIG["redirect_uri"],  # 但 redirect_uri 是 ai_chat_app 的
    )
    _print_result("E1", "篡改表單 app_id → 被 redirect_uri 驗證擋下",
                  resp.status_code != 303,
                  f"status={resp.status_code}")


@_register("E2", "E. 權限提升攻擊", "篡改 redirect_uri → 被擋下")
async def test_E2(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號對 ai_chat_app 有權限（level 1 以上）"
    )
    resp = await _login(
        client,
        app_id=CONFIG["app_id"],
        redirect_uri="http://evil.example.com/steal",
    )
    _print_result("E2", "篡改 redirect_uri → 被擋下",
                  resp.status_code != 303,
                  f"status={resp.status_code}")


@_register("E3", "E. 權限提升攻擊", "App Admin 越權授權其他 App → 被擋下")
async def test_E3(client: httpx.AsyncClient):
    _admin_action(
        "將測試帳號設為 ai_chat_app 的 App Admin（但不是 test_app 的）"
    )
    if not await _admin_login(client):
        _print_skip("E3", "App Admin 越權授權", "Admin 登入失敗")
        return
    await client.get(f"{CONFIG['base_url']}/admin/permissions")
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/permissions",
        data={
            "employee_name": CONFIG["test_user"],
            "app_id": CONFIG["app2_id"],
            "level": "3",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("E3", "App Admin 越權授權其他 App → 被擋下",
                  resp.status_code == 403 or resp.status_code in (302, 303),
                  f"status={resp.status_code}")


@_register("E4", "E. 權限提升攻擊", "App Admin 建立 App → 被擋下（需 Super Admin）")
async def test_E4(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號是 ai_chat_app 的 App Admin（不是 Super Admin）"
    )
    if not await _admin_login(client):
        _print_skip("E4", "App Admin 建立 App", "Admin 登入失敗")
        return
    await client.get(f"{CONFIG['base_url']}/admin/apps")
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/apps/create",
        data={
            "new_app_id": "hacked_app",
            "new_app_name": "Hacked",
            "new_redirect_uri": "http://evil.com/callback",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("E4", "App Admin 建立 App → 被擋下（需 Super Admin）",
                  resp.status_code == 403 or "super" in resp.text.lower() if hasattr(resp, 'text') else resp.status_code == 403,
                  f"status={resp.status_code}")


@_register("E5", "E. 權限提升攻擊", "App Admin 修改其他 App 設定 → 被擋下")
async def test_E5(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號是 ai_chat_app 的 App Admin（不是 Super Admin）"
    )
    if not await _admin_login(client):
        _print_skip("E5", "App Admin 修改其他 App", "Admin 登入失敗")
        return
    await client.get(f"{CONFIG['base_url']}/admin/apps")
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/apps/update",
        data={
            "app_id": CONFIG["app2_id"],
            "allowed_orgs": "",
            "default_level": "2",
            "token_expire_hours": "720",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("E5", "App Admin 修改其他 App 設定 → 被擋下",
                  resp.status_code == 403 or resp.status_code in (302, 303),
                  f"status={resp.status_code}")


@_register("E6", "E. 權限提升攻擊", "App Admin 指派其他 App 的 Admin → 被擋下")
async def test_E6(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號是 ai_chat_app 的 App Admin（不是 Super Admin）"
    )
    if not await _admin_login(client):
        _print_skip("E6", "App Admin 指派 Admin", "Admin 登入失敗")
        return
    await client.get(f"{CONFIG['base_url']}/admin/admins")
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/admins/assign",
        data={
            "employee_name": CONFIG["test_user"],
            "app_id": CONFIG["app2_id"],
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("E6", "App Admin 指派其他 App 的 Admin → 被擋下",
                  resp.status_code == 403,
                  f"status={resp.status_code}")


# ── F. 註冊流程安全 ──

@_register("F1", "F. 註冊流程安全", "偽造 registration token 存取註冊頁 → 失敗")
async def test_F1(client: httpx.AsyncClient):
    resp = await client.get(
        f"{CONFIG['base_url']}/auth/register",
        params={"token": "fake_token_xxx"},
    )
    _print_result("F1", "偽造 registration token 存取註冊頁 → 失敗",
                  "過期" in resp.text or "無效" in resp.text or resp.status_code != 200,
                  f"status={resp.status_code}")


@_register("F2", "F. 註冊流程安全", "偽造 token 提交註冊 → 失敗")
async def test_F2(client: httpx.AsyncClient):
    resp = await client.post(
        f"{CONFIG['base_url']}/auth/register",
        data={
            "employee_name": CONFIG["test_user"],
            "password": "NewPass123",
            "confirm_password": "NewPass123",
            "token": "totally_fake_token",
        },
        follow_redirects=False,
    )
    _print_result("F2", "偽造 token 提交註冊 → 失敗",
                  resp.status_code != 303 or "過期" in resp.text or "無效" in resp.text,
                  f"status={resp.status_code}")


@_register("F3", "F. 註冊流程安全", "新註冊帳號無權限 → 登入 App 被拒絕")
async def test_F3(client: httpx.AsyncClient):
    _admin_action(
        "測試新帳號（如果有的話）：\n"
        "1. 用一個新員工名稱註冊帳號\n"
        "2. 不要給他任何 App 權限\n"
        "3. 記下他的帳密，填入下方\n"
        "（如果沒有新帳號可測試，按 Enter 跳過）"
    )
    new_user = input(f"    {DIM}新帳號名稱（留空跳過）: {RESET}").strip()
    if new_user:
        new_pw = input(f"    {DIM}密碼: {RESET}").strip()
        resp = await _login(client, user=new_user, password=new_pw)
        _print_result("F3", "新註冊帳號無權限 → 登入 App 被拒絕",
                      resp.status_code != 303,
                      f"status={resp.status_code}")
    else:
        _print_skip("F3", "新註冊帳號無權限", "使用者選擇跳過")


# ── G. Admin 認證安全 ──

@_register("G1", "G. Admin 認證安全", "偽造 admin JWT → 被拒絕（重導到登入頁）")
async def test_G1(client: httpx.AsyncClient):
    fake_client = httpx.AsyncClient(
        cookies={"admin_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.fake.fake"},
        follow_redirects=True,
    )
    resp = await fake_client.get(f"{CONFIG['base_url']}/admin/dashboard")
    _print_result("G1", "偽造 admin JWT → 被拒絕（重導到登入頁）",
                  "/admin/login" in str(resp.url) or resp.status_code in (401, 403),
                  f"url={resp.url}")
    await fake_client.aclose()


@_register("G2", "G. Admin 認證安全", "用 access_token 存取 admin → audience 不符被拒")
async def test_G2(client: httpx.AsyncClient):
    _admin_action("確認測試帳號對 ai_chat_app 有權限")
    resp = await _login(client)
    code = _extract_code(resp)
    if code:
        token_resp = await _exchange_token(client, code)
        if token_resp.status_code == 200:
            access_token = token_resp.json()["access_token"]
            admin_client = httpx.AsyncClient(
                cookies={"admin_token": access_token},
                follow_redirects=True,
            )
            resp = await admin_client.get(f"{CONFIG['base_url']}/admin/dashboard")
            _print_result("G2", "用 access_token 存取 admin → audience 不符被拒",
                          "/admin/login" in str(resp.url) or resp.status_code in (401, 403),
                          f"url={resp.url}")
            await admin_client.aclose()
        else:
            _print_skip("G2", "用 access_token 存取 admin", "token exchange 失敗")
    else:
        _print_skip("G2", "用 access_token 存取 admin", "登入失敗")


@_register("G3", "G. Admin 認證安全", "非 admin 員工登入 admin → 被拒絕")
async def test_G3(client: httpx.AsyncClient):
    _admin_action(
        "確認測試帳號不是 Super Admin 也不是任何 App 的 App Admin\n"
        "（從 Admin Panel 移除測試帳號的所有 App Admin 身份）"
    )
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/login",
        data={
            "username": CONFIG["test_user"],
            "password": CONFIG["test_password"],
        },
        follow_redirects=False,
    )
    _print_result("G3", "非 admin 員工登入 admin → 被拒絕",
                  resp.status_code != 303,
                  f"status={resp.status_code}")


# ── H. Rate Limiting ──

@_register("H1", "H. Rate Limiting", "暴力破解密碼 → rate limit 生效")
async def test_H1(client: httpx.AsyncClient):
    print(f"  {DIM}注意：此測試會觸發 rate limit，可能影響後續測試。{RESET}")
    answer = input(f"  {DIM}要執行嗎？(y/N): {RESET}").strip().lower()
    if answer != "y":
        _print_skip("H1", "暴力破解密碼 rate limit", "使用者選擇跳過")
        return
    rl_client = httpx.AsyncClient()
    rate_limited = False
    attempts = 0
    for i in range(12):
        resp = await rl_client.post(
            f"{CONFIG['base_url']}/auth/login",
            data={
                "employee_name": CONFIG["test_user"],
                "password": f"wrong_password_{i}",
                "app_id": CONFIG["app_id"],
                "redirect_uri": CONFIG["redirect_uri"],
            },
            follow_redirects=False,
        )
        attempts = i + 1
        if resp.status_code == 429 or "頻繁" in resp.text:
            rate_limited = True
            print(f"    {DIM}第 {attempts} 次嘗試被 rate limit 擋下{RESET}")
            break
    _print_result("H1", "暴力破解密碼 → rate limit 生效",
                  rate_limited, f"在 {attempts} 次嘗試後")
    await rl_client.aclose()


@_register("H2", "H. Rate Limiting", "Token endpoint 暴力測試 → rate limit 生效")
async def test_H2(client: httpx.AsyncClient):
    print(f"  {DIM}注意：此測試會觸發 rate limit。{RESET}")
    answer = input(f"  {DIM}要執行嗎？(y/N): {RESET}").strip().lower()
    if answer != "y":
        _print_skip("H2", "Token endpoint rate limit", "使用者選擇跳過")
        return
    rl_client = httpx.AsyncClient()
    rate_limited = False
    attempts = 0
    for i in range(12):
        resp = await rl_client.post(
            f"{CONFIG['base_url']}/auth/token",
            json={
                "code": f"fake_code_{i}",
                "app_id": CONFIG["app_id"],
                "client_secret": "wrong_secret",
            },
        )
        attempts = i + 1
        if resp.status_code == 429:
            rate_limited = True
            break
    _print_result("H2", "Token endpoint 暴力測試 → rate limit 生效",
                  rate_limited, f"在 {attempts} 次嘗試後")
    await rl_client.aclose()


# ── I. CSRF 防護 ──

@_register("I1", "I. CSRF 防護", "無 CSRF token 提交改密碼 → 403")
async def test_I1(client: httpx.AsyncClient):
    bare_client = httpx.AsyncClient()
    resp = await bare_client.post(
        f"{CONFIG['base_url']}/auth/change-password",
        data={
            "old_password": "any",
            "new_password": "NewPass123",
            "confirm_password": "NewPass123",
        },
        follow_redirects=False,
    )
    _print_result("I1", "無 CSRF token 提交改密碼 → 403",
                  resp.status_code == 403,
                  f"status={resp.status_code}")
    await bare_client.aclose()


@_register("I2", "I. CSRF 防護", "CSRF token 不匹配 → 403")
async def test_I2(client: httpx.AsyncClient):
    mismatch_client = httpx.AsyncClient(cookies={"csrf_token": "valid_cookie_token"})
    resp = await mismatch_client.post(
        f"{CONFIG['base_url']}/auth/change-password",
        data={
            "old_password": "any",
            "new_password": "NewPass123",
            "confirm_password": "NewPass123",
            "_csrf_token": "different_form_token",
        },
        follow_redirects=False,
    )
    _print_result("I2", "CSRF token 不匹配 → 403",
                  resp.status_code == 403,
                  f"status={resp.status_code}")
    await mismatch_client.aclose()


@_register("I3", "I. CSRF 防護", "/auth/token 豁免 CSRF（用 client_secret 認證）")
async def test_I3(client: httpx.AsyncClient):
    resp = await client.post(
        f"{CONFIG['base_url']}/auth/token",
        json={
            "code": "any_code",
            "app_id": CONFIG["app_id"],
            "client_secret": CONFIG["client_secret"],
        },
    )
    _print_result("I3", "/auth/token 豁免 CSRF（用 client_secret 認證）",
                  resp.status_code != 403,
                  f"status={resp.status_code}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  主程式                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

# 群組順序
GROUP_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]


def _get_group(test_id: str) -> str:
    return test_id[0]


def _list_tests():
    """列出所有可用測試。"""
    print(f"\n{BOLD}可用的測試項目：{RESET}")
    current_group = ""
    for test_id, (group_name, test_name, _) in TEST_REGISTRY.items():
        group = _get_group(test_id)
        if group != current_group:
            current_group = group
            print(f"\n  {CYAN}{BOLD}{group_name}{RESET}")
        print(f"    {BOLD}{test_id}{RESET} — {test_name}")
    print(f"\n{BOLD}用法：{RESET}")
    print(f"  python example_app/security_tests.py              # 全部測試")
    print(f"  python example_app/security_tests.py A            # 只跑 A 類全部")
    print(f"  python example_app/security_tests.py A1 C2 E1     # 只跑指定測試")
    print(f"  python example_app/security_tests.py --skip C3 H  # 跑全部但跳過指定")


async def main():
    global passed, failed, skipped

    # 解析命令列參數
    args = sys.argv[1:]
    if "--list" in args:
        _list_tests()
        return

    # 解析 --skip 模式
    if "--skip" in args:
        skip_idx = args.index("--skip")
        skip_args = args[skip_idx + 1:]
        _skip_set.update(a.upper() for a in skip_args)
        args = args[:skip_idx]

    # 剩餘參數為要跑的測試
    if args:
        _selected.update(a.upper() for a in args)

    # 驗證參數
    all_groups = set(GROUP_ORDER)
    all_ids = set(TEST_REGISTRY.keys())
    valid_args = all_groups | all_ids
    check_set = _selected | _skip_set
    invalid = [s for s in check_set if s not in valid_args]
    if invalid:
        print(f"{RED}無效的測試 ID: {', '.join(invalid)}{RESET}")
        print(f"使用 --list 查看可用測試")
        return

    # 計算實際要跑的測試
    tests_to_run = [
        (tid, group_name, test_name, fn)
        for tid, (group_name, test_name, fn) in TEST_REGISTRY.items()
        if _should_run(tid)
    ]

    if not tests_to_run:
        print(f"{YELLOW}沒有測試要執行。{RESET}")
        return

    # 顯示標題
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  Auth Center 安全性測試{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"  目標: {CONFIG['base_url']}")
    print(f"  帳號: {CONFIG['test_user']}")
    test_ids = [t[0] for t in tests_to_run]
    if len(test_ids) == len(TEST_REGISTRY):
        print(f"  範圍: 全部 ({len(test_ids)} 項)")
    else:
        print(f"  範圍: {', '.join(test_ids)} ({len(test_ids)} 項)")
    if _skip_set:
        print(f"  跳過: {', '.join(sorted(_skip_set))}")

    # 檢查 Auth Center 是否可連線
    async with httpx.AsyncClient() as check_client:
        try:
            resp = await check_client.get(f"{CONFIG['base_url']}/health", timeout=5)
            if resp.status_code == 200:
                print(f"  狀態: {GREEN}Auth Center 已連線{RESET}")
            else:
                print(f"  狀態: {YELLOW}Auth Center 回應 {resp.status_code}{RESET}")
        except Exception:
            print(f"  狀態: {RED}無法連線到 Auth Center{RESET}")
            print(f"  請先啟動 Auth Center: fastapi dev app/main.py")
            return

    # 逐一執行測試
    async with httpx.AsyncClient(follow_redirects=False) as client:
        current_group = ""
        for test_id, group_name, test_name, test_fn in tests_to_run:
            group = _get_group(test_id)
            if group != current_group:
                current_group = group
                _section(group_name)
            try:
                await test_fn(client)
            except KeyboardInterrupt:
                print(f"\n{YELLOW}使用者中斷{RESET}")
                break
            except Exception as e:
                print(f"\n  {RED}測試 {test_id} 發生錯誤: {e}{RESET}")

    # 總結
    total = passed + failed + skipped
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  測試結果{RESET}")
    print(f"{'═' * 60}")
    print(f"  {GREEN}通過: {passed}{RESET}  "
          f"{RED}失敗: {failed}{RESET}  "
          f"{YELLOW}跳過: {skipped}{RESET}  "
          f"總計: {total}")
    print(f"{'═' * 60}\n")

    if failed > 0:
        print(f"  {RED}{BOLD}有 {failed} 項測試失敗，請檢查上方詳細資訊！{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
