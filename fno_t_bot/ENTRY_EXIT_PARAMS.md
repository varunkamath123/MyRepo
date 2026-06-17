# FnO_T_Bot — Master Strategy Document
**PATH-A ORB Only | Instruments: NIFTY · BANKNIFTY · SENSEX**
**Broker: Fyers API v3**

> **Maintenance rule:** update this file whenever a live parameter changes.
> All values are runtime-verified from EC2 config unless noted otherwise.
> See CHANGELOG at the bottom for history.

---

## 0. CURRENT LIVE STATUS

| Instrument | Mode | Capital allocated | Gate condition |
|---|---|---|---|
| NIFTY | **LIVE** | ₹26,000 | Always live (anchor) |
| BANKNIFTY | Paper | ₹26,000 | Live when combined capital ≥ ₹50,000 |
| SENSEX | Paper | ₹50,000 | Live when combined capital ≥ ₹75,000 |

Capital gate evaluated at bot startup from Fyers balance (--fyers flag). No manual flag changes needed.

---

## 1. BLANKET BLOCKS

These override everything. No signal, no scoring, no exceptions.

| Block | Instrument | Day | Direction | Type |
|---|---|---|---|---|
| *(none — all blanket blocks removed May 2026)* | — | — | — | — |

### Soft block — Tuesday elevated gate

Applies to **all three instruments** on Tuesday via `skip_tuesday=True`.
Not a hard block — passes under a confirmed strong trend. Applies to PATH-A ORB and
re-entry signals. PATH-REV signals are exempt.

Two separate gates fire after the OR signal passes all standard gates:

**CALL gate (Tuesday):**

| | NIFTY | BANKNIFTY | SENSEX |
|---|---|---|---|
| Min ADX | 30 | **35** | 30 |
| Min DI+ spread (DI+ − DI−) | 8 | **12** | 8 |

**PUT gate (Tuesday):**

| | NIFTY | BANKNIFTY | SENSEX |
|---|---|---|---|
| Min ADX | 30 | **35** | 30 |
| Min DI− spread (DI− − DI+) | 8 | **12** | 8 |

BANKNIFTY stricter: Tuesday uses 8-day options (Wed expiry → Mon options), giving low gamma.
A strong confirmed trend is required to justify the lower per-point premium movement.

### Previously removed blocks (for reference)

| Removed | When | Reason |
|---|---|---|
| Tuesday CALL hard block (NIFTY) | May 2026 | Replaced by elevated ADX+DI gate |
| Thursday CALL hard block (all) | May 2026 | Static block over-fit average; Apr 17 CALL was valid |
| Wednesday PUT-only | Apr 2026 | Live OI now determines direction dynamically |
| Tue/Wed OI CONFIRM required | May 2026 | NEUTRAL allowed; REJECT still hard-blocks |

---

## 2. ENTRY — PER-DAY PARAMETERS

Runtime-verified (EC2 `_get_day_cfg()` output, May 13 2026):

| Day | OR Bars | OR Forms | Entry Window | ADX Floor | OI Gate | Checkpoint |
|---|---|---|---|---|---|---|
| **Mon** | 4 | 09:15–09:35 | 09:30 → 14:00 | 25 | REJECT blocks | **12:00** |
| **Tue** | 5 | 09:15–09:40 | 09:30 → 14:00 | 25 + elevated gate | REJECT blocks | **12:00** |
| **Wed** | 3 | 09:15–09:25 | 09:30 → **10:55** | 25 | REJECT blocks | **10:55** |
| **Thu** | 5 | 09:15–09:40 | 09:30 → 14:00 | 25 | REJECT blocks | **12:00** |
| **Fri** | 5 | 09:15–09:40 | 09:30 → 14:00 | **20** | REJECT blocks | **12:00** |

OR bars are 5-minute bars starting at 09:15. OR_high = highest high; OR_low = lowest low.

**Gap-Reversal supplement** extends entry to 12:30 (recovery direction only — see §4).

**Direction per day:**

