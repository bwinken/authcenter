#!/usr/bin/env bash
# ============================================================
# 一鍵部署：OAuth2 Proxy + HFS 檔案伺服器
# ============================================================
# 使用方式：
#   bash setup.sh
#
# 互動式引導：
#   1. 輸入網域名稱、AuthCenter URL、Client 憑證
#   2. 自動產生 Cookie Secret
#   3. 自動部署 Nginx + Docker Compose
#
# 前置需求：
#   - Docker + Docker Compose
#   - Nginx (systemd)
#   - openssl（產生 Cookie Secret）
# ============================================================

set -euo pipefail

# ─── 顏色定義 ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Step 0：前置檢查 ─────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  OAuth2 Proxy + HFS 檔案伺服器 — 一鍵部署腳本  ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

info "檢查前置需求..."

missing=()
command -v docker    >/dev/null 2>&1 || missing+=("docker")
command -v nginx     >/dev/null 2>&1 || missing+=("nginx")
command -v openssl   >/dev/null 2>&1 || missing+=("openssl")

if [[ ${#missing[@]} -gt 0 ]]; then
    err "缺少必要工具：${missing[*]}"
    echo "  請先安裝後再執行此腳本。"
    exit 1
fi

# 檢查 docker compose (v2) 或 docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
else
    err "找不到 docker compose，請安裝 Docker Compose v2。"
    exit 1
fi

ok "前置需求檢查通過（docker, nginx, openssl, $DOCKER_COMPOSE）"
echo ""

# ─── Step 1：互動式輸入 ───────────────────────────────────
echo -e "${BOLD}── Step 1：基本設定 ──${NC}"
echo ""

read -rp "  檔案伺服器網域名稱（例如 files.company.com）: " SERVER_NAME
if [[ -z "$SERVER_NAME" ]]; then
    err "網域名稱不能為空。"
    exit 1
fi

read -rp "  使用 HTTPS？(y/N): " USE_HTTPS
USE_HTTPS="${USE_HTTPS,,}"  # lowercase

if [[ "$USE_HTTPS" == "y" ]]; then
    SCHEME="https"
    read -rp "  SSL 憑證路徑 (fullchain.pem): " SSL_CERT
    read -rp "  SSL 私鑰路徑 (privkey.pem): " SSL_KEY
    if [[ ! -f "$SSL_CERT" ]] || [[ ! -f "$SSL_KEY" ]]; then
        err "SSL 憑證或私鑰檔案不存在，請確認路徑。"
        exit 1
    fi
else
    SCHEME="http"
fi

echo ""
echo -e "${BOLD}── Step 2：AuthCenter (OIDC) 設定 ──${NC}"
echo ""

read -rp "  AuthCenter URL（例如 https://auth.company.com）: " OIDC_ISSUER_URL
if [[ -z "$OIDC_ISSUER_URL" ]]; then
    err "AuthCenter URL 不能為空。"
    exit 1
fi
# 移除尾端斜線
OIDC_ISSUER_URL="${OIDC_ISSUER_URL%/}"

read -rp "  Client ID（在 AuthCenter 註冊的 app_id）[hfs_file_server]: " CLIENT_ID
CLIENT_ID="${CLIENT_ID:-hfs_file_server}"

read -rp "  Client Secret（明文，非 bcrypt hash）: " CLIENT_SECRET
if [[ -z "$CLIENT_SECRET" ]]; then
    err "Client Secret 不能為空。"
    exit 1
fi

REDIRECT_URL="${SCHEME}://${SERVER_NAME}/oauth2/callback"
info "Redirect URL 自動設定為：${REDIRECT_URL}"
echo "  （此 URL 必須與 AuthCenter apps.yaml 的 redirect_uri 完全一致）"

# 從 OIDC_ISSUER_URL 提取 hostname
OIDC_HOSTNAME="$(echo "$OIDC_ISSUER_URL" | sed -E 's|https?://||; s|[:/].*||')"

echo ""
read -rp "  AuthCenter 是內網 alias 嗎？Docker 容器需要 extra_hosts 才能解析。(y/N): " NEED_EXTRA_HOST
NEED_EXTRA_HOST="${NEED_EXTRA_HOST,,}"
OIDC_EXTRA_HOST=""
if [[ "$NEED_EXTRA_HOST" == "y" ]]; then
    read -rp "  ${OIDC_HOSTNAME} 對應的 IP 位址: " OIDC_HOST_IP
    if [[ -z "$OIDC_HOST_IP" ]]; then
        err "IP 位址不能為空。"
        exit 1
    fi
    OIDC_EXTRA_HOST="${OIDC_HOSTNAME}:${OIDC_HOST_IP}"
    ok "extra_hosts 設定為 ${OIDC_EXTRA_HOST}"
fi

echo ""
echo -e "${BOLD}── Step 3：HFS 設定 ──${NC}"
echo ""

read -rp "  HFS 管理員帳號 [admin]: " HFS_ADMIN_USER
HFS_ADMIN_USER="${HFS_ADMIN_USER:-admin}"

read -rp "  HFS 管理員密碼 [自動產生]: " HFS_ADMIN_PASSWORD
if [[ -z "$HFS_ADMIN_PASSWORD" ]]; then
    HFS_ADMIN_PASSWORD="$(openssl rand -base64 12)"
    info "自動產生 HFS 管理員密碼：${HFS_ADMIN_PASSWORD}"
fi

read -rp "  檔案儲存路徑 [./data/files]: " FILES_PATH
FILES_PATH="${FILES_PATH:-./data/files}"

# ─── Step 4：產生設定檔 ───────────────────────────────────
echo ""
echo -e "${BOLD}── Step 4：產生設定檔 ──${NC}"
echo ""

COOKIE_SECRET="$(openssl rand -base64 32)"
ok "Cookie Secret 已自動產生"

# 建立資料目錄
mkdir -p "$SCRIPT_DIR/data/files" "$SCRIPT_DIR/data/hfs-config"
ok "資料目錄已建立"

# 產生 .env（用 printf 避免 CLIENT_SECRET 中的特殊字元被 shell 展開）
{
    echo "# ── 此檔案由 setup.sh 自動產生 ──"
    echo "OIDC_ISSUER_URL=${OIDC_ISSUER_URL}"
    echo "CLIENT_ID=${CLIENT_ID}"
    printf 'CLIENT_SECRET=%s\n' "$CLIENT_SECRET"
    echo "REDIRECT_URL=${REDIRECT_URL}"
    printf 'COOKIE_SECRET=%s\n' "$COOKIE_SECRET"
    echo "HFS_ADMIN_USER=${HFS_ADMIN_USER}"
    printf 'HFS_ADMIN_PASSWORD=%s\n' "$HFS_ADMIN_PASSWORD"
} > "$SCRIPT_DIR/.env"
ok ".env 已產生"

# 複製一份 docker-compose.yml 作為工作副本（保留原始模板）
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
COMPOSE_TEMPLATE="$SCRIPT_DIR/docker-compose.yml.template"
if [[ ! -f "$COMPOSE_TEMPLATE" ]]; then
    cp "$COMPOSE_FILE" "$COMPOSE_TEMPLATE"
else
    # 重複執行時，從模板還原再修改
    cp "$COMPOSE_TEMPLATE" "$COMPOSE_FILE"
fi
ok "docker-compose.yml 工作副本已建立（原始模板保存為 .template）"

# 如果使用者自訂了 FILES_PATH，更新 docker-compose.yml 的 volume
if [[ "$FILES_PATH" != "./data/files" ]]; then
    sed -i "s|./data/files:/home/hfs/files|${FILES_PATH}:/home/hfs/files|" "$COMPOSE_FILE"
    ok "docker-compose.yml 檔案路徑已更新為 ${FILES_PATH}"
fi

# 如果需要 extra_hosts，取消註解並填入實際值
if [[ -n "$OIDC_EXTRA_HOST" ]]; then
    sed -i 's|    # extra_hosts:|    extra_hosts:|' "$COMPOSE_FILE"
    sed -i "s|    #   - \"auth.company.com:192.168.1.100\"|      - \"${OIDC_EXTRA_HOST}\"|" "$COMPOSE_FILE"
    ok "extra_hosts 已啟用：${OIDC_EXTRA_HOST}"
fi

# 如果使用 HTTPS，更新 docker-compose 的 COOKIE_SECURE
if [[ "$SCHEME" == "https" ]]; then
    sed -i 's/OAUTH2_PROXY_COOKIE_SECURE: "false"/OAUTH2_PROXY_COOKIE_SECURE: "true"/' "$COMPOSE_FILE"
    ok "Cookie Secure 已設為 true（HTTPS 模式）"
fi

# ─── Step 5：部署 Nginx ───────────────────────────────────
echo ""
echo -e "${BOLD}── Step 5：部署 Nginx ──${NC}"
echo ""

NGINX_CONF="/etc/nginx/sites-available/${SERVER_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SERVER_NAME}"

# 檢查 WebSocket upgrade map 是否存在
if ! grep -q 'map.*\$http_upgrade.*\$connection_upgrade' /etc/nginx/nginx.conf 2>/dev/null; then
    warn "Nginx 主設定中缺少 WebSocket upgrade map"
    echo "  請手動將以下內容加到 /etc/nginx/nginx.conf 的 http {} 區塊內："
    echo ""
    echo "    map \$http_upgrade \$connection_upgrade {"
    echo "        default upgrade;"
    echo "        ''      close;"
    echo "    }"
    echo ""
fi

# 產生 Nginx 設定檔
if [[ "$SCHEME" == "https" ]]; then
    cat > /tmp/nginx-hfs.conf << NGINX_EOF
server {
    listen 80;
    server_name ${SERVER_NAME};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${SERVER_NAME};

    ssl_certificate     ${SSL_CERT};
    ssl_certificate_key ${SSL_KEY};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 10G;
    proxy_buffering off;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:4180;

        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host  \$host;
        proxy_set_header X-Forwarded-Port  \$server_port;

        proxy_http_version 1.1;
        proxy_set_header Upgrade    \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;

        proxy_connect_timeout 60s;
        proxy_send_timeout    600s;
        proxy_read_timeout    600s;
    }
}
NGINX_EOF
else
    cat > /tmp/nginx-hfs.conf << NGINX_EOF
server {
    listen 80;
    server_name ${SERVER_NAME};

    client_max_body_size 10G;
    proxy_buffering off;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:4180;

        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host  \$host;
        proxy_set_header X-Forwarded-Port  \$server_port;

        proxy_http_version 1.1;
        proxy_set_header Upgrade    \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;

        proxy_connect_timeout 60s;
        proxy_send_timeout    600s;
        proxy_read_timeout    600s;
    }
}
NGINX_EOF
fi

sudo cp /tmp/nginx-hfs.conf "$NGINX_CONF"
rm -f /tmp/nginx-hfs.conf
ok "Nginx 設定檔已寫入 ${NGINX_CONF}"

# 建立 symlink（如果尚未存在）
if [[ ! -L "$NGINX_ENABLED" ]]; then
    sudo ln -s "$NGINX_CONF" "$NGINX_ENABLED"
    ok "Nginx site 已啟用"
else
    info "Nginx site symlink 已存在，跳過"
fi

# 測試 Nginx 設定
if sudo nginx -t 2>&1; then
    ok "Nginx 設定語法正確"
    sudo systemctl reload nginx
    ok "Nginx 已重新載入"
else
    err "Nginx 設定語法錯誤，請手動修正 ${NGINX_CONF}"
    exit 1
fi

# ─── Step 6：啟動 Docker 容器 ─────────────────────────────
echo ""
echo -e "${BOLD}── Step 6：啟動服務 ──${NC}"
echo ""

cd "$SCRIPT_DIR"
$DOCKER_COMPOSE pull
ok "Docker 映像已拉取"

$DOCKER_COMPOSE up -d
ok "Docker 容器已啟動"

# ─── 完成 ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║              部署完成！                          ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  檔案伺服器：  ${GREEN}${SCHEME}://${SERVER_NAME}${NC}"
echo -e "  OIDC Provider：${CYAN}${OIDC_ISSUER_URL}${NC}"
echo -e "  Client ID：    ${CLIENT_ID}"
echo ""
echo -e "${BOLD}── AuthCenter 端設定提醒 ──${NC}"
echo ""
echo "  請確認 AuthCenter 的 config/apps.yaml 已註冊此 App："
echo ""
echo "    - app_id: \"${CLIENT_ID}\""
echo "      name: \"內部檔案伺服器\""
echo "      client_secret: \"<bcrypt hash>\"    # 用 python -c \"from passlib.hash import bcrypt; print(bcrypt.hash('${CLIENT_SECRET}'))\" 產生"
echo "      redirect_uri: \"${REDIRECT_URL}\""
echo ""
echo -e "${BOLD}── 常用指令 ──${NC}"
echo ""
echo "  查看日誌：     cd ${SCRIPT_DIR} && $DOCKER_COMPOSE logs -f"
echo "  重啟服務：     cd ${SCRIPT_DIR} && $DOCKER_COMPOSE restart"
echo "  停止服務：     cd ${SCRIPT_DIR} && $DOCKER_COMPOSE down"
echo "  HFS 管理員：   帳號=${HFS_ADMIN_USER}  密碼=${HFS_ADMIN_PASSWORD}"
echo ""
