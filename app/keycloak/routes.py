"""Keycloak 登入流程：把使用者導去 Keycloak 驗證身分，回來後接回原本的 auth code 流程。

    /auth/login 或 /oidc/authorize
        → GET /auth/keycloak/start      建立一次性 state，導向 Keycloak
        → Keycloak 登入
        → GET /auth/keycloak/callback   驗證 id_token → 對應 nt_account → 查員工主檔
                                        → 檢查 App 權限 → 發 auth code → 導回 App

哪些 App 走這條路由 apps.yaml 的 auth_provider 決定（local / keycloak / both）。
"""

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.config import (
    AUTH_PROVIDER_BOTH,
    AUTH_PROVIDER_KEYCLOAK,
    AUTH_PROVIDER_LOCAL,
    get_app_auth_provider,
    get_settings,
    load_registered_apps,
)
from app.database import get_mssql_session, get_sqlite_session
from app.keycloak import client
from app.keycloak.client import KeycloakError, is_keycloak_enabled

router = APIRouter(prefix="/auth/keycloak", tags=["keycloak"])
templates: Jinja2Templates | None = None

FLOW_AUTH = "auth"   # 從 /auth/login 發起
FLOW_OIDC = "oidc"   # 從 /oidc/authorize 發起（導回時要帶 state，auth code 要存 nonce）

STATE_TTL = 600                # 使用者在 Keycloak 登入頁最多停留 10 分鐘
BINDING_COOKIE = "kc_login"    # 把 state 綁定在發起登入的瀏覽器，防止 login CSRF
MAX_PARAM_LENGTH = 4096        # App 帶來的 state / nonce / scope 長度上限

KEYCLOAK_ONLY_MSG = "此應用程式僅支援使用{name}登入。"
LOGIN_UNAVAILABLE_MSG = "此應用程式的登入方式尚未設定完成，請聯繫管理員。"


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


# ─── 登入頁選項 ──────────────────────────────────────────────

def login_options(
    app_info: dict | None,
    app_id: str,
    redirect_uri: str,
    *,
    flow: str = FLOW_AUTH,
    oidc_state: str = "",
    oidc_nonce: str = "",
    oidc_scope: str = "",
) -> dict:
    """依 App 的 auth_provider 算出登入頁要顯示哪些登入方式。

    回傳可直接併入 login.html context 的 dict：
    - allow_local：是否可用 AuthCenter 本地密碼
    - keycloak_url：發起 Keycloak 登入的網址；App 不允許或 Keycloak 未設定時為 None
    - keycloak_name：使用者看到的名稱（KEYCLOAK_DISPLAY_NAME，預設「公司帳號」）
    """
    provider = get_app_auth_provider(app_info) if app_info else AUTH_PROVIDER_LOCAL
    keycloak_url = None
    if provider in (AUTH_PROVIDER_KEYCLOAK, AUTH_PROVIDER_BOTH) and is_keycloak_enabled():
        params = {"app_id": app_id, "redirect_uri": redirect_uri}
        if flow == FLOW_OIDC:
            params.update(flow=FLOW_OIDC, state=oidc_state, nonce=oidc_nonce, scope=oidc_scope)
        keycloak_url = f"{router.prefix}/start?{urlencode(params)}"
    return {
        "allow_local": provider in (AUTH_PROVIDER_LOCAL, AUTH_PROVIDER_BOTH),
        "keycloak_url": keycloak_url,
        "keycloak_name": get_settings().KEYCLOAK_DISPLAY_NAME,
    }


def keycloak_only_message() -> str:
    return KEYCLOAK_ONLY_MSG.format(name=get_settings().KEYCLOAK_DISPLAY_NAME)


def _login_page(request: Request, pending: dict, app_info: dict | None, error: str, fatal: bool = False):
    """以原本登入頁的樣子顯示錯誤（保留 App 與 OIDC 參數，讓使用者可以直接重試）。"""
    ctx = {
        "request": request,
        "error": error,
        "app_id": pending["app_id"],
        "redirect_uri": pending["redirect_uri"],
        "app_name": app_info.get("name", pending["app_id"]) if app_info else "Unknown",
        "fatal": fatal,
    }
    ctx.update(login_options(
        app_info, pending["app_id"], pending["redirect_uri"],
        flow=pending["flow"], oidc_state=pending["oidc_state"],
        oidc_nonce=pending["oidc_nonce"], oidc_scope=pending["oidc_scope"],
    ))
    if pending["flow"] == FLOW_OIDC:
        ctx.update(
            form_action="/oidc/authorize",
            oidc_state=pending["oidc_state"],
            oidc_nonce=pending["oidc_nonce"],
            oidc_scope=pending["oidc_scope"],
        )
    return templates.TemplateResponse("login.html", ctx)


