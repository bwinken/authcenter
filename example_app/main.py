"""
AI App 整合 Auth Center 完整範例
=================================

這是一個完整可運行的 FastAPI AI App 範例，示範如何整合 Auth Center SSO。

功能：
    1. /docs Swagger UI 支援 — 點右上角 Authorize 輸入帳密即可取得 Token
    2. 瀏覽器 Cookie 認證 — 正式使用時透過 OAuth2 redirect flow
    3. 權限檢查 — 依據 JWT 中的 scopes（由 level 自動映射）控制 API 存取
    4. 簡易前端 — 顯示使用者資訊、JWT 內容、權限測試

啟動方式：
    1. 確保 Auth Center 已啟動 (http://localhost:8000)
    2. 確保 apps.yaml 中已註冊此 App
    3. 複製 Auth Center 的 public.pem 到本專案的 keys/ 目錄
    4. 設定 .env（見下方）
    5. 執行: fastapi dev example_app/main.py --port 8001

.env 範例：
    AUTH_CENTER_BASE_URL=http://localhost:8000
    APP_ID=ai_chat_app
    CLIENT_SECRET=chat_secret_123
    REDIRECT_URI=http://localhost:8001/auth/callback
    PUBLIC_KEY_PATH=./keys/public.pem
"""

import html
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse, parse_qs

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel

load_dotenv()

# ╔══════════════════════════════════════════════════════════════╗
# ║  設定                                                       ║
# ╚══════════════════════════════════════════════════════════════╝

AUTH_CENTER_BASE_URL = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")
APP_ID = os.getenv("APP_ID", "ai_chat_app")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "chat_secret_123")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8001/auth/callback")
PUBLIC_KEY_PATH = os.getenv("PUBLIC_KEY_PATH", "./keys/public.pem")

ALGORITHM = "RS256"

LOGIN_URL = f"{AUTH_CENTER_BASE_URL}/auth/login?app_id={APP_ID}&redirect_uri={REDIRECT_URI}"


@lru_cache
def _load_public_key() -> str:
    """讀取 Auth Center 的 RS256 公鑰（僅第一次讀檔，之後快取）。"""
    return Path(PUBLIC_KEY_PATH).read_text()


# ╔══════════════════════════════════════════════════════════════╗
# ║  HTML 模板                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

STYLE = """
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #f5f7fa; color: #333; line-height: 1.6; }
    .navbar { background: #1a1a2e; color: #fff; padding: 12px 24px;
              display: flex; justify-content: space-between; align-items: center; }
    .navbar .brand { font-size: 18px; font-weight: 600; }
    .navbar a { color: #a8b2d1; text-decoration: none; margin-left: 16px; font-size: 14px; }
    .navbar a:hover { color: #fff; }
    .navbar .active { color: #64ffda; }
    .container { max-width: 900px; margin: 32px auto; padding: 0 24px; }
    .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 24px; margin-bottom: 20px; }
    .card h2 { font-size: 18px; margin-bottom: 12px; color: #1a1a2e; }
    .card h3 { font-size: 15px; margin-bottom: 8px; color: #555; }
    .info-grid { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; font-size: 14px; }
    .info-grid .label { color: #888; font-weight: 500; }
    .info-grid .value { color: #333; word-break: break-all; }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
             font-size: 12px; font-weight: 600; margin-right: 4px; }
    .badge-read { background: #e8f5e9; color: #2e7d32; }
    .badge-write { background: #e3f2fd; color: #1565c0; }
    .badge-admin { background: #fff3e0; color: #e65100; }
    .test-section { margin-top: 16px; }
    .test-row { display: flex; align-items: center; gap: 12px; padding: 12px 0;
                border-bottom: 1px solid #f0f0f0; }
    .test-row:last-child { border-bottom: none; }
    .test-label { flex: 1; }
    .test-label .endpoint { font-weight: 600; font-size: 14px; }
    .test-label .desc { font-size: 12px; color: #888; margin-top: 2px; }
    .test-label .required { font-size: 11px; color: #aaa; }
    .btn { display: inline-block; padding: 6px 16px; border-radius: 6px;
           font-size: 13px; font-weight: 500; text-decoration: none; cursor: pointer;
           border: none; transition: all 0.15s; }
    .btn-primary { background: #1a73e8; color: #fff; }
    .btn-primary:hover { background: #1557b0; }
    .btn-success { background: #2e7d32; color: #fff; }
    .btn-success:hover { background: #1b5e20; }
    .btn-warning { background: #e65100; color: #fff; }
    .btn-warning:hover { background: #bf360c; }
    .btn-outline { background: transparent; border: 1px solid #ddd; color: #555; }
    .btn-outline:hover { background: #f5f5f5; }
    .btn-danger { background: #c62828; color: #fff; }
    .btn-danger:hover { background: #b71c1c; }
    .btn-lg { padding: 12px 32px; font-size: 16px; border-radius: 8px; }
    .result-box { background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px;
                  padding: 16px; margin-top: 12px; font-family: 'Cascadia Code', 'Fira Code',
                  monospace; font-size: 13px; white-space: pre-wrap; word-break: break-all;
                  display: none; }
    .result-box.show { display: block; }
    .result-box.error { border-color: #ffcdd2; background: #fff5f5; color: #c62828; }
    .result-box.success { border-color: #c8e6c9; background: #f1f8e9; }
    .hero { text-align: center; padding: 60px 24px; }
    .hero h1 { font-size: 32px; color: #1a1a2e; margin-bottom: 12px; }
    .hero p { font-size: 16px; color: #666; margin-bottom: 32px; }
    .jwt-raw { background: #263238; color: #aed581; border-radius: 6px;
               padding: 16px; font-family: monospace; font-size: 12px;
               white-space: pre-wrap; word-break: break-all; max-height: 200px;
               overflow-y: auto; margin-top: 8px; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
    .status-dot.online { background: #4caf50; }
    .footer { text-align: center; padding: 24px; color: #aaa; font-size: 12px; }
</style>
"""

