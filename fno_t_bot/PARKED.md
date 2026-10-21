# PARKED - 2026-10-21

The multi-strategy FnO_T_Bot (REV / TREND / ORB / Paths A-G, NIFTY + BANKNIFTY +
SENSEX, compounding sizing) is **parked**. It is not deleted. Its final state is
tagged **`v2.0-parked-multistrategy-oct2026`** (commit `569205b`).

It was replaced by a single BANKNIFTY-only strategy in [`../bnf_breakout/`](../bnf_breakout/).

## Why

* Several strategies ran in parallel against one Fyers key and exhausted the
  Standard plan's 5,000 requests/day on Oct 19 (14:43) and Oct 21 (13:05).
* After ~7 months the real book had no demonstrated edge (REV +₹400/trade with a
  95% CI of [-₹282, +₹1,125]; -₹3,867 without its best two days).
* Defects found by running it against the live broker, not by its tests.

## What was stopped on the EC2 host (all reversible)

`fno_t_bot_nifty`, `fno_t_bot_banknifty`, `fno_t_bot_sensex` (services) and the
timers `fno_t_bot_ovn@{amo,entry,morning,reconcile}`, `fno_t_bot_weekly_analyzer`,
`fno_t_bot_debrief`, `fno_t_bot_morning`. **Still scheduled:** `fno_t_bot_auth`
(the daily token the new bot also uses), `fno_t_bot_datacollect` (nightly history
the new bot reads for levels and baselines), `fno_t_bot_oi_eod`.

## To revive

```bash
git checkout v2.0-parked-multistrategy-oct2026
sudo systemctl enable --now fno_t_bot_nifty fno_t_bot_banknifty fno_t_bot_sensex
# and any of the timers above you want back
```
Do not run it alongside the new strategy. `REBUILD_PLAN.md` here is still the record
of what was tested and falsified.
