#!/bin/bash
# Install or refresh the India overnight timers on EC2. Paper only until logs/overnight/LIVE_ENABLED exists.
set -euo pipefail
REPO=/opt/trading_bot/repo
TIMERS="entry amo morning reconcile"

sudo cp "$REPO/scripts/fno_t_bot_ovn@.service" /etc/systemd/system/
for t in $TIMERS; do
  sudo cp "$REPO/scripts/fno_t_bot_ovn@$t.timer" /etc/systemd/system/
done
sudo systemctl daemon-reload
for t in $TIMERS; do
  sudo systemctl enable --now "fno_t_bot_ovn@$t.timer"
done
systemctl list-timers 'fno_t_bot_ovn@*' --no-pager
