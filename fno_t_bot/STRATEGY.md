# FnO_T_Bot — Live Trading Strategy
**FnO_T_Bot | PATH-A ORB Only | Published May 13 2026**
**Instruments:** NIFTY (LIVE) · BANKNIFTY (paper, capital-gated) · SENSEX (paper)
**Broker:** Fyers API v3 | **Capital:** ₹52,000 live deployed

---

## 1. OVERVIEW

Single-path intraday options strategy. Buys ATM options (CALL or PUT) on the first clean
Opening Range Breakout each day. All other paths (TMS, CONT, ST_FLIP, Path E, Path F)
are disabled. One trade per instrument per day; one re-entry allowed after a stop-loss.

Entry is not a simple gate pass/fail. Every signal that clears the hard gates is then
evaluated by a three-layer scoring system that determines (a) whether to block it,
(b) how many lots to trade, and (c) which scorer applies based on time of day.

---

## 2. ENTRY

### 2a. Timing Windows

| Day | OR Bars | OR Formation | Entry Open | Entry Cutoff | Checkpoint |
|-----|---------|-------------|------------|--------------|------------|
| Mon | 4 bars  | 09:15–09:35 | 09:30      | 12:00        | 12:00      |
| Tue | 5 bars  | 09:15–09:40 | 09:30      | 12:00        | 12:00      |
| Wed | 3 bars  | 09:15–09:30 | 09:30      | **10:55**    | **10:55**  |
| Thu | 5 bars  | 09:15–09:40 | 09:30      | **10:55**    | **10:55**  |
| Fri | 5 bars  | 09:15–09:40 | 09:30      | 12:00        | 12:00      |

- Each bar is **5 minutes**. OR formation starts at 09:15.
- Gap-Reversal supplement: entry window extended to **12:30** on gap-fade days.
- Re-entry window closes at **13:00** regardless of day.
- OR_high = highest high across OR bars; OR_low = lowest low.

### 2b. Hard Gates (All Must Pass to Generate a Signal)

These run in sequence. Any failure stops evaluation immediately.

**1. OR Breakout**
```
CALL: current 5m bar closes ABOVE (OR_high + PATH_A_BUFFER)
PUT:  current 5m bar closes BELOW (OR_low  − PATH_A_BUFFER)
```
PATH_A_BUFFER ≈ 0.1% of index. Dynamic OR: DI-dominated pre-open → OR expanded by 1 bar.

**2. Timing Gate**
Signal must arrive between 09:30 and per-day entry cutoff (see table above).
Gap-Reversal supplement extends window to 12:30 in recovery direction only.

**3. ADX Gate**

| Day | Base ADX Floor | Special Rule |
|-----|----------------|-------------|
| Mon | 30             |             |
| Tue | 28             | OI CONFIRM required |
| Wed | 25             | OI CONFIRM required |
| Thu | 25             | PUT-only bias (CALL suppressed) |
| Fri | 20             |             |

Dynamic OR active → floor raised to max(day_floor, 30).

**Gap-Reversal ADX Supplement (GAP_REV):**
On gap-open days, the 14-period ADX is suppressed (stays 17–24) by initial
gap-direction bars even while DI confirms the recovery rally. The supplement
allows entry at a reduced ADX floor when ALL conditions hold:

| Condition | Required |
|-----------|---------|
| Open gap vs prev_close | ≥ 0.5% |
| Price recovery toward prev_close | ≥ 50% of gap |
| ADX at bar | ≥ 18 |
| DI-spread in recovery direction | ≥ 12 pts |

GAP_FADE_DN → CALL supplement only. GAP_FADE_UP → PUT supplement only.

**4. Trend Alignment**
```
CALL:  price ≥ VWAP  AND  EMA_9 ≥ EMA_21  AND  5m SuperTrend = BULL
PUT:   price ≤ VWAP  AND  EMA_9 ≤ EMA_21  AND  5m SuperTrend = BEAR
```

**5. OR Width Gate**
Rejects if OR is too wide — options already overpriced relative to remaining move.
Per-instrument width limits defined in config.

**6. OI Direction Bias (Binary: CONFIRM / NEUTRAL / REJECT)**

Scored from four NSE components (each ±1, total range −4 to +4):