def _fatal_page(request: Request, error: str):
    """沒有可信的 App 資訊時的錯誤頁（state 無效、逾時等）。"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "app_id": "",
        "redirect_uri": "",
        "app_name": "Unknown",
        "fatal": True,
    })


# ─── 一次性 state（SQLite） ──────────────────────────────────

def _hash_binding(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _create_login_state(sqlite_session: AsyncSession, binding: str, pending: dict) -> dict:
    """建立一筆待完成的 Keycloak 登入。回傳 state / nonce / code_verifier。"""
    now = time.time()
    secrets_ = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_verifier": secrets.token_urlsafe(48),
    }
    # 順手清掉過期的，避免未完成的登入一直累積到下次定期清理
    await sqlite_session.execute(
        text("DELETE FROM keycloak_login_states WHERE expires_at < :now"), {"now": now},
    )
    await sqlite_session.execute(
        text(
            "INSERT INTO keycloak_login_states "
            "(state, binding_hash, nonce, code_verifier, app_id, redirect_uri, "
            " flow, oidc_state, oidc_nonce, oidc_scope, expires_at) "
            "VALUES (:state, :binding_hash, :nonce, :code_verifier, :app_id, :redirect_uri, "
            " :flow, :oidc_state, :oidc_nonce, :oidc_scope, :expires_at)"
        ),
        {
            **secrets_,
            **pending,
            "binding_hash": _hash_binding(binding),
            "expires_at": now + STATE_TTL,
        },
    )
    await sqlite_session.commit()
    return secrets_


async def _consume_login_state(sqlite_session: AsyncSession, state: str) -> dict | None:
    """取出並刪除 state（DELETE ... RETURNING，同一個 state 只能用一次）。"""
    result = await sqlite_session.execute(
        text(
            "DELETE FROM keycloak_login_states WHERE state = :state "
            "RETURNING binding_hash, nonce, code_verifier, app_id, redirect_uri, "
            "flow, oidc_state, oidc_nonce, oidc_scope, expires_at"
        ),
        {"state": state},
    )
    row = result.fetchone()
    await sqlite_session.commit()
    if row is None:
        return None
    return {
        "binding_hash": row[0],
        "nonce": row[1],
        "code_verifier": row[2],
        "app_id": row[3],
        "redirect_uri": row[4],
        "flow": row[5],
        "oidc_state": row[6] or "",
        "oidc_nonce": row[7] or "",
        "oidc_scope": row[8] or "",
        "expires_at": row[9],
    }


# ─── Routes ──────────────────────────────────────────────────

@router.get("/start")
async def keycloak_start(
    request: Request,
    app_id: str = Query(...),
    redirect_uri: str = Query(...),
    flow: str = Query(FLOW_AUTH),
    state: str = Query(""),
    nonce: str = Query(""),
    scope: str = Query("openid"),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """發起 Keycloak 登入：驗證 App 後建立一次性 state，導向 Keycloak。"""
    pending = {
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "flow": FLOW_OIDC if flow == FLOW_OIDC else FLOW_AUTH,
        "oidc_state": state,
        "oidc_nonce": nonce,
        "oidc_scope": scope,
    }
    app_info = load_registered_apps().get(app_id)
    if app_info is None:
        return _login_page(request, pending, None, "未註冊的應用程式。", fatal=True)
    if app_info["redirect_uri"] != redirect_uri:
        return _login_page(
            request, pending, app_info,
            "Redirect URI 不匹配，請聯繫應用程式管理員確認設定。", fatal=True,
        )
    if max(len(state), len(nonce), len(scope)) > MAX_PARAM_LENGTH:
        return _login_page(request, pending, app_info, "登入參數過長，請從 App 重新發起登入。", fatal=True)

    options = login_options(app_info, app_id, redirect_uri)
    if not options["keycloak_url"]:
        # App 不允許 Keycloak，或 Keycloak 尚未設定
        if options["allow_local"]:
            return _login_page(request, pending, app_info, "此應用程式不支援此登入方式。")
        return _login_page(request, pending, app_info, LOGIN_UNAVAILABLE_MSG, fatal=True)

    binding = request.cookies.get(BINDING_COOKIE) or secrets.token_urlsafe(32)
    try:
        # 先確認連得到 Keycloak 再寫入 state
        await client.get_discovery()
        login = await _create_login_state(sqlite_session, binding, pending)
        url = await client.build_authorize_url(
            login["state"], login["nonce"], client.pkce_challenge(login["code_verifier"]),
        )
    except KeycloakError as e:
        logger.error("Keycloak 登入無法發起 (app={}): {}", app_id, e)
        return _login_page(
            request, pending, app_info,
            f"目前無法使用{options['keycloak_name']}登入，請稍後再試或聯繫管理員。",
        )

    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        key=BINDING_COOKIE, value=binding, max_age=STATE_TTL,
        path=router.prefix, httponly=True, samesite="lax",
        secure=get_settings().AUTH_CENTER_BASE_URL.lower().startswith("https://"),
    )
    return response


@router.get("/callback")
async def keycloak_callback(
    request: Request,
    state: str = Query(""),
    code: str = Query(""),
    error: str = Query(""),
    error_description: str = Query(""),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """Keycloak 導回：驗證身分後接回 AuthCenter 的權限檢查與 auth code 流程。"""
    # 1. state 必須存在、未過期，而且是同一個瀏覽器發起的
    pending = await _consume_login_state(sqlite_session, state) if state else None
    binding = request.cookies.get(BINDING_COOKIE, "")
    if (
        pending is None
        or time.time() > pending["expires_at"]
        or not binding
        or not hmac.compare_digest(_hash_binding(binding), pending["binding_hash"])
    ):
        logger.warning("Keycloak callback 被拒絕：state 無效、逾時或瀏覽器不符")
        return _fatal_page(request, "登入逾時或連結已失效，請回到應用程式重新登入。")

    app_id = pending["app_id"]
    redirect_uri = pending["redirect_uri"]

    # 2. 重新確認 App 設定（等待期間 apps.yaml 可能已變更）
    app_info = load_registered_apps().get(app_id)
    if app_info is None or app_info["redirect_uri"] != redirect_uri:
        return _fatal_page(request, "應用程式驗證失敗，請從 App 重新發起登入。")
    if not login_options(app_info, app_id, redirect_uri)["keycloak_url"]:
        return _login_page(request, pending, app_info, "此應用程式不支援此登入方式。")

    # 3. Keycloak 回報錯誤（使用者取消、client 設定錯誤等）
    if error or not code:
        logger.warning(
            "Keycloak 回報錯誤 (app={}): error={} description={}",
            app_id, error or "(無 code)", error_description[:200],
        )
        msg = "您已取消登入。" if error == "access_denied" else "登入未完成，請再試一次。"
        return _login_page(request, pending, app_info, msg)

    # 4. 換 token、驗證 id_token、取出帳號
    try:
        tokens = await client.exchange_code(code, pending["code_verifier"])
        claims = await client.verify_id_token(tokens["id_token"], pending["nonce"])
        employee_name = await client.resolve_username(claims, tokens.get("access_token", ""))
    except KeycloakError as e:
        logger.error("Keycloak 登入驗證失敗 (app={}): {}", app_id, e)
        return _login_page(
            request, pending, app_info,
            "登入驗證失敗，請再試一次；若持續發生請聯繫管理員。",
        )

    # 5. 之後與本地登入相同：查員工主檔 → 檢查 App 權限 → 發 auth code
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        logger.warning(
            "Keycloak 登入失敗：帳號 {} 不在員工主檔 (app={}, sub={})",
            employee_name, app_id, claims.get("sub"),
        )
        return _login_page(
            request, pending, app_info,
            f"帳號（{employee_name}）不在公司員工名單中，請聯繫管理員。",
        )

    allowed, reason, _scopes = await service.check_app_access(sqlite_session, staff, app_info)
    if not allowed:
        return _login_page(request, pending, app_info, reason)

    logger.info("Keycloak 登入成功: {} (app={})", staff.employee_name, app_id)
    if pending["flow"] == FLOW_OIDC:
        auth_code = await service.generate_auth_code(
            sqlite_session, staff.employee_name, app_id, nonce=pending["oidc_nonce"],
        )
        params = {"code": auth_code}
        if pending["oidc_state"]:
            params["state"] = pending["oidc_state"]
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=303)

    auth_code = await service.generate_auth_code(sqlite_session, staff.employee_name, app_id)
    return RedirectResponse(f"{redirect_uri}?code={auth_code}", status_code=303)
