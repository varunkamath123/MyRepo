# -*- coding: utf-8 -*-
"""Every tunable number for the BANKNIFTY breakout strategy, and nothing else.

THE POINT OF THIS FILE. Strategy logic lives in bnf_strategy.py and takes this
module as its only source of parameters. That separation is what lets a bad
result be diagnosed instead of argued about:

    setups almost never fire        -> a CONFIG problem  (thresholds too strict)
    setups fire, thesis is wrong    -> a STRATEGY problem (the idea does not hold)
    thesis right, still loses money -> a CONFIG problem  (exits / sizing / costs)

Each value below carries its origin. A number with no stated origin is a guess
and is marked as one.

STATUS: PAPER ONLY. There is no order-placement code anywhere in this package.
It reads prices and records what it WOULD have done. Going live is a separate,
deliberate piece of work, not a flag in this file.

ABOUT THE EVIDENCE (read before trusting any of it). Pre-registered tests on 420
BANKNIFTY sessions (research/breakout_study.py, breakout_study2.py) found that no
price, volume or compression setup beats a random entry once costs are paid:

    random entry, random direction      -0.058% of spot per trade
    best pre-registered setup           -0.052%   (fade a failed break)
    best of 36 tuned combos, in-sample  -0.033%
    same combo out-of-sample            -0.050%  vs random -0.071% on that period

Gross edge is ~0 and costs (~0.08% of spot) decide the sign. This strategy
therefore ships as a TESTBED with a pre-registered verdict (EVALUATION.md), not
as a claim that breakouts can be anticipated.
"""

# ── identity & mode ─────────────────────────────────────────────────────────
INSTRUMENT = 'BANKNIFTY'
INDEX_SYMBOL = 'NSE:NIFTYBANK-INDEX'
OPTION_PREFIX = 'NSE:BANKNIFTY'
LOT_SIZE = 30
STRIKE_GAP = 100                  # exchange lists BANKNIFTY strikes 100 apart
EXPIRY_WEEKDAY = 1                # Tuesday; BANKNIFTY has MONTHLY expiry only
                                  # (SEBI removed weeklies Nov 2023)
MIN_DAYS_TO_EXPIRY = 2
PAPER_TRADE_MODE = True           # there is no live path; see module docstring

# ── capital & sizing ────────────────────────────────────────────────────────
STARTING_CAPITAL = 50_000         # user-specified
COMPOUND_ENABLED = True           # book = start + realised P&L (own trade log)
COMPOUND_FLOOR_PCT = 0.25         # sizing book never below 25% of start
RISK_PCT_OF_BOOK = 0.08           # rupee risk per trade = 8% of the book.
                                  # ORIGIN: the 8% default of the parked system,
                                  # chosen so sizing visibly engages as the book
                                  # grows. With Rs50k and one lot risking ~Rs2.9k
                                  # (premium ~390 x 30 x 25%), this is 1 lot until
                                  # the book reaches ~Rs73k, then 2.
MAX_DEPLOY_PCT = 0.50             # premium outlay never above 50% of the book
MAX_LOTS = 4                      # hard ceiling regardless of the book
MIN_LOTS = 1                      # never silence the strategy by sizing to zero
DD_THROTTLE_ENABLED = True        # risk falls as the book drops below its peak
DD_THROTTLE_FULL = 0.25           # full throttle at 25% drawdown ...
DD_THROTTLE_MIN_MULT = 0.25       # ... down to a quarter of normal risk

# ── when it may trade ───────────────────────────────────────────────────────
BAR_MINUTES = 5
ENTRY_START = '09:55'             # BAR CLOSE time. The opening range (09:15-09:45)
                                  # exists from 09:45; 09:55 is the first decision
                                  # the research tested (FIRST_BAR=7)
ENTRY_END = '13:45'               # BAR CLOSE time. ORIGIN: the last decision the
                                  # research tested (LAST_BAR=54). Matching the
                                  # tested window matters more than a preferred one.
