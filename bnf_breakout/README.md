# BANKNIFTY breakout strategy (paper)

One strategy, one index, ₹50,000. Replaces the parked multi-strategy FnO_T_Bot.

**Read first:** [`LEARNINGS.md`](LEARNINGS.md) (what the data says),
[`EVALUATION.md`](EVALUATION.md) (how it will be judged, fixed in advance).
The research found no setup of this family that beats a random entry after costs.
This is a testbed built to produce a clean forward verdict, not a claim of edge.

## The idea being tested

> Price coils against a key level (previous-day high/low, opening range, day
> high/low) on above-normal volume, in a volatile regime. Anticipate the break, in
> the direction of the level, **before** it happens. Leave if the premium falls 25%,
> if price closes back through the coil (thesis broken), or if the break fails to
> carry within 6 bars (thesis stale).

## Layout: config and strategy are deliberately separate

| file | owns | change it when |
|---|---|---|
| `bnf_config.py` | **every number**, each with its origin | thresholds, exits, sizing, API budget |
| `bnf_strategy.py` | **pure decisions**: entry gates, exits, sizing, costs. No I/O. | the *idea* changes |
| `bnf_data.py` | CSV history and Fyers candle shapes | a data format changes |
| `bnf_broker.py` | Fyers adapter with a persisted API budget. Read-only. | the API behaviour changes |
| `bnf_runner.py` | the live paper loop, state, logs, heartbeat | operations change |
| `bnf_replay.py` | replays `bnf_strategy` over history vs a random baseline | you want to try a config |
| `bnf_chain.py` | records the option chain in a background thread and supplies the latest snapshot to the OI/PCR entry gate. | you want more fields recorded |
| `tests/test_bnf.py` | 282 assertions, incl. a simulated trading day | always, before deploying |
| `research/` | the pre-registered studies behind `LEARNINGS.md` | — |

The strategy takes the config as its only parameter source, so the live runner and
the replay execute the **same** decision code.

## Run it

```bash
python tests/test_bnf.py                  # must be 0 failed
python bnf_replay.py [HISTORY_DIR]        # in-sample replay vs random baseline + gate diagnosis
python bnf_runner.py --preflight          # no API calls; checks config + history
python bnf_runner.py --status             # heartbeat + API budget
```
Deployment: [`deploy/INSTALL.md`](deploy/INSTALL.md).

## Logs (in `/opt/trading_bot/bnf_state/logs/`)

| file | content |
|---|---|
| `bnf_decisions_<date>.jsonl` | every evaluated bar, every gate's state, enter or not |
| `bnf_trades_<date>.jsonl` | each closed trade: thesis, entry gates, sizing, peak/trough, costs, API calls used |
| `bnf_path_<date>.jsonl` | every premium poll while a position is open (for trail research) |
| `bnf_chain_<date>.jsonl` | option chain every 15 min: PCR, max-pain, ATM IV, skew, OI and IV by strike. The OI/PCR entry gate reads the latest snapshot. Free (NSE's site, not Fyers). |
| `bnf.log` | human-readable log, rotated daily at midnight |

## API budget (Fyers Standard plan: 5,000 requests/day, 5/second)

One bars call per **closed** 5-minute bar (~54/day while flat), one option quote per
20 s **only while a position is open**, a few to resolve the contract. Expected
300–900/day; hard cap 3,000 for new work, **exits are never refused by the cap**.
A daily-cap reply is recognised by meaning, suspends all calls for 30 minutes, and
is never mistaken for "contract not listed".

## Known limits

* **Paper only. No order path exists** (a test asserts it).
* **Cannot enter before 10:35**: the compression gate needs 15 bars of history.
* Replay is index-level (premium inferred at 71 premium-% per index-%) and
  in-sample for the thresholds. It checks the code and the trade frequency; it is
  not evidence of an edge.
* Replay fills trails/targets at the level; live fills at the next bid.