| Day | NIFTY | BANKNIFTY | SENSEX |
|---|---|---|---|
| Mon | CALL + PUT | CALL + PUT | CALL + PUT |
| Tue | Both (elevated gate) | Both (elevated gate, stricter) | Both (elevated gate) |
| Wed | CALL + PUT | CALL + PUT | CALL + PUT |
| Thu | CALL + PUT | CALL + PUT | **FULLY BLOCKED** |
| Fri | CALL + PUT | CALL + PUT | CALL + PUT |

**Stop / target / trail — all days, all instruments:**

| Parameter | Value |
|---|---|
| Stop-loss | 25% |
| Target backstop | 55% (IV-scaled at entry — see §5 Exit 4) |
| Trail activation | 18% gain |
| Trail distance | 10% below peak |

---

## 3. ENTRY — SIGNAL GATES

All must pass in sequence. Any failure stops evaluation.

### Gate 1 — OR Breakout
```
CALL: current 5m bar closes ABOVE  (OR_high + PATH_A_BUFFER)
PUT:  current 5m bar closes BELOW  (OR_low  − PATH_A_BUFFER)
PATH_A_BUFFER ≈ 0.1% of index price
```
**Dynamic OR:** if the pre-open DI spread shows directional dominance ≥ 10 pts,
the OR is expanded by 1 extra bar (higher quality filter). ADX floor then raised to
`max(day_floor, PATH_A_DYNAMIC_OR_ADX_MIN=30)`.

### Gate 2 — Timing window
Signal must arrive between 09:30 and per-day `entry_end` (see §2 table).
Wednesday hard cutoff 10:55 — only exception.
Gap-Reversal supplement extends to 12:30 in recovery direction (see §4).

### Gate 3 — ADX floor
Must meet per-day floor from §2 table.

**Important:** PATH-A calls `get_path_a_signal()` directly — it does NOT pass through
`get_signal()` and its `min(call_adx_min, put_adx_min)` pre-filter. Friday's floor of
20 is the real floor, not 25. The pre-filter only applies to disabled paths (TMS/CONT/etc.).

Gap-Reversal supplement: if gap ≥ 0.5% + price recovery ≥ 50% of gap + DI-spread in
recovery direction ≥ 12 → ADX floor reduced to 18 (see §4).

### Gate 4 — Trend alignment
```
CALL:  price ≥ VWAP  AND  EMA_9 ≥ EMA_21  AND  5m SuperTrend = BULL
PUT:   price ≤ VWAP  AND  EMA_9 ≤ EMA_21  AND  5m SuperTrend = BEAR
```

### Gate 5 — OI direction bias (4 components, each ±1)

| Component | +1 for CALL | +1 for PUT |
|---|---|---|
| Live PCR | PCR ≤ 0.90 | PCR ≥ 1.10 |
| 30-min PCR drift | PCR rising | PCR falling |
| MaxPain gravity | Price above MaxPain | Price below MaxPain |
| IV Skew (2% threshold) | Call-skew dominant | Put-skew dominant |

```
Score ≥ +2  → CONFIRM  — proceed
Score −1..+1 → NEUTRAL — allowed all days
Score ≤ −2  → REJECT  — hard block (all days)
```
SENSEX: BSE data, PCR unavailable → runs MaxPain + IV skew only (2 components max).
Score rarely reaches ≤ −2; effectively never hard-blocked via OI alone.

### Gate 6 — Tuesday elevated gate
Fires after Gates 1–5 pass, on Tuesdays only (skip_tuesday=True instruments).
See §1 for per-instrument ADX and DI-spread thresholds.

---

## 4. ENTRY — SPECIAL RULES

### Gap-Reversal ADX Supplement
Fires when Gate 3 would normally block (ADX below day floor) AND all of:

| Condition | Value |
|---|---|
| Gap size (open vs prev_close) | ≥ 0.5% (`GAP_REV_MIN_GAP_PCT`) |
| Price recovery toward prev_close | ≥ 50% of gap magnitude (`GAP_REV_RECOVERY_PCT`) |
| ADX at signal bar | ≥ 18 (`GAP_REV_ADX_MIN`) |
| DI-spread in recovery direction | ≥ 12 pts (`GAP_REV_DI_SPREAD_MIN`) |
| Entry window extension | to 12:30 (`GAP_REV_ENTRY_EXT`) — recovery direction only |

