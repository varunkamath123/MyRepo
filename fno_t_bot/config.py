# ─── Fyers Credentials ────────────────────────────────────────────────────────
# LOCAL DEV : values below are used as fallback
# AWS / PROD : set these as environment variables in .env or systemd EnvironmentFile
# NEVER commit real credentials to git — see .gitignore

import os
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    pass

FYERS_APP_ID     = os.getenv('FYERS_APP_ID',     'YFSVRV8W47-200')
FYERS_SECRET_KEY = os.getenv('FYERS_SECRET_KEY',  '5STK89PY97')
FYERS_CLIENT_ID  = os.getenv('FYERS_CLIENT_ID',   'FAI64160')
REDIRECT_URI     = os.getenv('REDIRECT_URI',       'http://127.0.0.1:8000')

# For automated daily auth on AWS (set in .env)
FYERS_PIN        = os.getenv('FYERS_PIN',      '')   # 4-digit trading PIN
FYERS_TOTP_KEY   = os.getenv('FYERS_TOTP_KEY', '')   # TOTP secret from Fyers app (not used if WhatsApp OTP)
FYERS_WA_SENDER  = os.getenv('FYERS_WA_SENDER', 'Fyers')  # WhatsApp contact/business name that sends OTP

# ─── Bot Identity ────────────────────────────────────────────────────────────
BOT_NAME = "FnO_T_Bot"     # Futures & Options Trading Bot

# ─── Trading Mode ─────────────────────────────────────────────────────────────
# Global fallback. Per-instrument 'live_mode' in INSTRUMENT_STRATEGY overrides this.
# Set True to paper-trade all; set False to use per-instrument live_mode flags.
PAPER_TRADE_MODE = False   # Global flag — overridden per-instrument via live_mode below

# ─── Multi-Instrument Configuration ──────────────────────────────────────────
# Lot sizes (confirmed Mar 2026):
#   NIFTY 50  : 65  (confirmed)
#   BANKNIFTY : 30  (confirmed — SEBI raised from 15)
#   SENSEX    : 20  (confirmed)
INSTRUMENTS = {
    'NIFTY': {
        'index_symbol'       : 'NSE:NIFTY50-INDEX',
        'option_prefix'      : 'NSE:NIFTY',
        'lot_size'           : 65,        # confirmed Mar 2026
        'strike_gap'         : 50,
        'expiry_weekday'     : 1,         # Tuesday (Mon=0) — 2026 regime: weekly Tuesday expiry
        'monthly_expiry_only': False,     # NIFTY has weekly Tuesday expiries (not monthly-only)
                                          # Fyers compact format: NSE:NIFTY266DD[strike]CE
                                          # Confirmed Jun 2026: fills land on weekly Tue contracts
        'capital'            : 26000,     # ₹26k — unified bot (ORB 09:30–11:00 + main 11:00–14:30)
    },
    'BANKNIFTY': {
        'index_symbol'       : 'NSE:NIFTYBANK-INDEX',
        'option_prefix'      : 'NSE:BANKNIFTY',
        'lot_size'           : 30,        # confirmed Mar 2026 (SEBI raised from 15)
        'strike_gap'         : 100,
        'expiry_weekday'     : 1,         # Tuesday (Mon=0) — 2026 regime: monthly last Tuesday
        'monthly_expiry_only': True,      # SEBI Nov 2023: NSE dropped BANKNIFTY weeklies
                                          # Only monthly expiry (last Tue of month) is valid
        'skip_monday_before_expiry': True, # Mon before last-Tue rolls DTE to ~29 — wrong option
        'capital'            : 26000,     # ₹26k — unified bot (ORB 09:30–11:00 + main 11:00–14:30)
    },
    'SENSEX': {
        'index_symbol'  : 'BSE:SENSEX-INDEX',
        'option_prefix' : 'BSE:SENSEX',  # BSE weekly options: BSE:SENSEX26515[strike]PE
        'lot_size'      : 20,        # confirmed Mar 2026
        'strike_gap'    : 200,       # SENSEX ~77,000+; 200pt gaps
        'expiry_weekday': 3,         # Thursday (Mon=0) — 2026 regime: SENSEX weekly Thu +
                                     # monthly last-Thu (verified via Breeze Jun 12 2026).
                                     # FIXED Jul 9: was 4 (Friday) — the Jun 12 fix was made
                                     # on EC2 but never committed (deploy-drift casualty #5).
                                     # Friday built invalid symbols (e.g. SENSEX26717...CE,
                                     # Jul 17 = Friday) → Fyers errmsg → LTP None → every
                                     # SENSEX weekly-contract live order aborted (Jul 9), and
                                     # position monitoring was blind (May 4 -52% overrun).
        'capital'       : 26000,     # Live capital — same allocation as NIFTY/BNF (May 2026)
    },
}
TOTAL_CAPITAL   = 78000     # ₹78,000 — ₹26k NIFTY + ₹26k BANKNIFTY + ₹26k SENSEX (all live)
INITIAL_CAPITAL = TOTAL_CAPITAL  # backward-compat for bot.py backtest

# ─── Per-Instrument Strategy Overrides (used by variant vX) ──────────────────
# Each instrument has been individually tuned on 14-month Fyers data (Jan 2025–Mar 2026).
# Conclusions:
#   NIFTY    : v9c filters work well; restrict entries before 14:00 (14:xx = 25% WR)
#   BANKNIFTY: CALL WR 60% (relax CALL ADX to 25); PUT WR 43% (raise PUT ADX to 35);
#              allow 2 concurrent positions — more signals, fewer daily conflicts
#   SENSEX   : 11:xx entries are a disaster (29% WR, -₹8,246) — shift start to 12:00;
#              skip Thursday (net -₹1,599 despite 8 trades)
INSTRUMENT_STRATEGY = {
    # ── NIFTY ──────────────────────────────────────────────────────────────────
    # Data: 14-month Fyers backtest Jan 2025–Mar 2026 (25 trades, 72% WR, v9c)
    # Key findings:
    #   - CALL WR 100% (5/5) with ADX≥30 — strong filter working perfectly
    #   - PUT  WR 65%  (20/20) with ADX≥25 — reliable
    #   - 14:xx entries: 25% WR, too close to 14:30 force-close — blocked
    #   - Tuesday: confirmed danger zone (v7 data: 31.2% WR, -₹3,472)
    'NIFTY': {
        'live_mode'     : True,   # LIVE — real orders via Fyers
        'capital'       : 26000,  # ₹26k — unified bot (ORB + main session)
        'skip_tuesday'  : True,
        'skip_thursday' : False,  # Thu 60% WR, +₹3,258 — keep
        'call_adx_min': 30,     # Confirmed: ADX 25-29 CALL entries whipsaw and stop out
        'put_adx_min': 25,     # PUT resilient at standard threshold
        # Tuesday CALL conditions: standard CALL threshold — ADX≥35 was over-fit on thin data
        # (31.2% base WR was unfiltered; ADX≥30 already eliminates weak bounces)
        'tuesday_call_adx_min'   : 30,  # matches standard call_adx_min — no extra penalty
        'tuesday_call_di_spread' : 8,   # DI+ must dominate clearly (DI+ minus DI- ≥ 8)
        # Tuesday PUT conditions: keep slightly elevated — gap-fill risk real on Tuesdays
        'tuesday_put_adx_min'   : 30,  # must be a clearly established downtrend (vs standard 25)
        'tuesday_put_di_spread' : 8,   # DI- must dominate convincingly (DI- minus DI+ ≥ 8)
        'entry_start'   : '11:00',# Skip 9:15-10:59 (10:xx = 11% WR danger zone)
        'entry_end'     : '14:00',# 14:xx = 25% WR; too close to force-close — blocked
        'max_concurrent': 1,      # No pyramiding — signal quality already high at 72% WR
        # ── Per-instrument PATH-A exit parameters (override global PATH_A_*) ──
        # NIFTY: baseline — matches current global settings (no change)
        'path_a_stop'       : 0.25,   # 25% stop — Challenger->Champion May 2026 (was 50% for 2-DTE)
        'path_a_target'     : 0.55,   # 55% backstop — spike+trail primary; 55% fires on exceptional days
        'path_a_otm1_target': 1.20,   # 120% OTM+1 target (kept for reference; OTM disabled May 2026)
        'path_a_trail_act'  : 0.18,   # trail at 18% — arms halfway to target; trail is primary exit
        'path_a_trail_dist' : 0.10,   # 10% trail distance (global PATH_A_TRAIL_DIST)
    },
    # ── BANKNIFTY ──────────────────────────────────────────────────────────────
    # Data: 14-month Fyers backtest (24 trades v9c: CALL 10tr/60%WR, PUT 14tr/43%WR)
    # Key findings:
    #   - CALL at ADX≥30: 60% WR — threshold confirmed; DO NOT relax (25-29 ADX = 43% WR)
    #   - PUT ADX=30 paradox: 5 PUTs, 0% WR, -₹4,791 — counter-trend in bull market.
    #     At ADX≥30 in a bull regime, bearish EMA crosses are false breakouts.
    #     At ADX 25-29 (moderate trend), PUTs have genuine reversal credibility (43% WR).
    #     → Keep PUT ADX at 25 (v9c baseline): 14 PUTs, 43% WR, +₹1,680 (marginally profitable)
    #   - 14:xx entries: 56% WR for BANKNIFTY (better than NIFTY's 25%) — keep 14:45 end
    #   - concurrent=2 caused cascading losses (4 stops, -₹13,165 on May 2 2025)
    #   - Best BANKNIFTY setting = v9c baseline (keep consistent with proven filters)
    'BANKNIFTY': {
        'live_mode'     : True,   # INTENDED live — actual mode gated by capital_gate.py
                                  # (LIVE only when combined capital >= CAPITAL_GATE_BNF_LIVE=50k)
        'capital'       : 26000,  # ₹26k — unified bot (ORB + main session)
        'skip_tuesday'  : True,
        'skip_thursday' : False,
        'call_adx_min': 30,     # Confirmed 60% WR — maintain this threshold
        'put_adx_min': 25,     # Reverted to 25: higher threshold = worse (0% WR in test)
        # Tuesday CALL conditions: extra strict — Wed expiry forces 8-day next-week options
        # (low gamma). A CALL is only worth it if there's a very strong, sustained bull trend.
        'tuesday_call_adx_min'   : 35,  # strong established uptrend (vs standard 30)
        'tuesday_call_di_spread' : 12,  # DI+ must clearly dominate — mirror the PUT gate
        # Tuesday PUT conditions: extra strict — Wednesday expiry forces 8-day next-week
        # options (low gamma), making the 80% target hard to hit on weak moves.
        'tuesday_put_adx_min'   : 35,  # needs a genuinely strong bear trend for 8-day options
        'tuesday_put_di_spread' : 12,  # high DI dominance to justify low-gamma contract
        'entry_start'   : '11:00',
        'entry_end'     : '14:00',# Cut from 14:45 → 14:00 (Apr 24 2026): 14:10 2-lot entry had only 20 min to EOD force-close (14:30). 56% WR stat pre-dates Strength=2 lot-doubling. 30-min minimum runway required.
        'max_concurrent': 1,      # Concurrent=2 not valid — causes cascading losses
        # ── Per-instrument PATH-A exit parameters (override global PATH_A_*) ──
        # BNF: tighter stop (decisive trends — cut losses sooner), higher target (premium
        # spikes more on BNF), earlier trail (BNF fast-moving, lock in gains sooner).
        'path_a_stop'       : 0.25,   # 25% stop — Challenger->Champion May 2026 (was 45% for 2-DTE)
        'path_a_target'     : 0.55,   # 55% backstop — spike+trail primary; 55% fires on exceptional days
        'path_a_otm1_target': 1.35,   # 135% OTM+1 target (kept for reference; OTM disabled May 2026)
        'path_a_trail_act'  : 0.18,   # trail at 18% — arms halfway to target; trail is primary exit
        'path_a_trail_dist' : 0.08,   # 8% trail distance (vs 10%): tighter — BNF reverses fast
    },
    # ── SENSEX ─────────────────────────────────────────────────────────────────
    # Data: 14-month Fyers backtest (28 trades v9c: 67.9% WR)
    # Key findings:
    #   - 11:xx entries: disaster zone (29% WR, -₹8,246) — shifted start to 12:00
    #   - 12:xx entries: 77.8% WR after start shift — excellent
    #   - 13:xx entries: 87.5% WR — prime window
    #   - 14:xx entries: 33% WR — same problem as NIFTY, cut at 14:00
    #   - Thursday: -₹1,599 net despite 8 trades on unfiltered pre-scoring data.
    #     Blanket block removed May 2026 — three-layer scoring (Unified≥55, Post-11≥40)
    #     handles quality. 12:00 start already excludes the bad 11:xx window.
    #   - Only 2 stops with 12:00 start (vs 5 in v9c) — quality confirmed improved
    'SENSEX': {
        'live_mode'     : False,  # Overridden LIVE by FORCE_SENSEX_LIVE=True (May 2026)
                                  # capital_gate.py honours FORCE_SENSEX_LIVE before threshold check
        'capital'       : 26000,  # Live capital — ₹26k matching NIFTY/BNF allocation
        'skip_tuesday'  : True,
        'skip_thursday' : False,  # Blanket block removed May 2026 — scoring gates handle quality
        'call_adx_min': 25,     # CALL WR 61.5% — standard threshold fine
        'put_adx_min': 25,     # PUT WR 69.2% — reliable
        # Tuesday CALL conditions
        'tuesday_call_adx_min'   : 30,
        'tuesday_call_di_spread' : 8,
        # Tuesday PUT conditions
        'tuesday_put_adx_min'   : 30,
        'tuesday_put_di_spread' : 8,
        'entry_start'   : '11:00',# Aligned with NIFTY/BNF window (May 2026)
                                  # Note: PATH-A ORB fires on OR break independently of this
        'entry_end'     : '14:00',# 14:xx = 33% WR — blocked (consistent with NIFTY finding)
        'max_concurrent': 1,
        # ── Per-instrument PATH-A exit parameters (override global PATH_A_*) ──
        # SENSEX: BSE less liquid — premium moves slower. Wider trail gives positions
        # room to breathe before reversing. Stop/target same as NIFTY baseline.
        'path_a_stop'       : 0.25,   # 25% stop — Challenger->Champion May 2026 (was 50% for 2-DTE)
        'path_a_target'     : 0.55,   # 55% backstop — spike+trail primary; 55% fires on exceptional days
        'path_a_otm1_target': 1.20,   # 120% OTM+1 target (kept for reference; OTM disabled May 2026)
        'path_a_trail_act'  : 0.18,   # trail at 18% — arms halfway to target; trail is primary exit
        'path_a_trail_dist' : 0.12,   # 12% trail distance (vs 10%): wider — SENSEX needs room
    },
}

