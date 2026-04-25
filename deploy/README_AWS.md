# AWS Deployment Guide — Kronos Futures Bot

## Architecture (Cost-Optimized ~₹3,000/month)

```
┌─────────────────────────────────────────────────────────┐
│  t3.small (always-on) — ap-south-1                      │
│  - futures_bot.py (live loop)                           │
│  - Kronos-mini inference (CPU, 4.1M params)             │
│  - Upstox API calls                                     │
│  - FinGPT via Claude Haiku API (not hosted locally)     │
│  ~$15/month                                             │
└─────────────────────────────────────────────────────────┘
         +
┌─────────────────────────────────────────────────────────┐
│  Spot t3.medium (pre-market only, 08:30–09:15 IST)      │
│  - MiroFish-Offline (Ollama + Neo4j)                    │
│  - Runs swarm sim → writes score to shared file         │
│  - Auto-terminates after run                            │
│  ~$3/month (45 min/day × 22 days)                       │
└─────────────────────────────────────────────────────────┘
         +
┌─────────────────────────────────────────────────────────┐
│  Spot t3.large (on-demand, backtesting only)            │
│  - VectorBT India backtester                            │
│  - Kronos fine-tuning runs                              │
│  ~$5/month (occasional use)                             │
└─────────────────────────────────────────────────────────┘
```

## Step-by-Step EC2 Setup

### 1. Launch the live bot instance

```bash
# Launch t3.small, Amazon Linux 2023, ap-south-1
# Security group: allow SSH (22) from your IP only
# Storage: 20GB gp3

aws ec2 run-instances \
  --image-id ami-0f5ee92e2d63afc18 \
  --instance-type t3.small \
  --key-name kronos-key \
  --security-group-ids sg-XXXXXXXX \
  --region ap-south-1 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=kronos-futures-bot}]'
```

### 2. Initial setup on EC2

```bash
ssh -i kronos-key.pem ec2-user@<ELASTIC_IP>

# System setup
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git

# Create venv
sudo python3.11 -m venv /opt/kronos_bot/venv
sudo chown -R ec2-user:ec2-user /opt/kronos_bot

# Clone repo
git clone https://github.com/<YOUR_USERNAME>/kronos-futures-bot.git /opt/kronos_bot/app
cd /opt/kronos_bot/app
git submodule update --init --recursive

# Install dependencies
/opt/kronos_bot/venv/bin/pip install -r requirements.txt

# Set environment variables
echo "UPSTOX_API_KEY=your_key" >> /opt/kronos_bot/.env
echo "UPSTOX_API_SECRET=your_secret" >> /opt/kronos_bot/.env
echo "ANTHROPIC_API_KEY=your_key" >> /opt/kronos_bot/.env
```

### 3. Systemd service for live bot

```bash
sudo tee /etc/systemd/system/kronos_futures_bot.service > /dev/null <<EOF
[Unit]
Description=Kronos Futures Bot
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/kronos_bot/app
EnvironmentFile=/opt/kronos_bot/.env
ExecStart=/opt/kronos_bot/venv/bin/python futures_bot.py NIFTY BANKNIFTY SENSEX
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kronos_futures_bot
sudo systemctl start kronos_futures_bot
```

### 4. Systemd timer for Upstox daily auth (08:40 IST = 03:10 UTC)

```bash
sudo tee /etc/systemd/system/kronos_auth.service > /dev/null <<EOF
[Unit]
Description=Kronos Upstox Auth Refresh

[Service]
Type=oneshot
User=ec2-user
WorkingDirectory=/opt/kronos_bot/app
EnvironmentFile=/opt/kronos_bot/.env
ExecStart=/opt/kronos_bot/venv/bin/python brokers/upstox_auth.py --refresh
EOF

sudo tee /etc/systemd/system/kronos_auth.timer > /dev/null <<EOF
[Unit]
Description=Daily Upstox token refresh

[Timer]
OnCalendar=Mon-Fri 03:10:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl enable --now kronos_auth.timer
```

### 5. Monitor

```bash
# Live logs
journalctl -u kronos_futures_bot -f

# Today's trades
grep "\[ENTRY\]\|\[EXIT\]" /opt/kronos_bot/app/logs/futures_bot.log | grep $(date +%Y-%m-%d)

# Restart bot (after deploying new code)
sudo systemctl restart kronos_futures_bot
```

### 6. Deploy code updates

```bash
PEM=/path/to/kronos-key.pem
IP=<ELASTIC_IP>

# From local machine
git push origin main
ssh -i $PEM ec2-user@$IP "cd /opt/kronos_bot/app && git pull && sudo systemctl restart kronos_futures_bot"
```

## Cost Breakdown

| Resource | Cost |
|---|---|
| t3.small (always-on) | ~$15/month |
| Elastic IP | $3.65/month |
| Claude Haiku (sentiment, ~100 calls/month) | ~$0.50/month |
| Spot t3.medium (MiroFish pre-market) | ~$3/month |
| **Total** | **~$22/month (~₹1,850)** |