| Component | CALL +1 condition | PUT +1 condition |
|-----------|-------------------|-----------------|
| Live PCR | PCR ≤ 0.90 | PCR ≥ 1.10 |
| 30-min PCR drift | PCR rising | PCR falling |
| MaxPain gravity | price above MaxPain | price below MaxPain |
| IV Skew (2% threshold) | call-skew | put-skew |

```
Score ≥ +2  → CONFIRM   — proceed
Score −1..+1 → NEUTRAL  — OK on Mon/Fri; BLOCKED on Tue/Wed
Score ≤ −2  → REJECT    — hard block regardless of day
```
SENSEX: BSE data (PCR unavailable). Typically NEUTRAL; never hard-blocked by OI alone.

### 2c. Strike Selection

Always ATM (PATH_A_OTM_ENABLED = False, effective May 13 2026).
ATM = nearest strike to current index price.
Budget fallback: if ATM × lot_size > capital, step one strike OTM until within budget.

---

## 3. ENTRY SCORING (Three-Layer System)

After all hard gates pass, every signal enters a three-layer scoring pipeline.
Scoring determines lot size and can block low-quality signals.

```
Signal generated
      │
      ▼
┌─────────────────────────────────────────────────┐
│  Layer 1: UNIFIED SCORER (55/100 gate)          │
│  Time-band weighted across 11 components.       │
│  < 55 → entry BLOCKED regardless of lots.       │
└────────────────────┬────────────────────────────┘
                     │ PASS ≥ 55
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 2: COMPOSITE SIGNAL SCORER (0–100)       │
│  8 weighted components, active all day.         │
│  < 40 → cap to 1 lot (REDUCE)                   │
│  ≥ 65 → upgrade to 2 lots (HIGH CONVICTION)     │
└────────────────────┬────────────────────────────┘
                     │
          ┌──────────▼──────────┐
          │ Is entry time ≥ 11:00? │
          └──────────┬──────────┘
                     │ YES
                     ▼
┌─────────────────────────────────────────────────┐
│  Layer 3: POST-11 SCORER (0–100)                │
│  Replaces EMA freshness with live DI spread.    │
│  < 40 (SKIP) → entry BLOCKED                    │
│  40–44 (MARGINAL) → 1 lot, caution              │
│  45–69 (TRADE) → 1 lot                          │
│  ≥ 70 (STRONG) → 2 lots                         │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
              enter_trade(lots)
```

### Layer 1 — Unified Scorer (UNIFIED_SCORE_THRESHOLD = 55/100)

Time-band weighted: weights shift every 30–60 min to reflect which signals matter most
at each phase. 11 components. Score < 55 → entry suppressed entirely.

| Time Band | or_breakout | or_thrust | adx | di_align | st5 | st15 | ema | vwap | oi | exhaustion | momentum |
|-----------|-------------|-----------|-----|----------|-----|------|-----|------|----|-----------|---------|
| 09:30     | **25**      | 10        | 15  | 10       | 5   | 10   | 5   | 5    | 5  | 0         | 10      |
| 10:00     | **20**      | 10        | 15  | 10       | 5   | 15   | 10  | 5    | 5  | 0         | 5       |
| 11:00     | 10          | 5         | 15  | 10       | 10  | **15**| 10  | 5   | 10 | 5         | 5       |
| 12:00     | 5           | 0         | 15  | 10       | 10  | **20**| 10  | 5   | 10 | 10        | 5       |
| 13:00     | 0           | 0         | 15  | 5        | 10  | **25**| 10  | 5   | 10 | 15        | 5       |

Key: HTF (15m SuperTrend) weight grows from 10 → 25 as day progresses; OR breakout
weight falls from 25 → 0 after early morning. Phase 3 (80+ trades): weights replaced by
logistic regression coefficients fitted on actual trade outcomes.

### Layer 2 — Composite Signal Scorer (signal_scorer.py)

8 weighted components, 0–100 total. Active all day (before post-11 layer fires).