# ─── Strategy Settings ────────────────────────────────────────────────────────
# !! CALIBRATION NOTE (Apr 2026) !!
# All three instruments are now MONTHLY-ONLY expiry (SEBI banned weekly options Oct 2024).
# DTE at entry is typically 9–14 days (not 2). Option % sensitivity is ~4x lower per NIFTY point.
# Parameters below are recalibrated for 12-DTE options:
#   - Old 80% target needed 429pt NIFTY move (almost never intraday) → 89% of trades hit EOD close
#   - New 28% target needs ~165pt NIFTY move (achievable on strong trend days)
#   - Old 50% stop = 120pt NIFTY adverse move (too wide, held losers too long)
#   - New 25% stop = ~80pt NIFTY adverse move (tight but right for this gamma)
# Prior 2-DTE params archived: SL=50%, TGT=80%, TrailAct=60%, TrailDist=20%
MAX_CONCURRENT_POSITIONS = 1        # No doubling down in choppy markets
STOP_LOSS           = 0.25         # 25% stop — recalibrated for 12-DTE (was 50% for 2-DTE)
                                   # 25% = ~80pt adverse NIFTY move. Firm but fair for monthly options.
BASE_TARGET         = 0.55         # 55% backstop — spike exits first on explosive moves;
                                   # trail from 18% captures sustained trends; 55% fires on exceptional days.
                                   # 55/25 stop = 2.2:1 R:R. Trail arms at 18% and is primary profit exit.
USE_TRAILING_PROFIT = True
TRAILING_ACTIVATION  = 0.12        # Start trailing at 12% gain — applies to REV and RECLAIM paths.
                                   # ORB_HELD (A_HELD) uses the wider TRAIL_ACT_ORB_HELD below.
                                   # REV/RECLAIM are shorter-lived reversals; arm trail earlier to protect gains.
TRAIL_ACT_ORB_HELD   = 0.18        # Trail activation for ORB_HELD (held past 12:00 checkpoint).
                                   # June comparison: 18% trail preserved +₹4,339 on Jun 1 BNF ORB_HELD
                                   # vs 12% which fired on a transient dip, capturing only +₹1,209.
                                   # ORB_HELD positions are strong-trend survivors — wider trail is correct.
TRAILING_DISTANCE    = 0.10        # Trail distance 10% from peak (was 20%)
                                   # Tighter trail: locks in gains sooner on slower-moving 12-DTE options.

# Never-Progressed exit (Jul 2026, from live-trade analysis Apr-Jun):
# Trades that never gained traction bled to EOD force-close: the 90-180min hold
# bucket ran 24% WR / -₹18.8k while every trailing-stop winner passed +12% well
# before 90min. Cuts a position that is (a) ≥90min old, (b) never peaked +3%,
# and (c) currently red — instead of letting it ride to 14:30.
# Covers the gap left by the checkpoint loss-stop: entries AFTER the checkpoint
# (REV at 12:00, late PATH-A) previously had no progress-based exit at all
# (e.g. Jun 9 BNF REV -₹4,180 bled the full 150min to force-close).
NEVER_PROGRESS_ENABLED   = True
NEVER_PROGRESS_MINUTES   = 45      # min age before the check applies. LOWERED 90→45
                                   # (Jul 21 diag): at 90 the exit was DEAD CODE — 0
                                   # fires ever. It was pincered: morning entries hit the
                                   # 12:00 checkpoint first, afternoon Path B entries hit
                                   # the 14:30 force-close first (they enter 13:40+ with
                                   # <50min runway). 45min fits inside the afternoon runway
                                   # so a never-progressed Path B bleeder gets cut ~14:25
                                   # instead of riding to force-close (Jul 16 losers peaked
                                   # +1.9%/+3.0%, force-closed -5.7%/-11.6%).
NEVER_PROGRESS_MIN_PEAK  = 0.03    # peaked ≥ +3% at any point = exempt (has shown life)

# Checkpoint loss-stop exemption (Jul 23 2026 — v1.9). The 12PM hard loss-stop
# was killing red-at-that-instant positions unconditionally, regardless of how
# strong the thesis had been intraday — using P&L SIGN as the only signal.
# Two documented casualties: Jul 17 SENSEX peaked +8.3% -> killed -1.3%;
# Jul 22 NIFTY peaked +15.4% -> killed -1.7%. Meanwhile Jul 22 SENSEX at only
# +0.2% at checkpoint got the exit-score evaluation (score 65 HOLD) and ran to
# +1,095 — the SAME mechanism exists, it just never got applied to red trades.
# Fix: a position that peaked >= this threshold is red-but-strong, not
# red-and-dead — route it to the SAME exit-score evaluation used for green
# positions instead of an automatic kill. Set above NEVER_PROGRESS_MIN_PEAK
# (3%) since converting to A_HELD has a bigger consequence (runs for hours)
# than the never-progress cut — a barely-positive peak (e.g. 2.9%, still
# correctly killed live on Jul 22 BANKNIFTY) shouldn't earn a second look.
CHECKPOINT_LOSS_STOP_MIN_PEAK = 0.05

# Dynamic profit target — scale BASE_TARGET with ATM-IV at entry (May 2026)
# High IV = bigger daily swings → option can reach 50% gain on same move
# that only earns 20% on a quiet low-IV day. Formula: BASE_TARGET * (atm_iv / REF).
# Example: REF=15%, BASE=28% → IV=10% → target=18.7%; IV=20% → target=37.3%
TARGET_IV_DYNAMIC  = True    # enable IV-scaled target (False = always use BASE_TARGET)
TARGET_IV_REF      = 15.0    # IV% at which BASE_TARGET is the "natural" target
TARGET_IV_MIN_PCT  = 0.30    # floor: low-IV days; raised from 20% to match 55% base (25% floor→30%)
TARGET_IV_MAX_PCT  = 0.85    # ceiling: raised from 50% to allow high-IV days to reach 85% backstop

MAX_HOLDING_DAYS    = 1            # Same-day exits only
DAYS_TO_EXPIRY      = 2            # Minimum DTE filter — get_next_expiry() uses MIN_DAYS_TO_EXPIRY
MIN_DAYS_TO_EXPIRY  = 2            # Skip to next month's expiry if < 2 days left

# Strategy flags
USE_TREND_MOMENTUM  = True
USE_LIQUIDITY_SWEEP = False   # Needs real tick/order-flow volume — index OHLCV insufficient
USE_MEAN_REVERSION  = False
USE_BREAKOUT        = False

# Trend Momentum
MOMENTUM_ADX_THRESHOLD = 25        # Back to 25 — EMA 9/21 alone cuts noise enough
MOMENTUM_EMA_FAST      = 9         # Raised from 6  — less whipsaw than 6/12
MOMENTUM_EMA_SLOW      = 21        # Raised from 12 — classic 9/21 trend pair

# Mean Reversion (future)
MEAN_REVERSION_RSI_OVERSOLD   = 30
MEAN_REVERSION_RSI_OVERBOUGHT = 70
MEAN_REVERSION_BB_PERIOD      = 20
MEAN_REVERSION_BB_STD         = 2
MEAN_REVERSION_STOP           = 0.35
MEAN_REVERSION_TARGET         = 0.80
MEAN_REVERSION_MAX_HOLD_HOURS = 4

# Breakout
BREAKOUT_PERIOD     = 12
BREAKOUT_VOLUME_MULT= 1.2
BREAKOUT_ADX_MIN    = 18

# ─── Timeframes ───────────────────────────────────────────────────────────────
PRIMARY_TIMEFRAME          = "5min"
LOOKBACK_PERIODS           = 100
USE_MULTI_TIMEFRAME        = True
TREND_MOMENTUM_TIMEFRAME   = "1hour"
BREAKOUT_TIMEFRAME         = "15min"
GAP_FADE_TIMEFRAME         = "5min"

# ─── Multi-Timeframe Confirmation ────────────────────────────────────────────
# 15min SuperTrend direction must agree with 5min EMA signal.
# SuperTrend = ATR-based trend overlay, excellent at filtering whipsaws.
SUPERTREND_PERIOD          = 10        # ATR period for SuperTrend computation
SUPERTREND_MULTIPLIER      = 3.0       # ATR multiplier for trend bands
USE_SUPERTREND_FILTER      = False     # v8 tested: too few trades (23); W/L 1.59x promising — revisit with more data
USE_HTF_EMA_FILTER         = False     # v8 tested: too few trades (15); WR 33% — not useful

# ─── Pivot Support / Resistance ──────────────────────────────────────────────
# Previous day's pivot = (High + Low + Close) / 3
# CALL must be above pivot (bullish stance), PUT must be below
USE_PIVOT_SR               = False     # v8 tested: W/L 1.00x — no improvement

