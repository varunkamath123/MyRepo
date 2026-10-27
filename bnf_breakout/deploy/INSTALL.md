# Installing on the EC2 host

```bash
sudo /opt/trading_bot/deploy.sh                 # pulls the repo (includes bnf_breakout/)
sudo mkdir -p /opt/trading_bot/bnf_state/logs
sudo chown -R tradingbot:tradingbot /opt/trading_bot/bnf_state
sudo cp /opt/trading_bot/repo/bnf_breakout/deploy/bnf_breakout.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/fno_t_bot_auth.service.d
sudo cp /opt/trading_bot/repo/bnf_breakout/deploy/fno_t_bot_auth.override.conf \n        /etc/systemd/system/fno_t_bot_auth.service.d/override.conf   # REQUIRED: stops the daily token job restarting the parked bots
sudo systemctl daemon-reload

# dry check - makes NO API calls (reads local history, builds the day context)
sudo -u tradingbot bash -c 'cd /opt/trading_bot/bnf_state && /opt/trading_bot/venv/bin/python /opt/trading_bot/repo/bnf_breakout/bnf_runner.py --preflight'

sudo systemctl enable --now bnf_breakout
sudo -u tradingbot /opt/trading_bot/venv/bin/python /opt/trading_bot/repo/bnf_breakout/bnf_runner.py --status
```

Reviving the parked multi-strategy system: `git checkout v2.0-parked-multistrategy-oct2026`
and `sudo systemctl enable --now fno_t_bot_nifty fno_t_bot_banknifty fno_t_bot_sensex`
(plus the `fno_t_bot_ovn@*.timer`, `fno_t_bot_debrief`, `fno_t_bot_morning`,
`fno_t_bot_weekly_analyzer` timers). Do not run both: together they exhausted the
Fyers daily quota.