| Component | Weight | What it Scores |
|-----------|--------|---------------|
| htf_align | 20 | 15m SuperTrend + 15m EMA both aligned: 20; one: 10; neither: 0 |
| adx_mag | 15 | ADX ≥40: 15 | ≥35: 12 | ≥30: 9 | ≥25: 6 | <25: 0 |
| pcr_align | 20 | PCR >1.2 for CALL: 20; PCR 1.0–1.2: 12; PCR <0.7 for PUT: 20 |
| ema_fresh | 15 | Bars since EMA cross: 1–2b: 15; 3–4b: 11; 5–7b: 7; 8–10b: 4; >10b: 0 |
| vwap_dist | 10 | Dist from VWAP: ≥0.20%: 10; ≥0.10%: 7; ≥0.05%: 4; <0.05%: 1 |
| oi_zone | 5 | BOOST: 5; TAKE: 3; REDUCE: 1; SKIP: 0 |
| max_pain | 5 | MaxPain >1.0% tailwind: 5; 0.3–1.0%: 3; near: 2; headwind: 0 |
| consolidation | 10 | ATR ratio <0.70 (coiled): 10; <0.90: 7; <1.10: 5; <1.30: 3; >1.30: 0 |

**Gates (Phase 2 — active now):**
```
Total < 40  → REDUCE: cap to 1 lot (weak signal quality)
Total 40–64 → TRADE:  1 lot (standard)
Total ≥ 65  → HIGH CONVICTION: upgrade to 2 lots
Total < 30  → SKIP (Phase 3 only — not yet live; Phase 2 just reduces to 1 lot)
```

### Layer 3 — Post-11 Scorer (post11_scorer.py)

Only fires for Path-A ORB signals at or after **11:00 IST**. Replaces EMA freshness
(which is meaningless at 11am) with live DI spread and OR extension.
**Can SKIP (block) the trade; can only reduce lots, not raise above Layer 2 output.**

| Component | Weight | What it Scores |
|-----------|--------|---------------|
| or_extension | 25 | OR extension ≥0.80%: 25; ≥0.50%: 20; ≥0.25%: 13; ≥0.10%: 7; <0.10%: 2 |
| di_strength | 20 | DI spread ≥25: 20; ≥15: 15; ≥8: 9; ≥3: 4; ≥0: 1; negative: 0 |
| time_theta | 15 | 11:00–11:30: 15; 11:30–12:00: 11; 12:00–12:30: 7; 12:30–13:00: 4; 13:00+: 2 |
| adx_strength | 15 | ADX ≥45: 15; ≥38: 12; ≥30: 9; ≥25: 5; <25: 1 |
| oi_context | 15 | PCR (5) + MaxPain (5) + OI wall proximity (5) |
| vwap_structure | 10 | Wrong side: 0; dist ≥0.40%: 10; ≥0.20%: 7; ≥0.08%: 4; <0.08%: 1 |

ATR adjustment: expanding ATR (>1.5×) → −6 bonus; consolidating (<0.8×) → +4 bonus.

**Gates:**
```
Total < 40  → SKIP:     entry BLOCKED (aggregate quality too low)
Total 40–44 → MARGINAL: 1 lot, caution log
Total 45–69 → TRADE:    1 lot
Total ≥ 70  → STRONG:   2 lots + optional OTM boost (if POST11_OTM_BOOST=True)
```

No single component can block — only the aggregate total triggers SKIP.

---

## 4. LOT SIZING SUMMARY

```
Base strength (from get_path_a_signal):
  ADX≥35 (+1) + DI-spread≥5 (+1) + VWAP dist≥0.10% (+1) → strength score
  strength ≥ 2 → 2 lots (base)
  else          → 1 lot (base)

Layer 2 override:
  Composite score < 40 → cap to 1 lot (overrides 2-lot base)
  Composite score ≥ 65 → upgrade to 2 lots (overrides 1-lot base)

Layer 3 override (≥11:00 entries only):
  Post-11 SKIP        → 0 lots (trade blocked)
  Post-11 MARGINAL    → max(1, base)
  Post-11 TRADE       → min(base, 1)
  Post-11 STRONG      → min(base, 2)

Layer 3 can only reduce vs Layer 2 result; it never raises above it.
```

---

## 5. CAPITAL & INSTRUMENT CONFIG

| Instrument | Lot Size | Capital | Mode |
|------------|----------|---------|------|
| NIFTY      | 65       | ₹26,000 | LIVE |
| BANKNIFTY  | 30       | ₹26,000 | paper (capital < ₹50k gate) |
| SENSEX     | 20       | ₹50,000 | paper (capital < ₹75k gate) |

