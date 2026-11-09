# How this strategy will be judged

Fixed on 2026-10-21, **before any forward result exists**. If the verdict rule is
decided after seeing results it is worthless, so this file is not edited to fit
what happens. A change to it is a new, dated section below, never a rewrite.

## What is being tested

*H0:* this strategy's net P&L per trade is no better than a random entry under the
same exits. *H1:* it is better, and positive.

The honest prior, from `LEARNINGS.md` and `research/`, is **H0**. In-sample replay
over 420 sessions gave the strategy −0.083% of spot per trade (n = 197, win 26%)
against −0.076% for random entry (10 seeds, range −0.088% to −0.057%); it beat 3 of
10 random seeds. The value of running it is a *clean forward verdict*, at ₹50,000
of paper risk and well under 1,000 API requests a day, not an expectation of profit.

## The sample

Replay frequency is ~0.47 trades per session (the entry gates are strict by
design). **n = 30 closed trades therefore takes roughly 64 sessions, about three
months.** That is the price of a verdict that means something. Interim reads:

| closed trades | what it may be used for |
|---|---|
| 1 – 9 | nothing about performance. Confirm the plumbing: fills at ask/bid, costs, logs reconcile, no API-budget alarm. |
| 10 – 29 | sanity only: are setups firing at roughly the replay rate; are exits the expected mix? No tuning. |
| 30 | **the verdict below** |
| 60 | second look if the verdict was inconclusive |

## The verdict at n = 30 closed trades

Computed on net rupee P&L per trade (after costs and the ask/bid fills), with a
bootstrap 95% confidence interval of the mean.

- **PASS** — the CI lower bound is above zero, **and** both chronological halves
  have a positive mean. Only then is the thesis worth taking seriously, and even
  then the next step is a larger paper run, not live money.
- **FAIL** — the mean is at or below zero, **or** the CI upper bound sits below the
  replay's random baseline. Retire the thesis. Do not respond by loosening gates.
- **INCONCLUSIVE** — anything else. Continue to n = 60 with no changes, then decide
  by the same rule.

A mean that is positive but whose CI includes zero is **not** a pass. The old
system's `rv_iv` finding looked like a p-value of 4.9e-77 and was really n = 234.

## Rules while it runs

1. **No config change before n = 30** except fixing a demonstrable bug. Each change
   resets the clock for what it touches.
2. After n = 30, at most **one change per 20 trades**, and only the change the
   diagnosis below points at.
3. A change that was suggested *by* the paper results must be checked on the
   replay's held-out period before it is adopted.
4. **Pause** if the book falls below ₹35,000 (−30%), and say so.

## Config problem or strategy problem?

That is why the two live in separate files. The decision log
(`bnf_decisions_<date>.jsonl`) records every bar's gate states, and the replay
(`python bnf_replay.py`) prints which gate is the *only* one failing most often.

| observation | it is a… | look at |
|---|---|---|
| far fewer setups per session than the replay's ~0.47 | **config** | the gate that is the only failure most often (replay: `coil`, `regime`, `volume`) |
| setups fire, but exits are mostly `thesis_stale` / `thesis_broken` | **strategy** | the thesis: price is not following through |
| setups fire, direction right, still losing | **config** | `HARD_STOP`, `TRAIL_ATR_MULT`, `THESIS_CONFIRM_*`, sizing |
| profitable only with `DIRECTION='away'` | **strategy** | research found pressing toward a level (−0.102%) worse than away (−0.062%); flip it only with the replay as evidence |
| good paper result, bad cost line | **config** | `SLIPPAGE_PCT`, entry timing, lots |

## What would change my mind

A PASS at n = 30 would overturn the conclusion of `LEARNINGS.md` #1–#3, and would
be surprising. A FAIL is the expected outcome and is a *useful* one: it closes this
family of ideas on a forward sample instead of an in-sample one.

## Amendment 2026-10-22: OI / PCR entry gate (added at the owner's direction)