Direction: `GAP_FADE_DN` (gap-down then recovering) → CALL supplement only.
`GAP_FADE_UP` (gap-up then fading) → PUT supplement only.

Confirmed reachable: PATH-A bypasses the get_signal() pre-filter, so ADX=18–24
entries can fire when gap-reversal conditions hold.

### Dynamic OR
When pre-open DI spread ≥ 10 pts, OR expands by 1 bar and ADX floor raises to ≥ 30.
Stop tightens to **20%** (`PATH_A_DYNAMIC_OR_STOP`) — market has settled 45–90 min,
lower volatility justifies tighter leash.

### Re-entry after stop-loss
- One re-entry allowed per day after a PATH-A stop-loss
- Must have ADX ≥ 35 (`PATH_A_REENTRY_ADX_MIN`) at re-entry bar
- Must fire before 13:00 (`PATH_A_REENTRY_CUTOFF`) — 90-min minimum runway
- `PATH_A_REENTRY_ENABLED` must be True in config

---

## 5. ENTRY — THREE-LAYER SCORING

Every signal that clears all hard gates enters this pipeline.
Scoring determines lot count and can block low-quality entries.

```
Gates 1–6 passed
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 1 — Unified Scorer      gate: score ≥ 55 / 100  │
│  11 time-band-weighted components.                      │
│  Below 55 → entry BLOCKED entirely.                     │
└──────────────────────┬──────────────────────────────────┘
                       │ PASS
                       ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 2 — Composite Signal Scorer   (0–100, all day)   │
│  8 weighted components. Adjusts lot count.              │
│  < 40 → cap to 1 lot  |  ≥ 65 → upgrade to 2 lots      │
└──────────────────────┬──────────────────────────────────┘
                       │
              entry time ≥ 11:00?
                       │ YES
                       ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 3 — Post-11 Scorer   (0–100, entries ≥ 11:00)   │
│  6 components. Replaces EMA freshness with live DI.     │
│  < 40 → SKIP (entry BLOCKED)                            │
│  ≥ 70 → STRONG (2 lots)                                 │
│  Layer 3 can only reduce; never raises above Layer 2.   │
└─────────────────────────────────────────────────────────┘
```

### Layer 1 — Unified Scorer (weights by time band, each band sums to 100)

| Component | 09:30 | 10:00 | 11:00 | 12:00 | 13:00 | What it measures |
|---|---|---|---|---|---|---|
| or_breakout | **25** | 20 | 10 | 5 | 0 | Closed above/below OR |
| or_thrust | 10 | 10 | 5 | 0 | 0 | Width-normalised extension beyond OR |
| adx | 15 | 15 | 15 | 15 | 15 | ADX percentile vs session history |
| di_align | 10 | 10 | 10 | 10 | 5 | DI+ vs DI− directional alignment |
| st5 | 5 | 5 | 10 | 10 | 10 | 5m SuperTrend direction |
| st15 | 10 | 15 | **15** | **20** | **25** | 15m SuperTrend — grows all session |
| ema | 5 | 10 | 10 | 10 | 10 | EMA 9/21 cross alignment |
| vwap | 5 | 5 | 5 | 5 | 5 | Price side of VWAP |
| oi | 5 | 5 | 10 | 10 | 10 | OI bias score context |
| exhaustion | 0 | 0 | 5 | 10 | **15** | Extension/reversal risk — grows late |
| momentum | 10 | 5 | 5 | 5 | 5 | 3-bar ROC magnitude |

*Pattern: or_breakout 25→0; st15 10→25. Early = ORB conviction; late = HTF trend.*
*Phase 3 (≥80 live trades): weights replaced by logistic regression on actual outcomes.*

### Layer 2 — Composite Signal Scorer (8 components, 0–100)