```
PATH_A_MAX_TRADES = 1    (one entry per day per instrument)
Re-entry          = 1    (after stop-loss, ADX ≥ 35 before 13:00)
Max concurrent    = 1    (no simultaneous positions)
Max daily loss    = ₹2,500
```

Capital gate auto-evaluated at bot startup; no manual flag changes needed.

---

## 6. EXIT (Priority Order — First Match Wins)

### EXIT 1 — Hard Stop-Loss

```
Stop = 25% below entry premium
```
Example: NIFTY ATM PE at ₹120 → stop at ₹90 (≈80 index points adverse at delta 0.5).
Fires within ~20 seconds (EXIT_POLL_INTERVAL = 20s).

### EXIT 2 — Rapid Spike Exit (PRIMARY Profit Exit)

Detects explosive LTP spike across the last 3 polls (3 × 20s window).
Fires BEFORE target and trail. Threshold scales with unrealized P&L/lot:

| Tier | Unrealized P&L / lot | Spike % threshold |
|------|----------------------|------------------|
| OK | < ₹2,500 | **DISABLED** — let it run |
| Good | ₹2,500 – ₹5,000 | 35% |
| V.Good | ₹5,000 – ₹8,000 | 20% |
| Excellent | ₹8,000 – ₹12,000 | 10% |
| Exceptional | ₹12,000+ | 5% |

Rationale: rapid LTP spike = market pricing in the expected move (implied peak).
Exiting at the spike captures more than waiting for realized-move decay.

### EXIT 3 — Trailing Stop (Secondary Profit Exit)

```
Arms at:   18% gain above entry premium
Distance:  10% below peak observed premium
```
Example: enter ₹120, peak ₹168 (+40%) → trail stop at ₹151.20 (+26%).

### EXIT 4 — Target Backstop

```
Base target = 55%   (IV-scaled: target = 55% × ATM_IV / 15%)
Floor: 30%  |  Cap: 85%
```

| ATM IV | Effective Target |
|--------|-----------------|
| 10% | 36.7% (floored at 30%) |
| 15% | 55% (reference) |
| 20% | 73% |
| 25% | 91.7% (capped at 85%) |

R:R = 55/25 = 2.2:1 on backstop exits. Spike and trail exit most trades earlier.

### EXIT 5 — Conditional Checkpoint

Evaluated once at per-day checkpoint time (12:00 Mon/Tue/Fri · 10:55 Wed/Thu):

```
P&L < 0  → UNCONDITIONAL HARD CLOSE

P&L ≥ 0  → HOLD if gain ≥ 15% AND ADX ≥ 30 AND EMA aligned
             (trail active, force-close at 14:30)
          → CLOSE immediately otherwise
```

### EXIT 6 — Force Close

14:30 hard close — all open positions, regardless of P&L.

---

## 7. EXIT PRIORITY DIAGRAM

```
Every 20-second poll:
  ┌──────────────────────────────┐
  │ 1. Stop-loss hit (−25%)?     │ YES → EXIT (loss)
  └─────────────┬────────────────┘
                │ NO
  ┌─────────────▼────────────────┐
  │ 2. Rapid spike triggered?    │ YES → EXIT (implied-peak profit)
  └─────────────┬────────────────┘
                │ NO
  ┌─────────────▼────────────────┐
  │ 3. Trail armed & hit?        │ YES → EXIT (sustained-move profit)
  └─────────────┬────────────────┘
                │ NO
  ┌─────────────▼────────────────┐
  │ 4. Target backstop ≥ 55%?    │ YES → EXIT (exceptional-day profit)
  └─────────────┬────────────────┘
                │ NO
  ┌─────────────▼────────────────┐
  │ 5. Checkpoint time?          │ → conditional hold or close
  └─────────────┬────────────────┘
                │ holding
  ┌─────────────▼────────────────┐
  │ 6. 14:30 force close         │ → EXIT unconditionally
  └──────────────────────────────┘
```

---

## 8. WHAT IS NOT TRADED

