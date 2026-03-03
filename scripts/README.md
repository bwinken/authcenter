# Scripts

Auth Center 維運與管理用的 CLI 工具與資料庫 Schema。

## CLI 工具

### manage_permissions.py — 管理使用者 App 權限

授予、撤銷或查詢使用者對各 App 的存取權限。

```bash
# 授予權限（level: 1=Read, 2=Read+Write, 3=Full Admin）
python scripts/manage_permissions.py grant <employee_name> <app_id> --level 2

# 撤銷權限
python scripts/manage_permissions.py revoke <employee_name> <app_id>

# 列出所有權限
python scripts/manage_permissions.py list

# 依使用者或 App 篩選
python scripts/manage_permissions.py list --user <employee_name>
python scripts/manage_permissions.py list --app <app_id>
```

### generate_register_link.py — 產生員工註冊連結

為尚未註冊的員工產生一次性註冊連結（24 小時有效），由管理員將連結發送給員工。

```bash
python scripts/generate_register_link.py <employee_name>
python scripts/generate_register_link.py <employee_name> --app-id <app_id> --redirect-uri <uri>
```

### reset_password.py — 重設使用者密碼

重設已註冊使用者的密碼，可指定新密碼或自動產生隨機密碼。

```bash
# 自動產生隨機密碼
python scripts/reset_password.py <employee_name>

# 指定新密碼（至少 8 字元）
python scripts/reset_password.py <employee_name> --password <new_password>
```

### preflight_check.py — 部署前環境檢查

逐步驗證部署環境是否正確設定，包含：

1. `.env` 環境變數
2. ODBC Driver 安裝
3. MSSQL 連線與資料表查詢
4. SQLite 讀寫與資料表建立
5. RSA 金鑰對與 JWT 簽發驗證
6. `apps.yaml` App 註冊
7. Super Admin 設定
8. Teams Webhook（可選）

```bash
python scripts/preflight_check.py
python scripts/preflight_check.py --test-user <employee_name>
```

## 資料庫 Schema

### init_db.sql

SQLite 本地資料庫的完整 Schema 定義，包含以下資料表：

| 資料表                 | 用途                   |
| ---------------------- | ---------------------- |
| `user_accounts`        | 使用者帳號與密碼雜湊   |
| `auth_codes`           | 一次性授權碼（5 分鐘）  |
| `registration_tokens`  | 註冊連結 Token（24 小時）|
| `user_app_permissions` | 使用者對各 App 的權限   |
| `app_admins`           | App 管理員指派紀錄      |
| `admin_audit_log`      | 管理操作稽核日誌        |

> 注意：資料表由 Auth Center 啟動時的 `lifespan` 自動建立，此 SQL 檔僅供參考。