| Component | Max | Scoring |
|---|---|---|
| htf_align | 20 | 15m ST + 15m EMA both aligned→20, one→10, neither→0 |
| pcr_align | 20 | CALL: PCR>1.2→20, 1.0–1.2→12, 0.7–1.0→5, <0.7→0 (PUT mirror) |
| adx_mag | 15 | ≥40→15, ≥35→12, ≥30→9, ≥25→6, <25→0 |
| ema_fresh | 15 | Bars since EMA cross: ≤2b→15, ≤4b→11, ≤7b→7, ≤10b→4, >10b→0. OR_BREAK path: full 15 (breakout IS the fresh signal) |
| consolidation | 10 | ATR now / ATR 20-bar mean: <0.70→10, <0.90→7, <1.10→5, <1.30→3, >1.30→0 |
| vwap_dist | 10 | Dist from VWAP: ≥0.20%→10, ≥0.10%→7, ≥0.05%→4, <0.05%→1 |
| oi_zone | 5 | BOOST→5, TAKE→3, REDUCE→1, SKIP→0 |
| max_pain | 5 | MM tailwind >1.0%→5, 0.3–1.0%→3, near→2, headwind→0 |

**Lot gates (Phase 2 — active):**

| Score | Action |
|---|---|
| ≥ 65 | Upgrade to **2 lots** |
| 40–64 | **1 lot** (standard) |
| < 40 | Cap to **1 lot** (REDUCE — weak signal, still enters) |

### Layer 3 — Post-11 Scorer (6 components, entries ≥ 11:00 only)

| Component | Max | Scoring |
|---|---|---|
| or_extension | 25 | Extension beyond OR: ≥0.80%→25, ≥0.50%→20, ≥0.25%→13, ≥0.10%→7, <0.10%→2 |
| di_strength | 20 | Live DI spread: ≥25→20, ≥15→15, ≥8→9, ≥3→4, ≥0→1, negative→0 |
| time_theta | 15 | 11:00→15, 11:30→11, 12:00→7, 12:30→4, 13:00+→2 (+2 if IV≥20%, −5 if IV<12% on DTE≤1) |
| adx_strength | 15 | ≥45→15, ≥38→12, ≥30→9, ≥25→5, <25→1 |
| oi_context | 15 | PCR fit (5) + MaxPain gravity (5) + OI wall distance (5) |
| vwap_structure | 10 | Wrong side→0; ≥0.40%→10, ≥0.20%→7, ≥0.08%→4, <0.08%→1 |

ATR adjustment (bonus/penalty, not a component): consolidating (<0.8×) → +4; expanding (>1.5×) → −6.

**Gates:**

| Score | Gate | Lots |
|---|---|---|
| ≥ 70 | STRONG | 2 (if Layer 2 permits) |
| 45–69 | TRADE | 1 |
| 40–44 | MARGINAL | 1 (caution log) |
| < 40 | **SKIP** | 0 — entry blocked |

No single component can block. Only the aggregate total triggers SKIP.

---

## 6. LOT SIZING — DECISION TREE

```
Step 1 — Base strength score from OR signal:
  ADX ≥ 35             → +1
  DI-spread ≥ 5 pts    → +1
  VWAP dist ≥ 0.10%   → +1
  Strength ≥ 2 → base = 2 lots
  Strength < 2 → base = 1 lot

Step 2 — Layer 2 Composite override:
  Score ≥ 65 → upgrade to 2 lots (overrides base)
  Score < 40 → cap to 1 lot    (overrides base)

Step 3 — Layer 3 Post-11 override (entries ≥ 11:00 only):
  SKIP (<40)     → 0 lots — BLOCKED
  STRONG (≥70)   → 2 lots (honours Layer 2 cap)
  TRADE (45–69)  → min(base, 1)
  MARGINAL(40–44)→ min(base, 1)
  Layer 3 only reduces — never raises above Layer 2 result.

Hard caps:
  Daily loss > ₹2,500           → no more entries that day
  trades_today ≥ MAX_TRADES (1) → no more entries (PATH-A max 1 per day)
  Re-entry: 1 allowed after SL, ADX ≥ 35, before 13:00
```

---

## 7. EXIT — SIX LEVELS (priority order, every 20-second poll)

### Exit 1 — Stop-Loss

```
Standard PATH-A:      −25%   (all days, all instruments)
Dynamic OR entry:     −20%   (tighter — higher-confidence setup, settled market)
```

At ATM delta ≈ 0.5, a 25% option loss ≈ 80 NIFTY index points adverse.
Fires within ~20 seconds of the move.

### Exit 2 — Rapid Spike Exit ← PRIMARY profit exit

