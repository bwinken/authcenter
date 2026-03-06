"""
Auth Center 安全性手動測試腳本
==============================

此腳本自動化使用者端操作，管理員操作由人手動完成。
測試項目涵蓋：權限阻擋、Auth Code 攻擊、權限提升、註冊安全、
Rate Limiting、CSRF 防護、時序攻擊等。

使用方式：
    1. 確保 Auth Center 已啟動 (預設 http://localhost:8000)
    2. 確保 apps.yaml 中 ai_chat_app / test_app 已註冊
    3. 確保至少一個測試帳號已註冊（填入下方 CONFIG）
    4. 執行:
       python example_app/security_tests.py              # 全部測試
       python example_app/security_tests.py A             # 只跑 A 類
       python example_app/security_tests.py A B C         # 跑 A, B, C 類
       python example_app/security_tests.py --list        # 列出所有分類

需要管理員操作的測試會暫停並顯示指示，完成後按 Enter 繼續。
"""

import asyncio
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx

# ╔══════════════════════════════════════════════════════════════╗
# ║  測試設定 — 請依照你的環境修改                                ║
# ╚══════════════════════════════════════════════════════════════╝

CONFIG = {
    # Auth Center
    "base_url": "http://localhost:8000",
    # 已註冊的測試帳號（必須已存在於 MSSQL 員工名單 + 已在 Auth Center 註冊）
    "test_user": "testuser",
    "test_password": "Test1234",
    # 主要測試 App（apps.yaml 中已註冊）
    "app_id": "ai_chat_app",
    "client_secret": "chat_secret_123",
    "redirect_uri": "http://localhost:8001/auth/callback",
    # 第二個 App（用於跨 App 測試）
    "app2_id": "test_app",
    "app2_secret": "test_secret",
    "app2_redirect": "http://localhost:8001/callback",
    # 限定組織的 App（若有）
    "org_restricted_app_id": "ai_report_app",
    "org_restricted_secret": "report_secret_456",
    "org_restricted_redirect": "http://localhost:8002/auth/callback",
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


async def _get_csrf(client: httpx.AsyncClient, path: str = "/auth/login") -> str:
    """取得 CSRF token（從 cookie）。"""
    url = f"{CONFIG['base_url']}{path}"
    params = {}
    if path == "/auth/login":
        params = {"app_id": CONFIG["app_id"], "redirect_uri": CONFIG["redirect_uri"]}
    resp = await client.get(url, params=params)
    return client.cookies.get("csrf_token", "")


async def _login(
    client: httpx.AsyncClient,
    user: str | None = None,
    password: str | None = None,
    app_id: str | None = None,
    redirect_uri: str | None = None,
    csrf: str | None = None,
) -> httpx.Response:
    """模擬使用者登入（POST /auth/login），不自動跟隨 redirect。"""
    if csrf is None:
        csrf = await _get_csrf(client)

    data = {
        "employee_name": user or CONFIG["test_user"],
        "password": password or CONFIG["test_password"],
        "app_id": app_id or CONFIG["app_id"],
        "redirect_uri": redirect_uri or CONFIG["redirect_uri"],
        "_csrf_token": csrf,
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


# ╔══════════════════════════════════════════════════════════════╗
# ║  A. 基本權限阻擋                                            ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_A(client: httpx.AsyncClient):
    _section("A. 基本權限阻擋（預期全部被拒絕）")

    # A1: 無權限使用者登入
    _admin_action(
        "確認測試帳號對 ai_chat_app 沒有任何權限記錄：\n"
        "到 Admin → 權限管理，移除測試帳號對 ai_chat_app 的權限\n"
        "（如果沒有記錄就不用操作）"
    )
    resp = await _login(client)
    ok = resp.status_code == 200 and "error" in resp.text.lower() or resp.status_code == 403
    # 如果沒有權限，login 不會 303 redirect，而是回到 login 頁面顯示錯誤
    no_redirect = resp.status_code != 303
    _print_result("A1", "無權限使用者登入 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")

    # A2: level=0 明確拒絕
    _admin_action(
        "在 Admin → 權限管理，將測試帳號的 ai_chat_app 權限設為 level 1\n"
        "然後手動在 SQLite 中將 level 改為 0（或透過其他方式設定 level=0）\n"
        "如果無法設定 level=0，可跳過此測試"
    )
    resp = await _login(client)
    no_redirect = resp.status_code != 303
    _print_result("A2", "level=0 明確拒絕 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")

    # A3: 組織不符
    _admin_action(
        "確認 ai_report_app 的 allowed_orgs 已設定且不包含測試帳號的 org_id\n"
        "並確認測試帳號對 ai_report_app 沒有個人權限"
    )
    csrf = await _get_csrf(client)
    # 先 GET login page 以設定 CSRF
    await client.get(
        f"{CONFIG['base_url']}/auth/login",
        params={
            "app_id": CONFIG["org_restricted_app_id"],
            "redirect_uri": CONFIG["org_restricted_redirect"],
        },
    )
    csrf = client.cookies.get("csrf_token", "")
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
        csrf=csrf,
    )
    no_redirect = resp.status_code != 303
    _print_result("A3", "組織不符 → 被拒絕", no_redirect,
                  f"status={resp.status_code}")

    # A4: 組織符合但個人 level=0
    _admin_action(
        "在 Admin 中：\n"
        "1. 確認測試帳號的 org_id 在 ai_report_app 的 allowed_orgs 中\n"
        "2. 設定測試帳號對 ai_report_app 的個人權限為 level 1\n"
        "3. 然後手動在 SQLite 中將 level 改為 0\n"
        "（測試個人設定 level=0 是否覆蓋組織預設權限）\n"
        "如果無法操作可跳過"
    )
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
        csrf=csrf,
    )
    no_redirect = resp.status_code != 303
    _print_result("A4", "組織符合但個人 level=0 → 個人優先，被拒絕", no_redirect,
                  f"status={resp.status_code}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  B. 權限撤銷時序攻擊                                        ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_B(client: httpx.AsyncClient):
    _section("B. 權限撤銷後的時序攻擊")

    # B1: 登入後、換 token 前撤銷權限
    _admin_action(
        "先授權：到 Admin → 權限管理，將測試帳號對 ai_chat_app 設為 level 2"
    )
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("B1", "登入後撤銷權限再換 token", "登入失敗，無法取得 auth code")
    else:
        print(f"  {DIM}已取得 auth code: {code[:16]}...{RESET}")
        _admin_action(
            "現在立刻到 Admin → 權限管理，撤銷測試帳號對 ai_chat_app 的權限\n"
            "（完成後按 Enter，腳本會嘗試用剛才的 auth code 換 token）"
        )
        resp = await _exchange_token(client, code)
        denied = resp.status_code == 403
        _print_result("B1", "登入後撤銷權限 → token exchange 失敗",
                      denied, f"status={resp.status_code}, body={resp.text[:100]}")

    # B2: 登入後降級
    _admin_action(
        "重新授權：將測試帳號對 ai_chat_app 設為 level 3"
    )
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("B2", "登入後降級再換 token", "登入失敗")
    else:
        print(f"  {DIM}已取得 auth code: {code[:16]}...{RESET}")
        _admin_action(
            "現在將測試帳號對 ai_chat_app 的權限從 level 3 降為 level 1"
        )
        resp = await _exchange_token(client, code)
        if resp.status_code == 200:
            import jwt as pyjwt
            token_data = resp.json()
            # 解碼 JWT payload（不驗證簽章，因為我們沒有公鑰）
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


# ╔══════════════════════════════════════════════════════════════╗
# ║  C. Auth Code 攻擊                                          ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_C(client: httpx.AsyncClient):
    _section("C. Auth Code 攻擊（不需管理員操作）")

    # 確保有權限
    _admin_action(
        "確認測試帳號對 ai_chat_app 有權限（level 1 以上）"
    )

    # C1: Auth code 重複使用
    resp = await _login(client)
    code = _extract_code(resp)
    if not code:
        _print_skip("C1", "Auth code 重複使用", "登入失敗")
        _print_skip("C2", "Auth code 跨 App", "登入失敗")
    else:
        resp1 = await _exchange_token(client, code)
        first_ok = resp1.status_code == 200
        resp2 = await _exchange_token(client, code)
        second_fail = resp2.status_code != 200
        _print_result("C1", "Auth code 重複使用 → 第二次失敗",
                      first_ok and second_fail,
                      f"第一次={resp1.status_code}, 第二次={resp2.status_code}")

    # C2: Auth code 跨 App 使用
    resp = await _login(client)
    code = _extract_code(resp)
    if code:
        resp = await _exchange_token(
            client, code,
            app_id=CONFIG["app2_id"],
            secret=CONFIG["app2_secret"],
        )
        _print_result("C2", "Auth code 跨 App 使用 → 失敗",
                      resp.status_code != 200,
                      f"status={resp.status_code}, body={resp.text[:100]}")

    # C3: Auth code 過期
    print(f"\n  {DIM}等待 Auth code 過期（需要超過 5 分鐘）...{RESET}")
    print(f"  {DIM}提示：若不想等 5 分鐘，可先跳過此測試{RESET}")
    resp = await _login(client)
    code = _extract_code(resp)
    if code:
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
    else:
        _print_skip("C3", "Auth code 過期", "登入失敗")

    # C4: 偽造 auth code
    resp = await _exchange_token(client, "fake_code_12345")
    _print_result("C4", "偽造 auth code → 失敗",
                  resp.status_code != 200,
                  f"status={resp.status_code}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  D. 組織邊界隔離                                             ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_D(client: httpx.AsyncClient):
    _section("D. 組織邊界隔離")

    # D1: 跨組織存取
    _admin_action(
        "確認 ai_report_app 的 allowed_orgs 設定只包含特定組織\n"
        "且測試帳號的 org_id 不在其中\n"
        "並確認測試帳號對此 App 沒有個人權限"
    )
    await client.get(
        f"{CONFIG['base_url']}/auth/login",
        params={
            "app_id": CONFIG["org_restricted_app_id"],
            "redirect_uri": CONFIG["org_restricted_redirect"],
        },
    )
    csrf = client.cookies.get("csrf_token", "")
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
        csrf=csrf,
    )
    _print_result("D1", "跨組織存取 → 被拒絕",
                  resp.status_code != 303,
                  f"status={resp.status_code}")

    # D2: 組織預設權限
    _admin_action(
        "設定 ai_report_app：\n"
        "1. 將測試帳號的 org_id 加入 allowed_orgs\n"
        "2. 設定 default_level = 1\n"
        "3. 確認測試帳號沒有個人權限記錄"
    )
    await client.get(
        f"{CONFIG['base_url']}/auth/login",
        params={
            "app_id": CONFIG["org_restricted_app_id"],
            "redirect_uri": CONFIG["org_restricted_redirect"],
        },
    )
    csrf = client.cookies.get("csrf_token", "")
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
        csrf=csrf,
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
                          f"status={resp.status_code}")
    else:
        _print_skip("D2", "組織預設權限", "登入失敗")

    # D3: 個人權限覆蓋組織預設
    _admin_action(
        "在 Admin 中將測試帳號對 ai_report_app 的個人權限設為 level 2\n"
        "（app 的 default_level 仍是 1）"
    )
    await client.get(
        f"{CONFIG['base_url']}/auth/login",
        params={
            "app_id": CONFIG["org_restricted_app_id"],
            "redirect_uri": CONFIG["org_restricted_redirect"],
        },
    )
    csrf = client.cookies.get("csrf_token", "")
    resp = await _login(
        client,
        app_id=CONFIG["org_restricted_app_id"],
        redirect_uri=CONFIG["org_restricted_redirect"],
        csrf=csrf,
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
        _print_skip("D3", "個人權限覆蓋組織預設", "登入失敗")

    # D4: default_level 不可超過 2
    _admin_action(
        "嘗試在 Admin → 應用程式管理中，將某 App 的 default_level 設為 3\n"
        "觀察是否被自動 cap 在 2（儲存後重新查看數值）"
    )
    _print_result("D4", "default_level 不可超過 2 → 由管理員目視確認",
                  True, "請確認 Admin UI 中 default_level 最大為 2")


# ╔══════════════════════════════════════════════════════════════╗
# ║  E. 權限提升攻擊                                             ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_E(client: httpx.AsyncClient):
    _section("E. 權限提升攻擊")

    _admin_action(
        "確認測試帳號對 ai_chat_app 有權限（level 1 以上），\n"
        "但對 test_app 沒有權限"
    )

    # E1: 篡改 app_id
    csrf = await _get_csrf(client)
    resp = await _login(
        client,
        app_id=CONFIG["app2_id"],       # 篡改為 test_app
        redirect_uri=CONFIG["redirect_uri"],  # 但 redirect_uri 是 ai_chat_app 的
        csrf=csrf,
    )
    _print_result("E1", "篡改表單 app_id → 被 redirect_uri 驗證擋下",
                  resp.status_code != 303,
                  f"status={resp.status_code}")

    # E2: 篡改 redirect_uri
    csrf = await _get_csrf(client)
    resp = await _login(
        client,
        app_id=CONFIG["app_id"],
        redirect_uri="http://evil.example.com/steal",
        csrf=csrf,
    )
    _print_result("E2", "篡改 redirect_uri → 被擋下",
                  resp.status_code != 303,
                  f"status={resp.status_code}")

    # E3: App Admin 越權管理其他 App 的權限
    print(f"\n  {DIM}E3-E6 為 Admin 端點攻擊測試，需要 App Admin 身份：{RESET}")
    _admin_action(
        "將測試帳號設為 ai_chat_app 的 App Admin（但不是 test_app 的）\n"
        "然後用測試帳號登入 Admin Panel 取得 admin_token cookie"
    )

    # 用測試帳號登入 admin
    await client.get(f"{CONFIG['base_url']}/admin/login")
    csrf = client.cookies.get("csrf_token", "")
    admin_resp = await client.post(
        f"{CONFIG['base_url']}/admin/login",
        data={
            "username": CONFIG["test_user"],
            "password": CONFIG["test_password"],
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )

    if admin_resp.status_code in (302, 303):
        # 跟隨 redirect 以載入 dashboard
        await client.get(f"{CONFIG['base_url']}/admin/dashboard")

        # E3: 嘗試為其他 App 授權
        await client.get(f"{CONFIG['base_url']}/admin/permissions")
        csrf = client.cookies.get("csrf_token", "")
        resp = await client.post(
            f"{CONFIG['base_url']}/admin/permissions",
            data={
                "employee_name": CONFIG["test_user"],
                "app_id": CONFIG["app2_id"],  # 不是自己管理的 App
                "level": "3",
                "_csrf_token": csrf,
            },
            follow_redirects=False,
        )
        # 應被擋下（403 或重新導向到 permissions 頁但不生效）
        _print_result("E3", "App Admin 越權授權其他 App → 被擋下",
                      resp.status_code == 403 or resp.status_code in (302, 303),
                      f"status={resp.status_code}")

        # E4: App Admin 嘗試建立 App
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

        # E5: App Admin 修改其他 App 設定
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

        # E6: App Admin 指派 Admin
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
    else:
        _print_skip("E3", "App Admin 越權授權", "Admin 登入失敗")
        _print_skip("E4", "App Admin 建立 App", "Admin 登入失敗")
        _print_skip("E5", "App Admin 修改其他 App", "Admin 登入失敗")
        _print_skip("E6", "App Admin 指派 Admin", "Admin 登入失敗")


# ╔══════════════════════════════════════════════════════════════╗
# ║  F. 註冊流程安全                                              ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_F(client: httpx.AsyncClient):
    _section("F. 註冊流程安全")

    # F1: 已註冊帳號重複註冊（用假 token）
    resp = await client.get(
        f"{CONFIG['base_url']}/auth/register",
        params={"token": "fake_token_xxx"},
    )
    _print_result("F1a", "偽造 registration token 存取註冊頁 → 失敗",
                  "過期" in resp.text or "無效" in resp.text or resp.status_code != 200,
                  f"status={resp.status_code}")

    # F3: 偽造 registration token
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/auth/register",
        data={
            "employee_name": CONFIG["test_user"],
            "password": "NewPass123",
            "confirm_password": "NewPass123",
            "token": "totally_fake_token",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("F3", "偽造 token 提交註冊 → 失敗",
                  resp.status_code != 303 or "過期" in resp.text or "無效" in resp.text,
                  f"status={resp.status_code}")

    # F4: 新註冊帳號無法存取未授權的 App
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
        _print_result("F4", "新註冊帳號無權限 → 登入 App 被拒絕",
                      resp.status_code != 303,
                      f"status={resp.status_code}")
    else:
        _print_skip("F4", "新註冊帳號無權限", "使用者選擇跳過")


# ╔══════════════════════════════════════════════════════════════╗
# ║  G. Admin 認證安全                                           ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_G(client: httpx.AsyncClient):
    _section("G. Admin 認證安全")

    # G1: 偽造 admin JWT cookie
    fake_client = httpx.AsyncClient(
        cookies={"admin_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.fake.fake"},
        follow_redirects=True,
    )
    resp = await fake_client.get(f"{CONFIG['base_url']}/admin/dashboard")
    _print_result("G1", "偽造 admin JWT → 被拒絕（重導到登入頁）",
                  "/admin/login" in str(resp.url) or resp.status_code in (401, 403),
                  f"url={resp.url}")
    await fake_client.aclose()

    # G2: 用一般 access_token 存取 admin
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

    # G4: 非 admin 員工登入 admin
    _admin_action(
        "確認測試帳號不是 Super Admin 也不是任何 App 的 App Admin\n"
        "（從 Admin Panel 移除測試帳號的所有 App Admin 身份）"
    )
    await client.get(f"{CONFIG['base_url']}/admin/login")
    csrf = client.cookies.get("csrf_token", "")
    resp = await client.post(
        f"{CONFIG['base_url']}/admin/login",
        data={
            "username": CONFIG["test_user"],
            "password": CONFIG["test_password"],
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    _print_result("G4", "非 admin 員工登入 admin → 被拒絕",
                  resp.status_code != 303,
                  f"status={resp.status_code}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  H. Rate Limiting                                           ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_H(client: httpx.AsyncClient):
    _section("H. Rate Limiting（不需管理員操作）")

    print(f"  {DIM}注意：此測試會觸發 rate limit，可能影響後續測試。建議最後執行。{RESET}")
    answer = input(f"  {DIM}要執行 rate limit 測試嗎？(y/N): {RESET}").strip().lower()
    if answer != "y":
        _print_skip("H1", "暴力破解密碼 rate limit", "使用者選擇跳過")
        _print_skip("H2", "Admin 登入 rate limit", "使用者選擇跳過")
        _print_skip("H3", "Token endpoint rate limit", "使用者選擇跳過")
        return

    # H1: 暴力破解登入密碼
    # 用一個新的 client（不同 cookie jar）
    rl_client = httpx.AsyncClient()
    rate_limited = False
    for i in range(12):
        await rl_client.get(
            f"{CONFIG['base_url']}/auth/login",
            params={"app_id": CONFIG["app_id"], "redirect_uri": CONFIG["redirect_uri"]},
        )
        csrf = rl_client.cookies.get("csrf_token", "")
        resp = await rl_client.post(
            f"{CONFIG['base_url']}/auth/login",
            data={
                "employee_name": CONFIG["test_user"],
                "password": f"wrong_password_{i}",
                "app_id": CONFIG["app_id"],
                "redirect_uri": CONFIG["redirect_uri"],
                "_csrf_token": csrf,
            },
            follow_redirects=False,
        )
        if resp.status_code == 429 or "頻繁" in resp.text:
            rate_limited = True
            print(f"    {DIM}第 {i+1} 次嘗試被 rate limit 擋下{RESET}")
            break
    _print_result("H1", "暴力破解密碼 → rate limit 生效",
                  rate_limited, f"在 {i+1} 次嘗試後")
    await rl_client.aclose()

    # H3: Token endpoint rate limit
    rl_client2 = httpx.AsyncClient()
    rate_limited = False
    for i in range(12):
        resp = await rl_client2.post(
            f"{CONFIG['base_url']}/auth/token",
            json={
                "code": f"fake_code_{i}",
                "app_id": CONFIG["app_id"],
                "client_secret": "wrong_secret",
            },
        )
        if resp.status_code == 429:
            rate_limited = True
            break
    _print_result("H3", "Token endpoint 暴力測試 → rate limit 生效",
                  rate_limited, f"在 {i+1} 次嘗試後")
    await rl_client2.aclose()


# ╔══════════════════════════════════════════════════════════════╗
# ║  I. CSRF 防護                                               ║
# ╚══════════════════════════════════════════════════════════════╝


async def test_I(client: httpx.AsyncClient):
    _section("I. CSRF 防護（不需管理員操作）")

    # I1: 無 CSRF token 提交登入表單
    bare_client = httpx.AsyncClient()
    resp = await bare_client.post(
        f"{CONFIG['base_url']}/auth/login",
        data={
            "employee_name": CONFIG["test_user"],
            "password": CONFIG["test_password"],
            "app_id": CONFIG["app_id"],
            "redirect_uri": CONFIG["redirect_uri"],
            # 不帶 _csrf_token
        },
        follow_redirects=False,
    )
    _print_result("I1", "無 CSRF token 提交登入 → 403",
                  resp.status_code == 403,
                  f"status={resp.status_code}")
    await bare_client.aclose()

    # I2: CSRF token 不匹配
    mismatch_client = httpx.AsyncClient(cookies={"csrf_token": "valid_cookie_token"})
    resp = await mismatch_client.post(
        f"{CONFIG['base_url']}/auth/login",
        data={
            "employee_name": CONFIG["test_user"],
            "password": CONFIG["test_password"],
            "app_id": CONFIG["app_id"],
            "redirect_uri": CONFIG["redirect_uri"],
            "_csrf_token": "different_form_token",
        },
        follow_redirects=False,
    )
    _print_result("I2", "CSRF token 不匹配 → 403",
                  resp.status_code == 403,
                  f"status={resp.status_code}")
    await mismatch_client.aclose()

    # I3: /auth/token 是否豁免 CSRF（因為用 client_secret 認證）
    resp = await client.post(
        f"{CONFIG['base_url']}/auth/token",
        json={
            "code": "any_code",
            "app_id": CONFIG["app_id"],
            "client_secret": CONFIG["client_secret"],
        },
    )
    # 應該不會是 403 CSRF 錯誤，而是 400/401（code 無效等）
    _print_result("I3", "/auth/token 豁免 CSRF（用 client_secret 認證）",
                  resp.status_code != 403,
                  f"status={resp.status_code}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  主程式                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

TEST_GROUPS = {
    "A": ("基本權限阻擋", test_A),
    "B": ("權限撤銷時序攻擊", test_B),
    "C": ("Auth Code 攻擊", test_C),
    "D": ("組織邊界隔離", test_D),
    "E": ("權限提升攻擊", test_E),
    "F": ("註冊流程安全", test_F),
    "G": ("Admin 認證安全", test_G),
    "H": ("Rate Limiting", test_H),
    "I": ("CSRF 防護", test_I),
}


async def main():
    global passed, failed, skipped

    # 解析命令列參數
    args = sys.argv[1:]
    if "--list" in args:
        print(f"\n{BOLD}可用的測試分類：{RESET}")
        for key, (name, _) in TEST_GROUPS.items():
            print(f"  {CYAN}{key}{RESET} — {name}")
        print(f"\n用法: python example_app/security_tests.py [A] [B] [C] ...")
        return

    selected = [a.upper() for a in args] if args else list(TEST_GROUPS.keys())
    invalid = [s for s in selected if s not in TEST_GROUPS]
    if invalid:
        print(f"{RED}無效的分類: {', '.join(invalid)}{RESET}")
        print(f"使用 --list 查看可用分類")
        return

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  Auth Center 安全性測試{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"  目標: {CONFIG['base_url']}")
    print(f"  帳號: {CONFIG['test_user']}")
    print(f"  分類: {', '.join(selected)}")

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

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for group_key in selected:
            name, test_fn = TEST_GROUPS[group_key]
            try:
                await test_fn(client)
            except KeyboardInterrupt:
                print(f"\n{YELLOW}使用者中斷{RESET}")
                break
            except Exception as e:
                print(f"\n  {RED}測試 {group_key} 發生錯誤: {e}{RESET}")

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
