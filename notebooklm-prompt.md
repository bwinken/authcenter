# Auth Center — OAuth2 / OIDC 統一認證平台簡報生成提示

請根據以下內容，生成一份技術簡報（投影片），對象是內部開發團隊與 IT 架構師。語言使用**繁體中文**，技術名詞保留英文。風格簡潔、重點清晰、附帶架構圖說明。

---

## 簡報主題

**Auth Center：為什麼我們需要 OAuth2 / OIDC，以及如何用它保護所有內部應用**

---

## 簡報大綱與各頁內容要點

### 第一部分：為什麼需要統一認證？（問題背景）

**頁 1 — 現狀痛點**
- 內部 AI 應用越來越多（聊天機器人、知識庫、檔案管理等）
- 每個應用各自管理帳號密碼 → 密碼疲勞、安全風險分散
- 員工離職或調部門時，無法統一停用所有應用權限
- 沒有統一的存取紀錄（誰在什麼時候用了什麼應用）

**頁 2 — 解決方案：集中式 SSO（Single Sign-On）**
- 一組帳密登入所有應用
- 統一的權限控管：誰能用哪個 App、能做什麼操作
- 集中式存取日誌（audit log）
- 員工異動時，一處修改全面生效

---

### 第二部分：OAuth2 與 OIDC 基礎概念

**頁 3 — OAuth2 是什麼？**
- OAuth2 是一個「授權框架」（Authorization Framework）
- 核心精神：**應用永遠不會接觸到使用者的密碼**
- 使用者在認證中心登入 → 認證中心發放一次性授權碼（Authorization Code）→ 應用拿授權碼換取 Token
- 類比：飯店入住時，櫃台驗證身份後給你房卡，你用房卡開門，而非每次都出示護照

**頁 4 — OIDC（OpenID Connect）是什麼？**
- OIDC 是建立在 OAuth2 之上的「身份驗證層」（Authentication Layer）
- OAuth2 只回答「你被授權做什麼」，OIDC 進一步回答「你是誰」
- 增加了標準端點：Discovery（`/.well-known/openid-configuration`）、JWKS、UserInfo
- 增加了 `id_token`：攜帶使用者身份資訊的 JWT
- 好處：任何支援 OIDC 的工具（如 OAuth2 Proxy）都能即插即用

**頁 5 — JWT（JSON Web Token）簡介**
- Token 是一個加密簽章的 JSON，包含使用者資訊和權限
- Auth Center 使用 RS256（非對稱加密）：私鑰簽發、公鑰驗證
- Token 內容範例：
  ```json
  {
    "sub": "kane.beh",
    "aud": "my_app",
    "org_id": "IT",
    "scopes": ["read", "write"],
    "exp": 1234611090
  }
  ```
- 好處：無狀態（Stateless），應用不需要回呼認證中心即可驗證 Token

---

### 第三部分：Auth Center 架構總覽

**頁 6 — 系統架構圖**
- 雙資料庫架構：
  - MSSQL（IT 人事主檔）— 唯讀，查詢員工身份（帳號、部門代碼、分機）
  - SQLite（認證本地資料庫）— 讀寫，儲存帳號密碼、授權碼、權限、管理設定
- FastAPI 後端 + Jinja2 模板渲染
- RSA 金鑰對（`keys/` 目錄）用於 JWT 簽發

**頁 7 — 權限模型（Per-User-Per-App Level）**
- 每個使用者對每個應用有獨立的權限等級：
  - Level 0：明確拒絕存取
  - Level 1：`["read"]` — 唯讀
  - Level 2：`["read", "write"]` — 讀寫
  - Level 3：`["read", "write", "admin"]` — 管理員
- 個人權限優先於部門預設（Personal > Org Default）
- Level 3 自動同步為 App Admin（可管理該應用設定）
- 應用在 `config/apps.yaml` 註冊，支援熱重載

**頁 8 — 兩層管理員架構**
- Super Admin（全域管理員）：可建立/刪除應用、管理所有權限
- App Admin（應用管理員）：僅能管理自己負責的應用設定
- 各有獨立的登入入口與 JWT Token（2 小時有效期）

---

### 第四部分：兩種整合模式

**頁 9 — 整合模式總覽**

Auth Center 提供兩種方式讓應用接入：

| 模式 | 適用場景 | 技術要求 |
|------|----------|----------|
| **模式 A：直接整合** | 自行開發的應用（如 FastAPI、Next.js） | 應用自己實作 OAuth2 Code Flow |
| **模式 B：OAuth2 Proxy 整合** | 不支援 OAuth2 的既有服務（如檔案伺服器、內部工具） | Nginx + OAuth2 Proxy，應用零改動 |

---

### 第五部分：模式 A — 直接整合（example_app 範例）

**頁 10 — 直接整合流程圖**