Detects an LTP spike across the **last 2 polls** (2 × 20s = **40-second window**).
Only arms when unrealized gain ≥ 10% (`RAPID_SPIKE_MIN_GAIN`).
Fires BEFORE trail and target.

| Tier | Unrealized P&L / lot | Spike % required in 40s |
|---|---|---|
| OK | < ₹2,500 | **DISABLED** — too early, let it run |
| Good | ₹2,500 – ₹5,000 | 35% |
| V.Good | ₹5,000 – ₹8,000 | 20% |
| Excellent | ₹8,000 – ₹12,000 | 10% |
| Exceptional | ₹12,000+ | 5% |

Logic: larger gain → lower spike threshold needed. At ₹12k+/lot you're near the
empirical option premium ceiling — any significant uptick is the implied peak.
2-lot positions use per-lot P&L for tier assignment (instrument-agnostic).

### Exit 3 — Trailing Stop ← secondary profit exit

```
Arms at:   18% gain above entry premium
Distance:  10% below highest observed premium
```

Example: entry ₹100 → arms at ₹118. Peak ₹155 → trail stop fires at ₹139.50 (+39.5%).
Captures sustained directional moves that never spike sharply.

### Exit 4 — Target Backstop

```
Base: 55%  ×  (ATM IV at entry / 15.0%)
Floor: 30%    Cap: 85%
Stored in pos['target_pct'] at entry — fixed for the life of the trade.
```

| ATM IV at entry | Effective target |
|---|---|
| 10% | 36.7% → floored at **30%** |
| 12% | 44% |
| 15% | **55%** (reference) |
| 18% | 66% |
| 20% | 73% |
| 25% | 91.7% → capped at **85%** |

R:R = 55 / 25 = 2.2:1. Spike and trail exit most trades before this fires.

### Exit 5 — Conditional Checkpoint

**Per-day checkpoint times:**

| Day | Checkpoint |
|---|---|
| Mon | 12:00 |
| Tue | 12:00 |
| Wed | **10:55** |
| Thu | 12:00 |
| Fri | 12:00 |

**Decision at checkpoint (evaluated once, in this order):**

```
P&L < 0              → HARD CLOSE — unconditional, no discretion

P&L ≥ 50%            → IMMEDIATE CLOSE — lock in exceptional gain,
                        skip hold evaluation entirely

0% ≤ P&L < 50%:
  gain ≥ 15%  AND  ADX ≥ 30  AND  EMA aligned  →  HOLD (trail to 14:30)
  any condition fails                            →  CLOSE immediately
```

`PATH_A_EXCEPTIONAL_PROFIT_CLOSE = 0.50` — the 50% fast-close threshold.
`PATH_A_MIN_PROFIT_TO_HOLD = 0.15` — minimum gain to enter hold evaluation.
`PATH_A_HOLD_ADX_MIN = 30` — ADX must remain strong to justify holding.

### Exit 6 — Force Close

```
14:30 — all open positions, all instruments, all days, unconditionally
```

---

## 8. EXIT PRIORITY FLOW

```
Every 20-second poll:

1. Stop-loss hit (−25% standard / −20% dynamic OR)?
   YES → EXIT (loss)

2. Rapid spike in last 40s AND gain ≥ 10% AND tier threshold met?
   YES → EXIT (implied-peak profit)  ← fires on explosive days

3. Trail armed (gain ever reached ≥ 18%) AND current < peak − 10%?
   YES → EXIT (sustained-move profit) ← fires on trending days

4. Target backstop hit (IV-scaled ~55%)?
   YES → EXIT (exceptional sustained day profit)

5. At checkpoint time (12:00 or 10:55 Wed)?
   YES → evaluate: hard close / immediate close / hold (see §7 Exit 5)

6. At 14:30?
   YES → EXIT unconditionally
```

---

## 9. CONFIG REFERENCE (live values, May 13 2026)

