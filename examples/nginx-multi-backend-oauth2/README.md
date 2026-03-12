# 多後端 OAuth2 Proxy 範例 — Nginx auth_request 模式

同一個 `server_name` 下掛載多個後端服務，共用一組 OAuth2 認證。

## 適用場景

```
sa-help.company.com
├── /          → 前端應用（Vue / React / 靜態頁面等）
└── /fs/       → HFS 檔案伺服器
```

兩個路徑共用一個 OAuth2 Proxy redirect URL：`http://sa-help.company.com/oauth2/callback`

## 與 hfs-oauth2-proxy 範例的差異

| | hfs-oauth2-proxy | 本範例 (auth_request) |
|---|---|---|
| **OAuth2 Proxy 角色** | 反向代理（轉發所有流量） | 僅認證判斷 |
| **後端數量** | 1 個 | 多個 |
| **路由** | OAuth2 Proxy 決定 | Nginx 決定 |
| **HFS port** | 不暴露 | 暴露 localhost:8080 |

## 架構

```
使用者瀏覽器
    │
    ▼
  Nginx (:80/443)
    │
    ├── GET /oauth2/auth ──────► OAuth2 Proxy (127.0.0.1:4180)
    │      「這人登入了嗎？」          │
    │      ◄── 202 已登入 ──────────┘
    │      ◄── 401 未登入 → 導向 /oauth2/sign_in → AuthCenter OIDC
    │
    ├── /          ──(auth OK)──► 前端應用 (127.0.0.1:3000)
    │
    └── /fs/       ──(auth OK)──► HFS 檔案伺服器 (127.0.0.1:8080)
```

## 部署步驟

### 1. 在 AuthCenter 註冊 App

將 `apps.yaml` 的內容加入 AuthCenter 的 `config/apps.yaml`，記得替換 `client_secret` 為 bcrypt hash。

### 2. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env，填入實際值
```

產生 Cookie Secret：
```bash
openssl rand -base64 32 | head -c 32
```

### 3. 啟動 OAuth2 Proxy + HFS

```bash
docker compose up -d
```

### 4. 部署前端應用

自行將前端部署到 `127.0.0.1:3000`（或修改 `nginx.conf` 中的 port）。

如果前端是靜態檔案，也可以直接讓 Nginx serve：
```nginx
location / {
    auth_request /oauth2/auth;
    error_page 401 = /oauth2/sign_in?rd=$scheme://$host$request_uri;

    root /var/www/sa-help;
    index index.html;
    try_files $uri $uri/ /index.html;
}
```

### 5. 設定 Nginx

```bash
# 複製設定檔
sudo cp nginx.conf /etc/nginx/sites-available/sa-help.company.com
sudo ln -s /etc/nginx/sites-available/sa-help.company.com /etc/nginx/sites-enabled/

# 確認 nginx.conf 主設定有 WebSocket map（見 nginx.conf 底部說明）

# 測試並重載
sudo nginx -t && sudo systemctl reload nginx
```

### 6. 驗證

1. 瀏覽 `http://sa-help.company.com/` → 應導向 AuthCenter 登入
2. 登入後 → 看到前端應用
3. 瀏覽 `http://sa-help.company.com/fs/` → 看到 HFS 檔案伺服器（不需要再次登入）

## 注意事項

- **HFS sub-path 掛載**：Nginx 的 `proxy_pass http://127.0.0.1:8080`（不帶尾部 `/`）會將路徑原樣保留。需要在 HFS 的 VFS 中建立 `/fs` 資料夾，讓瀏覽器 URL 與 HFS 內部路徑一致，避免 HFS 前端 SPA 路由錯亂。
- **`/fs` 無尾部斜線**：`location = /fs` 會 301 redirect 到 `/fs/`，避免請求落入 `location /` 導致登入後 redirect 到根路徑而非 `/fs/`。
- **Cookie 共享**：`/` 和 `/fs/` 在同一個域名下，OAuth2 Proxy 的 session cookie 自動共享。登入一次就能存取所有路徑。
- **HTTPS**：正式環境務必啟用 HTTPS，並將 `COOKIE_SECURE` 改為 `true`。
- **HFS localhost port**：雖然 HFS 暴露了 `127.0.0.1:8080`，但僅限本機存取。外部流量必須經過 Nginx 的 auth_request 認證才能到達。