# ─── India VIX Sentiment Filter ──────────────────────────────────────────────
# India VIX captures geopolitical/macro fear. Entries only in a "goldilocks" zone.
# VIX > 22: too chaotic (NIFTY whipsaws, options over-priced)
# VIX < 11: too quiet (small moves, 80% target rarely hit)
# Requires VIX data — auto-disabled if data not available.
USE_VIX_FILTER             = True
VIX_MAX                    = 22
VIX_MIN                    = 11
# Directional conviction override: when VIX > VIX_MAX, allow entry if BOTH
# 15m-ST + 15m-EMA agree on direction (HTF unambiguous).
# Rationale: high VIX on a confirmed trending day means large moves ARE coming.
# Block only when HTF is mixed — uncertain direction + overpriced options = bad.
# ADX is already enforced inside Mode A / Mode B signal logic — not re-gated here.

# ─── Market Hours (IST) ───────────────────────────────────────────────────────
MARKET_OPEN_TIME  = "09:15"
MARKET_CLOSE_TIME = "15:30"
BOT_CHECK_INTERVAL  = 60    # seconds between full scan cycles (index data + signals)
EXIT_POLL_INTERVAL  = 20    # seconds between quick option-LTP exit checks when a
                            # position is open.  Divides the 60s cycle into 3 polls
                            # (~t=0s, t=20s, t=40s) so spike/stop exits fire within
                            # 20s of the move rather than up to 60s later.
                            # Only fetches option LTP (no index API call) — cheap.

# ─── Intraday Timing ──────────────────────────────────────────────────────────
AVOID_FIRST_MINUTES = 105           # Skip 9:15–11:00 (10:xx = 11% WR danger zone per analyzer)
AVOID_LAST_MINUTES  = 45            # Skip 14:45–15:30 EOD noise
AVOID_LUNCH_HOURS   = False
LUNCH_START         = "12:30"
LUNCH_END           = "13:30"
INTRADAY_FORCE_CLOSE = True
FORCE_CLOSE_TIME     = "14:30"      # Exit 60min before close (was 15:10) — reduce drift

# ─── Risk Management ─────────────────────────────────────────────────────────
MAX_DAILY_LOSS           = 8000    # PRIMARY gate: stop trading when per-instrument P&L < -₹8,000
MAX_TRADES_PER_DAY       = 5       # Safety backstop only — real gate is MAX_DAILY_LOSS above.
                                   # Set to 5 (full 3-hr window can't produce more than this).
                                   # Raised from 2: no reason to cap winners on a trending day.
RISK_FREE_RATE           = 0.065

# Consolidated daily loss cap — shared across ALL running bot processes
# (paper_bot × 3 instruments + early_bot × 3 instruments = up to 6 processes).
# Enforced via shared_state.py — no new entries when grand_total ≤ -cap.
CONSOLIDATED_DAILY_LOSS_CAP = 8000   # ₹8,000 combined across all instruments/bots

# Per-trade rupee risk cap (REINSTATED Jul 8 2026 / v1.6 — the original Jun 10
# gate lived only on EC2 and was destroyed by deploy drift, same as the funds
# check and progress-decay exit). risk = premium × contracts × stop%.
# Sizing ladder in enter_trade(): a multi-lot entry that busts the cap first
# tries one strike further OTM at the same lots (cheaper premium — "multiple
# lots at the appropriate strike"), then shaves to 1 lot, and only then skips.
# Jun 11 live evidence: the original gate blocked 2 BNF CALLs whose Challenger
# shadow twins lost ₹5,988 + ₹6,222 (~₹12.2k saved on day one).
# ₹5,000 ≈ 10% of the ₹50k book — retune upward only when capital grows.
# v1.7: this is now the FALLBACK when the book is unreadable; the live cap is
# capital-scaled via MAX_RISK_PCT_OF_BOOK below (capital_gate.get_risk_params).
MAX_RISK_PER_TRADE = 5000

# Hard ceiling on lots per position regardless of conviction upgrades.
# At ₹50k capital, 2 ATM lots ≈ the entire per-instrument allocation; the
# risk cap above is what usually converts a 2-lot request into 2×OTM+1.
# v1.7: fallback only — the live ceiling comes from DYN_MAX_LOTS_LADDER.
DYN_MAX_LOTS = 2

# ── v1.7: capital-scaled risk (Jul 9 2026) ──────────────────────────────────
# cap = max(MAX_RISK_FLOOR, book × MAX_RISK_PCT_OF_BOOK); recomputed daily.
# 10% of book ≈ full-Kelly on the June live cohort (33% WR, avg win ₹3.1k vs
# avg loss ₹1.0k → f* ≈ 0.11). Scales up with growth, down in drawdown.
MAX_RISK_PCT_OF_BOOK = 0.10
MAX_RISK_FLOOR       = 2500          # cap never below this (keeps 1 OTM lot tradeable in drawdown)
# Lot ceiling unlocks with book growth: [(book ≥ threshold, max lots), ...]
DYN_MAX_LOTS_LADDER  = [(0, 2), (75_000, 3), (100_000, 4)]

# ─── Regime Detection ─────────────────────────────────────────────────────────
# Classifies last N trading days as TRENDING/CHOPPY/MIXED via ADX at 11:00.
# CHOPPY mode: cap lots to 1, suppress REV.
REGIME_DETECTION_ENABLED    = True
REGIME_LOOKBACK_DAYS        = 3      # trading days to inspect
REGIME_CHOPPY_ADX_MAX       = 25.0   # ADX@11:00 below this = choppy day
REGIME_TRENDING_ADX_MIN     = 28.0   # ADX@11:00 at/above this = trending day
REGIME_CHOPPY_DAYS_NEEDED   = 2      # N choppy days in window → CHOPPY mode
REGIME_CHOPPY_LOTS_CAP      = 1      # max lots in CHOPPY mode
REGIME_CHOPPY_REV_SKIP      = True   # suppress REV entirely in CHOPPY mode
REGIME_CHOPPY_LATE_ORB_SKIP = True   # block late-window ORB in CHOPPY (theta trap on INSIDE days)
REGIME_TRENDING_REV_SKIP    = True   # block REV in TRENDING regime (waning-ADX ≠ reversal in trend)

# ─── Rolling Signal Quality Gate ──────────────────────────────────────────────
# Tracks last N live trades. WR + combined loss both must breach thresholds
# before reducing exposure (REDUCED: 1 lot, no REV). Resets on 2 consec wins.
QUALITY_GATE_ENABLED        = True
QUALITY_GATE_LOOKBACK       = 5      # rolling window (live trades)
QUALITY_GATE_WR_MIN         = 0.40   # WR below this triggers check
QUALITY_GATE_LOSS_THRESHOLD = 5000.0 # combined P&L must also be < -₹5,000
QUALITY_GATE_RESET_WINS     = 2      # consecutive wins needed to exit REDUCED

# ─── Path G — OI Breakout Scout ──────────────────────────────────────────────
# Fires when price breaks through a MAJOR/WALL OI level without EMA cross.
# Window: 10:00–13:30 (earlier than vX — catches pre-EMA breakouts).
PATH_G_CAPITAL    = 10_000   # ₹10k per instrument (separate pool)
PATH_G_ADX_MIN    = 28       # slightly lower than vX (breakout itself confirms momentum)
PATH_G_STOP       = 0.50     # same stop as vX (updated Apr 2026)
PATH_G_TARGET     = 0.80     # same target as vX (updated Apr 2026)
PATH_G_TRAIL_ACT  = 0.60     # trailing activation (same as vX, updated Apr 2026)
PATH_G_TRAIL_DIST = 0.20     # trailing distance (same as vX)
PATH_G_MAX_TRADES = 2        # max Path G trades per instrument per day
PATH_F_ENTRY_END   = '13:00' # OTM options need runway: cap Path F entries at 13:00
                              # (90–180 min to force-close at 14:30, vs original shared vX end)

# ─── MaxPain Trap — Variant A: Opening MaxPain Displacement ──────────────────
# On expiry days (DTE ≤ 2), when spot opens ≥ 0.5% above/below MaxPain,
# writers are offside and will defend aggressively to pull price back.
# Paper-only until validated over 30+ live sessions.
MP_TRAP_ENABLED         = True    # master switch — enables evaluate_bar calls
# Per-instrument gap thresholds (how far spot must be from MaxPain to fire).
# BNF uses a tighter threshold: weekly expiry creates more frequent but smaller
# opening displacements vs NIFTY's monthly expiry where displacements are rarer
# but larger. After 30+ sessions the weekly_analyzer will suggest adjustments.
MP_TRAP_GAP_PCT = {
    'NIFTY'    : 0.005,   # 0.50% — monthly expiry, less frequent, larger moves
    'BANKNIFTY': 0.0035,  # 0.35% — weekly expiry, typical opening gap 0.2–0.4%
    'SENSEX'   : 0.005,   # 0.50% — monthly expiry (BSE: no MaxPain data anyway)
}
# Per-instrument PCR gates. BNF's PCR range differs slightly from NIFTY.
MP_TRAP_PCR_PUT_CONFIRM = {
    'NIFTY'    : 0.85,
    'BANKNIFTY': 0.87,    # slightly looser — BNF PCR sits in tighter 0.70–1.10 band
    'SENSEX'   : 0.85,
}
MP_TRAP_PCR_CALL_CONFIRM = {
    'NIFTY'    : 1.15,
    'BANKNIFTY': 1.13,
    'SENSEX'   : 1.15,
}
MP_TRAP_DTE_MAX         = 2       # only fire when DTE ≤ 2
MP_TRAP_STOP_PCT        = 0.25    # -25% on entry premium → stop
MP_TRAP_PAPER_CAPITAL   = 10_000  # ₹10k paper capital per instrument
MP_TRAP_DAILY_LOSS_LIMIT= 3_000   # halt if daily paper loss exceeds this
MP_TRAP_TARGET_SPEND    = 3_000   # target ₹3k deployment per trade

# ─── Path B: Morning Range Breakout (replaces EMA 9/21 crossover) ────────────
# The morning range (09:15–10:55) captures the first 1h45m of price action.
# Breakout above MR_high → CALL; below MR_low → PUT.
#
# 3-layer range definition:
#   1. OHLC base     : max(High) / min(Low) of all 09:15–10:55 bars
#   2. OI wall snap  : if a MAJOR/WALL OI level is within PATH_B_OI_SNAP_DIST of
#                      the OHLC edge, snap to that level — it IS the true market-
#                      defined resistance/support. More reliable than raw OHLC.
#   3. PCR gate      : suppress entries against strong OI bias:
#                      PCR > PATH_B_PCR_BULL_GATE → heavy put writing → suppress PUT
#                      PCR < PATH_B_PCR_BEAR_GATE → heavy call writing → suppress CALL
#                      (SENSEX excluded — BSE OI not available via NSE module)
#
# MaxPain guard: if price is within 0.5% of MaxPain, gravity pin risk is high
# and a genuine breakout away from MaxPain is unlikely — suppress entry.
# HTF alignment: 15m SuperTrend must agree with breakout direction (BULL for CALL,
# BEAR for PUT). Prevents trading against the dominant timeframe trend.
#
# Set PATH_B_ENABLED = False to revert to EMA 9/21 fresh crossover.
PATH_B_ENABLED         = True        # True = EMA crossover archived (don't revert)
PATH_B_LIVE            = True        # ENABLED Jul 8 2026 (v1.6). Was False on a 37.5% WR /
                                     # -₹249k backtest — but that was BS-premium-priced
                                     # (Jun 12 lesson: BS sims distort materially — REV showed
                                     # -₹4.9k on BS vs +₹66k on real premiums) and predates the
                                     # full live gate stack (UNIFIED, OI-BIAS, risk gate, regime
                                     # caps) that Path B signals now pass through. Live evidence
                                     # for the gap it fills: Jul 7 BNF 350-pt afternoon PUT drop
                                     # and Jul 8 waterfall (NF 24,20x→23,901 from 13:45) both
                                     # missed — no live path could fire 11:00-14:00.
                                     # Revert: git tag v1.5-live-calibration.
PATH_B_RANGE_END       = '10:55'     # morning range = 09:15–10:55 (all bars before entry window)
PATH_B_ADX_MIN         = 25          # uniform for CALL and PUT (no 2-DTE asymmetry at 12 DTE)
PATH_B_BUFFER          = 0.0008      # 0.08% buffer above MR_high / below MR_low
                                     # NIFTY ≈ 19–20 pts | BANKNIFTY ≈ 40 pts | SENSEX ≈ 63 pts
