#!/data/data/com.termux/files/usr/bin/bash
############################################
#  DealBot ONE-COMMAND INSTALLER (Android)
#  Use inside Termux app. Installs python,
#  starts bot 24/7 with auto-restart.
############################################
set -e
GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}   DealBot 24/7 Installer - @mck_deal_bot${NC}"
echo -e "${CYAN}========================================${NC}"

echo "[1/5] Installing Python (2-3 min first time)..."
pkg update -y >/dev/null 2>&1 || true
pkg install -y python >/dev/null 2>&1

echo "[2/5] Installing libraries..."
pip install --upgrade pip >/dev/null 2>&1 || true
pip install -q requests beautifulsoup4 lxml

echo "[3/5] Finding DealBot folder..."
if [ -d "$HOME/dealbot" ]; then
    cd "$HOME/dealbot"
elif [ -d "$HOME/storage/downloads/dealbot" ]; then
    cp -r "$HOME/storage/downloads/dealbot" "$HOME/dealbot"
    cd "$HOME/dealbot"
elif [ -d "/sdcard/Download/dealbot" ]; then
    cp -r /sdcard/Download/dealbot "$HOME/dealbot"
    cd "$HOME/dealbot"
else
    echo ""
    echo "❌ Put the 'dealbot' folder in Download folder first:"
    echo "   (extract telegram-deal-bot.zip there, then run this again)"
    exit 1
fi
mkdir -p logs data

echo "[4/5] Keeping phone awake for the bot..."
termux-wake-lock 2>/dev/null || echo "   (optional: install termux-api for wake-lock)"

echo "[5/5] Starting DealBot 24/7..."
nohup bash supervisor.sh > /dev/null 2>&1 &
sleep 4
if pgrep -f "python3 bot.py" > /dev/null; then
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  ✅ DealBot is RUNNING 24/7!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo "  NOW open Telegram →  t.me/mck_deal_bot"
    echo "  → press START → send  /start"
    echo ""
    echo "  Deals arrive automatically (every minute check)."
    echo "  Group: add @mck_deal_bot to group → send /start@mck_deal_bot"
    echo ""
    echo "  KEEP TERMUX APP OPEN (or in notifications)."
    echo "  Bot auto-restarts if it ever crashes."
    echo "  Stop bot:  pkill -f bot.py"
else
    echo "❌ Bot failed to start - check logs/bot.log"
fi