FETCH_SCRIPT = """
<script>
async function testEndpoint(btn, method, url) {
    const row = btn.closest('.test-row');
    let box = row.querySelector('.result-box');
    if (!box) {
        box = document.createElement('div');
        box.className = 'result-box';
        row.appendChild(box);
    }
    box.className = 'result-box show';
    box.textContent = 'Loading...';
    try {
        const resp = await fetch(url, { method, credentials: 'same-origin' });
        const data = await resp.json();
        box.className = 'result-box show ' + (resp.ok ? 'success' : 'error');
        box.textContent = resp.status + ' ' + resp.statusText + '\\n' + JSON.stringify(data, null, 2);
    } catch (e) {
        box.className = 'result-box show error';
        box.textContent = 'Request failed: ' + e.message;
    }
}
</script>
"""


def _navbar(active: str = "") -> str:
    def link(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if active == key else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""
    <nav class="navbar">
        <span class="brand">AI Chat App</span>
        <div>
            {link("/", "Dashboard", "home")}
            {link("/jwt", "JWT Details", "jwt")}
            {link("/docs", "API Docs", "docs")}
            {link("/logout", "Logout", "")}
        </div>
    </nav>
    """


def _landing_page() -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AI Chat App - Login</title>{STYLE}</head>
<body>
    <nav class="navbar">
        <span class="brand">AI Chat App</span>
        <div><a href="{html.escape(LOGIN_URL)}">Login</a></div>
    </nav>
    <div class="container">
        <div class="hero">
            <h1>AI Chat App</h1>
            <p>Auth Center SSO 整合測試應用程式</p>
            <a href="{html.escape(LOGIN_URL)}" class="btn btn-primary btn-lg">
                Login with Auth Center
            </a>
            <p style="margin-top: 16px; font-size: 13px; color: #999;">
                Auth Center: {html.escape(AUTH_CENTER_BASE_URL)}<br>
                App ID: {html.escape(APP_ID)}
            </p>
        </div>
    </div>
    <div class="footer">Example App for Auth Center OAuth2 Testing</div>
</body></html>"""


def _dashboard_page(user: dict) -> str:
    sub = html.escape(user.get("sub", ""))
    org = html.escape(user.get("org_id", user.get("dept", "N/A")))
    scopes = user.get("scopes", [])
    iat = user.get("iat", 0)
    exp = user.get("exp", 0)
    aud = html.escape(user.get("aud", ""))

    iat_str = datetime.fromtimestamp(iat).strftime("%Y-%m-%d %H:%M:%S") if iat else "N/A"
    exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S") if exp else "N/A"

    scope_badges = ""
    for s in scopes:
        cls = f"badge-{s}" if s in ("read", "write", "admin") else ""
        scope_badges += f'<span class="badge {cls}">{html.escape(s)}</span>'

    has_read = "read" in scopes
    has_write = "write" in scopes
    has_admin = "admin" in scopes

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dashboard - AI Chat App</title>{STYLE}{FETCH_SCRIPT}</head>
<body>
    {_navbar("home")}
    <div class="container">
        <div class="card">
            <h2><span class="status-dot online"></span> Logged In</h2>
            <div class="info-grid">
                <span class="label">Employee</span>
                <span class="value">{sub}</span>
                <span class="label">Organization</span>
                <span class="value">{org}</span>
                <span class="label">Scopes</span>
                <span class="value">{scope_badges or '<span style="color:#ccc">none</span>'}</span>
                <span class="label">Audience</span>
                <span class="value">{aud}</span>
                <span class="label">Issued At</span>
                <span class="value">{iat_str}</span>
                <span class="label">Expires At</span>
                <span class="value">{exp_str}</span>
            </div>
        </div>

        <div class="card">
            <h2>API Endpoint Test</h2>
            <p style="font-size: 13px; color: #888; margin-bottom: 12px;">
                Click each button to test the API with your current JWT Cookie.
            </p>
            <div class="test-section">
                <div class="test-row">
                    <div class="test-label">
                        <div class="endpoint">GET /api/me</div>
                        <div class="desc">Current user info</div>
                        <div class="required">No scope required</div>
                    </div>
                    <button class="btn btn-primary" onclick="testEndpoint(this,'GET','/api/me')">Test</button>
                </div>
                <div class="test-row">
                    <div class="test-label">
                        <div class="endpoint">GET /api/data</div>
                        <div class="desc">Read protected data</div>
                        <div class="required">Requires: <span class="badge badge-read">read</span></div>
                    </div>
                    <button class="btn btn-success" onclick="testEndpoint(this,'GET','/api/data')"
                        {"" if has_read else 'style="opacity:0.5"'}>Test</button>
                </div>
                <div class="test-row">
                    <div class="test-label">
                        <div class="endpoint">POST /api/data</div>
                        <div class="desc">Create data (write)</div>
                        <div class="required">Requires: <span class="badge badge-read">read</span> <span class="badge badge-write">write</span></div>
                    </div>
                    <button class="btn btn-primary" onclick="testEndpoint(this,'POST','/api/data')"
                        {"" if has_write else 'style="opacity:0.5"'}>Test</button>
                </div>
                <div class="test-row">
                    <div class="test-label">
                        <div class="endpoint">GET /api/admin</div>
                        <div class="desc">Admin-only resource</div>
                        <div class="required">Requires: <span class="badge badge-read">read</span> <span class="badge badge-admin">admin</span></div>
                    </div>
                    <button class="btn btn-warning" onclick="testEndpoint(this,'GET','/api/admin')"
                        {"" if has_admin else 'style="opacity:0.5"'}>Test</button>
                </div>
            </div>
        </div>
    </div>
    <div class="footer">Example App for Auth Center OAuth2 Testing</div>
</body></html>"""


def _jwt_detail_page(user: dict, raw_token: str) -> str:
    payload_json = json.dumps(user, indent=2, ensure_ascii=False, default=str)

    # Split JWT parts for display
    parts = raw_token.split(".")
    header_b64 = parts[0] if len(parts) > 0 else ""
    payload_b64 = parts[1] if len(parts) > 1 else ""
    sig_b64 = parts[2] if len(parts) > 2 else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>JWT Details - AI Chat App</title>{STYLE}</head>
<body>
    {_navbar("jwt")}
    <div class="container">
        <div class="card">
            <h2>JWT Payload (Decoded)</h2>
            <div class="jwt-raw">{html.escape(payload_json)}</div>
        </div>
        <div class="card">
            <h2>Raw JWT Token</h2>
            <h3>Header</h3>
            <div class="jwt-raw" style="color:#81d4fa">{html.escape(header_b64)}</div>
            <h3 style="margin-top:12px">Payload</h3>
            <div class="jwt-raw" style="color:#aed581">{html.escape(payload_b64)}</div>
            <h3 style="margin-top:12px">Signature</h3>
            <div class="jwt-raw" style="color:#ef9a9a">{html.escape(sig_b64)}</div>
        </div>
    </div>
    <div class="footer">Example App for Auth Center OAuth2 Testing</div>
</body></html>"""


# ╔══════════════════════════════════════════════════════════════╗
# ║  FastAPI App（含 httpx 連線池生命週期管理）                   ║
# ╚══════════════════════════════════════════════════════════════╝

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    yield
    await app.state.http_client.aclose()


app = FastAPI(
    title="AI Chat App（範例）",
    description="示範如何整合 Auth Center SSO",
    version="1.0.0",
    lifespan=lifespan,
)


# ╔══════════════════════════════════════════════════════════════╗
# ║  Swagger /docs 的 OAuth2 支援                               ║
# ║                                                              ║
# ║  讓開發者在 /docs 右上角 Authorize 輸入帳密，自動取得 Token  ║
# ╚══════════════════════════════════════════════════════════════╝

# 這會在 /docs 顯示 Authorize 按鈕（鎖頭圖示）
# tokenUrl 指向下方的 /token endpoint
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int


@app.post("/token", response_model=TokenResponse, tags=["auth"])
async def login_for_swagger(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    """供 Swagger /docs 使用的 Token 端點。

    在 /docs 右上角 Authorize 輸入 Auth Center 的帳密，
    此端點會自動向 Auth Center 完成 login -> code -> token 交換，
    回傳 JWT 讓 Swagger 記住。

    注意：此端點僅建議在開發環境使用。正式環境應走瀏覽器 redirect flow。
    """
    client: httpx.AsyncClient = request.app.state.http_client

    # 向 Auth Center 提交登入（/auth/login 已豁免 CSRF，不需帶 token）
    login_resp = await client.post(
        f"{AUTH_CENTER_BASE_URL}/auth/login",
        data={
            "employee_name": form_data.username,
            "password": form_data.password,
            "app_id": APP_ID,
            "redirect_uri": REDIRECT_URI,
        },
        follow_redirects=False,  # 不自動跟隨 redirect，我們要取 code
    )

    # Auth Center 登入成功會回 303，Location 帶 ?code=xxx
    if login_resp.status_code not in (302, 303):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤，或無權存取此 App。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 從 redirect URL 中取出 authorization code
    location = login_resp.headers.get("location", "")
    code = parse_qs(urlparse(location).query).get("code", [None])[0]

    if not code:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登入失敗：未取得授權碼。可能是帳號未註冊或權限不足。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Step 3: 用 code + client_secret 換取 JWT
    token_resp = await client.post(
        f"{AUTH_CENTER_BASE_URL}/auth/token",
        json={"code": code, "app_id": APP_ID, "client_secret": CLIENT_SECRET},
    )

    if token_resp.status_code != 200:
        error = token_resp.json().get("error", "unknown_error")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token 交換失敗：{error}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    data = token_resp.json()
    return TokenResponse(
        access_token=data["access_token"],
        token_type="bearer",
        expires_in=data["expires_in"],
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║  OAuth2 Redirect Flow（瀏覽器正式流程）                      ║
# ╚══════════════════════════════════════════════════════════════╝

@app.get("/auth/callback", tags=["auth"])
async def auth_callback(request: Request, code: str = Query(...)):
    """OAuth2 callback — 接收 Auth Center 回傳的 code，換取 JWT 存入 Cookie。"""
    client: httpx.AsyncClient = request.app.state.http_client
    resp = await client.post(
        f"{AUTH_CENTER_BASE_URL}/auth/token",
        json={"code": code, "app_id": APP_ID, "client_secret": CLIENT_SECRET},
    )

    if resp.status_code != 200:
        error = resp.json().get("error", "unknown")
        if error == "invalid_grant":
            return RedirectResponse(LOGIN_URL)
        raise HTTPException(500, f"Token 交換失敗：{error}")

    data = resp.json()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key="access_token",
        value=data["access_token"],
        httponly=True,
        secure=False,      # 本地開發用 HTTP，正式環境改 True
        samesite="lax",
        max_age=data["expires_in"],
    )
    return response


# ╔══════════════════════════════════════════════════════════════╗
# ║  JWT 驗證 — 同時支援 Bearer Token 和 Cookie                 ║
# ╚══════════════════════════════════════════════════════════════╝

def _decode_jwt(token: str) -> dict | None:
    """解碼並驗證 JWT，失敗回傳 None。"""
    try:
        return jwt.decode(token, _load_public_key(), algorithms=[ALGORITHM], audience=APP_ID)
    except jwt.PyJWTError:
        return None


def get_current_user(
    bearer_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    access_token: str | None = Cookie(default=None),
) -> dict:
    """從 Bearer Token（/docs 用）或 Cookie（瀏覽器用）取得並驗證 JWT。

    優先順序：Bearer Token > Cookie
    回傳 JWT payload dict，包含 sub, name, org_id, dept, scopes, aud 等欄位。
    """
    token = bearer_token or access_token

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登入。請先透過 Auth Center 登入。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            token,
            _load_public_key(),
            algorithms=[ALGORITHM],
            audience=APP_ID,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已過期，請重新登入。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此 Token 不是簽給本 App 的。",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token 驗證失敗：{e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def require_scopes(required: list[str]):
    """Dependency factory：檢查使用者是否擁有所需的 scopes。

    用法：
        @app.get("/admin")
        async def admin_page(user=Depends(require_scopes(["read", "admin"]))):
            ...
    """
    def _checker(user: dict = Depends(get_current_user)) -> dict:
        user_scopes = set(user.get("scopes", []))
        missing = set(required) - user_scopes
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"權限不足，缺少：{missing}",
            )
        return user

    return _checker


# ╔══════════════════════════════════════════════════════════════╗
# ║  前端頁面                                                    ║
# ╚══════════════════════════════════════════════════════════════╝

@app.get("/", response_class=HTMLResponse, tags=["pages"])
async def home(
    bearer_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    access_token: str | None = Cookie(default=None),
):
    """首頁 — 未登入顯示 Landing Page，已登入顯示 Dashboard。"""
    token = bearer_token or access_token
    user = _decode_jwt(token) if token else None

    if user is None:
        return HTMLResponse(_landing_page())

    return HTMLResponse(_dashboard_page(user))


@app.get("/jwt", response_class=HTMLResponse, tags=["pages"])
async def jwt_page(
    bearer_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    access_token: str | None = Cookie(default=None),
):
    """JWT 詳細資訊頁 — 顯示 decoded payload 與 raw token。"""
    token = bearer_token or access_token
    if not token:
        return RedirectResponse(LOGIN_URL)

    user = _decode_jwt(token)
    if user is None:
        return RedirectResponse(LOGIN_URL)

    return HTMLResponse(_jwt_detail_page(user, token))


# ╔══════════════════════════════════════════════════════════════╗
# ║  範例 API 路由                                               ║
# ╚══════════════════════════════════════════════════════════════╝

@app.get("/api/me", tags=["api"])
async def get_my_info(user: dict = Depends(get_current_user)):
    """取得目前登入使用者的資訊。"""
    return {
        "employee_name": user["sub"],
        "name": user.get("name"),
        "org_id": user.get("org_id", user.get("dept")),
        "scopes": user.get("scopes", []),
    }


@app.get("/api/data", tags=["api"])
async def get_data(user: dict = Depends(require_scopes(["read"]))):
    """取得資料（需要 read 權限）。"""
    return {
        "message": f"Hello {user['sub']}，這是受保護的資料。",
        "items": [
            {"id": 1, "name": "Item A"},
            {"id": 2, "name": "Item B"},
        ],
    }


@app.post("/api/data", tags=["api"])
async def create_data(
    user: dict = Depends(require_scopes(["read", "write"])),
):
    """新增資料（需要 read + write 權限）。"""
    return {"message": "資料新增成功", "created_by": user["sub"]}


@app.get("/api/admin", tags=["api"])
async def admin_panel(user: dict = Depends(require_scopes(["read", "admin"]))):
    """管理頁面（需要 read + admin 權限）。"""
    return {
        "message": f"{user['sub']} 您好，這是管理頁面。",
        "admin": True,
    }


@app.get("/logout", tags=["auth"])
async def logout():
    """登出 — 清除 Cookie 並導回首頁。"""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response