FORCE_CLOSE = '15:00'
MAX_TRADES_PER_DAY = 2
MAX_CONCURRENT_POSITIONS = 1
COOLDOWN_BARS_AFTER_LOSS = 6      # no re-entry for 30 min after a loss.
                                  # GUESS: stops an immediate re-entry into the
                                  # same failed thesis.

# ── the setup (the thesis being tested) ─────────────────────────────────────
# "Price coils against a key level on real participation, in a volatile regime;
#  anticipate the break in the direction of the level."
LEVELS = ('PDH', 'PDL', 'ORH', 'ORL', 'DAYH', 'DAYL')
LEVEL_PROX_PCT = 0.15             # close within 0.15% of the level.
                                  # ORIGIN: pre-registered in breakout_study.py
COIL_BARS = 6                     # 30 minutes
COIL_RATIO_MAX = 0.812            # range6 / (ATR14 * sqrt(6)) must be <= this.
                                  # ORIGIN: bottom third of the pooled ratio over
                                  # 420 sessions, from breakout_study.py. Re-derive
                                  # with bnf_replay.py --calibrate if history grows.
VOLUME_MODE = 'participate'       # 'participate' | 'dryup' | 'off'
VOL_REL_MIN = 1.0                 # mean volume of last 3 bars vs the median at the
                                  # same time of day over the previous 20 sessions
VOL_REL_MAX_DRYUP = 0.8
VOL_BASELINE_SESSIONS = 20
REQUIRE_HOT_REGIME = True         # previous session's realised vol above its own
                                  # trailing median. ORIGIN: the ONE conditioner
                                  # that survived testing (payable share +26.6 pts
                                  # top-vs-bottom vol quintile against an
                                  # IV-adjusted break-even, both halves).
HOT_REGIME_WINDOW = 60
DIRECTION = 'toward'              # 'toward' = anticipate the break (the thesis)
                                  # 'away'   = fade the level
                                  # ORIGIN: both are tested; 'toward' is what was
                                  # asked for. Research found pressing TOWARD a
                                  # level (-0.102%) worse than AWAY (-0.062%), so
                                  # flipping this one word is the first thing to
                                  # try if paper results disappoint.

# ── OI / PCR entry gate (added 2026-10-23 at the user's direction) ─────────────
# The setup is "coil against a level, anticipate the break". Two ideas from positioning:
#   WALLS  Option writers defend the biggest open-interest strikes (call writers cap a rally,
#          put writers floor a fall). A break toward a level needs ROOM; if the biggest OI wall
#          on the side being traded toward sits within OI_WALL_CLEAR_PCT, expect rejection.
#   PCR    Put-writing is read as support. The trade direction must agree with PCR measured
#          against ITS OWN recent history (z-score at the same time of day over the prior
#          OI_PCR_HISTORY_SESSIONS sessions), because raw PCR is near-constant (the parked
#          quad voted bullish on it 61% of the time and bearish 4%).
#
# HONEST STATUS. research/oi_pcr_adx_study.py found NO directional or magnitude information in
# PCR, max-pain or skew over the next hour (follow-the-sign means -1.3..+1.7 bps against ~8 bps
# of cost, 121 sessions), and the parked quad built on them fired 88 CALLs to 1 PUT. WALLS CANNOT
# BE BACKTESTED at all: no strike-level OI history exists before the recorder began. So this gate
# is a theory-driven filter with no supporting evidence, switched on because it was asked for.
# Expect it to cut the trade count, which lengthens the time to a verdict; every setup it blocks
# is logged with its would-be direction so its effect can be measured (research/oi_gate_shadow.py).
OI_GATE_ENABLED = True
OI_GATE_BLOCKS = False            # SHADOW (2026-10-24): evaluate and LOG the gate on every bar and on every
                                  # taken trade, but do not block. True = it refuses entries. Switched to
                                  # shadow after day 2: the gate blocked all 7 PUT setups on a -1.2% day
                                  # (the 'wall' was a modest 54,000 strike, 4.9% of put OI) and left almost
                                  # no trades to measure. Shadow keeps the trades flowing AND records what
                                  # the gate would have said, so gated vs ungated P&L can be compared.