```python
# ── Strike ────────────────────────────────────────────────────────────────
PATH_A_OTM_ENABLED           = False    # always ATM (May 2026)

# ── Risk / Reward ─────────────────────────────────────────────────────────
STOP_LOSS                    = 0.25     # 25% — also in PATH_A_DAY_CONFIG all days
BASE_TARGET                  = 0.55     # 55% backstop base
TRAILING_ACTIVATION          = 0.18     # trail arms at 18% gain
TRAILING_DISTANCE            = 0.10     # 10% below peak

PATH_A_DYNAMIC_OR_STOP       = 0.20     # 20% — dynamic OR entries (tighter)

# ── IV scaling ────────────────────────────────────────────────────────────
TARGET_IV_DYNAMIC            = True
TARGET_IV_REF                = 15.0     # reference IV %
TARGET_IV_MIN_PCT            = 0.30     # floor 30%
TARGET_IV_MAX_PCT            = 0.85     # cap 85%

# ── Spike exit ────────────────────────────────────────────────────────────
RAPID_SPIKE_ENABLED          = True
RAPID_SPIKE_BARS             = 2        # polls in window: 2 × 20s = 40s
RAPID_SPIKE_MIN_GAIN         = 0.10     # must be ≥ 10% up before spike logic arms
RAPID_SPIKE_TIERS            = [
    (12_000, 0.05),   # Exceptional  ₹12k+/lot : 5%
    ( 8_000, 0.10),   # Excellent    ₹8-12k/lot : 10%
    ( 5_000, 0.20),   # V.Good       ₹5-8k/lot  : 20%
    ( 2_500, 0.35),   # Good         ₹2.5-5k/lot: 35%
    (     0, inf),    # OK           <₹2.5k/lot  : disabled
]

# ── Checkpoint ────────────────────────────────────────────────────────────
PATH_A_CONDITIONAL_EXIT             = True
PATH_A_LOSS_STOP_AT_CHECKPOINT      = True
PATH_A_MIN_PROFIT_TO_HOLD           = 0.15
PATH_A_HOLD_ADX_MIN                 = 30
PATH_A_EXCEPTIONAL_PROFIT_CLOSE     = 0.50  # ≥50% at checkpoint → close immediately
PATH_A_FORCE_CLOSE_TIME             = '14:30'

# ── Scoring gates ─────────────────────────────────────────────────────────
UNIFIED_SCORER_ENABLED              = True
UNIFIED_SCORE_THRESHOLD             = 55    # Layer 1 gate
HIGH_CONVICTION_SCORER_THRESHOLD    = 65    # Layer 2: ≥ this → 2 lots
POST11_SCORE_SKIP_MIN               = 40    # Layer 3: < this → SKIP

# ── Gap-Reversal supplement ───────────────────────────────────────────────
GAP_REV_ENABLED                     = True
GAP_REV_MIN_GAP_PCT                 = 0.005  # ≥ 0.5% gap
GAP_REV_RECOVERY_PCT                = 0.50   # ≥ 50% recovered
GAP_REV_ADX_MIN                     = 18     # reduced ADX floor
GAP_REV_DI_SPREAD_MIN               = 12     # DI spread in recovery direction
GAP_REV_ENTRY_EXT                   = '12:30' # extended window

# ── Re-entry ──────────────────────────────────────────────────────────────
PATH_A_REENTRY_ENABLED              = True  # (verify in config)
PATH_A_REENTRY_ADX_MIN              = 35
PATH_A_REENTRY_CUTOFF               = '13:00'

# ── Dynamic OR ────────────────────────────────────────────────────────────
PATH_A_DYNAMIC_OR_ADX_MIN           = 30
PATH_A_DYNAMIC_OR_MIN_STRENGTH      = 2
PATH_A_DYNAMIC_OR_STOP              = 0.20  # fixed May 2026 (was 0.35)

# ── Per-instrument (all three identical as of May 2026) ───────────────────
# path_a_stop       = 0.25
# path_a_target     = 0.55
# path_a_trail_act  = 0.18
# path_a_trail_dist = 0.10

# ── Trade limits ──────────────────────────────────────────────────────────
MAX_TRADES_PER_DAY                  = 1     # PATH-A: 1 ORB + 1 re-entry
MAX_DAILY_LOSS                      = 2500  # ₹ — blocks entries after hit
```

---

## 10. WHAT IS NOT TRADED

