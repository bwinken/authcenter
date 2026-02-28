# AI App 整合 Auth Center 範例

這是一個完整可運行的 FastAPI App，示範如何整合 Auth Center SSO。

## 啟動

```bash
# 1. 確保 Auth Center 已在 :8000 運行
# 2. 確保 apps.yaml 中已註冊此 App（預設 ai_chat_app 已存在）
# 3. 確保 keys/public.pem 存在（與 Auth Center 共用）

fastapi dev example_app/main.py --port 8001
```

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

1. 使用者訪問 App → 未登入 → 自動跳轉 Auth Center 登入頁
2. 登入成功 → Auth Center 302 回 `/auth/callback?code=xxx`
3. App 用 code 換取 JWT → 存入 HttpOnly Cookie
4. 後續請求瀏覽器自動帶 Cookie → App 用 public.pem 本地驗證

```
使用者訪問 App
       │
       ▼
  有 Cookie？──── 有 ──► JWT 有效？──── 有效 ──► 正常使用 App
       │                     │
       無                   無效/過期
       │                     │
       ▼                     ▼
  302 → Auth Center 登入頁
       │
       ▼
  登入成功 → 302 回 /auth/callback?code=xxx
       │
       ▼
  App 用 code + client_secret 換 JWT
       │
       ▼
  JWT 存入 HttpOnly Cookie → 正常使用 App
```

## 雙模式驗證

`get_current_user` 同時支援兩種認證方式：

| 模式 | 來源 | 適用場景 |
|------|------|----------|
| Bearer Token | HTTP Header `Authorization: Bearer xxx` | /docs 開發、API 呼叫 |
| Cookie | `access_token` HttpOnly Cookie | 瀏覽器正式使用 |

優先順序：Bearer Token > Cookie。兩者都沒有則回 401。

## 權限檢查

用 `require_scopes` 限制 API 存取：

```python
# 任何已登入的使用者
@app.get("/api/me")
async def me(user=Depends(get_current_user)):
    ...

# 需要 read 權限
@app.get("/api/data")
async def data(user=Depends(require_scopes(["read"]))):
    ...

# 需要 read + admin 權限
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