PATH_B_MAX_BREAK_AGE_BARS = 3        # break freshness (Jul 13, first live fire): the ADX
                                     # filter can lag a drift-led breakout by 30-60 min —
                                     # SENSEX broke MR_high ~12:15 at ADX 10.6, ADX crossed
                                     # 25 only at 12:55 with price +0.39% extended = top tick
                                     # (-25.8%). Entry must be within 3 bars (15 min) of the
                                     # boundary cross; momentum-led breaks (ADX already high,
                                     # e.g. Jul 8 waterfall) pass, drift-then-ADX-lag chases
                                     # are skipped. 0 disables.
PATH_B_OI_SNAP_DIST    = 0.003       # snap range edge to OI MAJOR/WALL if within 0.3%
PATH_B_PCR_BULL_GATE   = 1.3         # PCR > 1.3 → heavy put writing → suppress PUT breakout
PATH_B_PCR_BEAR_GATE   = 0.75        # PCR < 0.75 → heavy call writing → suppress CALL breakout
PATH_B_MAX_PAIN_BUFFER = 0.005       # 0.5% from MaxPain → gravity pin risk → suppress entry
PATH_B_HTF_REQUIRED    = True        # 15m SuperTrend must agree with breakout direction
PATH_B_MAX_TRADES      = 1           # once per day per instrument (same discipline as Path E)

# ─── OI Direction Bias Engine (Apr 2026) ─────────────────────────────────────
# Combines live PCR + intraday PCR drift + live MaxPain gravity + ATM IV skew
# to produce a direction score for each ORB signal.  Works alongside the EOD
# OI zone gate (which handles wall proximity / range type).
#
# Score components (each ±1, total range −4 to +4):
#   PCR       : pcr < CALL_MAX → call-buyer dominant → +1 CALL / −1 PUT
#               pcr > PUT_MIN  → put-buyer dominant  → +1 PUT  / −1 CALL
#   PCR drift : 30-min trend in pcr → rising PUT pressure / falling CALL pressure
#   MaxPain   : gravity direction vs spot (only active when DAYS_TO_EXPIRY ≤ 2)
#   IV skew   : put_iv − call_iv at ATM; positive = fear premium (favours PUT)
#
# Bias output:
#   CONFIRM  (score ≥ +2) — OI context strongly supports the signal direction
#   NEUTRAL  (−1 to +1)   — mixed or insufficient data → allowed on most days
#   REJECT   (score ≤ −2) — OI context clearly contradicts → hard block if REJECT=True
#
# Per-day gate:
#   oi_confirm_required=True in PATH_A_DAY_CONFIG → NEUTRAL also blocks (Tue, Wed)
#   Only CONFIRM entries execute on those days — tighter filter for volatile days.
#
# Calibration note: PCR thresholds are starting values.  Review after 30+ live
# sessions.  If OI_DIRECTION_BIAS_REJECT is blocking too many good setups, raise
# PCR thresholds; if CALL bias is wrong after BNF gap-ups, keep thresholds tight.
OI_DIRECTION_BIAS_ENABLED   = True   # master switch for the live OI bias engine
OI_DIRECTION_BIAS_REJECT    = True   # hard-block entry when bias score ≤ −2 (REJECT)
OI_PCR_CALL_CONFIRM_MAX     = 0.90   # PCR below this  → CALL-buyer dominant  (+1 CALL)
OI_PCR_PUT_CONFIRM_MIN      = 1.10   # PCR above this  → PUT-buyer dominant   (+1 PUT)
OI_MAXPAIN_GRAVITY_PCT      = 0.008  # 0.8% from MaxPain triggers gravity bias (DTE≤2 only)
OI_IV_SKEW_THRESHOLD        = 2.0   # |put_iv − call_iv| ≥ this % triggers fear/greed bias
OI_PCR_DRIFT_LOOKBACK_MINS  = 30    # window for PCR drift computation from snapshot buffer
OI_PCR_DRIFT_THRESHOLD      = 0.05  # |drift| ≥ this = directional PCR signal

# ─── TMS: Trend Momentum Score (active primary main-session signal) ───────────
# Replaces the EMA 9/21 fresh-crossover gate (archived Apr 2026 — cross fires
# before the 11:00 entry window on morning-momentum days, is always stale by
# the time we can trade).
#
# Direction: EMA_fast > EMA_slow → CALL  |  EMA_fast < EMA_slow → PUT
#   (position only — no freshness gate)
#
# Score components (max = 7):
#   ADX ≥ TMS_ADX_HIGH                    → +2
#   ADX TMS_ADX_LOW ≤ ADX < TMS_ADX_HIGH  → +1
#   ADX rising vs TMS_SLOPE_BARS bars ago  → +1
#   DI dominant spread ≥ TMS_DI_HIGH      → +2
#   DI dominant spread ≥ TMS_DI_LOW       → +1
#   EMA spread widening vs N bars ago      → +1
#   5m SuperTrend (ST_5m) aligned          → +1
#
# Enter when score ≥ TMS_THRESHOLD (default 5) AND per-instrument ADX floor AND VWAP.
TMS_ENABLED        = False   # DISABLED Apr 2026: only 3 live trades in 2 days, 2 losses.
                             # Infrastructure bugs (stale feed, wrong lot size) confound assessment.
                             # Re-enable after 2-lot bug verified clean + 30 live ORB days.
TMS_THRESHOLD      = 5       # min score to fire (out of 7)
TMS_ADX_HIGH       = 35      # ADX >= 35 → +2 (strong trend)
TMS_ADX_LOW        = 25      # ADX 25-34 → +1 (moderate trend)
TMS_DI_HIGH        = 15      # DI dominant spread >= 15 → +2 (clear dominance)
TMS_DI_LOW         = 8       # DI dominant spread >= 8 → +1 (mild dominance)
TMS_SLOPE_BARS     = 3       # bars to look back for ADX slope + EMA spread check

# ─── Path E: HTF Trend Continuation ──────────────────────────────────────────
# Catches slow-grind days where 15m ST is clear all day but no fresh 5m
# crossover fires (ADX builds slowly, no momentum burst — e.g. Apr 6 2026).
# Entry requires: 15m ST aligned + DI+ sustained bull/bear for N bars + ADX
# floor + VWAP side. No crossover required. At most 1 trade per day.
PATH_E_ENABLED    = False    # DISABLED Apr 2026: ORB (Path A) is the only validated live signal.
                             # Path E has not fired live; no edge confirmed. Re-enable after ORB
                             # proves out with 30+ clean live days.
PATH_C_ENABLED    = False    # CONT: EMA spread widening 3 bars + ADX≥35 — DISABLED Apr 2026.
                             # No live edge confirmed; phantom trade risk on non-ORB days.
PATH_D_ENABLED    = False    # ST_FLIP: 5m SuperTrend direction flip — DISABLED Apr 2026.
                             # No live edge confirmed; phantom trade risk on non-ORB days.
PATH_F_ENABLED    = False    # Path F: OTM reversal sim — paper-only, disabled.
                             # Needs 30+ live observation days. Re-evaluate ~Jun 6 2026.
PATH_E_START      = '12:30'  # enter only after 90 min of trend establishing itself
PATH_E_END        = '13:45'  # must leave 45 min runway before 14:30 force-close
PATH_E_ADX_MIN    = 30       # same bar as Path B CALL — grind days need confirmed trend
PATH_E_DI_BARS    = 15       # DI must be aligned for 15 bars = 75 min (very sustained)

PATH_G_ENTRY_START = '10:00' # earlier than vX to catch pre-EMA breakouts
PATH_G_ENTRY_END   = '13:30' # tightened from 14:00: ATM wall-break needs 60+ min runway

# ─── Path A — Opening Range Breakout (09:30–11:00) ────────────────────────────
# First path in the unified signal chain. Uses the Opening Range (first 3 × 5-min
# bars: 09:15, 09:20, 09:25) as the day's key support/resistance reference.
# Entry: breakout above OR_high (CALL) or below OR_low (PUT) with momentum.
#
# Monday: included but with tighter criteria (ADX≥30, OR width≤0.25%).
#   Historically net -₹46k across all combos but that was without tailored filters.
#   Conservative defaults active — backtest to validate within first 30 live days.
# Tuesday: ADX≥25, OR width≤0.30%
# Wednesday/Friday: no OR width restriction, ADX≥20
# Thursday: OR width≤0.35%, CALL suppressed (WR 30.3%, -₹8.5k), PUT only
#
# Risk: wider stop/target than main session (early volatility = larger swings).
# Exits: stop 50% | target 80% | trail from 12% gain, 10% distance (recalibrated Apr 2026)
PATH_A_ORB_BARS    = 5         # GLOBAL default — per-day overrides via PATH_A_DAY_CONFIG['or_bars']
                               # 5-bar (25-min OR: 09:15–09:35) is the overall winner:
                               #   Backtest (Jan 2025–Apr 2026, 3 instruments, close-all-at-12PM):
                               #     3-bar: 225 trades, 49.8% WR, Rs -23,557
                               #     4-bar: 190 trades, 50.5% WR, Rs  -2,707
                               #     5-bar: 167 trades, 52.1% WR, Rs +26,715  ← BEST
                               #     6-bar: 151 trades, 51.7% WR, Rs +19,561
                               # Per-day best: Mon=4-bar (87.5%WR +Rs2,313), Fri=5-bar (58.2%WR +Rs40,796)
                               # Thu=5-bar (+Rs1,209). Tue/Wed disabled (all bar counts negative).
                               # Width thresholds kept same for all bar counts (tighter OR gate is better).
PATH_A_BUFFER      = 0.0005    # 0.05% buffer above OR_high / below OR_low
PATH_A_ADX_MIN     = 20        # global floor — per-day overrides below
PATH_A_START       = '09:30'   # ORB entry window open (OR not ready until 09:45 with 6-bar)
PATH_A_END         = '11:30'   # ORB entry window close — extended from 11:00 (Apr 2026).
                               # Rationale: 6-bar OR isn't established until 09:45, so the
                               # effective window was 75 min (09:45–11:00).  Extending to
                               # 11:30 restores 105 min of window (15 min more than 3-bar).
                               # Checkpoint (PATH_A_FORCE_CLOSE) shifted to 12:00 accordingly.
PATH_A_STOP        = 0.25      # 25% stop — Challenger->Champion May 2026 (was 50% for 2-DTE)
                               # 25% = ~80pt adverse NIFTY move on ATM with delta~0.5
PATH_A_TARGET      = 0.55      # 55% backstop — spike+trail primary exits; 55% fires on exceptional days
                               # 25% stop / 55% backstop = 2.2:1 R:R. Trail from 18% is the real workhorse.
PATH_A_TRAIL_ACT   = 0.18      # trail at 18% — Challenger->Champion May 2026 (was 12%)
PATH_A_TRAIL_DIST  = 0.10      # trail distance 10% from peak (was 0.15 — tightened Apr 2026)
                               # With 80% target, 15% give-back was too generous.
                               # 10% consistent with main-session trailing distance.
PATH_A_MAX_TRADES  = 1         # once per day — ORB is the opening move
PATH_A_FORCE_CLOSE = '12:00'  # Conditional checkpoint shifted from 11:30 → 12:00 (Apr 2026).
                               # Entry window extended to 11:30 (6-bar OR) — checkpoint must
                               # be far enough after latest entry so positions have 30+ min to
                               # develop before being assessed.  12:00 still gives held trades
                               # 2.5 hours of runway to 14:30.
                               # Original 11:30 checkpoint was validated for 3-bar OR entries
                               # (09:30–11:00 entries had 30–90 min before checkpoint).
                               # Same logic applies at 12:00 for 6-bar entries (09:45–11:30).