| Condition | Reason |
|---|---|
| *(no blanket day blocks — all removed May 2026)* | Scoring gates (Unified≥55, Post-11≥40, OI bias) handle quality filtering |
| Tuesday CALL/PUT under weak trend | Elevated ADX+DI gate — fails without strong confirmation |
| ADX below day floor AND GAP_REV fails | No trending structure |
| Price on wrong side of VWAP | Counter-trend |
| 5m SuperTrend opposes direction | Counter-trend |
| OI bias REJECT (score ≤ −2) | OI context hard-contradicts direction |
| Unified score < 55/100 | Overall setup quality insufficient |
| Post-11 score < 40/100 (SKIP) | Late aggregate quality too low |
| After first SL without ADX ≥ 35 | Re-entry requires stronger trend |
| After 13:00 (for re-entry) | Window closed — insufficient runway |
| After entry_end (14:00 or 10:55 Wed) | Entry window closed |
| Concurrent positions | max_concurrent=1 — cascading loss risk |
| TMS / CONT / Path E / Path F | All disabled since Apr 2026 |

---

## 11. MONITORING

```bash
# Live logs (EC2)
ssh -i $PEM ec2-user@3.108.16.113 'journalctl -u fno_t_bot_nifty -f'
ssh -i $PEM ec2-user@3.108.16.113 'journalctl -u fno_t_bot_banknifty -f'
ssh -i $PEM ec2-user@3.108.16.113 'journalctl -u fno_t_bot_sensex -f'

# Capital gate (ground truth — use this for live/paper decisions)
python /opt/trading_bot/live_bot/capital_status.py --fyers

# Morning brief
cat /opt/trading_bot/live_bot/logs/morning_brief_$(date +%Y%m%d).txt

# Verify config is in sync
python3 ~/chk_config.py
```

---

## CHANGELOG

| Date | Change | Impact |
|---|---|---|
| May 13 2026 | Challenger→Champion: ATM strike always, stop 50%→25%, target 80%→55%, trail_act 12%→18% | **Critical** |
| May 13 2026 | `PATH_A_DAY_CONFIG` stop/target/trail fixed — was reading 0.50/0.80/0.12 despite global config showing 0.25/0.55/0.18. Exit code reads per-day config directly; inst_cfg fallback never fired | **Critical bug fix** |
| May 13 2026 | `PATH_A_DYNAMIC_OR_STOP` 0.35→0.20. Was "stricter than 50%" but standard is now 25%, making 0.35 looser than standard | **High** |
| May 13 2026 | Gap-Reversal ADX supplement added: ADX≥18 + DI≥12 + recovery≥50% on gap-fade days. Entry extended to 12:30 | Medium |
| May 13 2026 | `PATH_A_OTM_ENABLED=False` — always ATM. OTM+1 on 12-DTE monthly options gives ~2× less delta, making 80% target structurally unreachable | High |
| May 15 2026 | SENSEX `skip_thursday=False` — last remaining blanket day-block removed. Thu WR 33% (8 trades) was pre-scoring era; three-layer scoring + 12:00 entry_start handle quality | Medium |
| May 2026 | Tuesday CALL hard block removed; replaced by elevated ADX+DI gate | Medium |
| May 2026 | Thursday CALL hard block removed (static block over-fit average) | Medium |
| May 2026 | Tue/Wed `oi_confirm_required` removed — NEUTRAL allowed, REJECT still blocks | Medium |
| May 2026 | `TARGET_IV_MAX_PCT` 0.50→0.85 — high-IV regimes previously capped target too low | Medium |
| May 2026 | `TARGET_IV_MIN_PCT` 0.20→0.30 — low-IV floor raised | Low |
| Apr 2026 | All non-ORB paths disabled: TMS, CONT, ST_FLIP, Path E, Path F | High |
| Apr 2026 | Capital gate added: BNF live ≥₹50k, SENSEX live ≥₹75k | High |
| Apr 2026 | OI Direction Bias engine active (Phase 2) | High |
| Apr 2026 | Conditional checkpoint 12:00 (was hard force-close at 11:30) | Medium |
| Apr 2026 | Rapid spike exit system added (tier-based) | High |
| Mar 2026 | NIFTY + BANKNIFTY went live (₹26k each). SENSEX paper. | Live milestone |