OI_USE_PCR = True
OI_USE_WALLS = True
OI_PCR_Z_MIN = 0.0                # direction-signed PCR z must be >= this (CALL: z>=+min, PUT: z<=-min)
OI_WALL_CLEAR_PCT = 0.5           # the biggest wall toward the trade must be farther than this (% of spot)
OI_WALL_LOOKAHEAD_PCT = 1.5       # walls are searched within this distance of spot
OI_MAX_SNAPSHOT_AGE_MIN = 20      # a chain snapshot older than this is treated as missing
OI_ON_MISSING = 'block'           # 'block' | 'pass'. Block, so the test only counts trades where
                                  # the condition could actually be evaluated, and it is VISIBLE
                                  # in the decision log. 'pass' would silently trade unfiltered.
OI_PCR_HISTORY_SESSIONS = 20
OI_PCR_MIN_HISTORY = 10


# ── exits ───────────────────────────────────────────────────────────────────
# Hard risk limit on the option premium.
HARD_STOP_PREMIUM_PCT = 0.25      # ORIGIN: the parked system's stop. Its trough
                                  # data showed the best winner dipped -10.9%
                                  # before running +56%, so anything tighter than
                                  # ~11% would have killed it; 25% survived.
TARGET_PREMIUM_PCT = 0.55         # ORIGIN: fired on Oct 21 within 1-2 points of
                                  # the peak twice; every LOWER target tested
                                  # lost money once capped winners were counted.

# Thesis exits: the idea, not the price, says get out.
THESIS_BREAK_BUFFER_PCT = 0.05    # exit if a bar CLOSES back through the far edge
                                  # of the coil by this much (spot %). GUESS.
THESIS_CONFIRM_PCT = 0.15         # the break must carry this far past entry ...
THESIS_CONFIRM_BARS = 6           # ... within this many bars, or the thesis is
                                  # stale and we leave. ORIGIN: parked system's
                                  # losers averaged a +2.6% peak against +29.4%
                                  # for winners -- a trade that has not worked
                                  # early rarely does. GUESS on the exact numbers.

# Profit protection, scaled to volatility rather than a fixed premium distance.
TRAIL_ARM_INDEX_PCT = 0.30        # arms once price is this far in favour (spot %)
TRAIL_ATR_MULT = 1.5              # then exits on a retrace of this many ATR14(5m)
                                  # from the best price. ORIGIN: a fixed 10-point
                                  # premium trail is ~0.13% of spot, much tighter
                                  # than a 30-minute consolidation, and sold the
                                  # Oct 21 NIFTY trade at +4% before it ran +88%.

# Round-trip cost in INDEX terms, for replay only (live uses real rupee costs).
# ORIGIN: ~Rs78 commission and charges on a ~Rs11.6k premium is 0.67% of the
# premium, i.e. ~0.05% of spot at 71 premium-% per index-%; 0.08% adds slippage.
REPLAY_COST_INDEX_PCT = 0.08

# Mapping between index moves and premium, for replay only. Live trading uses the
# real premium. ORIGIN: delta 0.5 on a ~390 premium at spot 55,500, so 0.35% of
# spot (194 pts x 0.5 = 97) is 25% of the premium.
PREMIUM_PCT_PER_INDEX_PCT = 71.4

# ── execution realism (paper fills must not flatter) ─────────────────────────
SLIPPAGE_PCT = 0.005              # fill 0.5% worse than LTP when no bid/ask given
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.001              # on option SELL premium value
EXCH_TXN_PCT = 0.0003503          # on option premium turnover
GST_PCT = 0.18                    # on brokerage + exchange charge
STAMP_BUY_PCT = 0.00003

# ── API budget (Fyers Standard plan: 5,000 requests/day, 5/second) ───────────
# The parked system exhausted this at 14:43 on Oct 19 and 13:05 on Oct 21 by
# polling three indices, three strategies and every phantom position. This one
# fetches ONE series once per closed bar and polls an option only while holding.
EXIT_POLL_SECONDS = 20
MAX_API_CALLS_PER_DAY = 3000      # 60% of the plan; entries stop scanning past
                                  # this, exits NEVER stop
