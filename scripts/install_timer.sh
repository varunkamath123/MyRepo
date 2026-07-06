#!/bin/bash
# Run once on EC2 to install the daily token refresh timer.
# Usage: bash scripts/install_timer.sh

set -e
REPO=/opt/kronos_bot/repo

echo "==> Installing systemd units..."
sudo cp $REPO/scripts/upstox_token_refresh.service /etc/systemd/system/
sudo cp $REPO/scripts/upstox_token_refresh.timer   /etc/systemd/system/

echo "==> Installing pyotp + playwright in venv..."
/opt/kronos_bot/venv/bin/pip install pyotp playwright --quiet
/opt/kronos_bot/venv/bin/playwright install chromium --with-deps

echo "==> Enabling and starting timer..."
sudo systemctl daemon-reload
sudo systemctl enable upstox_token_refresh.timer
sudo systemctl start  upstox_token_refresh.timer

echo ""
echo "Timer status:"
sudo systemctl status upstox_token_refresh.timer --no-pager

echo ""
echo "Next trigger:"
sudo systemctl list-timers upstox_token_refresh.timer --no-pager
