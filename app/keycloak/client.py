"""Keycloak（上游 OIDC 身分提供者）用戶端。

AuthCenter 在這裡是 Keycloak 的 relying party：只把「驗證使用者是誰」交給 Keycloak，
員工主檔比對、App 權限、auth code 與 JWT 仍由 AuthCenter 自己處理。

- discovery / JWKS 以記憶體快取（每個 process 各自一份）
- Authorization Code Flow + PKCE（S256）
- id_token 驗證：簽章（JWKS）、iss、aud、exp、nonce、azp
"""

import asyncio
import base64
import hashlib
import hmac
import ssl
import time
from urllib.parse import urlencode

import certifi
import httpx
import jwt
from jwt import PyJWK, PyJWKSet
from loguru import logger

from app.config import get_settings

# 只接受非對稱簽章；alg 來自 token header，必須在此清單內（擋掉 none / HS*）
ALLOWED_ALGS = (
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
)
DISCOVERY_TTL = 3600      # discovery 文件快取 1 小時
JWKS_MIN_REFRESH = 60     # 找不到 kid 時重新抓 JWKS 的最短間隔（秒）
HTTP_TIMEOUT = 10
CLOCK_SKEW_LEEWAY = 30    # 容許與 Keycloak 的時鐘誤差（秒）


class KeycloakError(Exception):
    """與 Keycloak 互動或驗證 token 失敗。訊息寫入 log，不直接顯示給使用者。"""


def is_keycloak_enabled() -> bool:
    """KEYCLOAK_ISSUER 與 KEYCLOAK_CLIENT_ID 都設定才啟用。"""
    settings = get_settings()
    return bool(settings.KEYCLOAK_ISSUER and settings.KEYCLOAK_CLIENT_ID)


def callback_url() -> str:
    """AuthCenter 接收 Keycloak 導回的網址（需登記為 Keycloak client 的 Valid redirect URI）。"""
    return f"{get_settings().AUTH_CENTER_BASE_URL.rstrip('/')}/auth/keycloak/callback"


def pkce_challenge(code_verifier: str) -> str:
    """RFC 7636 S256 code_challenge。"""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ─── HTTP ─────────────────────────────────────────────────────

# 連線層可能發生的錯誤（含網址格式錯誤、憑證檔讀不到）
_NETWORK_ERRORS = (httpx.HTTPError, httpx.InvalidURL, ssl.SSLError, OSError)

_ssl_context: ssl.SSLContext | None = None


def _get_ssl_context() -> ssl.SSLContext:
    """信任的 CA = 系統憑證庫 + certifi + KEYCLOAK_CA_BUNDLE（內部 CA）。"""
    global _ssl_context
    if _ssl_context is None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=certifi.where())
        ca_bundle = get_settings().KEYCLOAK_CA_BUNDLE
        if ca_bundle:
            ctx.load_verify_locations(cafile=ca_bundle)
        _ssl_context = ctx
    return _ssl_context


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=_get_ssl_context())