# Per-day OR-width gate (as % of price). Wide ORs = false breakout risk.
# Thresholds below are INTENTIONALLY kept the same as 3-bar (0.25%/0.30%/0.35%).
# 6-bar OR is always >= as wide as 3-bar (H-L range is non-decreasing with more bars).
# Keeping the SAME percentage threshold automatically blocks MORE 6-bar days than before —
# only the calmest openings get through, yielding higher WR and fewer noise trades.
#
# Backtest evidence (Jan 2025–Apr 2026, 3 instruments, 270+ days):
#   6-bar RELAXED gates (.40/.45/.50): 339 trades, 45.4% WR, -Rs66,949 (WORST)
#   6-bar SAME gates    (.25/.30/.35): 248 trades, 48.4% WR, -Rs15,432 (BEST WR)
#   3-bar orig gates    (.25/.30/.35): 308 trades, 45.8% WR, -Rs34,806
# The tight gate is the filter — don't relax it just because the OR is wider.
PATH_A_OR_WIDTH_MAX = {
    # OR width gates removed May 2026 — over-filtered profitable days (e.g. Apr 17
    # Thu: width 0.58-0.66% blocked a strong CALL trend day).
    # ADX floor + EMA + VWAP are sufficient noise filters; let the learner tune.
    'Mon': None,    # no restriction
    'Tue': None,    # no restriction
    'Wed': None,    # no restriction
    'Thu': None,    # no restriction — Apr 17 showed 0.66% wide open still trended strongly
    'Fri': None,    # no restriction — Fri is the best ORB day (53-56% WR)
}

# CALL suppression by day
PATH_A_NO_CALL_DAYS = set()      # No static CALL suppression — removed May 2026.
                                 # Apr 17 Thu was a strong CALL day; static block over-fits
                                 # backtest average. OI bias + ADX gate handle direction.
                                 # PATH_A_DAY_CONFIG per-day no_call=False supersedes anyway.

# ── Per-day ORB configuration (Apr 2026) ─────────────────────────────────────
# Unified dict for ALL per-day ORB parameters. Supersedes PATH_A_DAY_ADX_MIN,
# PATH_A_OR_WIDTH_MAX, and PATH_A_NO_CALL_DAYS for the options_bot live engine.
# The helper _get_day_cfg(day_abbr) in options_bot.py reads this dict, falling
# back to the legacy per-day dicts / global PATH_A_* defaults for any key that
# is not present — so missing keys are always safe.
#
# Parameters:
#   enabled      bool   — if False the bot skips ORB entirely for this day
#   adx_min      float  — per-day ADX floor (matches PATH_A_DAY_ADX_MIN)
#   or_width_max float  — max H-to-L OR width as fraction of price; None = no limit
#   no_call      bool   — suppress CALL signals this day
#   no_put       bool   — suppress PUT signals this day
#   stop         float  — stop-loss fraction (e.g. 0.50 = 50%)
#   target       float  — target fraction (e.g. 0.80 = 80%)
#   trail_act    float  — trailing-stop activation fraction
#   trail_dist   float  — trailing-stop distance fraction
#   checkpoint   str    — HH:MM time for the ORB conditional force-close
#
# Initial calibration (backtest Jan 2025–Apr 2026, 270+ days, 3 instruments):
#   Friday  : 129–136 trades, 54–56% WR, +₹26–32k — the ORB money-maker
#   Monday  : 3–12 trades with tight gate, borderline (depends heavily on filter)
#   Tuesday : 15–39 trades, 27–38% WR — CALL suppressed (consistent with main session)
#   Wednesday: 94 trades (no gate) at 37–43% WR, -₹39k — largest drag; gate added
#   Thursday : PUT-only, 7–14 trades, few but contained
PATH_A_DAY_CONFIG = {
    # ── Monday ────────────────────────────────────────────────────────────────
    # Best quality ORB day when the gate is tight enough.
    # 4-bar backtest (Jan 2025–Apr 2026, 3 instruments): 8 trades, 87.5% WR, +₹2,313.
    # The 09:30 institutional bar is included in the OR — eliminates noise-only
    # days before real directional flow has committed.
    # ADX=30 gate + 0.25% width together filter ~90% of chaotic Mon opens.
    'Mon': {
        'enabled'      : True,
        'or_bars'      : 4,        # 4-bar Mon: 87.5% WR (small sample) — captures institutional bar
        'adx_min'      : 25,       # lowered from 30 → learner tunes from here
        'or_width_max' : None,     # no restriction — width gate removed May 2026
        'no_call'      : False,
        'no_put'       : False,
        'stop'         : 0.25,     # Champion May 2026 (was 0.50)
        'target'       : 0.55,     # Champion May 2026 (was 0.80); IV-scaled at entry overrides via pos['target_pct']
        'trail_act'    : 0.18,     # Champion May 2026 (was 0.12)
        'trail_dist'   : 0.10,
        'checkpoint'   : '12:00',
    },
    # ── Tuesday ───────────────────────────────────────────────────────────────
    # OI-gated ORB (re-enabled Apr 2026).
    # Pure ORB fails on Tuesday (23–39% WR across all configs).  Root cause: gap
    # opens frequently reverse intraday; the OR gets established in the wrong
    # direction (bullish gap → sell-off, or vice-versa).
    #
    # New approach: keep ORB structure but REQUIRE OI CONFIRM before firing.
    # oi_confirm_required=True → NEUTRAL OI also blocks (only CONFIRM goes through).
    # OI CONFIRM = score ≥ +2 from: live PCR + 30-min PCR drift + MaxPain + IV skew.
    # When the gap is genuine (PCR confirms, drift aligns, MaxPain gravity supports),
    # the ORB is valid in either direction.  When OI is ambiguous → skip.
    #
    # Higher ADX floor (28 vs 25) adds extra noise filter for this volatile day.
    # Monitor: after 20+ live Tue OI-CONFIRM trades, compare WR vs OI-NEUTRAL.
    # If OI-CONFIRM Tue WR > 50%: keep.  If still < 45%: raise ADX to 32 or disable.
    'Tue': {
        'enabled'            : True,
        'or_bars'            : 5,
        'adx_min'            : 25,     # lowered from 28; learner will tune
        'or_width_max'       : None,   # width gate removed May 2026
        'no_call'            : False,
        'no_put'             : False,
        'oi_confirm_required': False,  # OI CONFIRM gate removed — OI hard-REJECT (score≤-2)
                                       # still blocks; NEUTRAL is now allowed to trade
        'stop'               : 0.25,   # Champion May 2026 (was 0.50)
        'target'             : 0.55,   # Champion May 2026 (was 0.80)
        'trail_act'          : 0.18,   # Champion May 2026 (was 0.12)
        'trail_dist'         : 0.10,
        'checkpoint'         : '12:00',
    },
    # ── Wednesday ─────────────────────────────────────────────────────────────
    # OI-gated early ORB, bidirectional (Apr 2026 revision from PUT-only).
    # BNF expires Wednesday — gap-up opens are common.  Static PUT-only hardcodes
    # the average outcome.  Better: let live OI determine direction this session.
    #
    # A genuine gap-up CALL setup (BNF rallying on expiry momentum):
    #   PCR < 0.90 (call buying), price below MaxPain, IV skew low → OI CONFIRMS CALL
    # A gap-fade PUT setup (the common reversal):
    #   PCR > 1.10 (put buying), price above MaxPain, IV skew > 2% → OI CONFIRMS PUT
    #
    # Backtest baseline (orb_expiry_backtest.py, 3-bar OR, 09:30–10:55):
    #   Wed PUT-only combined: 32 trades, 57% WR, +₹6,601
    #   Wed CALL combined:     56 trades, 36-50% WR, -₹24,721 (unfiltered)
    # The CALL drag includes gap-fade CALL signals that OI (PCR + MaxPain)
    # would have rejected.  oi_confirm_required=True means only clean setups fire.
    #
    # No OR width gate — BNF expiry creates naturally wider open ranges.
    # Hard cutoff 10:55: early tight model; exits before mid-morning chop.
    'Wed': {
        'enabled'            : True,
        'or_bars'            : 3,      # 3-bar OR (09:15–09:25) — BNF expiry day, early move
        'adx_min'            : 25,
        'or_width_max'       : None,   # no restriction
        'no_call'            : False,  # bidirectional
        'no_put'             : False,
        'oi_confirm_required': False,  # OI CONFIRM gate removed May 2026 — NEUTRAL allowed
        'entry_end'          : '10:55',
        'stop'               : 0.25,   # Champion May 2026 (was 0.50)
        'target'             : 0.55,   # Champion May 2026 (was 0.80)
        'trail_act'          : 0.18,   # Champion May 2026 (was 0.12)
        'trail_dist'         : 0.10,
        'checkpoint'         : '10:55',
    },
    # ── Thursday ──────────────────────────────────────────────────────────────
    # PUT-only; few trades but contained risk.
    # 4-bar backtest: 12 trades, 33.3% WR, -₹1,681. Slightly negative but low
    # absolute loss — keeping it as it gives PUT diversification on an expiry-
    # adjacent day (Wed is BNF expiry; Thu morning options have gamma left).
    # CALL suppressed: Thu CALL WR 30.3%, -₹8,513 across full backtest history.
    # Monitor: if 20+ live Thu ORB observations remain negative, disable.
    'Thu': {
        'enabled'      : True,
        'or_bars'      : 5,
        'adx_min'      : 25,
        'or_width_max' : None,     # width gate removed May 2026 (Apr 17 falsely blocked)
        'no_call'      : False,    # bidirectional — CALL block removed May 2026
        'no_put'       : False,    # Apr 17 was a strong CALL day; static block over-fits avg
        'stop'         : 0.25,     # Champion May 2026 (was 0.50)
        'target'       : 0.55,     # Champion May 2026 (was 0.80)
        'trail_act'    : 0.18,     # Champion May 2026 (was 0.12)
        'trail_dist'   : 0.10,
        'checkpoint'   : '12:00',
    },
    # ── Friday ────────────────────────────────────────────────────────────────
    # The ORB money-maker. Backtest (3 instruments, 270 days):
    #   3-bar: 136 trades, 53.7% WR, +₹26,682
    #   4-bar: 132 trades, 57.6% WR, +₹14,809
    #   6-bar: 129 trades, 55.8% WR, +₹32,180
    # No width gate needed — Fri ORB works across wide ranges.
    # ADX=20 (global floor) is sufficient; relaxing further risks noise.
    # Future tuning: try extending checkpoint to '12:30' for Fri specifically
    # after 30+ live observations show held positions extend profitably.
    'Fri': {
        'enabled'      : True,
        'or_bars'      : 5,        # 5-bar Fri: 58.2% WR, +Rs40,796 (122 trades) — THE MONEY DAY
                                   # Fri 5-bar vs next best 6-bar (+Rs34,309) shows 5-bar optimal.
                                   # 6-bar misses the early 09:40-09:45 breakout bars.
        'adx_min'      : 20,
        'or_width_max' : None,     # no restriction
        'no_call'      : False,
        'no_put'       : False,
        'stop'         : 0.25,     # Champion May 2026 (was 0.50)
        'target'       : 0.55,     # Champion May 2026 (was 0.80)
        'trail_act'    : 0.18,     # Champion May 2026 (was 0.12)
        'trail_dist'   : 0.10,
        'checkpoint'   : '12:00',
    },
}

# Conditional 11:30 checkpoint (replaces hard force-close — Apr 2026)
# At PATH_A_FORCE_CLOSE, evaluate each ORB position:
#   If profitable ≥ PATH_A_MIN_PROFIT_TO_HOLD AND ADX ≥ PATH_A_HOLD_ADX_MIN
#   AND EMA still aligned → convert to main-session trailing exit (hold to 14:30)
#   Else → hard close (capital protection on noise days)
# Rationale: backtest showed 11:30 force-close beats holding in 73–84% of days.
# But on the ~20% of true trend days, the ORB move often continues — the hold
# condition (profit≥15% + ADX≥30 + EMA aligned) isolates those days cleanly.
PATH_A_CONDITIONAL_EXIT    = True   # enable conditional checkpoint (False = always hard-close)
PATH_A_MIN_PROFIT_TO_HOLD  = 0.20   # must be ≥20% profitable to hold past checkpoint
                               # Raised 0.15→0.20 May 2026: trail arms at 18% (PATH_A_TRAIL_ACT).
                               # Holding below 18% = no trail protection in afternoon session
                               # → positions decayed to EOD loss (May 4, May 7, May 12 pattern).
                               # 20% ensures trail is already armed before we commit to holding.
