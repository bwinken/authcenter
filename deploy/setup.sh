#!/bin/bash
# AuthCenter 部署腳本（User-Level）
# 用法: bash deploy/setup.sh
# 部署到 ~/authcenter，以當前使用者身份執行（不需要 sudo）
set -e

APP_DIR="$HOME/authcenter"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Proxy 設定（只需設定 http_proxy 即可，不需要則留空）
PROXY_URL="${http_proxy:-}"
if [ -n "$PROXY_URL" ]; then
    export http_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL"
    export https_proxy="$PROXY_URL"
    export HTTPS_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,*.company.local"
    export NO_PROXY="$no_proxy"
    echo "使用 Proxy: $PROXY_URL"
fi

# === 前置檢查 ===
if ! command -v uv &>/dev/null; then
    echo "uv 未安裝，自動安裝中..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        echo "錯誤：uv 自動安裝失敗，請手動安裝 (https://docs.astral.sh/uv/)" >&2
        exit 1
    fi
    echo "uv 已安裝：$(uv --version)"
fi

echo "=== 1. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='config/apps.yaml' \
    --exclude='keys/' --exclude='.env' \
    "$SCRIPT_DIR/" "$APP_DIR/"

mkdir -p "$APP_DIR/config"
if [ ! -f "$APP_DIR/config/apps.yaml" ]; then
    echo "apps: {}" > "$APP_DIR/config/apps.yaml"
    echo "已建立空的 apps.yaml（透過管理後台註冊 App）"
fi

echo "=== 2. 安裝依賴（uv sync）==="
cd "$APP_DIR" && uv sync

echo "=== 3. 產生 RSA 金鑰（如尚未存在）==="
if [ ! -f "$APP_DIR/keys/private.pem" ]; then
    cd "$APP_DIR" && uv run python generate_keys.py
fi

echo "=== 4. 檢查 .env ==="
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "警告：$APP_DIR/.env 尚未建立，跳過服務啟動。" >&2
    echo "請執行以下步驟完成部署：" >&2
    echo "  1. cp $APP_DIR/.env.example $APP_DIR/.env" >&2
    echo "  2. nano $APP_DIR/.env  （填入實際設定）" >&2
    echo "  3. 重新執行此腳本" >&2
    exit 1
fi
chmod 600 "$APP_DIR/.env"
chmod 600 "$APP_DIR/keys/"*.pem

echo "=== 5. 安裝 user-level systemd service ==="
mkdir -p "$HOME/.config/systemd/user"
cp "$APP_DIR/deploy/authcenter.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable authcenter
systemctl --user restart authcenter

# 確保使用者登出後服務仍繼續執行
echo "=== 6. 啟用 lingering（登出後保持服務執行）==="
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami) 以確保登出後服務持續運行"

echo "=== 7. 安裝 nginx 設定（需要 sudo）==="
if command -v nginx &>/dev/null; then
    sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/authcenter.nginx.conf" \
        | sudo tee /etc/nginx/sites-available/authcenter > /dev/null
    sudo ln -sf /etc/nginx/sites-available/authcenter /etc/nginx/sites-enabled/authcenter
    sudo nginx -t && sudo systemctl reload nginx
else
    echo "提醒：nginx 未安裝，請手動設定反向代理"
fi

echo ""
echo "=== 部署完成 ==="
echo "應用目錄：$APP_DIR"
echo "服務管理："
echo "  systemctl --user status authcenter    # 查看狀態"
echo "  systemctl --user restart authcenter   # 重啟"
echo "  journalctl --user -u authcenter -f    # 查看日誌"
echo ""
echo "請確認："
echo "  1. 已建立 $APP_DIR/.env（參考 .env.example）"
echo "  2. 已設定 AUTH_CENTER_BASE_URL 為實際網址"
echo "  3. 已修改 nginx 設定中的 server_name 為實際域名"