**What changed.** An entry now also needs the option chain to agree with the trade:
(a) PCR, z-scored against the same time of day on the previous 20 sessions, must lean the
trade's way (CALL: PCR above its own norm; PUT: below), and (b) the biggest OI wall in the
trade's path (call OI for a CALL, put OI for a PUT, within 1.5%) must be at least 0.5% away.
No snapshot, or one older than 20 minutes, **blocks** the entry (`OI_ON_MISSING='block'`).

**Evidence against it, stated up front.** `research/oi_pcr_adx_study.py` found no directional
information in PCR, max-pain or skew, so the PCR half is an untested prior, not a finding.
The wall half cannot be backtested at all (no historical per-strike OI). The replay therefore
runs with the gate OFF and its numbers do not describe the live strategy any more.

**Consequences for the verdict.**
- Rule 1 applies: this is a config change, so the clock restarts. Only 1 trade existed.
  Trades from 2026-10-22 count toward the n = 30.
- The gate can only remove trades, so n = 30 will take longer than ~64 sessions.
- Every setup it blocks is logged in `bnf_decisions_<date>.jsonl` with a `shadow` record
  (would-be direction, thesis, entry price), so the gate itself can be scored: did the trades
  it refused do worse than the ones it allowed? Judge it that way, not by total P&L alone.
- If the gated book is no better than the ungated shadow over 30 setups, switch it off.

## Amendment 2026-10-24: OI gate switched to SHADOW (owner's call)

