# 更新紀錄（Changelog）

本專案採用 [語意化版本](https://semver.org/lang/zh-TW/)（Semantic Versioning）。

---

## [v0.1.0] — 2026-08-26

Auth Center 首次正式發布。為內部 AI App 提供統一的單一登入（SSO）服務，同時支援
自有的 OAuth2 Authorization Code Flow 與標準 OIDC（供 OAuth2 Proxy 類型的應用接入）。

### 支援範圍（Supported Scope）

#### 認證協定

| 項目 | 支援狀態 |
|------|----------|
| OAuth2 Authorization Code Flow（`/auth/login` → `/auth/token`） | ✅ |
| OpenID Connect：Discovery、JWKS、Authorize、Token、UserInfo | ✅ |
| `response_type` | `code` |
| `grant_type` | `authorization_code` |
| Client 認證方式 | `client_secret_post`、`client_secret_basic` |
| Scopes | `openid`、`profile`、`email` |
| ID Token 簽章演算法 | RS256（含 `kid`，可由 JWKS 取得公鑰輪替） |
| Auth Code | 一次性、5 分鐘有效、原子消耗（`DELETE ... RETURNING`）、支援 `nonce` 防重放 |
| Token 有效期 | 每個 App 可自訂 `token_expire_hours`（預設 12 小時）；Admin Token 固定 2 小時 |
| ID Token / UserInfo claims | `sub`、`iss`、`aud`、`exp`、`iat`、`nonce`、`name`、`preferred_username`、`org_id`、`email`、`email_verified` |

#### 權限模型

- Per-User-Per-App 權限，Level 0–3 自動對應 scopes：
  `0 → 拒絕`、`1 → read`、`2 → read, write`、`3 → read, write, admin`
- 無權限紀錄即拒絕存取（403）；Level 0 為明確拒絕
- 組織層級限制 `allowed_orgs` 與組織預設 `default_level`（僅支援 Level 1、2）
- 個人權限優先於組織限制：有個人紀錄時不再檢查 `allowed_orgs`
- Level 3 與 App Admin 自動同步（`auto_assigned`），降級／撤銷時保留手動指派的 App Admin

#### 使用者功能

- 登入 / 登出、使用者 Dashboard（顯示可存取 App 與各自 Level／Scopes）
- 註冊：管理員產生一次性註冊連結，或使用者自行申請後由管理員審核
- 修改密碼；忘記密碼 → 管理員產生一次性重設連結（6 小時有效）
- 密碼強度政策：至少 8 字元、含大小寫英文字母與數字、不可與使用者名稱相同

#### 管理後台

- 兩層 Admin：Super Admin（`.env` 固定帳密，或 `SUPER_ADMIN_EMPLOYEES` 指定員工）／App Admin
- App CRUD（寫回 `config/apps.yaml`，依 mtime 熱重載）；App Admin 可調整自己 App 的
  `allowed_orgs`、`default_level`、`token_expire_hours`，但不可新增／刪除 App
- 使用者權限管理、會員管理（重設密碼、刪除帳號）、App Admin 指派
- App 存取紀錄（`app_access_log`）與管理操作稽核紀錄（`admin_audit_log`）

#### 資料與整合

- 員工主檔：MSSQL 唯讀（ODBC Driver 17 for SQL Server），查詢 `nt_account`、`org_id`、`extension`
- 本地認證資料：SQLite（WAL mode），啟動時自動建立 8 張表
- App 註冊：`config/apps.yaml`（bcrypt 雜湊的 `client_secret`）
- 通知：Microsoft Teams Webhook（管理員 Channel）＋ Power Automate 使用者 1:1 Chat（可選）
- 內網環境可透過 `HTTP_PROXY` 對外連線

#### 部署與工具

- Python 3.11+（Docker 映像使用 3.12-slim）
- 部署方式：`fastapi run`、Docker / docker-compose、systemd + nginx 一鍵腳本（`deploy/setup.sh`）
- `GET /health` 檢查 SQLite + MSSQL 連線狀態，供 Load Balancer 探針使用
- 環境檢查工具 `scripts/preflight_check.py`
- CLI：`manage_permissions.py`、`generate_register_link.py`、`reset_password.py`
- 整合範例：`example_app/`，以及 3 組 OAuth2 Proxy 部署範例（`examples/`）
- 安全測試腳本 `example_app/security_tests.py`：9 大類、31 項自動化驗證

### 本版尚未支援（Known Limitations）

| 項目 | 說明 |
|------|------|
| PKCE（RFC 7636） | 僅支援 confidential client（`client_secret`），不支援公開客戶端／SPA／行動 App 的 PKCE 流程 |
| Refresh Token | 無 refresh flow，Token 過期需重新登入。OAuth2 Proxy 部署請設定 `OAUTH2_PROXY_COOKIE_REFRESH`（例如 `1h`）讓 Proxy 定期檢查 `exp` |
| RP-Initiated Logout | 未提供 `end_session_endpoint`，亦無 Front/Back-Channel Logout；登出僅清除 Auth Center 自身 Cookie |
| Dynamic Client Registration | App 需由管理員在 `config/apps.yaml`／Admin 後台註冊 |
| 多因素認證（MFA / 2FA） | 尚未支援 |
| AD / LDAP 直接認證 | 密碼由本地 SQLite 以 bcrypt 保管；MSSQL 員工主檔僅用於唯讀比對身分 |
| 其他員工資料來源 | 員工主檔目前僅支援 MSSQL |
| 資料庫 migration | 無 Alembic，表結構於啟動時以 `CREATE TABLE IF NOT EXISTS` 建立；欄位變更需手動處理 |
| 單元測試套件 | 尚無 pytest 測試，目前僅有需啟動服務的整合式 `security_tests.py` |
| 多實例水平擴展 | Rate limiting 為單一 process 的記憶體滑動視窗，多 worker／多實例時各自獨立計算；SQLite 亦需共用檔案儲存 |
| 介面語言 | UI 僅提供繁體中文 |

### 升級注意事項

首次發布，無升級路徑。全新部署請依 README 的
[快速開始](README.md#3-快速開始安裝與啟動) 完成 ODBC Driver、RSA 金鑰、`.env`
與 `config/apps.yaml` 設定，並以 `python scripts/preflight_check.py` 驗證環境。
