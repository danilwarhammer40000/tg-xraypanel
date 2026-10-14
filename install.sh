#!/bin/bash
set -euo pipefail

echo "=== XrayPanel Installer ==="

# -------------------------
# CONFIG
# -------------------------
DEFAULT_DIR="/opt/xraypanel"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"
# Where the code is cloned from. Point it at YOUR repository:
#   REPO_URL=https://github.com/<you>/tg-xraypanel.git sudo -E bash install.sh
REPO_URL="${REPO_URL:-https://github.com/danilwarhammer40000/tg-xraypanel.git}"
# Installing on top of a running manually-installed stand: skip the system-wide
# `apt upgrade` (it can restart nginx/xray in the middle of the migration).
SKIP_APT_UPGRADE="${SKIP_APT_UPGRADE:-0}"

# -------------------------
# SYSTEM DEPENDENCIES
# -------------------------
echo "[1/9] Installing system dependencies..."

export DEBIAN_FRONTEND=noninteractive
apt update -y
if [ "$SKIP_APT_UPGRADE" != "1" ]; then
    apt upgrade -y
fi
# rsync: delivers subscription files to the subscription host; dnsutils: dig for CDN "resolve" entries
apt install -y python3 python3-venv python3-pip git curl ca-certificates rsync dnsutils

# -------------------------
# INSTALL / UPDATE
# -------------------------
echo "[2/9] Preparing install directory..."

if [ -d "$PROJECT_DIR/.git" ]; then
    echo "[INFO] Updating repository..."
    cd "$PROJECT_DIR"
    git fetch --all
    git reset --hard origin/main
    git pull --ff-only
else
    echo "[INFO] Cloning repository..."
    rm -rf "$PROJECT_DIR"
    git clone "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

# -------------------------
# VENV
# -------------------------
echo "[3/9] Setting up virtual environment..."

if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi

source "$PROJECT_DIR/venv/bin/activate"

python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# -------------------------
# INPUT
# -------------------------
echo "[4/9] Configuration"

read -r -p "BOT_TOKEN: " BOT_TOKEN
read -r -p "ADMIN_ID: " ADMIN_ID

if [[ -z "$BOT_TOKEN" || -z "$ADMIN_ID" ]]; then
    echo "[ERROR] Required variables missing"
    exit 1
fi

BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '\r')
ADMIN_ID=$(echo "$ADMIN_ID" | tr -d '\r')


# Optional — AI auto-renewal (🤖 Автопродление). Both empty = feature
# stays off and unconfigured; the bot itself also refuses to let it be
# turned ON without LOG_CHANNEL_ID (every decision must be logged).
echo ""
echo "AI auto-renewal (optional) — reads payment receipts via Gemini and can"
echo "auto-extend access at night / when a request sits unanswered too long."
echo "Get a free key: https://aistudio.google.com/apikey"
read -r -p "GEMINI_API_KEY (Enter to skip this feature): " GEMINI_API_KEY
GEMINI_API_KEY=$(echo "$GEMINI_API_KEY" | tr -d '\r')

read -r -p "LOG_CHANNEL_ID (numeric channel id, needed for auto-renewal, Enter to skip): " LOG_CHANNEL_ID
LOG_CHANNEL_ID=$(echo "$LOG_CHANNEL_ID" | tr -d '\r')

# Optional — proxy for outbound Gemini API calls. Google's Gemini API
# enforces a region allowlist independent of API-key validity; a
# Russian-hosted VPS IP typically gets FAILED_PRECONDITION ("User
# location is not supported") even with a valid key. Two forms accepted,
# same as the in-bot "🌐 Прокси для Gemini API" setting (which can
# override this later without touching .env or restarting anything):
#   - a Worker / HTTPS reverse-proxy base URL, e.g.
#     https://your-worker.workers.dev
#   - a SOCKS5 endpoint, e.g. socks5h://user:pass@host:port
# Leave blank for a direct connection (no proxy).
echo ""
echo "Прокси для Gemini API (опционально, можно поменять позже в боте) —"
echo "Worker/HTTPS URL (https://...) или SOCKS5 (socks5h://user:pass@host:port)."
read -r -p "GEMINI_PROXY_URL (Enter чтобы пропустить): " GEMINI_PROXY_URL
GEMINI_PROXY_URL=$(echo "$GEMINI_PROXY_URL" | tr -d '\r')

# -------------------------
# ENV
# -------------------------
echo "[5/9] Writing .env..."

cat > "$PROJECT_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_ID=$ADMIN_ID
GEMINI_API_KEY=$GEMINI_API_KEY
GEMINI_PROXY_URL=$GEMINI_PROXY_URL
LOG_CHANNEL_ID=$LOG_CHANNEL_ID
PYTHONPATH=$PROJECT_DIR
EOF

chmod 600 "$PROJECT_DIR/.env"

# -------------------------
# DATA DIRECTORY (users.json etc. contain plaintext VPN passwords)
# -------------------------
echo "[6/9] Preparing data directory..."

mkdir -p "$PROJECT_DIR/data"
chmod 700 "$PROJECT_DIR/data"

# -------------------------
# SYSTEMD UNITS
# -------------------------
echo "[7/9] Installing systemd units..."

install_unit () {
    local name=$1
    local src="$PROJECT_DIR/systemd/$name"

    if [ -f "$src" ]; then
        echo "[INFO] Installing $name"
        cp "$src" "/etc/systemd/system/$name"
        systemctl enable "$name"
    else
        echo "[SKIP] $name not found"
    fi
}

# CHANGED: xraypanel-bot.service now comes from the repo, same as the
# cleanup/backup units, instead of a separate heredoc maintained here. This
# was a real source of drift — the old heredoc had different hardening
# settings than the file committed under systemd/, and only the heredoc
# version was ever actually installed.
install_unit "xraypanel-bot.service"
install_unit "xraypanel-cleanup.service"
install_unit "xraypanel-backup.service"
install_unit "xraypanel-post-disable.service"
install_unit "xraypanel-auto-renewal-check.service"
install_unit "xraypanel-xray-sub.service"
install_unit "xraypanel-cleanup.timer"
install_unit "xraypanel-backup.timer"
install_unit "xraypanel-post-disable.timer"
install_unit "xraypanel-auto-renewal-check.timer"
install_unit "xraypanel-xray-sub.timer"

# -------------------------
# SYSTEMD APPLY
# -------------------------
echo "[8/9] Reloading systemd..."

systemctl daemon-reload

systemctl stop xraypanel-bot.service 2>/dev/null || true

systemctl enable xraypanel-bot.service
systemctl restart xraypanel-bot.service

systemctl start xraypanel-cleanup.timer 2>/dev/null || true
systemctl start xraypanel-backup.timer 2>/dev/null || true
systemctl start xraypanel-post-disable.timer 2>/dev/null || true
systemctl start xraypanel-auto-renewal-check.timer 2>/dev/null || true
systemctl start xraypanel-xray-sub.timer 2>/dev/null || true

# -------------------------
# HEALTH CHECK
# -------------------------
echo "[9/9] Checking services..."

sleep 3

echo ""
echo "=== BOT STATUS ==="
if systemctl is-active --quiet xraypanel-bot.service; then
    echo "✅ BOT RUNNING"
else
    echo "❌ BOT FAILED"
    systemctl status xraypanel-bot.service --no-pager || true
fi

echo ""
echo "=== TIMERS ==="
systemctl list-timers | grep xraypanel || true

echo ""
echo "DONE"
echo "Bot logs: journalctl -u xraypanel-bot.service -f"