API_MIN_GAP_SECONDS = 0.35        # stays under 5 requests/second
RATE_LIMIT_BACKOFF_SECONDS = 1800 # on a daily-cap error, wait half an hour

# ── option-chain recorder: LOGGING ONLY (bnf_chain.py) ───────────────────────
# PCR, max-pain, IV skew and open interest by strike. research/oi_pcr_adx_study.py found no
# directional or magnitude information in them, so NOTHING reads this. It is recorded
# because it cannot be rebuilt later (Fyers deletes expired chains) and the untested ideas
# - event IV crush, strike/expiry choice, positioning near expiry - need it. Source is NSE's
# public website, so it costs NO Fyers requests.
CHAIN_RECORD_ENABLED = True
CHAIN_RECORD_EVERY_MIN = 5            # 15 until 2026-10-22: one failed fetch then left a 30-min-old snapshot, past OI_MAX_SNAPSHOT_AGE_MIN (20), blocking entries
CHAIN_STRIKES_EACH_SIDE = 15          # +/- 15 strikes of 100 = +/- 1,500 points around ATM
CHAIN_FETCH_STALE_SEC = 180           # a fetch hung this long is abandoned so later slots run
CHAIN_LEGACY_HISTORY_FILE = '/opt/trading_bot/live_bot/logs/oi_intraday.jsonl'
                                      # also continue the old 5-min file in its original shape


# ── data & files ────────────────────────────────────────────────────────────
HISTORY_DIR = '/opt/trading_bot/data/banknifty_5min'   # written by the nightly
                                                       # data collector; used for
                                                       # levels and baselines, so
                                                       # they cost no API calls
STATE_DIR = '/opt/trading_bot/bnf_state'
TOKEN_FILE = '/opt/trading_bot/live_bot/logs/token.txt'
LEGACY_DIR = '/opt/trading_bot/live_bot'   # only for FyersAuth (the shared token job)


def validate() -> list:
    """Return a list of problems; empty means the config is coherent."""
    p = []
    if not (0 < RISK_PCT_OF_BOOK <= 0.25):
        p.append('RISK_PCT_OF_BOOK outside (0, 0.25]')
    if not (0 < HARD_STOP_PREMIUM_PCT < 1):
        p.append('HARD_STOP_PREMIUM_PCT must be in (0,1)')
    if TARGET_PREMIUM_PCT <= HARD_STOP_PREMIUM_PCT:
        p.append('target must exceed the stop or the payoff is inverted')
    if ENTRY_START >= ENTRY_END:
        p.append('ENTRY_START must precede ENTRY_END')
    if ENTRY_END >= FORCE_CLOSE:
        p.append('ENTRY_END must precede FORCE_CLOSE')
    if DIRECTION not in ('toward', 'away'):
        p.append("DIRECTION must be 'toward' or 'away'")
    if VOLUME_MODE not in ('participate', 'dryup', 'off'):
        p.append("VOLUME_MODE must be 'participate', 'dryup' or 'off'")
    if MAX_LOTS < MIN_LOTS:
        p.append('MAX_LOTS < MIN_LOTS')
    if STARTING_CAPITAL <= 0:
        p.append('STARTING_CAPITAL must be positive')
    if API_MIN_GAP_SECONDS < 0.2:
        p.append('API_MIN_GAP_SECONDS would breach 5 requests/second')
    if CHAIN_RECORD_EVERY_MIN < 5:
        p.append('CHAIN_RECORD_EVERY_MIN under 5 would hammer NSE, which rate-limits')
    if OI_ON_MISSING not in ('block', 'pass'):
        p.append("OI_ON_MISSING must be 'block' or 'pass'")
    if OI_GATE_ENABLED and not (OI_USE_PCR or OI_USE_WALLS):
        p.append('OI_GATE_ENABLED with neither OI_USE_PCR nor OI_USE_WALLS gates nothing')
    if PAPER_TRADE_MODE is not True:
        p.append('this package has no live path; PAPER_TRADE_MODE must be True')
    if not set(LEVELS) <= {'PDH', 'PDL', 'ORH', 'ORL', 'DAYH', 'DAYL'}:
        p.append('unknown level name in LEVELS')
    return p
