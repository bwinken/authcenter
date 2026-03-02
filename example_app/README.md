# AI App 整合 Auth Center 範例

這是一個完整可運行的 FastAPI App，示範如何整合 Auth Center SSO。包含簡易前端頁面，方便測試完整 OAuth2 流程。

## 啟動

```bash
# 1. 確保 Auth Center 已在 :8000 運行
# 2. 確保 apps.yaml 中已註冊此 App（預設 ai_chat_app 已存在）
# 3. 確保 keys/public.pem 存在（與 Auth Center 共用）
# 4. 確保使用者已被授予 level 權限（見下方「授予權限」）

fastapi dev example_app/main.py --port 8001
```

## 前端頁面

| 路徑 | 說明 |
|------|------|
| `/` | Landing Page（未登入）/ Dashboard（已登入） |
| `/jwt` | JWT 詳細資訊（decoded payload + raw token 三段拆解） |
| `/docs` | Swagger API 文件（可直接 Authorize 測試） |
| `/logout` | 清除 Cookie 並登出 |

### Dashboard 功能

登入後的 Dashboard 顯示：

- **使用者資訊** — employee name, org_id, scopes badges, audience, token 有效期
- **API 測試面板** — 四個按鈕可一鍵測試不同權限等級的 endpoint，直接在頁面內顯示 response

## 授予權限

使用者必須有明確的 level 授權才能存取此 App（無權限 = 拒絕存取）：

```bash
# Level 1: 只能存取 GET /api/data (read)
python scripts/manage_permissions.py grant kane.beh ai_chat_app --level 1

# Level 2: 可以存取 GET + POST /api/data (read + write)
python scripts/manage_permissions.py grant kane.beh ai_chat_app --level 2

# Level 3: 可以存取所有 API 含 /api/admin (read + write + admin)
python scripts/manage_permissions.py grant kane.beh ai_chat_app --level 3
```

也可以在 Auth Center Admin 後台（`http://localhost:8000/admin/login`）的「權限管理」頁面操作。

## 開發階段：用 /docs 測試 API

適合後端開發時快速測試，不需要瀏覽器跑完整 OAuth2 流程。

1. 打開 `http://localhost:8001/docs`
2. 點右上角 **Authorize**（鎖頭圖示）
3. 輸入 Auth Center 的帳號密碼
4. 點 **Authorize** 確認
5. 完成！之後在 /docs 裡呼叫任何 API 都會自動帶上 Token

背後原理：`POST /token` 端點會自動向 Auth Center 完成 `登入 → 取得 code → 換取 JWT` 整個流程，Swagger 再把拿到的 JWT 記住。

```
/docs 點 Authorize
       │
       ▼
POST /token (username, password)
       │
       ├─► Auth Center POST /auth/login  → 取得 code
       │
       └─► Auth Center POST /auth/token  → 換取 JWT
                                              │
                                              ▼
                                   Swagger 記住 Token ✓
                                   後續 API 自動帶上
```

## 正式環境：瀏覽器 OAuth2 Flow

使用者透過瀏覽器操作，走標準 OAuth2 redirect 流程。

1. 使用者訪問 App → 未登入 → 顯示 Landing Page 含 Login 按鈕
2. 點擊 Login → 跳轉 Auth Center 登入頁
3. 登入成功 → Auth Center 302 回 `/auth/callback?code=xxx`
4. App 用 code 換取 JWT → 存入 HttpOnly Cookie → 顯示 Dashboard
5. 後續請求瀏覽器自動帶 Cookie → App 用 public.pem 本地驗證

```
使用者訪問 App
       │
       ▼
  有 Cookie？──── 有 ──► JWT 有效？──── 有效 ──► 顯示 Dashboard
       │                     │
       無                   無效/過期
       │                     │
       ▼                     ▼
  顯示 Landing Page（Login 按鈕）
       │
       ▼
  點擊 Login → Auth Center 登入頁
       │
       ▼
  登入成功 → 302 回 /auth/callback?code=xxx
       │
       ▼
  App 用 code + client_secret 換 JWT
       │
       ▼
  JWT 存入 HttpOnly Cookie → 顯示 Dashboard
```

## API Endpoints

| Method | Path | Required Scopes | 對應 Level |
|--------|------|-----------------|-----------|
| `GET` | `/api/me` | (none) | any |
| `GET` | `/api/data` | `read` | 1+ |
| `POST` | `/api/data` | `read`, `write` | 2+ |
| `GET` | `/api/admin` | `read`, `admin` | 3 |

## 雙模式驗證

`get_current_user` 同時支援兩種認證方式：

| 模式 | 來源 | 適用場景 |
|------|------|----------|
| Bearer Token | HTTP Header `Authorization: Bearer xxx` | /docs 開發、API 呼叫 |
| Cookie | `access_token` HttpOnly Cookie | 瀏覽器正式使用 |

優先順序：Bearer Token > Cookie。兩者都沒有則回 401。

## 權限檢查

用 `require_scopes` 限制 API 存取（scopes 由 Auth Center 的 level 自動映射）：

```python
# 任何已登入的使用者
@app.get("/api/me")
async def me(user=Depends(get_current_user)):
    ...

# 需要 read 權限（level 1+）
@app.get("/api/data")
async def data(user=Depends(require_scopes(["read"]))):
    ...

# 需要 read + admin 權限（level 3）
@app.get("/api/admin")
async def admin(user=Depends(require_scopes(["read", "admin"]))):
    ...
```

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `AUTH_CENTER_URL` | Auth Center 位址 | `http://localhost:8000` |
| `APP_ID` | 在 Auth Center 註冊的 App ID | `ai_chat_app` |
| `CLIENT_SECRET` | App 的明文密鑰 | `chat_secret_123` |
| `REDIRECT_URI` | OAuth2 callback URL | `http://localhost:8001/auth/callback` |
| `PUBLIC_KEY_PATH` | Auth Center 公鑰路徑 | `./keys/public.pem` |

## 你的 App 需要複製哪些部分

從 `main.py` 複製到你的專案：

1. **必要** — `get_current_user`、`require_scopes`、`/auth/callback`
2. **建議** — `POST /token`（讓 /docs 開發更方便）
3. **參考** — 範例 API 路由（`/api/me`、`/api/data` 等）