| Condition | Reason |
|-----------|--------|
| 09:15–09:30 | Pre-OR; range not yet formed |
| ADX below day floor AND GAP_REV fails | No trending structure |
| OR width > per-day max | Options already overpriced |
| Price on wrong side of VWAP | Counter-trend |
| OI bias REJECT (score ≤ −2) | OI context hard-contradicts direction |
| OI bias NEUTRAL on Tue/Wed | Confirmation required on expiry-adjacent days |
| Unified score < 55/100 | Overall setup quality insufficient |
| Post-11 score < 40/100 | Late-day aggregate quality too low |
| After first stop-loss, ADX < 35 | Re-entry requires stronger trend confirmation |
| After 13:00 | Re-entry window closed |
| NIFTY CALL on Tuesday | skip_tuesday=True (CALL risk on Tue; PUT still valid) |
| BNF CALL on Thursday | PUT-only bias on BNF expiry day |
| SENSEX on Thursday | skip_thursday=True (Thu WR = 29%) |
| Non-ORB second-half entries | All 11:00–14:00 non-ORB combos tested negative |
| TMS / CONT / Path E / Path F | All disabled (ORB-only mode, Apr 2026) |

---

## 9. CONFIG REFERENCE (live values — May 13 2026)

```python
# Strike
PATH_A_OTM_ENABLED       = False   # always ATM

# Risk / Reward
STOP_LOSS                = 0.25    # 25%
BASE_TARGET              = 0.55    # 55% backstop
TRAILING_ACTIVATION      = 0.18    # trail arms at 18% gain
TRAILING_DISTANCE        = 0.10    # 10% below peak

# IV scaling
TARGET_IV_DYNAMIC        = True
TARGET_IV_REF            = 15.0    # reference IV %
TARGET_IV_MIN_PCT        = 0.30    # floor (30%)
TARGET_IV_MAX_PCT        = 0.85    # cap (85%)

# Spike exit
RAPID_SPIKE_ENABLED      = True
RAPID_SPIKE_MIN_GAIN     = 0.10    # spike fires only if unrealized gain ≥ 10%
RAPID_SPIKE_BARS         = 3       # polls in detection window (3 × 20s)
RAPID_SPIKE_TIERS        = [(2_500,0.35),(5_000,0.20),(8_000,0.10),(12_000,0.05)]

# Unified scorer
UNIFIED_SCORER_ENABLED   = True
UNIFIED_SCORE_THRESHOLD  = 55      # below → entry blocked

# Composite scorer (signal_scorer.py) — Phase 2 active
HIGH_CONVICTION_SCORER_THRESHOLD = 65   # ≥ this → 2 lots

# Post-11 scorer (post11_scorer.py)
POST11_SCORE_SKIP_MIN    = 40      # below → SKIP (block entry)

# Gap-Reversal supplement
GAP_REV_ENABLED          = True
GAP_REV_MIN_GAP_PCT      = 0.005   # ≥ 0.5% gap
GAP_REV_RECOVERY_PCT     = 0.50    # ≥ 50% recovered
GAP_REV_ADX_MIN          = 18      # reduced ADX floor
GAP_REV_DI_SPREAD_MIN    = 12      # DI spread in recovery direction
GAP_REV_ENTRY_EXT        = '12:30' # extended entry window

# Checkpoint
PATH_A_LOSS_STOP_AT_CHECKPOINT  = True
PATH_A_CHECKPOINT_HOLD_MIN_PCT  = 0.15
PATH_A_CHECKPOINT_ADX_MIN       = 30
PATH_A_FORCE_CLOSE_TIME         = '14:30'

# Per-instrument (NIFTY / BANKNIFTY / SENSEX — all identical May 2026)
# path_a_stop      = 0.25
# path_a_target    = 0.55
# path_a_trail_act = 0.18
# path_a_trail_dist = 0.10
```

---

## 10. MONITORING

```bash
ssh -i $PEM ec2-user@3.108.16.113 'journalctl -u fno_t_bot_nifty -f'
ssh -i $PEM ec2-user@3.108.16.113 'journalctl -u fno_t_bot_banknifty -f'
python /opt/trading_bot/live_bot/capital_status.py --fyers
cat /opt/trading_bot/live_bot/logs/morning_brief_$(date +%Y%m%d).txt
```

---

*Published May 13 2026. Champion: ATM + 25% stop + 55% target + 18% trail.
Gap-Reversal supplement added May 13 2026. Three-layer entry scorer active.*