async def _get_json(url: str, headers: dict | None = None) -> dict:
    try:
        async with _client() as client:
            resp = await client.get(url, headers=headers)
    except _NETWORK_ERRORS as e:
        raise KeycloakError(f"無法連線 {url}: {type(e).__name__}: {e}") from e
    if not resp.is_success:
        raise KeycloakError(f"{url} 回應 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise KeycloakError(f"{url} 回應不是 JSON") from e
    if not isinstance(data, dict):
        raise KeycloakError(f"{url} 回應格式不正確")
    return data


# ─── Discovery / JWKS ────────────────────────────────────────

_discovery: dict | None = None
_discovery_at: float = 0.0
_jwks: PyJWKSet | None = None
_jwks_at: float = 0.0
_discovery_lock = asyncio.Lock()
_jwks_lock = asyncio.Lock()


async def get_discovery() -> dict:
    """取得 Keycloak 的 OIDC discovery 文件（快取 1 小時；更新失敗時沿用舊的）。"""
    global _discovery, _discovery_at
    if _discovery is not None and time.time() - _discovery_at < DISCOVERY_TTL:
        return _discovery

    issuer = get_settings().KEYCLOAK_ISSUER
    async with _discovery_lock:
        if _discovery is not None and time.time() - _discovery_at < DISCOVERY_TTL:
            return _discovery
        try:
            data = await _get_json(f"{issuer}/.well-known/openid-configuration")
            if str(data.get("issuer", "")).rstrip("/") != issuer:
                raise KeycloakError(
                    f"discovery 的 issuer ({data.get('issuer')!r}) 與 KEYCLOAK_ISSUER ({issuer!r}) 不一致"
                )
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not data.get(key):
                    raise KeycloakError(f"discovery 缺少 {key}")
        except KeycloakError:
            if _discovery is not None:
                logger.warning("Keycloak discovery 更新失敗，沿用快取")
                return _discovery
            raise
        _discovery, _discovery_at = data, time.time()
        return data


async def _load_jwks(force: bool = False) -> PyJWKSet:
    global _jwks, _jwks_at
    if _jwks is not None and not force:
        return _jwks
    async with _jwks_lock:
        if _jwks is not None and (not force or time.time() - _jwks_at < JWKS_MIN_REFRESH):
            return _jwks
        discovery = await get_discovery()
        data = await _get_json(discovery["jwks_uri"])
        try:
            _jwks = PyJWKSet.from_dict(data)
        except jwt.PyJWTError as e:
            raise KeycloakError(f"JWKS 無法解析: {e}") from e
        _jwks_at = time.time()
        return _jwks


def _find_key(jwks: PyJWKSet, kid: str | None) -> PyJWK | None:
    sig_keys = [k for k in jwks.keys if k.public_key_use in (None, "sig")]
    if kid:
        return next((k for k in sig_keys if k.key_id == kid), None)
    # token 沒帶 kid：只有一把簽章金鑰時才能確定用哪把
    return sig_keys[0] if len(sig_keys) == 1 else None


async def _get_signing_key(kid: str | None) -> PyJWK:
    key = _find_key(await _load_jwks(), kid)
    if key is None:
        # Keycloak 可能剛輪替金鑰，重新抓一次（有最短間隔，避免被亂帶 kid 的 token 打爆）
        key = _find_key(await _load_jwks(force=True), kid)
    if key is None:
        raise KeycloakError(f"JWKS 中找不到 kid={kid!r} 的簽章金鑰")
    return key


def reset_cache() -> None:
    """清除 discovery / JWKS / SSL context 快取（設定變更或測試用）。"""
    global _discovery, _discovery_at, _jwks, _jwks_at, _ssl_context
    _discovery, _discovery_at, _jwks, _jwks_at, _ssl_context = None, 0.0, None, 0.0, None


# ─── Authorization Code Flow ─────────────────────────────────

async def build_authorize_url(state: str, nonce: str, code_challenge: str) -> str:
    """組出導向 Keycloak 登入頁的網址。"""
    settings = get_settings()
    discovery = await get_discovery()
    params = urlencode({
        "response_type": "code",
        "client_id": settings.KEYCLOAK_CLIENT_ID,
        "redirect_uri": callback_url(),
        "scope": settings.KEYCLOAK_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    endpoint = discovery["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{params}"


async def exchange_code(code: str, code_verifier: str) -> dict:
    """用 authorization code 向 Keycloak 換 token。回傳 token endpoint 的 JSON。"""
    settings = get_settings()
    discovery = await get_discovery()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url(),
        "client_id": settings.KEYCLOAK_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if settings.KEYCLOAK_CLIENT_SECRET:
        form["client_secret"] = settings.KEYCLOAK_CLIENT_SECRET
    try:
        async with _client() as client:
            resp = await client.post(discovery["token_endpoint"], data=form)
    except _NETWORK_ERRORS as e:
        raise KeycloakError(f"無法連線 token endpoint: {type(e).__name__}: {e}") from e
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not resp.is_success or not isinstance(data, dict):
        detail = ""
        if isinstance(data, dict):
            detail = f"{data.get('error', '')} {data.get('error_description', '')}".strip()
        raise KeycloakError(f"token endpoint 回應 HTTP {resp.status_code}: {detail or resp.text[:200]}")
    if not data.get("id_token"):
        raise KeycloakError("token endpoint 未回傳 id_token（scope 需包含 openid）")
    return data


async def verify_id_token(id_token: str, nonce: str) -> dict:
    """驗證 Keycloak 簽發的 id_token，回傳 claims。失敗時 raise KeycloakError。"""
    settings = get_settings()
    discovery = await get_discovery()
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise KeycloakError(f"id_token 格式錯誤: {e}") from e
    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        raise KeycloakError(f"id_token 使用不允許的簽章演算法: {alg!r}")

    key = await _get_signing_key(header.get("kid"))
    try:
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=[alg],
            audience=settings.KEYCLOAK_CLIENT_ID,
            issuer=discovery["issuer"],
            leeway=CLOCK_SKEW_LEEWAY,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as e:
        raise KeycloakError(f"id_token 驗證失敗: {type(e).__name__}: {e}") from e

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        raise KeycloakError("id_token 的 nonce 不符")
    azp = claims.get("azp")
    if azp is not None and azp != settings.KEYCLOAK_CLIENT_ID:
        raise KeycloakError(f"id_token 的 azp ({azp!r}) 不是本 client")
    return claims


# ─── 帳號對應 ────────────────────────────────────────────────

def map_username(value: str) -> str:
    """把 Keycloak claim 的值轉成 nt_account（小寫）。

    設定 KEYCLOAK_USERNAME_DOMAIN 時，值必須是 `<帳號>@<該網域>`，否則拒絕；
    未設定時原樣使用（只做去空白與轉小寫）。
    """
    value = value.strip()
    domain = get_settings().KEYCLOAK_USERNAME_DOMAIN
    if domain:
        local, sep, value_domain = value.rpartition("@")
        if not sep or not local or value_domain.lower() != domain:
            raise KeycloakError(f"帳號 {value!r} 不屬於網域 @{domain}（KEYCLOAK_USERNAME_DOMAIN）")
        value = local
    if not value:
        raise KeycloakError("帳號 claim 是空的")
    return value.lower()


async def resolve_username(claims: dict, access_token: str = "") -> str:
    """依 KEYCLOAK_USERNAME_CLAIM 取出帳號並轉成 nt_account。

    先看 id_token；Keycloak 的 mapper 若只把 claim 放進 userinfo，則再查 userinfo endpoint。
    """
    claim = get_settings().KEYCLOAK_USERNAME_CLAIM
    value = claims.get(claim)

    if not value and access_token:
        endpoint = (await get_discovery()).get("userinfo_endpoint")
        if endpoint:
            info = await _get_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            # userinfo 的 sub 必須與 id_token 相同，避免 token 被替換
            if info.get("sub") != claims.get("sub"):
                raise KeycloakError("userinfo 的 sub 與 id_token 不一致")
            value = info.get(claim)

    if not isinstance(value, str) or not value.strip():
        raise KeycloakError(
            f"token 中沒有可用的 {claim!r} claim（請檢查 KEYCLOAK_USERNAME_CLAIM 或 Keycloak 的 mapper 設定）"
        )
    return map_username(value)