PATH_A_HOLD_ADX_MIN        = 30     # ADX must still be ≥30 at 11:30 to justify holding
PATH_A_LOSS_STOP_AT_CHECKPOINT = True
                               # 12PM hard loss stop (Apr 2026).
                               # At the per-day checkpoint time (12:00 default):
                               #   If position P&L < 0 → UNCONDITIONAL hard close.
                               #     No conditional hold evaluation — losses cut immediately.
                               #   If position P&L ≥ 0 → apply conditional hold logic above.
                               # Rationale: backtest (3/4/5/6-bar, all instruments) shows that
                               # profitable positions held to 14:30 without ADX/EMA conditions
                               # lose more via theta than they gain (e.g. Fri 5-bar: close-at-12PM
                               # +Rs40,796 vs hold-profits-to-14:30 -Rs15,205).  So losses are
                               # cut hard at 12PM; profitable-but-weak positions also close.
                               # Only ADX≥30 + EMA-aligned + profit≥15% positions are held to 14:30.

# Checkpoint exceptional profit close — lock in gains on gap-and-go days (May 2026)
# On days where price gaps and runs hard in the first hour, the option can be
# up 50–150% by the checkpoint time.  Trailing post-checkpoint often gives it back
# in afternoon consolidation (Apr 29 sim: trail=₹8.6k vs checkpoint close=₹11.6k).
# When gain >= this % at checkpoint → close IMMEDIATELY; skip hold evaluation.
PATH_A_EXCEPTIONAL_PROFIT_CLOSE = 0.50   # ≥50% gain at checkpoint → lock in, no trail

# Re-entry after PATH-A stop-loss (Apr 2026)
# After a stop-loss exit, allow one re-entry if the OR level re-tests and holds
# with higher conviction (ADX≥35) before 13:00. Captures "second break" days
# where the first break was a false start but the underlying trend re-asserts.
PATH_A_REENTRY_ENABLED     = True    # allow one re-entry after PATH-A stop-loss
PATH_A_REENTRY_ADX_MIN     = 35     # higher bar than initial entry (day min is 20-30)
PATH_A_REENTRY_CUTOFF      = '13:00' # no re-entry after 13:00 (need 90 min runway to EOD)

# ── Rapid-spike exit (Apr 2026) ───────────────────────────────────────────────
# Rationale: a sharp spike in option premium in a short window = the market is
# pricing in the expected index move RIGHT NOW (forward expectation).  Once the
# move begins to be realised, premium often stalls or decays (gamma / theta
# balance shifts).  Exiting during the spike captures the implied-peak value
# rather than waiting for the realised peak that may never arrive.
#
# Mechanics: check_exits() tracks the last RAPID_SPIKE_BARS option LTPs.
#   If the gain over that rolling window >= RAPID_SPIKE_PCT AND the position is
#   already profitable >= RAPID_SPIKE_MIN_GAIN, exit immediately.
# Priority: fires AFTER stop-loss / force-close checks but BEFORE target / trail.
#
# Default values are conservative starting-points for live observation.
# Run second_half_sim.py after 30+ live days to calibrate these.
RAPID_SPIKE_ENABLED  = True   # set False to disable without touching other code
RAPID_SPIKE_BARS     = 2      # window = last N quick-poll intervals
                            # EXIT_POLL_INTERVAL=20s → window = 2×20 = 40 seconds
                            # (was 3 bars at 60s = 3 min; quick polls make it tighter)
RAPID_SPIKE_MIN_GAIN = 0.10   # must already be >=10% up from entry (suppresses noise)

# Tier-based spike threshold — the closer to the P&L ceiling, the smaller the
# spike needed to trigger exit.  Observed empirically: a single lot tops out at
# Rs12-15k in most cases, rarely Rs17k+.  A trade at Rs9k unrealized has very
# limited upside vs one at Rs2.5k.
#
# Each tuple: (min_pnl_per_lot_Rs, window_gain_pct_needed_to_exit)
# Evaluated top-down — first matching tier wins.
# Per-lot P&L = (current_opt - entry_price) x base_lot_size (normalised for 2-lot entries).
#
#   OK          Rs 0 – 2,500 /lot : 28% spike needed — still has meaningful room
#   Good        Rs 2,500 – 5,000  : 20% — moderate upside remaining
#   Very good   Rs 5,000 – 8,000  : 12% — limited upside, exit on a decent surge
#   Excellent   Rs 8,000 – 12,000 : 6%  — near ceiling, any clear momentum spike
#   Exceptional Rs 12,000+        : 3%  — beyond normal ceiling, exit on any uptick
RAPID_SPIKE_TIERS = [
    # (min_pnl_per_lot_Rs, window_gain_pct_to_trigger_exit)
    # float('inf') = disabled — position too early, let it run.
    (12_000, 0.05),            # Exceptional  Rs12k+ /lot : 5%  — beyond ceiling, any uptick
    ( 8_000, 0.10),            # Excellent    Rs8-12k/lot : 10% — near ceiling, clear surge
    ( 5_000, 0.20),            # Very good    Rs5-8k /lot : 20% — meaningful but not hair-trigger
    ( 2_500, 0.35),            # Good         Rs2.5-5k    : 35% — truly explosive spike only
    (     0, float('inf')),    # OK           Rs0-2.5k    : DISABLED — too early, more room to run
]
RAPID_SPIKE_PCT = 0.20   # legacy fallback (used if RAPID_SPIKE_TIERS not defined)

# Late ORB extension (11:30–13:00) — RE-ENABLED May 8 2026
# Original hypothesis (Apr 2026): REJECTED by data.
#   9 entries, 11% WR, -Rs10,520. All EOD exits. No momentum continuation.
#   Diagnosis (May 8 2026): those 9 entries were weak setups (low ADX, thin DI spread,
#   no ST5 confirmation). Today's BNF was blocked at 11:30 despite ADX=35, DI spread=15,
#   ST5=BEAR, 3-hour OR consolidation → 292pt directional move to 55,212.
# Re-enabled with 3 hard guards that the original 9 trades would have failed:
#   1. ADX ≥ PATH_A_LATE_ADX_MIN (32) — confirmed trend strength
#   2. |DI+ − DI-| ≥ PATH_A_LATE_DI_SPREAD (12) — clear directional dominance
#   3. ST5 confirms direction (PATH_A_LATE_ST5_REQUIRED=True) — no counter-trend
#   4. Always 1 lot (PATH_A_LATE_LOTS=1) — conservative sizing, short runway
# Unified scorer still gates quality at 55/100. Bad trades die there.
PATH_A_LATE_END           = '13:00'  # outer ORB scan window: 09:30 ≤ t < 13:00
                                     # 11:30-13:00 = late window (requires all 4 guards)
                                     # 13:00+ = no new entries (force-close runway)
PATH_A_LATE_ADX_MIN       = 32       # raised from 25 floor — late entries need confirmed trend
PATH_A_LATE_MIN_STRENGTH  = 1        # lowered from 2 (DI+ST gate compensates for strength req)
PATH_A_LATE_DI_SPREAD     = 12       # NEW: min |DI+ − DI-| — directional clarity gate
PATH_A_LATE_ST5_REQUIRED  = True     # ST5 must confirm direction — blocks counter-trend late
PATH_A_LATE_HTF_REQUIRED  = True     # 15m SuperTrend must align — hard gate, not just SCORER -20pts
PATH_A_LATE_LOTS          = 1        # late entries always 1 lot regardless of strength score

# Per-day ADX minimum
PATH_A_DAY_ADX_MIN = {
    'Mon': 25,   # harmonised May 2026 — learner tunes from uniform 25 baseline
    'Tue': 25,
    'Wed': 25,   # kept 25 (BNF expiry day; 3-bar OR tight by nature)
    'Thu': 25,   # PUT-focused day; ADX≥25 confirms direction
    'Fri': 20,   # no width gate → global floor sufficient
}

# Backward-compat aliases — early_bot.py reads these; keep until archived
EARLY_SESSION_ENABLED     = True
EARLY_SESSION_ORB_BARS    = PATH_A_ORB_BARS
EARLY_SESSION_ORB_BUFFER  = PATH_A_BUFFER
EARLY_SESSION_ADX_MIN     = PATH_A_ADX_MIN
EARLY_SESSION_ENTRY_START = PATH_A_START
EARLY_SESSION_ENTRY_END   = PATH_A_END
EARLY_SESSION_FORCE_CLOSE = '10:55'
EARLY_SESSION_HARD_CLOSE  = '14:25'
EARLY_SESSION_STOP        = PATH_A_STOP
EARLY_SESSION_TARGET      = PATH_A_TARGET
EARLY_SESSION_TRAIL_ACT   = PATH_A_TRAIL_ACT
EARLY_SESSION_TRAIL_DIST  = PATH_A_TRAIL_DIST
EARLY_SESSION_MAX_TRADES  = PATH_A_MAX_TRADES
EARLY_SESSION_CAPITAL     = 16667      # deprecated — unified bot uses ₹26k
EARLY_SESSION_DAYS_TO_EXP = 2
EARLY_SESSION_TRADE_DAYS  = {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}
EARLY_SESSION_OR_WIDTH_MAX = PATH_A_OR_WIDTH_MAX
EARLY_SESSION_NO_CALL_DAYS = PATH_A_NO_CALL_DAYS
EARLY_SESSION_DAY_ADX_MIN  = PATH_A_DAY_ADX_MIN

# Backward-compat aliases (used by legacy bot.py references)
NIFTY_LOT_SIZE     = 65            # confirmed Mar 2026
BANKNIFTY_LOT_SIZE = 30            # confirmed Mar 2026
SENSEX_LOT_SIZE    = 20            # confirmed Mar 2026

# ─── Signal Quality Filters ───────────────────────────────────────────────────
USE_VOLUME_FILTER      = True       # Auto-disabled when volume data unavailable
VOLUME_MULTIPLIER      = 1.2
EMA_CROSSOVER_LOOKBACK = 3          # Reduced from 5 — must be a very fresh cross
MIN_OPTION_PRICE       = 15.0       # Skip if premium < ₹15 (wide spread risk)

# VWAP directional filter (intraday): CALL only above VWAP, PUT only below
USE_VWAP_FILTER = True

# Liquidity Sweep parameters
SWEEP_LOOKBACK     = 10    # bars to look back for swing high/low (10 bars = 50 min)
SWEEP_VOLUME_MULT  = 1.5   # sweep bar must have volume > N× the 20-bar MA

# RSI confirmation — disabled: EMA 9/21 + VWAP + ADX together are sufficient
# Re-enable only if win rate still poor after tuning EMA pair
USE_RSI_FILTER  = False
RSI_BULL_MIN    = 40    # CALL: RSI must be > 40 (upward momentum confirmed)
RSI_BULL_MAX    = 65    # CALL: RSI must be < 65 (not overbought at entry)
RSI_BEAR_MIN    = 35    # PUT:  RSI must be > 35 (not oversold at entry)
RSI_BEAR_MAX    = 60    # PUT:  RSI must be < 60 (downward momentum confirmed)

# ─── Transaction Costs (NSE circulars) ────────────────────────────────────────
BROKERAGE_PER_ORDER      = 20               # ₹20 flat per order (Fyers)
STT_RATE                 = 0.0625 / 100    # 0.0625% on sell-side premium
NSE_EXCHANGE_CHARGE_RATE = 0.053  / 100    # 0.053% on premium turnover
SEBI_CHARGES_RATE        = 0.0001 / 100    # ₹10 per crore
STAMP_DUTY_RATE          = 0.003  / 100    # 0.003% on buy-side premium
GST_RATE                 = 0.18            # 18% on brokerage + exchange + SEBI

