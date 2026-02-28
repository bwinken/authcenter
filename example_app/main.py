"""
AI App 整合 Auth Center 完整範例
=================================

這是一個完整可運行的 FastAPI AI App 範例，示範如何整合 Auth Center SSO。

功能：
    1. /docs Swagger UI 支援 — 點右上角 Authorize 輸入帳密即可取得 Token
    2. 瀏覽器 Cookie 認證 — 正式使用時透過 OAuth2 redirect flow
    3. 權限檢查 — 依據 JWT 中的 scopes 控制 API 存取

啟動方式：
    1. 確保 Auth Center 已啟動 (http://localhost:8000)
    2. 確保 apps.yaml 中已註冊此 App
    3. 複製 Auth Center 的 public.pem 到本專案的 keys/ 目錄
    4. 設定 .env（見下方）
    5. 執行: fastapi dev example_app/main.py --port 8001

.env 範例：
    AUTH_CENTER_URL=http://localhost:8000
    APP_ID=ai_chat_app
    CLIENT_SECRET=chat_secret_123
    REDIRECT_URI=http://localhost:8001/auth/callback
    PUBLIC_KEY_PATH=./keys/public.pem
"""

import os
from pathlib import Path
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse, parse_qs

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel

load_dotenv()

# ╔══════════════════════════════════════════════════════════════╗
# ║  設定                                                       ║
# ╚══════════════════════════════════════════════════════════════╝

AUTH_CENTER_URL = os.getenv("AUTH_CENTER_URL", "http://localhost:8000")
APP_ID = os.getenv("APP_ID", "ai_chat_app")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "chat_secret_123")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8001/auth/callback")
PUBLIC_KEY_PATH = os.getenv("PUBLIC_KEY_PATH", "./keys/public.pem")

ALGORITHM = "RS256"


@lru_cache
def _load_public_key() -> str:
    """讀取 Auth Center 的 RS256 公鑰（僅第一次讀檔，之後快取）。"""
    return Path(PUBLIC_KEY_PATH).read_text()


# ╔══════════════════════════════════════════════════════════════╗
# ║  FastAPI App                                                ║
# ╚══════════════════════════════════════════════════════════════╝

app = FastAPI(
    title="AI Chat App（範例）",
    description="示範如何整合 Auth Center SSO",
    version="1.0.0",
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
async def login_for_swagger(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """供 Swagger /docs 使用的 Token 端點。

    在 /docs 右上角 Authorize 輸入 Auth Center 的帳密，
    此端點會自動向 Auth Center 完成 login → code → token 交換，
    回傳 JWT 讓 Swagger 記住。

    注意：此端點僅建議在開發環境使用。正式環境應走瀏覽器 redirect flow。
    """
    async with httpx.AsyncClient() as client:
        # Step 1: 向 Auth Center 提交登入（模擬表單 POST）
        login_resp = await client.post(
            f"{AUTH_CENTER_URL}/auth/login",
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
        parsed = urlparse(location)
        code = parse_qs(parsed.query).get("code", [None])[0]

        if not code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登入失敗：未取得授權碼。可能是帳號未註冊或權限不足。",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Step 2: 用 code + client_secret 換取 JWT
        token_resp = await client.post(
            f"{AUTH_CENTER_URL}/auth/token",
            json={
                "code": code,
                "app_id": APP_ID,
                "client_secret": CLIENT_SECRET,
            },
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
        expires_in=data.get("expires_in", 43200),
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║  OAuth2 Redirect Flow（瀏覽器正式流程）                      ║
# ╚══════════════════════════════════════════════════════════════╝

@app.get("/auth/callback", tags=["auth"])
async def auth_callback(code: str = Query(...)):
    """OAuth2 callback — 接收 Auth Center 回傳的 code，換取 JWT 存入 Cookie。"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{AUTH_CENTER_URL}/auth/token",
            json={
                "code": code,
                "app_id": APP_ID,
                "client_secret": CLIENT_SECRET,
            },
        )

    if resp.status_code != 200:
        error = resp.json().get("error", "unknown")
        if error == "invalid_grant":
            # Code 過期或已使用 → 重新登入
            return RedirectResponse(
                f"{AUTH_CENTER_URL}/auth/login?app_id={APP_ID}&redirect_uri={REDIRECT_URI}"
            )
        raise HTTPException(500, f"Token 交換失敗：{error}")

    data = resp.json()

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key="access_token",
        value=data["access_token"],
        httponly=True,
        secure=False,      # 本地開發用 HTTP，正式環境改 True
        samesite="lax",
        max_age=43200,      # 12 小時
    )
    return response


# ╔══════════════════════════════════════════════════════════════╗
# ║  JWT 驗證 — 同時支援 Bearer Token 和 Cookie                 ║
# ╚══════════════════════════════════════════════════════════════╝

def get_current_user(
    bearer_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    access_token: str | None = Cookie(default=None),
) -> dict:
    """從 Bearer Token（/docs 用）或 Cookie（瀏覽器用）取得並驗證 JWT。

    優先順序：Bearer Token > Cookie
    回傳 JWT payload dict，包含 sub, name, dept, scopes, aud 等欄位。
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
# ║  範例 API 路由                                               ║
# ╚══════════════════════════════════════════════════════════════╝

@app.get("/", response_class=HTMLResponse, tags=["pages"])
async def home(
    bearer_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    access_token: str | None = Cookie(default=None),
):
    """首頁 — 未登入導向 Auth Center，已登入顯示歡迎頁。"""
    token = bearer_token or access_token
    if not token:
        return RedirectResponse(
            f"{AUTH_CENTER_URL}/auth/login?app_id={APP_ID}&redirect_uri={REDIRECT_URI}"
        )

    try:
        user = jwt.decode(token, _load_public_key(), algorithms=[ALGORITHM], audience=APP_ID)
    except jwt.PyJWTError:
        return RedirectResponse(
            f"{AUTH_CENTER_URL}/auth/login?app_id={APP_ID}&redirect_uri={REDIRECT_URI}"
        )

    scopes = ", ".join(user.get("scopes", []))
    return f"""
    <html>
    <body style="font-family: sans-serif; max-width: 600px; margin: 40px auto;">
        <h1>Welcome, {user.get('name', user['sub'])}!</h1>
        <p>Department: {user.get('dept', 'N/A')}</p>
        <p>Scopes: {scopes}</p>
        <hr>
        <p><a href="/docs">Open API Docs (/docs)</a></p>
        <p><a href="/api/me">My Info (JSON)</a></p>
        <p><a href="/api/data">Sample Data (requires read)</a></p>
        <p><a href="/api/admin">Admin Panel (requires admin)</a></p>
    </body>
    </html>
    """


@app.get("/api/me", tags=["api"])
async def get_my_info(user: dict = Depends(get_current_user)):
    """取得目前登入使用者的資訊。"""
    return {
        "employee_name": user["sub"],
        "name": user.get("name"),
        "department": user.get("dept"),
        "scopes": user.get("scopes", []),
    }


@app.get("/api/data", tags=["api"])
async def get_data(user: dict = Depends(require_scopes(["read"]))):
    """取得資料（需要 read 權限）。"""
    return {
        "message": f"Hello {user.get('name', user['sub'])}，這是受保護的資料。",
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
        "message": f"{user.get('name', user['sub'])} 您好，這是管理頁面。",
        "admin": True,
    }


@app.get("/logout", tags=["auth"])
async def logout():
    """登出 — 清除 Cookie 並導回首頁。"""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response