```
使用者 ──→ 應用首頁 ──→ 點擊「使用 Auth Center 登入」
                            │
                            ▼
                     Auth Center 登入頁
                     （驗證帳密、檢查權限）
                            │
                            ▼
                     產生一次性授權碼（5 分鐘有效）
                     303 Redirect → 應用 /auth/callback?code=xxx
                            │
                            ▼
                     應用後端拿 code + client_secret
                     呼叫 POST /auth/token
                            │
                            ▼
                     取得 RS256 JWT Token（12 小時有效）
                     存入 HttpOnly Cookie
                            │
                            ▼
                     使用者進入應用 Dashboard ✓
```

**頁 11 — 應用端關鍵程式碼（FastAPI 範例）**

1. **設定 OAuth2 參數**
   ```python
   AUTH_CENTER_URL = "http://auth.company.com"
   APP_ID = "my_app"
   CLIENT_SECRET = "my_secret"
   REDIRECT_URI = "http://myapp.com/auth/callback"
   ```

2. **登入按鈕** — 表單 POST 到 `{AUTH_CENTER_URL}/auth/login?app_id={APP_ID}&redirect_uri={REDIRECT_URI}`

3. **Callback 路由** — 接收 `?code=xxx`，呼叫 `/auth/token` 換取 JWT

4. **驗證 JWT** — 使用 Auth Center 的公鑰（`public.pem`）驗證 RS256 簽章

5. **權限檢查** — 從 JWT 的 `scopes` 欄位判斷使用者可執行的操作
   - `require_scopes(["read"])` → 只有包含 `read` scope 的使用者才能存取
   - `require_scopes(["read", "admin"])` → 需要管理員權限

---

### 第六部分：模式 B — OAuth2 Proxy 整合（full-stack-oauth2-proxy 範例）⭐重點

**頁 12 — 為什麼需要 OAuth2 Proxy？**
- 許多內部服務本身不支援 OAuth2（檔案伺服器、舊系統、第三方工具）
- 不想為每個服務都寫認證邏輯
- 解法：在 Nginx 層加一個「認證守門員」— OAuth2 Proxy
- Auth Center 扮演 OIDC Provider（身份提供者），OAuth2 Proxy 扮演 OIDC Client（認證代理）

**頁 13 — 完整架構圖（full-stack-oauth2-proxy）**

```
使用者瀏覽器
    │
    ▼
┌──────────────────────────────────────────────────┐
│  Nginx（反向代理 + 認證閘道）                       │
│  sa-help.company.com                              │
│                                                    │
│  ┌─────────────────────┐                          │
│  │ auth_request         │ ← 每個請求先問 OAuth2 Proxy│
│  │ /oauth2/auth         │   「這個使用者有沒有登入？」│
│  └────────┬────────────┘                          │
│           │ ✓ 已登入 → 放行 + 注入 JWT             │
│           │ ✗ 未登入 → 302 跳轉登入               │
│           ▼                                        │
│  ┌────────────────┬──────────────┬───────────┐    │
│  │ /admin/* /api/* │    /fs/*     │    /*     │    │
│  │  業務 API 後端  │  HFS 檔案伺服│  前端靜態  │    │
│  │  :8058          │  器 :8080    │  檔案     │    │
│  └────────────────┴──────────────┴───────────┘    │
└──────────────────────────────────────────────────┘
                    │
    ┌───────────────┘
    ▼
┌─────────────────────────────────────┐
│  OAuth2 Proxy（Docker :4180）        │
│  OIDC Client                         │
│  ← 處理登入/登出/callback            │
│  ← 驗證 session cookie              │
│  ← 注入 X-Auth-Request-Access-Token │
└───────────────┬─────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│  Auth Center（OIDC Provider）        │
│  auth.company.com                    │
│                                      │
│  /.well-known/openid-configuration  │
│  /.well-known/jwks.json             │
│  /oidc/authorize                    │
│  /oidc/token                        │
│  /oidc/userinfo                     │
└─────────────────────────────────────┘
```

**頁 14 — 認證流程詳解（Sequence Diagram 風格）**

```
1. 使用者 → Nginx：GET /api/data
2. Nginx → OAuth2 Proxy：auth_request /oauth2/auth（檢查 session cookie）
3. OAuth2 Proxy → Nginx：401（未登入）
4. Nginx → 使用者：302 → /oauth2/start（觸發 OIDC 登入流程）
5. 使用者 → OAuth2 Proxy → Auth Center：/oidc/authorize
6. Auth Center：顯示登入頁面
7. 使用者輸入帳密 → Auth Center 驗證
8. Auth Center → OAuth2 Proxy：302 + code（一次性授權碼）
9. OAuth2 Proxy → Auth Center：POST /oidc/token（code + client_secret）
10. Auth Center → OAuth2 Proxy：access_token + id_token（JWT）
11. OAuth2 Proxy → 使用者：設定 session cookie + 302 回原始頁面
12. 使用者 → Nginx：GET /api/data（帶 session cookie）
13. Nginx → OAuth2 Proxy：auth_request（驗證 cookie → 有效）
14. OAuth2 Proxy → Nginx：200 + X-Auth-Request-Access-Token header
15. Nginx → 後端 API：GET /api/data + Authorization: Bearer <jwt>
16. 後端 API：驗證 JWT → 回傳資料
```