# ─── SENSEX Live Trading Threshold ───────────────────────────────────────────
# SENSEX starts as PAPER trade (live_mode=False). Enable live trading only when
# NIFTY + BANKNIFTY combined capital — starting ₹52k (₹26k each) — reaches ₹75k
# through cumulative live P&L from those two instruments.
# Required P&L gain: ₹23,000 net from NIFTY + BANKNIFTY live trades combined.
#
# To check current progress:
#   python capital_status.py                      # local
#   python /opt/trading_bot/live_bot/capital_status.py   # EC2
#
# When threshold is met:
#   1. Set INSTRUMENT_STRATEGY['SENSEX']['live_mode'] = True
#   2. Restart fno_t_bot_sensex service
SENSEX_LIVE_THRESHOLD     = 75_000   # ₹75k target combined NF+BNF capital
SENSEX_LIVE_START_CAPITAL = 56_116   # recalibrated Jul 8 2026 after JSONL dedup cleanup:
                                     # cleaned NF+BNF cumulative P&L = -6,116; actual Fyers
                                     # balance Rs50,000 (Jul 6 deposit, no trades since)
                                     # → 56,116 - 6,116 = 50,000. (Jul 6 value 52,997 was
                                     # calibrated against dup-inflated JSONL data.)
LIVE_SWITCH_DATE          = '2026-04-06'  # Date NIFTY + BANKNIFTY went live (Apr 6 2026)
                                          # capital_status.py only counts trades from this date

# ── PATH-A OTM Strike Selection (Apr 2026) ───────────────────────────────────
# On strong ORB days buy OTM instead of ATM — lower entry cost, higher % gain
# when the index genuinely runs.  ATM remains the fallback (otm_strikes=0 is
# identical to pre-OTM behaviour — no code path changes for low-ADX entries).
#
# OTM degree is set by ADX at the breakout bar:
#   ADX < PATH_A_OTM_1_ADX          →  ATM  (degree 0)
#   PATH_A_OTM_1_ADX ≤ ADX < _2_ADX →  1 strike OTM  (degree 1)
#   ADX ≥ PATH_A_OTM_2_ADX          →  2 strikes OTM  (degree 2)
#
# For NIFTY (strike_gap=50): OTM+1 = 50pts, OTM+2 = 100pts away from ATM.
# For BANKNIFTY (gap=100) : OTM+1 = 100pts, OTM+2 = 200pts.
# For SENSEX (gap=200)    : OTM+1 = 200pts, OTM+2 = 400pts.
PATH_A_OTM_ENABLED = False  # DISABLED May 2026: Challenger->Champion. ATM+28% > OTM+1+120% for 12-DTE. # True → always ATM (pre-OTM fallback)
# OTM degree is driven by distance to the next significant OI S/R level in the
# breakout direction — not by ADX.  ADX is only a minimum guard:
PATH_A_OTM_MIN_ADX = 25    # ADX must be >= this to use any OTM at all.
                            # Below 25 the trend is unconfirmed; S/R distance alone
                            # is not enough to justify OTM.
PATH_A_GAP_OTM_BOOST = True # GAP_AND_GO day + aligned ORB breakout → +1 OTM (capped at 2).
                             # Rationale: gap momentum + OR momentum in same direction =
                             # momentum stack → higher probability the move extends.
                             # Only boosts when S/R already grants OTM+1 (wall nearby
                             # returns OTM+0 and that cap is respected).
                             # Set False to disable (fallback to pure S/R distance logic).
# S/R-to-OTM mapping (evaluated in _otm_degree_from_sr()):
#   WALL < 2 strike_gaps away  → ATM   (wall will pin the move)
#   WALL 2-4 strike_gaps       → OTM+1 (room to move, wall is the target zone)
#   WALL 4+ strike_gaps        → OTM+2
#   MAJOR < 1.5 strike_gaps    → ATM   (MAJOR resistance close, respect it)
#   MAJOR 1.5-3 strike_gaps    → OTM+1
#   MAJOR 3+ strike_gaps       → OTM+2
#   No MAJOR/WALL ahead        → OTM+2 (free run — be aggressive)
#   SENSEX or no OI data       → ATM   (no OI for BSE; safe fallback)

# Exit targets by OTM degree.
# ATM (degree 0) uses PATH_A_TARGET = 0.80.
# OTM positions need room to travel from OTM → ATM → ITM before the target clips them.
# The rapid-spike tier system handles the ATM-crossing surge and fires first on
# explosive days; the target below is the steady-trend backstop.
PATH_A_OTM_1_TARGET  = 1.20   # 1-strike OTM: 120% target
                               # At Rs80 entry → Rs176 exit → Rs6,240/lot (V.Good tier).
                               # Spike/trail handle explosive moves; this is the steady backstop.
PATH_A_OTM_2_TARGET  = 1.50   # 2-strike OTM: 150% target (was 2.50 — too high)
                               # At Rs50 entry → Rs125 exit → Rs4,875/lot.
                               # 2-strike OTM needs more room than ATM but 250% was
                               # unreachable without going deep ITM — spike fires first anyway.
# Trail activation: arms earlier for OTM to protect gains on the way in
PATH_A_OTM_TRAIL_ACT = 0.10   # 10% activation (ATM uses PATH_A_TRAIL_ACT = 0.12)
# PATH_A_TRAIL_DIST (10%) is unchanged across all OTM degrees

# 11:30 conditional hold threshold by OTM degree.
# OTM positions at 11:30 may not have gone ITM yet at 15% gain.
# Higher threshold ensures the position has genuinely moved before we hold past 11:30.
PATH_A_OTM_1_MIN_PROFIT_HOLD = 0.20  # 1-strike OTM: need 20% at 11:30 to hold
PATH_A_OTM_2_MIN_PROFIT_HOLD = 0.30  # 2-strike OTM: need 30% at 11:30 to hold
# ATM threshold unchanged: PATH_A_MIN_PROFIT_TO_HOLD = 0.15

# ── Dynamic OR (fallback when original OR width exceeds per-day limit) ────────
# When the 09:15–09:25 OR is too wide (e.g. choppy Monday open), the bot scans
# post-open 5-min bars for a tight consolidation zone and uses that as the OR.
# Entry then works exactly like standard PATH-A ORB — breakout of the dynamic OR
# high/low with ADX, VWAP, gap-context and strength gates — but with elevated
# requirements and a stricter stop loss (smaller risk since market has settled).
#
# Today's scenario (Apr 27): original OR 0.549% blocked, but by 10:00 the last
# 5 bars were 24,082–24,099 (0.07% width) with ADX=34 — exactly this pattern.
PATH_A_DYNAMIC_OR_ENABLED      = True
PATH_A_DYNAMIC_OR_BARS         = 5      # 5× 5-min bars = 25 min settling window
PATH_A_DYNAMIC_OR_MAX_WIDTH    = 0.0020 # ≤0.20% H-to-L (tighter than any day gate)
PATH_A_DYNAMIC_OR_SEARCH_END   = '10:30'# stop searching after 10:30 (30 min runway to 11:00)
PATH_A_DYNAMIC_OR_ADX_MIN      = 30     # elevated floor — original OR already rejected
PATH_A_DYNAMIC_OR_MIN_STRENGTH = 2      # must be 2-lot quality (no weak entries in fallback)
PATH_A_DYNAMIC_OR_STOP         = 0.20   # 20% stop — tighter than standard 25% (May 2026)
                                         # Was 0.35 "stricter than 50%"; standard is now 25%
                                         # so 0.35 was actually looser. Dynamic OR = higher
                                         # confidence setup (45-90 min settled market, DI
                                         # confirmed) → tighter stop justified.
                                         # Still wider than A_HELD (25%) since still morning.
PATH_A_DYNAMIC_OR_DI_GATE     = True    # DI alignment gate for Dynamic OR (Apr 2026).
                                         # Prevents counter-trend entries after the day's
                                         # trend has already been established.  By the time
                                         # a Dynamic OR fires (09:45+), DI± often already
                                         # reveals the dominant direction clearly — blocking
                                         # the weaker side prevents gap-fade whipsaws.
                                         # Evidence: Apr 27 DYN-OR PUT fired with DI+=33,
                                         # DI-=14 (bullish all day) → -₹550 at 11:30 close.
PATH_A_DYNAMIC_OR_DI_MIN_SPREAD = 15   # If |DI+ − DI−| ≥ 15 pts, only the dominant
                                         # direction (higher DI) is allowed.
                                         # Below 15 pts: no DI gate (trend unclear → both OK).

# ── Gap-Reversal ADX Supplement (May 2026) ───────────────────────────────────
# Problem: on gap-fade days (gap ≥ 0.5%, price recovering), the 14-period ADX
# is biased by the initial gap-direction bars and stays suppressed (17–24) even
# as DI dominance builds strongly in the recovery direction.  The normal ADX
# floor (25–30) blocks entry on the entire recovery move — all alpha is missed.
#
# Solution: when normal ADX gate fails, check a supplementary condition:
#   1. Gap magnitude ≥ GAP_REV_MIN_GAP_PCT (confirms genuine gap context)
#   2. Price has recovered ≥ GAP_REV_RECOVERY_PCT of the gap (fade is real)
#   3. ADX ≥ reduced GAP_REV_ADX_MIN (some trend building, just not full 25)
#   4. DI-spread in recovery direction ≥ GAP_REV_DI_SPREAD_MIN (directional conviction)
# Entry window also extended to GAP_REV_ENTRY_EXT for the recovery direction
# (gap-fades typically complete 2–4 hours after open, not at ORB time).
#
# Evidence (May 13 2026): NIFTY gap-dn, PUT caught at 09:35 (+44%), but
# 247-pt CALL recovery (23,263→23,510) fully missed — ADX stuck at 17–24
# while DI+=28–31 dominated for 5+ hours.
GAP_REV_ENABLED        = True
GAP_REV_MIN_GAP_PCT    = 0.005   # ≥0.5% open gap to qualify (smaller = noise)
GAP_REV_RECOVERY_PCT   = 0.50    # price must recover ≥ 50% of gap before entry
GAP_REV_ADX_MIN        = 18      # reduced ADX floor (normal 25–30 biased by gap bars)
GAP_REV_DI_SPREAD_MIN  = 12      # DI+ − DI− in recovery direction (trend conviction)
GAP_REV_ENTRY_EXT      = '12:30' # extended entry window for gap-reversal direction

# ── Capital-gated live trading (Apr 2026) ─────────────────────────────────────
# The bot reads cumulative NF+BNF live P&L from JSONL files at startup and
# automatically switches each instrument between live and paper based on
# combined account capital.  No manual config change needed on threshold crossing.
#
# Phase 1  (Rs 0 → 50k combined)  : NIFTY LIVE | BNF paper | SENSEX paper
# Phase 2  (Rs 50k → 75k)         : NIFTY LIVE | BNF LIVE  | SENSEX paper
# Phase 3  (Rs 75k+)              : all three LIVE
#
# Rationale: concentrate on the highest-WR instrument (NIFTY 77%) first.
# Adding BNF at Rs50k and SENSEX at Rs75k matches existing manual thresholds
# while making the transitions automatic.

# -- PATH_BTR: Bull / Bear Trap Reversal --------------------------------------
# Fires when an OI-REJECTED ORB is followed by price reversing back through
# the OR boundary.  Double OI filter provides high conviction:
#   (1) OI REJECTED the original direction  (score <= -2)
#   (2) OI CONFIRMS the reversal direction  (score >= +2)
# Bull-trap: CALL blocked -> watches for PUT reversal.
# Bear-trap: PUT  blocked -> watches for CALL reversal.
# Live from day 1 — tighter ADX (22) compensated by OI double-filter.
PATH_BTR_ENABLED       = True    # activate detection + live entry
PATH_BTR_ADX_MIN       = 22      # lower than ORB 25 -- OI context compensates
PATH_BTR_OI_REQUIRED   = True    # OI MUST CONFIRM reversal (no NEUTRAL allowed)
PATH_BTR_REVERSAL_BARS = 2       # consecutive closes back through OR boundary
PATH_BTR_MIN_HOUR      = '10:30' # don't fire too early (reversal needs to develop)
PATH_BTR_MAX_HOUR      = '12:30' # no BTR after 12:30 (insufficient session runway)
PATH_BTR_LOTS          = 1       # 1 lot only -- conservative until WR proven

