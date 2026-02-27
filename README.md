# Auth Center

中央認證系統，為多個內部 AI App 提供統一的單一登入 (SSO) 服務。

## 功能特色

- **OAuth2 Authorization Code Flow** — App 重導登入、code 換 token 標準流程
- **RS256 JWT** — 非對稱加密，Auth Center 簽發、各 App 用公鑰驗證
- **雙資料庫架構** — MySQL（員工主檔，唯讀）+ SQLite（帳號與權限，讀寫）
- **自動註冊** — 員工首次登入自動引導設定密碼
- **權限分級** — Level 1/2/3 自動映射為 `read` / `read+write` / `read+write+admin` scopes
- **App 存取控制** — 依部門與等級限制 App 存取權限
- **忘記密碼** — 透過 Microsoft Teams Webhook 通知管理員處理
- **Jinja2 UI** — 內建登入、註冊、忘記密碼頁面

## 專案結構

```
auth-center/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # 環境變數與 apps.yaml 讀取
│   ├── database.py          # 雙 DB 連線管理
│   ├── models.py            # SQLAlchemy models
│   ├── schemas.py           # Pydantic schemas
│   ├── auth/
│   │   ├── routes.py        # API 路由
│   │   ├── service.py       # 核心業務邏輯
│   │   └── jwt_handler.py   # RS256 JWT 簽發與驗證
│   ├── webhook/
│   │   └── teams.py         # Teams Webhook 通知
│   └── templates/           # Jinja2 前端模板
├── config/
│   └── apps.yaml            # 已註冊 App 清單
├── keys/                    # RSA 金鑰對（gitignore）
├── scripts/
│   └── init_db.sql          # SQLite 表結構
├── middleware_example/
│   └── app_middleware.py    # App 端驗證範例
├── generate_keys.py         # 金鑰產生腳本
├── requirements.txt
└── .env.example
```

## 快速開始

```bash
# 安裝依賴
pip install -r requirements.txt

# 產生 RSA 金鑰對
python generate_keys.py

# 設定環境變數
cp .env.example .env
# 編輯 .env 填入 MySQL 連線資訊與 Teams Webhook URL

# 啟動服務
uvicorn app.main:app --reload --port 8000
```

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `MYSQL_HOST` | IT Master DB 主機 | `localhost` |
| `MYSQL_PORT` | MySQL 連接埠 | `3306` |
| `MYSQL_USER` | MySQL 使用者（唯讀） | `root` |
| `MYSQL_PASSWORD` | MySQL 密碼 | — |
| `MYSQL_DATABASE` | MySQL 資料庫名稱 | `it_master` |
| `SQLITE_PATH` | SQLite 檔案路徑 | `./auth_local.db` |
| `PRIVATE_KEY_PATH` | RS256 私鑰路徑 | `./keys/private.pem` |
| `PUBLIC_KEY_PATH` | RS256 公鑰路徑 | `./keys/public.pem` |
| `TEAMS_WEBHOOK_URL` | Microsoft Teams Webhook URL | — |
| `AUTH_CENTER_BASE_URL` | Auth Center 對外 URL | `http://localhost:8000` |

## OAuth2 認證流程

```
┌──────────┐     1. 302 重導        ┌─────────────┐
│  AI App  │ ──────────────────────→ │ Auth Center │
│ (Client) │                         │  /auth/login │
└──────────┘                         └──────┬──────┘
                                            │
                                     2. 使用者登入
                                     3. 驗證帳密
                                     4. 檢查 App 權限
                                            │
┌──────────┐     5. 302 + code       ┌──────┴──────┐
│  AI App  │ ←────────────────────── │ Auth Center │
│ /callback│                         └─────────────┘
└────┬─────┘
     │
     │  6. POST /auth/token
     │     { code, app_id, client_secret }
     │
     ▼
┌─────────────┐     7. JWT           ┌──────────┐
│ Auth Center │ ───────────────────→ │  AI App  │
│ /auth/token │                      │ Set Cookie│
└─────────────┘                      └──────────┘
```

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/auth/login?app_id=X&redirect_uri=Y` | 登入頁面 |
| `POST` | `/auth/login` | 提交登入表單 |
| `GET` | `/auth/register?staff_id=X` | 註冊頁面（首次登入） |
| `POST` | `/auth/register` | 提交註冊表單 |
| `POST` | `/auth/token` | 用 authorization code 換取 JWT |
| `GET` | `/auth/forgot-password` | 忘記密碼頁面 |
| `POST` | `/auth/forgot-password` | 發送 Teams 通知給管理員 |

## JWT Token 格式

```json
{
  "sub": "EMP001",
  "name": "王小明",
  "dept": "IT",
  "scopes": ["read", "write"],
  "aud": "ai_chat_app",
  "iat": 1709000000,
  "exp": 1709043200
}
```

**權限映射規則：**

| Level | Scopes |
|-------|--------|
| 1 | `["read"]` |
| 2 | `["read", "write"]` |
| 3 | `["read", "write", "admin"]` |

## 註冊新的 AI App

編輯 `config/apps.yaml`：

```yaml
apps:
  - app_id: "my_new_app"
    client_secret: "$2b$12$..."   # bcrypt hash of your secret
    redirect_uri: "https://my-app.example.com/auth/callback"
    name: "My New App"
```

產生 bcrypt hash：

```python
from passlib.hash import bcrypt
print(bcrypt.hash("your_plain_secret"))
```

## App 端整合

參考 `middleware_example/app_middleware.py`，核心步驟：

```python
from jose import jwt

# 1. 從 Cookie 讀取 JWT
token = request.cookies.get("access_token")

# 2. 用公鑰驗證
payload = jwt.decode(token, public_key, algorithms=["RS256"], audience="my_app_id")

# 3. 檢查 scopes
if "read" not in payload["scopes"]:
    raise HTTPException(403, "Insufficient permissions")
```

App 收到 callback 後，須將 JWT 存入 Cookie：

```python
response.set_cookie(
    key="access_token",
    value=jwt_token,
    httponly=True,
    secure=True,
    samesite="lax",
    max_age=43200,  # 12 hours
)
```

## 資料庫架構

**IT Master DB (MySQL，唯讀)**

| 欄位 | 型別 | 說明 |
|------|------|------|
| `staff_id` | VARCHAR(50) PK | 員工編號 |
| `name` | VARCHAR | 姓名 |
| `dept_code` | VARCHAR | 部門代碼 |
| `level` | INT | 權限等級 (1-3) |

**Auth Local DB (SQLite，讀寫)**

`user_accounts` — 員工帳號密碼

| 欄位 | 型別 | 說明 |
|------|------|------|
| `staff_id` | VARCHAR(50) PK | 員工編號 |
| `password_hash` | VARCHAR(255) | bcrypt 雜湊 |
| `created_at` | DATETIME | 建立時間 |
| `updated_at` | DATETIME | 更新時間 |

`app_access_rules` — App 存取門檻

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | INTEGER PK | 自增 ID |
| `app_id` | VARCHAR(100) UNIQUE | App 識別碼 |
| `allowed_depts` | TEXT | JSON 陣列，允許部門（空 = 全部） |
| `min_level` | INTEGER | 最低等級要求 |
