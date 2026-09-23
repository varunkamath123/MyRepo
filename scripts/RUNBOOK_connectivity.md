# Runbook — when the bot host is unreachable

## 1. Work out whose fault it is (30 seconds)

```bash
ping -n 2 3.108.16.113      # the bot host
ping -n 2 8.8.8.8           # general IPv4
ping -6 -n 2 2001:4860:4860::8888   # general IPv6
tracert -4 -h 6 -w 1500 3.108.16.113
```

**If `8.8.8.8` also fails, it is not EC2.** The known signature of the recurring
ISP fault is:

```
ping 3.108.16.113   100% loss
ping 8.8.8.8        100% loss      <- general IPv4 down
ping ::8888 (v6)    0% loss, 10ms  <- IPv6 healthy

tracert -4:
  1  192.168.1.1     3 ms    router
  2  223.178.56.1    4 ms    ISP gateway
  3+ * * *                   dies one hop past the ISP gateway
```

Seen 2026-09-18 and at least once before. **The bot keeps trading through this** —
it runs on EC2 and does not depend on the home connection. Nothing is broken on
the trading side; we are simply blind to it until IPv4 returns.

Do not guess at results while blind. Wait, then pull the logs.

### You are not actually cut off from AWS

The *classic* endpoints (`ec2.ap-south-1.amazonaws.com`) are IPv4-only and will
time out. The **dual-stack** endpoints answer over IPv6 and keep working.
Verified 2026-09-20, two days into an outage:

```
ec2.ap-south-1.api.aws         HTTP 301 in 0.41s over IPv6   reachable
ec2.ap-south-1.amazonaws.com   timeout after 12s             unreachable
ssm.ap-south-1.api.aws         HTTP 400 in 0.63s over IPv6   reachable
```

So AWS work — including the IPv6 fix below and SSM setup — **can be done during
an outage**, not only after it:

```bash
export AWS_USE_DUALSTACK_ENDPOINT=true
aws sts get-caller-identity --region ap-south-1
```

GitHub, by contrast, publishes no AAAA record, so `git push` stays blocked until
IPv4 returns. Commit locally and push later.

## 2. When the link returns

```bash
key="/c/Users/Varun Kamath/.ssh/fno-t-bot-new-key"
ssh -i "$key" ec2-user@3.108.16.113 \
  'systemctl is-active fno_t_bot_nifty fno_t_bot_banknifty fno_t_bot_sensex'
```

Then pull whatever was missed: closed trades in
`/opt/trading_bot/live_bot/logs/FnO_T_Bot_*_trades_<date>.jsonl`, the
counterfactual rows in `counterfactual_*.jsonl`, and the journal.

## 3. Durable fixes (pick either, ideally both)

### A. IPv6 on the instance — `enable_ipv6_ec2.sh`

Your IPv6 path stays up when v4 dies, so this specific outage stops blinding us.
Additive: it changes no IPv4 setting, so the existing SSH route is untouched.

```bash
bash scripts/enable_ipv6_ec2.sh --check     # read-only, prints the plan
bash scripts/enable_ipv6_ec2.sh --apply     # make the changes
```

Idempotent — re-run it to refresh the security-group rule if your residential
IPv6 prefix rotates (it can). The script scopes SSH to **your /64 only**, never
`::/0`; opening port 22 to the whole IPv6 internet to fix a connectivity
annoyance would be a straight security downgrade.

**Not yet tested against live AWS** — written during the outage with no access to
the account. Run `--check` and read the plan before `--apply`.

### B. SSM Session Manager

Strictly better long-term: no inbound port at all, works without a public IP, and
survives security-group mistakes. Blocked on one thing:

```bash
aws sts get-caller-identity     # currently fails — credentials expired
aws configure                   # fix that first
```

Then the instance needs the SSM agent running and an instance profile carrying
`AmazonSSMManagedInstanceCore`. Worth doing regardless of the IPv6 work, because
it also removes the dependency on a static home IP in the security group.

## 4. What NOT to do

- Do not open SSH to `0.0.0.0/0` or `::/0` to get around a routing problem.
- Do not restart or redeploy anything based on an assumption about what the bot
  did while you were blind — read the logs first.
- Do not treat a `DATA-STALE` burst as an outage without checking the calendar;
  market holidays look identical (2026-09-14 produced 2,247 of them).