CAPITAL_GATE_ENABLED     = True
CAPITAL_GATE_BNF_LIVE    = 50_000   # BNF goes live when combined capital >= Rs50k
CAPITAL_GATE_SENSEX_LIVE = 75_000   # SENSEX goes live when combined >= Rs75k
# NIFTY is always live — anchor instrument, highest win-rate.

# Manual override: bypass CAPITAL_GATE_BNF_LIVE threshold (May 2026)
# Set True to allow BNF live orders even when combined capital < Rs50k.
# Used when strategy conviction warrants accepting BNF live at lower capital.
# capital_gate.py checks this flag BEFORE the threshold gate.
FORCE_BNF_LIVE    = True   # Rs~40k combined capital — bypass Rs50k threshold
FORCE_SENSEX_LIVE = True   # Go live with current capital (May 11 2026) — bypass Rs75k threshold

# Capital fallback: max fraction of instrument capital for a single option entry.
# If entry_price × lot_size > capital × this threshold → step OTM until within budget.
# Prevents allocating >80% of instrument capital to a single high-premium trade.
CAPITAL_FALLBACK_THRESHOLD = 0.80   # fallback kicks in above 80% of instrument capital

# ─── ORB Minimum Extension Gate ──────────────────────────────────────────────
# Minimum % price must have moved beyond OR high/low before the ORB signal fires.
# Prevents thin fakeout entries right at the OR boundary (e.g. May 4: ext=0.063%,
# OI wall 0.16% above, reversed immediately).
# Set to 0.0 to disable (allow any breakout including boundary touches).
# Learner can tune this per-DOW using 'or_extension' field in market_learnings.jsonl.
PATH_A_MIN_OR_EXTENSION = 0.10   # 0.10% minimum extension beyond OR — filters boundary fakes

# ─── Futures Volume Conviction Gate ──────────────────────────────────────────
# Fetches current-month index futures 5-min OHLCV alongside spot index data.
# Volume on the OR-break bar vs rolling mean of last PATH_A_FUT_VOL_LOOKBACK bars.
# High-volume ORB (ratio ≥ HIGH_RATIO) → +1 strength (broad participation, conviction break).
# Thin-volume ORB (ratio < LOW_RATIO)  → warn in log (no penalty; just flags fakeout risk).
# No hard block — volume adjusts conviction, not eligibility.
PATH_A_FUT_VOL_ENABLED    = True    # fetch futures vol and score OR breakout conviction
PATH_A_FUT_VOL_LOOKBACK   = 20      # rolling bar window (20 × 5min = 100 min)
PATH_A_FUT_VOL_HIGH_RATIO = 1.5     # ratio ≥ this → +1 strength (high-conviction ORB)
PATH_A_FUT_VOL_LOW_RATIO  = 0.70    # ratio < this → log thin-volume warning

# ─── SMA Levels (intraday S/R + trend-alignment strength scoring) ─────────────
# SMA_fast (20-bar = 100 min): short-term intraday trend reference.
# SMA_slow (50-bar = 250 min): structural ~4-hour bias used for trend-alignment scoring.
# Price on correct side of SMA_slow = trend-aligned breakout → +1 strength.
# PDH/PDL (previous day high/low): key price-memory S/R loaded from historical df.
# Proximity logging warns when an OR break heads into a SMA_slow or PDH/PDL wall.
PATH_A_SMA_ENABLED      = True    # add SMA alignment to strength scoring
PATH_A_SMA_FAST         = 20      # SMA_fast period (20 × 5min = 100 min)
PATH_A_SMA_SLOW         = 50      # SMA_slow period (50 × 5min = 250 min)
PATH_A_SMA_PROX_PCT     = 0.003   # within 0.3% of SMA_slow = potential headwind (log only)
PATH_A_PDH_PDL_ENABLED  = True    # track prev-day high/low as S/R reference
PATH_A_PDH_PDL_PROX     = 0.002   # within 0.2% of PDH (CALL) or PDL (PUT) = log wall proximity

# ─── Anticipation-first entry engine (Jul 22 2026) ───────────────────────────
# Shadow module (anticipation_scout.py): enter at a LEVEL price is holding,
# BEFORE the breakout — the "capture before it happens" thesis. Logs would-be
# entries + tracks the underlying to resolution; places NO orders. Runs live to
# gather forward evidence vs the confirmation-breakout paths on the same tape.
# Promote to a live-order path only if it beats breakouts forward.
ANTICIPATION_SHADOW_ENABLED = True

# ─── Chase gate (Jul 16 2026) ────────────────────────────────────────────────
# chase_pos = direction-normalized entry location in today's range (0=pullback
# entry, 1=bought the extreme). Backfill: chase_pos>0.7 = 39t -₹15,872.
# BUT the top-10 analysis showed chase ALONE does not separate the biggest
# wins (mean chase 0.80 wins vs 0.87 losses) — REV noon fades and morning
# trend breakouts are high-chase AND win. So the gate is narrowed to the
# afternoon non-REV exhaustion chase (07-13 SEN B -2,370, 04-24 -4,444) and
# REV is exempt (backbone; anti-chase by design).
# MODE:
#   'off'    — disabled
#   'shadow' — log '[CHASE-GATE] SHADOW would-block' but TAKE the trade
#              (gathers forward out-of-sample evidence; no P&L impact)
#   'active' — actually skip the entry
# Ships in SHADOW: the rule is in-sample only (n=39 historical, 0 forward).
# Flip to 'active' after the shadow log confirms it fires on losers forward.
CHASE_GATE_MODE         = 'shadow'
CHASE_GATE_MAX          = 0.75     # chase_pos above this = bought the extreme
CHASE_GATE_AFTER        = '12:00'  # morning breakouts have runway → exempt
CHASE_GATE_EXEMPT_PATHS = ('REV',) # REV fades are anti-chase — never gate them

# ─── PATH_REV: MaxPain Snap Reversal ─────────────────────────────────────────
# Fires after the ORB window closes when the morning trend exhausts and price
# snaps toward MaxPain.  Strongest on DTE≤2 (options-pinning effect is sharpest).
#
# Score system (max 6):
#   DI convergence  0–2   gap narrowed ≥50% from peak (+1) or DI crossed (+2)
#   IVSkew flip     0–2   drifting ≥½ threshold toward reversal (+1) or crossed (+2)
#   MaxPain prox    0–1   price within PATH_REV_MAXPAIN_PROX_PCT of MaxPain
#   ADX waning      0–1   current ADX < peak × PATH_REV_ADX_WANE_RATIO
#
# PAPER-ONLY (PATH_REV_LIVE=False) — logs [PATH-REV PAPER].  Set True after 30 trades.
PATH_REV_ENABLED            = True    # enable reversal detection (logs paper signal)
PATH_REV_LIVE               = True    # LIVE — deployed May 5 2026 after retroactive validation
PATH_REV_START              = '12:00' # earliest fire time (after ORB window)
PATH_REV_END                = '13:30' # latest fire time (need runway to 14:30 force-close)
PATH_REV_MIN_SCORE          = 3       # minimum score (0–6) to fire
PATH_REV_MIN_MORNING_ADX    = 30      # morning must have had a real trend (ADX peak ≥ this)
PATH_REV_MIN_DI_SPREAD_PEAK = 12      # morning DI spread peak ≥ this (real directional trend)
PATH_REV_IVSKEW_FLIP_PCT    = 4.0     # IVSkew must shift ≥ this % toward reversal direction
PATH_REV_MAXPAIN_PROX_PCT   = 0.005   # within 0.5% of MaxPain → proximity bonus
PATH_REV_ADX_WANE_RATIO     = 0.85    # ADX < peak × this = momentum waning

# ─── Post-11 Scorer Thresholds ───────────────────────────────────────────────
# Aggregate score below POST11_SCORE_SKIP_MIN → skip entry (quality too low).
# No single component blocks — only the aggregate can gate.
# POST11_OTM_BOOST: for STRONG signals (≥POST11_SCORE_STRONG_MIN), add +1 OTM
# strike to buy a currently-OTM option that converts to near-ATM/ITM on continuation.
POST11_SCORE_SKIP_MIN   = 40    # aggregate < 40 → skip (bad overall quality)
POST11_SCORE_STRONG_MIN = 70    # aggregate ≥ 70 → STRONG (2 lots, OTM boost eligible)
POST11_OTM_BOOST        = True  # STRONG signal → +1 OTM strike for better leverage payout

# ─── High-Conviction Pre-11 Sizing ────────────────────────────────────────────
# Composite signal_scorer ≥ this threshold → upgrade to 2 lots even for pre-11
# Path-A ORB entries. Complements post-11 scorer STRONG gate.
# Raise if 2-lot pre-11 trades show poor WR after 20+ occurrences.
HIGH_CONVICTION_SCORER_THRESHOLD = 65   # composite score ≥ 65/100 → 2 lots

# ─── Cohesive Strategy Time Phases ───────────────────────────────────────────
# The trading day is divided into 3 phases with different conviction weights:
#   Phase 1 (ORB Prime,   09:30–11:00): highest conviction — full ORB structure,
#            fresh momentum, 1–2 lots. Entry gated by PATH_A_ENTRY_END per-day config.
#   Phase 2 (ORB Extended, 11:00–13:00): post-11 scorer gate applies (score≥40 to enter,
#            ≥70 = STRONG → 2 lots). Continuation of ORB direction only.
#   Phase 3 (Late Harvest, 13:00–14:30): PATH-REV only (DI-flip + MaxPain snap).
#            Target scaled to STRATEGY_PHASE3_TARGET_SCALE — take profits quicker
#            (less runway before 14:30 force-close; premium decay accelerates post-13:00).
STRATEGY_PHASE1_END          = '11:00'  # Phase 1 → Phase 2 transition
STRATEGY_PHASE3_START        = '13:00'  # Phase 2 → Phase 3 transition (PATH-A stops here)
STRATEGY_PHASE3_TARGET_SCALE = 0.70    # Phase 3 target = 70% of normal (take profit quicker)

# ─── Unified Scorer ───────────────────────────────────────────────────────────
# Single time-weighted 0-100 score replaces discrete PATH-A/REV gates.
# Weights shift across 5 time bands (09:30 / 10:00 / 11:00 / 12:00 / 13:00)
# and recalibrate weekly via weekly_analyzer.recalibrate_unified_weights().
# Threshold 55: permissive enough not to over-filter clean ORB setups,
# strict enough to block weak reversal trades (e.g. PUT vs ST15=BULL).
# Raise threshold to 60-65 after 30+ calibration trades.
UNIFIED_SCORER_ENABLED  = True   # gate: True = block weak signals; False = log only
UNIFIED_SCORE_THRESHOLD = 55     # minimum score to enter (0-100)
# Per-band threshold offsets, added to UNIFIED_SCORE_THRESHOLD for entries in
# that time band. Live trades Apr-Jul: 11:00-12:00 entries ran 0/4 (-₹7,420) —
# lunchtime-drift breakouts fail; demand extra quality there. Other bands: no
# offset (early window 50% WR, noon REV is the top live earner — leave alone).
UNIFIED_BAND_THRESHOLD_OFFSET = {'11:00': 5}

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_DIRECTORY    = "logs"
DATA_DIRECTORY   = "data"
ENABLE_EMAIL_ALERTS = False
EMAIL_ADDRESS    = "your_email@example.com"

# ─── Options Settings (legacy, used by bot.py) ────────────────────────────────
OPTION_SYMBOL    = "NIFTY"
STRIKE_SELECTION = "atm"
