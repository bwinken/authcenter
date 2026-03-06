#!/bin/bash
# AuthCenter 部署腳本
# 用法: sudo bash deploy/setup.sh
set -e

APP_DIR="/opt/authcenter"
APP_USER="authcenter"

# === 前置檢查 ===
if [ "$(id -u)" -ne 0 ]; then
    echo "錯誤：請使用 sudo 執行此腳本" >&2
    exit 1
fi

if ! command -v uv &>/dev/null; then
    echo "錯誤：找不到 uv，請先安裝 uv (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

if ! command -v rsync &>/dev/null; then
    echo "錯誤：找不到 rsync，請先安裝（apt install rsync）" >&2
    exit 1
fi

if ! command -v nginx &>/dev/null; then
    echo "錯誤：找不到 nginx，請先安裝（apt install nginx）" >&2
    exit 1
fi

# 首次部署時提醒建立 .env
if [ -d "$APP_DIR" ] && [ ! -f "$APP_DIR/.env" ]; then
    echo "錯誤：$APP_DIR/.env 不存在" >&2
    echo "請先建立：cp $APP_DIR/.env.example $APP_DIR/.env && nano $APP_DIR/.env" >&2
    exit 1
fi

# Proxy 設定（依環境修改，不需要則留空）
PROXY_URL="${HTTP_PROXY:-}"
if [ -n "$PROXY_URL" ]; then
    export HTTP_PROXY="$PROXY_URL"
    export HTTPS_PROXY="$PROXY_URL"
    export NO_PROXY="localhost,127.0.0.1"
    echo "使用 Proxy: $PROXY_URL"
fi

echo "=== 1. 建立系統使用者 ==="
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    echo "使用者 $APP_USER 已建立"
fi

echo "=== 2. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='config/apps.yaml' \
    --exclude='keys/' --exclude='.env' \
    "$(dirname "$0")/../" "$APP_DIR/"

mkdir -p "$APP_DIR/config"
if [ ! -f "$APP_DIR/config/apps.yaml" ]; then
    echo "apps: {}" > "$APP_DIR/config/apps.yaml"
    echo "已建立空的 apps.yaml（透過管理後台註冊 App）"
fi

echo "=== 3. 安裝依賴（uv sync）==="
cd "$APP_DIR" && uv sync

echo "=== 4. 產生 RSA 金鑰（如尚未存在）==="
if [ ! -f "$APP_DIR/keys/private.pem" ]; then
    cd "$APP_DIR" && uv run python generate_keys.py
fi

echo "=== 5. 設定檔案權限 ==="
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "警告：$APP_DIR/.env 尚未建立，跳過服務啟動。" >&2
    echo "請執行以下步驟完成部署：" >&2
    echo "  1. cp $APP_DIR/.env.example $APP_DIR/.env" >&2
    echo "  2. nano $APP_DIR/.env  （填入實際設定）" >&2
    echo "  3. 重新執行此腳本" >&2
    exit 1
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 600 "$APP_DIR/keys/"*.pem

echo "=== 6. 安裝 systemd service ==="
cp "$APP_DIR/deploy/authcenter.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable authcenter
systemctl restart authcenter

echo "=== 7. 安裝 nginx 設定 ==="
cp "$APP_DIR/deploy/authcenter.nginx.conf" /etc/nginx/sites-available/authcenter
ln -sf /etc/nginx/sites-available/authcenter /etc/nginx/sites-enabled/authcenter
nginx -t && systemctl reload nginx

echo ""
echo "=== 部署完成 ==="
echo "請確認："
echo "  1. 已建立 /opt/authcenter/.env（參考 .env.example）"
echo "  2. 已設定 AUTH_CENTER_BASE_URL 為實際網址"
echo "  3. 已修改 nginx 設定中的 server_name 為實際域名"
echo "  4. systemctl status authcenter 檢查服務狀態"