**頁 15 — 部署步驟（6 步完成）**

1. **Auth Center 註冊應用** — 在 `config/apps.yaml` 新增應用，設定 `redirect_uri` 指向 OAuth2 Proxy 的 callback（`/oauth2/callback`）
2. **設定環境變數** — `.env` 包含 `OIDC_ISSUER_URL`、`CLIENT_ID`、`CLIENT_SECRET`、`COOKIE_SECRET`
3. **啟動 Docker** — `docker compose up -d`（OAuth2 Proxy + HFS）
4. **設定 Nginx** — 複製 `nginx.conf` 到 `/etc/nginx/sites-available/`，啟用 `auth_request`
5. **授予使用者權限** — `python scripts/manage_permissions.py grant <user> <app_id> --level 2`
6. **驗證** — 開啟瀏覽器，訪問受保護的 URL，應自動跳轉到 Auth Center 登入

**頁 16 — Nginx 核心設定解析**

```nginx
# 每個請求先經過 OAuth2 Proxy 驗證
location /api/ {
    auth_request /oauth2/auth;

    # 從 OAuth2 Proxy 的回應中取得 JWT
    auth_request_set $token $upstream_http_x_auth_request_access_token;

    # 將 JWT 注入後端請求的 Authorization header
    proxy_set_header Authorization "Bearer $token";

    proxy_pass http://127.0.0.1:8058;
}
```
- `auth_request`：Nginx 在轉發請求前，先向 OAuth2 Proxy 確認使用者是否已登入
- 如果已登入：OAuth2 Proxy 回傳 200 + Token，Nginx 將 Token 注入 header 後轉發給後端
- 如果未登入：OAuth2 Proxy 回傳 401，Nginx 自動 302 跳轉到登入頁面

**頁 17 — Token 過期與自動續約機制**

```
Auth Center                    OAuth2 Proxy              使用者
token_expire_hours: 24    COOKIE_REFRESH: 1h
     │                           │                         │
     │                           │◀── 每 1 小時檢查 ──────│
     │                           │    id_token 是否過期     │
     │                           │                         │
     │  JWT 24 小時後過期         │    id_token exp 已過    │
     │                           │──→ 觸發重新登入 ───────→│
     │                           │                         │
```
- Auth Center 的 `token_expire_hours` 控制 JWT 有效期
- OAuth2 Proxy 的 `COOKIE_REFRESH` 控制多久檢查一次 Token 是否過期
- 當 JWT 過期，OAuth2 Proxy 自動要求使用者重新登入

---

### 第七部分：安全機制

**頁 18 — 內建安全防護**

| 機制 | 說明 |
|------|------|
| **速率限制** | 每 IP 每 5 分鐘最多 10 次登入嘗試（滑動視窗） |
| **時序攻擊防護** | 未知使用者也執行 bcrypt 雜湊（防止透過回應時間推測帳號是否存在） |
| **授權碼原子消費** | `DELETE ... RETURNING` 確保一次性授權碼不被重複使用 |
| **OIDC Nonce** | 防止重放攻擊 |
| **CSRF 保護** | Double-submit cookie 機制 |
| **RS256 非對稱簽章** | 私鑰只在 Auth Center，應用端只需公鑰驗證 |
| **HttpOnly Cookie** | 防止 XSS 竊取 Token |
| **密碼原則** | 最少 8 字元、需含大小寫字母與數字 |

---

### 第八部分：總結

**頁 19 — 為什麼選擇 Auth Center + OIDC**

- ✅ **標準協定**：基於 OAuth2 / OIDC 業界標準，不是自製黑箱
- ✅ **雙模式整合**：自行開發的應用可直接整合，既有服務用 OAuth2 Proxy 零改動接入
- ✅ **細緻權限**：Per-User-Per-App 多級權限（read / write / admin）
- ✅ **部門管控**：`allowed_orgs` 限制特定部門才能存取
- ✅ **安全可靠**：RS256 JWT + 速率限制 + CSRF + 時序攻擊防護
- ✅ **可稽核**：完整的存取日誌紀錄
- ✅ **易於管理**：兩層管理員 + 統一 Dashboard + YAML 熱重載

**頁 20 — 快速開始**

新應用要接入 Auth Center，只需要：
1. 在 Auth Center 管理後台註冊應用（或編輯 `apps.yaml`）
2. 取得 `app_id` 和 `client_secret`
3. 選擇整合模式：
   - **自行開發的應用** → 參考 `example_app/` 實作 OAuth2 Code Flow
   - **既有服務** → 參考 `examples/full-stack-oauth2-proxy/` 部署 OAuth2 Proxy
4. 通知管理員授予使用者權限

---

## 簡報風格指引

- 每頁保持 3-5 個要點，避免過多文字
- 架構圖和流程圖是重點，幫助理解
- 使用「問題 → 解決方案」的敘事結構
- 對比傳統方式 vs. OAuth2/OIDC 方式的差異
- 強調「為什麼」而非只有「怎麼做」
- 適度使用表格整理對比資訊
