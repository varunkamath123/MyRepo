"""
Kronos Futures Bot — Central Configuration
Broker: Upstox API | Indices: NIFTY, BANKNIFTY, SENSEX futures
"""

# ── Capital ────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 250_000          # INR
MAX_POSITIONS = 1                # one position at a time across all instruments
CAPITAL_PER_TRADE = 250_000      # all-in on highest conviction

# ── Instruments ────────────────────────────────────────────────────────────────
INSTRUMENTS = {
    "NIFTY": {
        "symbol": "NSE_FO|NIFTY",
        "lot_size": 75,
        "tick_size": 0.05,
        "margin_approx": 95_000,   # approx per lot at current levels
        "live": True,
    },
    "BANKNIFTY": {
        "symbol": "NSE_FO|BANKNIFTY",
        "lot_size": 35,
        "tick_size": 0.05,
        "margin_approx": 85_000,
        "live": True,
    },
    "SENSEX": {
        "symbol": "BSE_FO|SENSEX",
        "lot_size": 20,
        "tick_size": 0.05,
        "margin_approx": 80_000,
        "live": False,             # paper until validated
    },
}

# ── Signal layers ──────────────────────────────────────────────────────────────
KRONOS_MODEL = "NeoQuasar/Kronos-mini"   # 4.1M params, CPU-feasible
KRONOS_FORECAST_BARS = 12                # bars ahead to forecast (12 × 5min = 1hr intraday)
KRONOS_CONFIDENCE_MIN = 0.60            # min directional confidence to use signal

FINGPT_SENTIMENT_MODEL = "FinGPT/fingpt-sentiment-all-llama2-13b-lora"
SENTIMENT_BEARISH_THRESHOLD = -0.3      # below → bearish
SENTIMENT_BULLISH_THRESHOLD = 0.3       # above → bullish

MIROFISH_ENABLED = True
MIROFISH_NUM_AGENTS = 500               # start small, scale up
MIROFISH_RUN_TIME = "08:45"             # pre-market daily run (IST)

# ── Entry filters ──────────────────────────────────────────────────────────────
MIN_ADX = 25                            # minimum trend strength
ORB_WINDOW_START = "09:30"              # Opening Range Breakout start
ORB_WINDOW_END = "11:00"               # ORB window end
MAIN_SESSION_END = "14:30"             # no new entries after this

# ── Dynamic exit conditions (any one triggers exit) ───────────────────────────
STOP_LOSS_PCT = 0.25                   # 25% of premium / futures margin
TRAIL_ACTIVATE_PCT = 0.18              # start trailing from 18% gain
TRAIL_DISTANCE_PCT = 0.10              # trail distance 10%

EXIT_ON_KRONOS_REVERSAL = True         # exit if Kronos flips direction
EXIT_ON_SENTIMENT_FLIP = True          # exit if FinGPT sentiment flips
EXIT_ON_SUPERTREND_FLIP = True         # exit if 5m SuperTrend flips
# No hard max hold days — position held until one exit condition fires

# ── Upstox API ─────────────────────────────────────────────────────────────────
UPSTOX_API_KEY = ""                    # set via env: UPSTOX_API_KEY
UPSTOX_API_SECRET = ""                 # set via env: UPSTOX_API_SECRET
UPSTOX_REDIRECT_URI = "http://localhost:8080/"
UPSTOX_ACCESS_TOKEN_PATH = "logs/upstox_token.txt"

# ── Scheduling (IST) ───────────────────────────────────────────────────────────
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
PRE_MARKET_BRIEF = "08:45"
EOD_DEBRIEF = "15:40"
DATA_COLLECT_TIME = "15:35"

# ── Data ───────────────────────────────────────────────────────────────────────
DATA_DIR = "data"
LOG_DIR = "logs"
TIMEFRAME = "5minute"
LOOKBACK_DAYS = 30                     # bars fed to Kronos
