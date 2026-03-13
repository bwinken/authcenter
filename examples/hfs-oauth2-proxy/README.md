# HFS 檔案伺服器 + OAuth2 Proxy 部署範例

透過 [OAuth2 Proxy](https://oauth2-proxy.github.io/oauth2-proxy/) 為 [Rejetto HFS](https://github.com/rejetto/hfs) 檔案伺服器加上 AuthCenter OIDC 認證保護。

## 架構

```
使用者瀏覽器
    │
    ▼
┌─────────────────┐
│  Nginx (Host)   │  :80 / :443
│  反向代理        │
└────────┬────────┘
         │ proxy_pass 127.0.0.1:4180
         ▼
┌─────────────────┐      OIDC       ┌─────────────────┐
│  OAuth2 Proxy   │ ◄─────────────► │   AuthCenter    │
│  (Docker)       │  登入/驗證       │   (OIDC Provider)│
└────────┬────────┘                 └─────────────────┘
         │ Docker 內網
         ▼
┌─────────────────┐
│  HFS 檔案伺服器  │
│  (Docker)       │
└─────────────────┘
```

**流量動線**：使用者 → Nginx → OAuth2 Proxy（檢查登入狀態）→ HFS

- 未登入 → OAuth2 Proxy 導向 AuthCenter 進行 OIDC 認證
- 已登入 → 直接轉發至 HFS 檔案伺服器

**安全設計**：
- OAuth2 Proxy 僅綁定 `127.0.0.1:4180`，不對外暴露
- HFS 不暴露任何 Port，僅允許 Docker 內網存取
- 所有對外流量必須經過 Nginx → OAuth2 Proxy 認證鏈

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `setup.sh` | 一鍵互動式部署腳本（推薦） |
| `docker-compose.yml` | OAuth2 Proxy + HFS 容器編排（`setup.sh` 會自動備份為 `.template` 再修改） |
| `.env.example` | 環境變數範本 |
| `nginx.conf` | Nginx 反向代理參考設定（手動部署用） |
| `apps.yaml` | AuthCenter App 註冊範本 |

## 快速部署（一鍵腳本）

```bash
bash setup.sh
```

腳本會互動式引導你完成所有設定：

1. **基本設定** — 網域名稱、HTTP/HTTPS 選擇
2. **OIDC 設定** — AuthCenter URL、Client ID/Secret
3. **HFS 設定** — 管理員帳密、檔案儲存路徑
4. **自動產生** — `.env`、Cookie Secret（`docker-compose.yml` 原始模板自動備份為 `.template`）
5. **部署 Nginx** — 設定檔寫入 `sites-available/`，建立 `sites-enabled/` symlink
6. **啟動容器** — `docker compose up -d`

### 前置需求

- Docker + Docker Compose v2
- Nginx（systemd 管理）
- openssl

## 手動部署

如果不使用 `setup.sh`，按以下步驟手動部署：

### 1. 在 AuthCenter 註冊 App

將 `apps.yaml` 的內容合併到 AuthCenter 的 `config/apps.yaml`：

```yaml
- app_id: "hfs_file_server"
  name: "內部檔案伺服器"
  client_secret: "<bcrypt hash>"       # 見下方產生方式
  redirect_uri: "https://files.company.com/oauth2/callback"
  app_url: "https://files.company.com" # Dashboard「前往」按鈕連結目標
  allowed_orgs: []
  default_level: 0
  token_expire_hours: 24
```

產生 bcrypt hash：

```bash
python -c "from passlib.hash import bcrypt; print(bcrypt.hash('你的明文密碼'))"
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入：

| 變數 | 說明 |
|------|------|
| `OIDC_ISSUER_URL` | AuthCenter 的對外 URL（如 `https://auth.company.com`） |
| `CLIENT_ID` | 在 AuthCenter 註冊的 `app_id` |
| `CLIENT_SECRET` | 明文 Client Secret（AuthCenter 端存 bcrypt hash） |
| `REDIRECT_URL` | OAuth2 callback URL（如 `https://files.company.com/oauth2/callback`） |
| `COOKIE_SECRET` | 用 `openssl rand -base64 32` 產生 |
| `COOKIE_REFRESH` | Cookie 重新驗證間隔（預設 `1h`），token 過期後自動強制重新登入 |
| `OIDC_EXTRA_HOST` | 內網 DNS 對應，格式 `hostname:ip`（不需要則留空） |
| `HFS_ADMIN_USER` | HFS 管理員帳號（預設 `admin`） |
| `HFS_ADMIN_PASSWORD` | HFS 管理員密碼 |

### 3. 部署 Nginx

```bash
# 複製設定檔（修改 server_name 為你的網域）
sudo cp nginx.conf /etc/nginx/sites-available/files.company.com

# 建立 symlink 啟用
sudo ln -s /etc/nginx/sites-available/files.company.com /etc/nginx/sites-enabled/

# 確認 nginx.conf 的 http {} 區塊內有 WebSocket upgrade map：
#   map $http_upgrade $connection_upgrade {
#       default upgrade;
#       ''      close;
#   }

# 測試並重新載入
sudo nginx -t && sudo systemctl reload nginx
```

### 4. 啟動容器

```bash
docker compose up -d
```

## 內網 DNS 問題

如果 AuthCenter 使用內網 DNS alias（如 `auth.company.com` 指向內網 IP），Docker 容器無法解析該 hostname。

**使用 `setup.sh`**：腳本會詢問是否需要設定，自動在 `docker-compose.yml` 中啟用 `extra_hosts`。

**手動部署**：取消 `docker-compose.yml` 中 `extra_hosts` 的註解，填入實際值：

```yaml
    extra_hosts:
      - "auth.company.com:192.168.1.100"
```

效果等同 `docker run --add-host`。如果 AuthCenter 有正式 DNS 記錄（公網可解析），則不需要設定。

## HTTPS 設定

`setup.sh` 支援 HTTPS 模式，會自動：
- 產生含 SSL 的 Nginx 設定（HTTP 301 重導至 HTTPS）
- 將 `COOKIE_SECURE` 設為 `true`

手動部署時，參考 `nginx.conf` 底部的 HTTPS 範本，並修改 `docker-compose.yml`：

```yaml
OAUTH2_PROXY_COOKIE_SECURE: "true"
```

## 常用指令

```bash
# 查看日誌
docker compose logs -f

# 僅看 OAuth2 Proxy 日誌
docker compose logs -f oauth2-proxy

# 重啟服務
docker compose restart

# 停止服務
docker compose down

# 更新映像
docker compose pull && docker compose up -d
```

## 疑難排解

### OAuth2 Proxy 啟動失敗

```bash
docker compose logs oauth2-proxy
```

常見原因：
- `OIDC_ISSUER_URL` 無法連線 — 檢查 AuthCenter 是否正常運行，以及 DNS 是否可解析
- `CLIENT_SECRET` 錯誤 — 確認填入的是明文，不是 bcrypt hash
- `REDIRECT_URL` 不匹配 — 必須與 AuthCenter `apps.yaml` 的 `redirect_uri` 完全一致

### 登入後 403

- 使用者在 AuthCenter 沒有該 App 的權限（`user_app_permissions` 未設定）
- 如果 `allowed_orgs` 不為空，使用者的組織不在允許清單中

### HFS 管理員登入

HFS 自帶的管理介面帳密在 `.env` 中設定（`HFS_ADMIN_USER` / `HFS_ADMIN_PASSWORD`）。這是 HFS 內部的帳號，與 AuthCenter 無關。