`OI_GATE_BLOCKS = False`. The gate is still evaluated on every bar and its verdict is recorded
(`oi_would_block` on each decision and on each trade's thesis), but it no longer refuses entries.
Why: on Oct 23 (-1.2% day) the 'wall' rule blocked all 7 PUT setups, and the wall was a modest
54,000 strike (26k put OI = 4.9% of total, 1.6x the next strike) with no size floor. Blocking left
almost no trades to measure. The PCR half is also coincident rather than leading (PCR changes
correlate 0.50 with the same snapshot's index move, 0.25 with the next).
Consequence: the live trades are the ungated strategy again, so trades from this date count toward
n = 30 as the base strategy. At the verdict, split the trades by `thesis.oi_would_block` and compare
their P&L; only then decide whether the gate should block.

## Amendment 2026-10-27: volume gate OFF (owner's call)

`VOLUME_MODE = 'off'`. Evidence (`research/volume_study.py`; replay on the 322 sessions that have volume):
gate on n=200, -0.089%/trade; gate off n=268, -0.086%; random entry -0.080%. The mean does not move; trades
rise 34%. Volume forecasts the next hour's RANGE weakly (rank rho ~0.17, both halves positive, but 0.34% -> 0.39%
low -> high) and DIRECTION not at all (a big-volume bar's direction continues -2.4 bps, 46% hit).
This is a config change, so the n = 30 clock restarts (1 trade existed). The live strategy is now:
coil + key level + hot regime, OI gate in shadow.

## Note 2026-10-28: OI gate skips on the nearest series' expiry day (logging/shadow only, no trade effect)

Oct 27 was the monthly expiry. NSE's feed (jugaad/nse_oi) returns only the NEAREST expiry's rows, so on
expiry day the chain is the series that is expiring, while the bot trades the next monthly. That chain's
PCR and IV collapse through the day (PCR 0.80 -> 0.60, ATM IV 20.7 -> 5.3), which produced a PCR z of -3.4
and a 'wall' that were artefacts. `OI_SKIP_ON_EXPIRY_DAY=True`: the gate passes and says why. The gate is
in shadow, so no trade is affected; the shadow verdict for the Oct 27 13:45 trade (`oi_would_block=true`)
is an artefact and must be excluded when the shadow record is scored.

## Note 2026-10-28: the OI gate now reads the BROKER's chain (owner's suggestion)

`OI_SOURCE='fyers'`: the gate's PCR and OI walls come from Fyers' option chain for the series the bot trades
(the next monthly on the nearest series' expiry day), ~77 calls/day from the API budget (label `chain`).
NSE is still recorded in parallel (`bnf_chain_<date>.jsonl`) because only NSE has IV and skew. Fyers' PCR is a
different quantity from the NSE PCR in `oi_intraday.jsonl` (traded series, all strikes), so it has its own
history, starting empty: the PCR condition shows 'warming up' (passes) until 10 sessions exist; walls apply
immediately. The gate is still in shadow, so no trade is affected. Shadow verdicts logged before 2026-10-29
used the NSE nearest-series chain; do not pool them with later ones without noting the source.

## Note 2026-11-05: stale-rule shadow arm (owner's request) - no effect on the live trades

`bnf_shadow.py`, `SHADOW_STALE_ENABLED=True`. Every trade the STALE rule closes is also followed as if it
had stayed open, managed by every other rule (premium stop/target, thesis-broken, ATR trail, 15:00
force close). It places nothing, never touches the real position or trade log (a test asserts the real
trades are identical with it on or off), and uses non-essential API calls only.

Why: Nov 5 the stale rule closed two CALLs for +1,252 and +9 on a day the index rose 1.2%; the model puts the
first at about +12,400 had it run. Earlier it saved Oct 29 (about -1,600 avoided) and Nov 3 (about -2,400).
One day settles nothing; the forward record can.

HOW TO READ IT. `bnf_shadow_<date>.jsonl`: per stale-closed trade, `real` (the stale result) beside `shadow`
(what the other rules did), `delta_vs_real`, and - because only one position is held at a time - the real
trades that opened while the shadow ran (`real_trades_during_shadow`, `real_pnl_during_shadow`) and
`delta_vs_real_incl_overlap = shadow - (stale trade + those later trades)`. Judge the rule on the INCLUDING-
overlap column. Pre-registered: review at 20 stale-closed trades; the stale rule is judged worth keeping only
if the sum of `delta_vs_real_incl_overlap` is <= 0 with a bootstrap CI that does not lie wholly above 0 and
both chronological halves agree; if it lies wholly above 0 in both halves, the rule is costing money and
the removal proposal goes to the owner (a rule change restarts the n = 30 count).

## Note 2026-11-09: profit-wait shadow arm (owner's idea) - no effect on the live trades

`ProfitWaitShadow`, `SHADOW_PROFIT_WAIT_ENABLED=True`. It follows EVERY real entry from the moment it opens. While the
position has not been net profitable (after round-trip costs, judged at a premium poll) the stale, thesis-broken
and trail rules do not close it; once it has been, the existing rules apply exactly as normal. Always on: the 25%
premium hard stop, the target, and the 15:00 force close. Virtual: no orders, real trades untouched (a test asserts
the real trades are identical with every shadow arm on or off).

Replay evidence (`research/profit_wait_study.py`, 430 sessions, realistic 30%/1% leverage, 'profitable' = a bar
closes beyond the 0.08% cost): as built -0.073%/trade; profit-wait -0.047%/trade (CI [-0.084,-0.011]), win rate
30% -> 58%, but average loss -0.18% -> -0.30% (payoff ratio 1.00 -> 0.46) and worst trade -0.74% -> -0.91%.
Paired session-level gain +0.0237% (CI [+0.0048,+0.0438]), positive in both halves. It turns small losers into small
winners; it does NOT raise the size of the winners, and expectancy stays negative. A replay cannot see premium
paths, so this is a reason to run the shadow, not to adopt the rule.

HOW TO READ IT. `bnf_shadow_<date>.jsonl`, records with `arm: profit_wait`: per real trade, `real` beside `shadow`,
`delta_vs_real`, and the real trades that opened while the shadow ran (`delta_vs_real_incl_overlap`).
Pre-registered: review at 20 profit-wait shadows. Adopt (a rule change; restarts the n=30 count) only if the sum of
`delta_vs_real_incl_overlap` is > 0 with a bootstrap CI lower bound > 0 AND both halves are positive AND the single
worst shadow loss is tolerable at the live lot size. Otherwise keep the exits as built.

