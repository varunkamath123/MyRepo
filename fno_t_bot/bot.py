# -*- coding: utf-8 -*-
"""
FnO_T_Bot — Strategy Backtest
==============================
Runs on real Fyers 5-min data (local CSV database from data_collector.py).
Falls back to yfinance 60-day data if no local database found.

Supports both NIFTY and BANKNIFTY simultaneously.

Strategy Variants (set VARIANT below or pass as CLI arg)
---------------------------------------------------------
  v7   : baseline — 80% target, 40% SL, trailing from 55% (BEST so far)
  A    : 200% target, floor exit at 150% minimum
  B    : partial exit — 50% at 80%, rest runs to 200%
  C    : 5-8 day S/R breakout with volume confirmation
  D    : EMA 6/12, ADX>18, Stop 50%, Target 180%
  E    : Mon+Tue only — high-gamma window test (true 2-day DTE)
  v9   : smart loss avoidance — skip Tuesday + CALL ADX≥30
  v9c  : v9b + dynamic lots — 3 lots when signal_strength≥2, else 2
  v9d  : v9c + trend continuation re-entry (re-enter if EMA still intact after target/trail exit)
  v13  : v9c + vertical spread hedge (buy ATM CE/PE + sell OTM CE/PE 3×strike_gap away)
  v10  : v9 + heavyweight alignment (≥2 of Reliance/HDFC/Airtel confirm)
  v11  : v9 + ATR regime filter (ATR_ratio ≥ 0.70 — skip choppy entries)
  v12  : v9 + ATR regime + VWAP 2σ stretch filter (combined)
  v14  : v9c + Tuesday PUT allowed when ADX ≥ 35 (high conviction short setups)
  v15  : v9c + CALL ADX threshold relaxed 30 → 27 (recover near-threshold entries)
  v16  : v9c + entry window starts at 10:30 IST (AVOID_FIRST_MINUTES=75)
  v17  : v14 + v16 combined (Tue PUT ≥35 + 10:30 start)
  v18  : v9c + RSI-50 crossover secondary trigger (mid-trend re-entry in EMA direction)
  v19  : v9c + 3 lots always (no 2-lot baseline; max conviction sizing on every trade)
  v20  : v14 + v19 combined (Tuesday PUT ≥35 + 3 lots always) — NEW BEST CANDIDATE
  vX   : Per-instrument optimised strategy (reads config.INSTRUMENT_STRATEGY)
           NIFTY    — CALL ADX≥30, PUT ADX≥25, skip Tue, entry 11:00–14:00, max_conc=1
           BANKNIFTY — CALL ADX≥25, PUT ADX≥35, skip Tue, entry 11:00–14:45, max_conc=2
           SENSEX   — CALL ADX≥25, PUT ADX≥25, skip Tue+Thu, entry 12:00–14:45, max_conc=1
  vX2  : vX + VWAP-breach secondary signal + Tuesday CALL-only block (PUT allowed Tue)
  vST  : Supertrend — replaces EMA 9/21 crossover with Supertrend(7, 2.5) flip as trigger.
           Same ADX + VWAP filters and per-instrument settings as vX. Clean A/B vs vX:
           same exit params (SL 40%, target 130%, trail 55%/20%), only entry trigger changes.
           Hypothesis: ST flip requires sustained price action beyond ATR band → fewer
           whipsaws than EMA crossover, potentially cleaner entries with similar trade count.
  vXS  : vX + Scale-In + Mean-Reversion Gate — enter 1 lot always; add 2nd lot at 55%
           profit (TRAILING_ACTIVATION); skip entries when VWAP-ATR Z > 3.0 (overstretched).
  vCH  : Challenger — identical to vX but uses OTM+1 strike (CALL: ATM+gap, PUT: ATM-gap)
           Tests whether a 1-strike OTM option would outperform ATM on the same signals.
           OTM options have lower entry price → same 130% target needs less underlying move.
           Clean A/B vs vX: same signal logic, same BS pricing model, only K differs.
           Primary:   EMA crossover + ADX + VWAP (same as vX)
           Secondary: EMA already directional + price just crossed VWAP + ADX ok
                      Fires when primary missed because VWAP wasn't breached at cross time,
                      or when EMA crossover happened before the entry window (e.g. SENSEX overnight)
  vG   : Greeks-Informed — vX base signals + 4 options-market quality filters:
           (1) HV Rank 3-93% (like IV rank: only skip extreme frozen-vol / vol-spike outliers)
           (2) ATR_ratio ≥ 0.75 (volatility must be expanding, not compressing; tighter than v11's 0.70)
           (3) RSI quality gate (CALL: RSI < 78, PUT: RSI > 22 — no exhausted entries)
           (4) Round-number clearance: skip when price within 0.08% of OI wall strike
               (NIFTY: 50-pt steps | BANKNIFTY: 100-pt | SENSEX: 200-pt)
           Same per-instrument settings as vX (ADX, time gates, lot sizing).
           Hypothesis: filtering by options-market conditions improves entry quality
           independent of the underlying trend signal quality.
  vXST : Ensemble — EMA 9/21 crossover (primary) + Supertrend(7,2.5) direction (secondary).
           Reduces EMA lag: ST flip + EMA already aligned triggers early entry even before
           the EMA crossover fires.  Prevents vST noise: ST flip alone (EMA not aligned)
           is blocked — EMA alignment is mandatory gate for ST-triggered entries.
           Entry fires when:
             A) Fresh EMA cross + ST direction agrees     → 1 lot  (standard + confirmation)
             B) Fresh ST flip  + EMA direction aligned    → 1 lot  (early entry, lag fix)
             C) Fresh EMA cross AND fresh ST flip match   → 2 lots (maximum conviction)
           Blocked: EMA cross when ST opposes (divergence); ST flip without EMA alignment.
           Per-instrument settings identical to vX.  Same exits as vX (SL 40%, T 130%).

Usage
-----
  python bot.py           # runs v7 (default)
  python bot.py A         # runs Strategy A
  python bot.py E         # runs variant E (Mon+Tue only)
  python bot.py v9        # runs variant v9 (smart filters)
  python bot.py v9c       # runs variant v9c (dynamic lot sizing)
  python bot.py v9d       # runs variant v9d (v9c + trend continuation re-entry)
  python bot.py v13       # runs variant v13 (vertical spread hedge)
  python bot.py v11       # runs variant v11 (ATR regime filter)
  python bot.py v12       # runs variant v12 (ATR + VWAP band filter)
  python bot.py compare   # runs all 12 variants and prints comparison table
"""

import math
import os
import sys
import datetime as dt
from datetime import time as dtime

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.stats import norm
from ta.trend import ADXIndicator, EMAIndicator, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
import warnings
warnings.filterwarnings('ignore')

# Force UTF-8 output on Windows (avoids UnicodeEncodeError with ₹ ▲ ✓ etc.)
import sys as _sys
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(encoding='utf-8')
    _sys.stderr.reconfigure(encoding='utf-8')

plt.style.use('seaborn-v0_8-darkgrid')
try:
    get_ipython().run_line_magic('matplotlib', 'inline')
except NameError:
    pass

import config

# ─── Variant selection ────────────────────────────────────────────────────────
VALID_VARIANTS = ('v7', 'A', 'B', 'C', 'D', 'E', 'v9', 'v9b', 'v9c', 'v9d', 'v10', 'v11', 'v12', 'v13', 'v14', 'v15', 'v16', 'v17', 'v18', 'v19', 'v20', 'vX', 'vX2', 'vPW', 'vCH', 'vST', 'vXS', 'vXST', 'vG', 'vXDI', 'vXF', 'vB', 'vB1', 'vB2', 'compare', 'blind', 'vXP')
VARIANT = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in VALID_VARIANTS else 'v7'

# ─── Real Options Price Cache ─────────────────────────────────────────────────
# Load real 5-min options OHLCV data collected by options_data_collector.py.
# When available, bot uses ACTUAL prices instead of Black-Scholes estimates.
# Falls back silently to BS when real data is missing.

_OPTIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'data', 'nifty_options')
_options_cache = {}   # {(expiry_date, strike, 'CALL'/'PUT'): DataFrame}

def _load_options_cache():
    global _options_cache
    if not os.path.exists(_OPTIONS_DIR):
        return
    count = 0
    for fname in os.listdir(_OPTIONS_DIR):
        if not fname.endswith('.csv'):
            continue
        parts = fname.replace('.csv', '').split('_')
        if len(parts) != 4 or parts[0] != 'NIFTY':
            continue
        try:
            import pytz as _pytz
            _IST = _pytz.timezone('Asia/Kolkata')
            exp    = dt.datetime.strptime(parts[1], '%Y%m%d').date()
            strike = int(parts[2])
            otype  = 'CALL' if parts[3] == 'CE' else 'PUT'
            df     = pd.read_csv(os.path.join(_OPTIONS_DIR, fname),
                                 index_col=0, parse_dates=True)
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize(_IST)
            _options_cache[(exp, strike, otype)] = df
            count += 1
        except Exception:
            continue
    if count > 0:
        print(f"  Real options data: {count} contracts loaded from {_OPTIONS_DIR}")
    return count

_real_options_count = _load_options_cache()

# ─── Heavyweight Stock Cache ──────────────────────────────────────────────────
# Load 5-min OHLCV for top-3 Nifty stocks (Reliance, HDFC Bank, Bharti Airtel).
# Used by variant v10 to confirm signal direction via heavyweight alignment.
# Stocks collected via: python data_collector.py stocks_backfill 400
#
# Alignment rule:
#   CALL signal → need 2+ stocks with Close > their own daily VWAP
#   PUT  signal → need 2+ stocks with Close < their own daily VWAP
#
# If the stocks that drive ~22% of NIFTY's weighting agree with the signal,
# the trend has fundamental support beyond the EMA crossover alone.

_stocks_data = {}   # {ticker: DataFrame with daily-VWAP column added}

def _load_stocks_cache():
    """Load and preprocess heavyweight stock data at startup."""
    global _stocks_data
    try:
        from data_collector import load_stocks_data
        raw = load_stocks_data()
    except Exception:
        return 0

    count = 0
    for ticker, df in raw.items():
        if len(df) == 0:
            continue
        # Add daily VWAP (reset each trading day)
        df = df.copy()
        df['_date'] = df.index.date
        df['TP']    = (df['High'] + df['Low'] + df['Close']) / 3
        has_vol = df['Volume'].sum() > 0
        if has_vol:
            df['_tp_vol'] = df['TP'] * df['Volume']
            df['VWAP']    = (df.groupby('_date')['_tp_vol'].cumsum() /
                            df.groupby('_date')['Volume'].cumsum())
        else:
            df['VWAP']    = df.groupby('_date')['TP'].transform(
                lambda x: x.expanding().mean()
            )
        df.drop(columns=['_date', 'TP', '_tp_vol'], inplace=True, errors='ignore')
        _stocks_data[ticker] = df
        count += 1
    if count > 0:
        print(f"  Heavyweight stocks: {count} loaded "
              f"({', '.join(_stocks_data.keys())})")
    return count

_stocks_count = _load_stocks_cache()


def _heavyweight_alignment(timestamp, signal_type: str) -> bool:
    """
    Returns True if ≥2 of the 3 heavyweight stocks confirm the signal direction.

    CALL → stock Close must be ABOVE its own daily VWAP
    PUT  → stock Close must be BELOW its own daily VWAP

    Falls back gracefully: if no stock data is loaded, always returns True
    (i.e., the filter is a no-op when data is unavailable).
    """
    if not _stocks_data:
        return True   # no data → bypass filter

    ts         = pd.Timestamp(timestamp)
    aligned    = 0
    total_avail = 0

    for ticker, df in _stocks_data.items():
        # Find nearest 5-min bar within ±10 min
        try:
            nearest = df.index[abs(df.index - ts).argmin()]
            if abs((nearest - ts).total_seconds()) > 600:
                continue   # too far from our bar, skip this stock
        except Exception:
            continue

        row = df.loc[nearest]
        if pd.isna(row.get('VWAP', float('nan'))):
            continue

        total_avail += 1
        if signal_type == 'CALL' and row['Close'] > row['VWAP']:
            aligned += 1
        elif signal_type == 'PUT' and row['Close'] < row['VWAP']:
            aligned += 1

    if total_avail == 0:
        return True   # no data available at this timestamp → bypass

    # Require majority (≥2 of 3, or ≥1 of 2 if one stock is missing at this bar)
    needed = max(2, (total_avail + 1) // 2)
    return aligned >= needed


def _expiry_for_date(trade_date, min_dte=2):
    """Find the next Thursday expiry with >= min_dte days from trade_date."""
    check = trade_date
    for _ in range(14):
        if isinstance(check, dt.datetime):
            check = check.date()
        if check.weekday() == 3 and (check - (trade_date.date() if isinstance(trade_date, dt.datetime) else trade_date)).days >= min_dte:
            return check
        check += dt.timedelta(days=1)
    return None

def _get_real_option_price(expiry, strike, opt_type, timestamp):
    """
    Look up real options Close price at timestamp.
    Returns float price, or None if not in cache.
    """
    key = (expiry, strike, opt_type)
    if key not in _options_cache:
        return None
    df  = _options_cache[key]
    ts  = pd.Timestamp(timestamp)
    if ts in df.index:
        return float(df.loc[ts, 'Close'])
    # Nearest within ±10 min
    try:
        nearest = df.index[abs(df.index - ts).argmin()]
        if abs((nearest - ts).total_seconds()) <= 600:
            return float(df.loc[nearest, 'Close'])
    except Exception:
        pass
    return None

# 5-min bars per trading day: 09:15 → 15:30 = 375 min / 5 = 75 bars
BARS_PER_DAY = 75

print("=" * 70)
print(f"  {config.BOT_NAME} — STRATEGY BACKTEST  [variant={VARIANT}]")
print("=" * 70)


# ─── 1. Load Data (NIFTY + BANKNIFTY) ─────────────────────────────────────────

def load_instrument_data(instrument: str) -> pd.DataFrame:
    """Load all 5-min CSVs for an instrument from local database."""
    import pytz
    IST = pytz.timezone('Asia/Kolkata')

    folder_map = {
        'NIFTY':     'nifty_5min',
        'BANKNIFTY': 'banknifty_5min',
    }
    folder = folder_map.get(instrument, f"{instrument.lower()}_5min")
    data_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data', folder
    )

    df = pd.DataFrame()
    if os.path.exists(data_dir):
        files = sorted(f for f in os.listdir(data_dir) if f.endswith('.csv'))
        dfs = []
        for fname in files:
            try:
                tmp = pd.read_csv(
                    os.path.join(data_dir, fname), index_col=0, parse_dates=True
                )
                if len(tmp) > 0:
                    dfs.append(tmp)
            except Exception:
                pass
        if dfs:
            df = pd.concat(dfs).sort_index()
            df = df[~df.index.duplicated(keep='first')]

    if len(df) == 0:
        # No local CSV — skip this instrument entirely (no yfinance fallback).
        # yfinance data is too short (~60 days) and unreliable for options backtesting.
        # Collect real data first: python data_collector.py backfill 400
        print(f"  ⚠ No local CSV for {instrument} — skipping (run data_collector.py backfill)")
        return pd.DataFrame()

    # Normalise to IST
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    return df


print("\nLoading NIFTY data...")
df_nifty = load_instrument_data('NIFTY')
print(f"  NIFTY   : {len(df_nifty):,} candles  "
      f"({df_nifty.index[0].date()} to {df_nifty.index[-1].date()})" if len(df_nifty) > 0 else "  NIFTY: no data")

print("Loading BANKNIFTY data...")
df_bnf = load_instrument_data('BANKNIFTY')
if len(df_bnf) > 0:
    print(f"  BANKNIFTY: {len(df_bnf):,} candles  "
          f"({df_bnf.index[0].date()} → {df_bnf.index[-1].date()})")
else:
    print("  BANKNIFTY: no local data found — run: python data_collector.py backfill BANKNIFTY 400")

print("Loading SENSEX data...")
df_sensex = load_instrument_data('SENSEX')
if len(df_sensex) > 0:
    print(f"  SENSEX   : {len(df_sensex):,} candles  "
          f"({df_sensex.index[0].date()} → {df_sensex.index[-1].date()})")
else:
    print("  SENSEX   : no local data found — run: python data_collector.py backfill SENSEX 400")


# ─── 2. Indicators ─────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()

    data['EMA_fast'] = EMAIndicator(close=data['Close'], window=config.MOMENTUM_EMA_FAST).ema_indicator()
    data['EMA_slow'] = EMAIndicator(close=data['Close'], window=config.MOMENTUM_EMA_SLOW).ema_indicator()
    data['SMA_200']  = SMAIndicator(close=data['Close'], window=200).sma_indicator()

    # Variant D: EMA 6/12 (faster, more signals, more whipsaw)
    data['EMA_fast_D'] = EMAIndicator(close=data['Close'], window=6).ema_indicator()
    data['EMA_slow_D'] = EMAIndicator(close=data['Close'], window=12).ema_indicator()

    adx = ADXIndicator(high=data['High'], low=data['Low'], close=data['Close'], window=14)
    data['ADX']      = adx.adx()
    data['DI_plus']  = adx.adx_pos()   # +DI: bullish directional strength
    data['DI_minus'] = adx.adx_neg()   # -DI: bearish directional strength

    data['RSI']       = RSIIndicator(close=data['Close'], window=14).rsi()
    data['Volume_MA'] = data['Volume'].rolling(20).mean()

    # VWAP (daily reset)
    _date_grp = data.index.date
    _week_grp = [f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}" for t in data.index]
    data['_date'] = _date_grp
    data['_week'] = _week_grp
    data['TP']    = (data['High'] + data['Low'] + data['Close']) / 3

    has_volume = data['Volume'].sum() > 0
    if has_volume:
        data['_tp_vol']     = data['TP'] * data['Volume']
        data['VWAP']        = (data.groupby('_date')['_tp_vol'].cumsum() /
                               data.groupby('_date')['Volume'].cumsum())
        data['VWAP_Weekly'] = (data.groupby('_week')['_tp_vol'].cumsum() /
                               data.groupby('_week')['Volume'].cumsum())
    else:
        data['VWAP']        = data.groupby('_date')['TP'].transform(lambda x: x.expanding().mean())
        data['VWAP_Weekly'] = data.groupby('_week')['TP'].transform(lambda x: x.expanding().mean())

    # VWAP bands (±1σ and ±2σ) — for stretched-entry filter (v12)
    # Expanding mean of squared TP-deviation within each day gives intraday VWAP std
    data['_tp_sq_dev']  = (data['TP'] - data['VWAP']) ** 2
    data['VWAP_std']    = data.groupby('_date')['_tp_sq_dev'].transform(
        lambda x: x.expanding().mean().pow(0.5)
    )
    data['VWAP_upper1'] = data['VWAP'] + data['VWAP_std']
    data['VWAP_lower1'] = data['VWAP'] - data['VWAP_std']
    data['VWAP_upper2'] = data['VWAP'] + 2 * data['VWAP_std']
    data['VWAP_lower2'] = data['VWAP'] - 2 * data['VWAP_std']

    data.drop(columns=['_date', '_week', 'TP', '_tp_vol', '_tp_sq_dev'], inplace=True, errors='ignore')

    # Historical Volatility — annualised for 5-min bars
    data['Returns'] = data['Close'].pct_change()
    data['HV']      = data['Returns'].rolling(30).std() * np.sqrt(252 * BARS_PER_DAY)
    data['HV']      = data['HV'].bfill().fillna(0.18)

    # ── HV Rank (3-month rolling min-max normalization) ────────────────────────
    # Used by vG to gauge volatility regime (like IV rank, but using HV as proxy).
    # Window: 63 trading days × 75 bars = 4,725 bars (~3 months of 5-min data)
    # HV_rank ∈ [0, 1]:
    #   0.00 - 0.15 → very low vol regime (options won't move enough; skip)
    #   0.15 - 0.80 → normal range     (sweet spot — medium vol; enter)
    #   0.80 - 1.00 → very high vol    (options too expensive; mean reversion risk; skip)
    _hv_window       = 63 * BARS_PER_DAY  # 4,725 bars (~3 months)
    data['HV_63min'] = data['HV'].rolling(_hv_window, min_periods=250).min()
    data['HV_63max'] = data['HV'].rolling(_hv_window, min_periods=250).max()
    _hv_spread       = (data['HV_63max'] - data['HV_63min']).clip(lower=1e-9)
    data['HV_rank']  = ((data['HV'] - data['HV_63min']) / _hv_spread).clip(0.0, 1.0)

    # ATR-14 and ATR ratio (current volatility vs 20-trading-day baseline)
    # ATR_ratio < 0.70 = choppy low-vol regime → skip entry (v11/v12 filter)
    # Each trading day has BARS_PER_DAY 5-min bars; 20-day lookback = 20 × BARS_PER_DAY
    _tr = pd.concat([
        data['High'] - data['Low'],
        (data['High'] - data['Close'].shift(1)).abs(),
        (data['Low']  - data['Close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    data['ATR14']       = _tr.rolling(14).mean()
    data['ATR_20d_avg'] = data['ATR14'].rolling(20 * BARS_PER_DAY).mean()
    data['ATR_ratio']   = data['ATR14'] / data['ATR_20d_avg']

    # ── VWAP-ATR Z-score (mean-reversion risk gauge) ──────────────────────────
    # Measures price distance from VWAP in units of current ATR14 volatility.
    # Positive = price above VWAP, negative = price below VWAP.
    # |Z| > 3.0 means price is >3× today's ATR away from the intraday mean
    # → statistically overextended; reversion is more probable than continuation.
    # Used in vXS to block entries on stretched moves (mean-reversion gate).
    if 'VWAP' in data.columns:
        data['VWAP_ATR_Z'] = (data['Close'] - data['VWAP']) / data['ATR14'].clip(lower=1.0)
    else:
        data['VWAP_ATR_Z'] = 0.0

    # ── Supertrend (period=7, multiplier=2.5) ────────────────────────────────
    # ATR-based trailing band that flips between support/resistance.
    # ST_dir: +1 = bullish (price above lower band), -1 = bearish (price below upper band)
    # ST_line: the active band value (lower band when bullish, upper band when bearish)
    _st_period = 7
    _st_mult   = 2.5
    _atr_st    = _tr.rolling(_st_period).mean()
    _hl2       = (data['High'] + data['Low']) / 2
    _raw_upper = _hl2 + _st_mult * _atr_st
    _raw_lower = _hl2 - _st_mult * _atr_st

    st_upper = _raw_upper.copy()
    st_lower = _raw_lower.copy()
    st_dir   = pd.Series(1, index=data.index)

    for idx in range(1, len(data)):
        prev_close = data['Close'].iloc[idx - 1]
        # Upper band: only tighten (decrease) when previous close was below it
        st_upper.iloc[idx] = (min(_raw_upper.iloc[idx], st_upper.iloc[idx - 1])
                               if prev_close <= st_upper.iloc[idx - 1]
                               else _raw_upper.iloc[idx])
        # Lower band: only tighten (increase) when previous close was above it
        st_lower.iloc[idx] = (max(_raw_lower.iloc[idx], st_lower.iloc[idx - 1])
                               if prev_close >= st_lower.iloc[idx - 1]
                               else _raw_lower.iloc[idx])
        # Direction flip
        prev_dir = st_dir.iloc[idx - 1]
        cur_close = data['Close'].iloc[idx]
        if prev_dir == -1 and cur_close > st_upper.iloc[idx - 1]:
            st_dir.iloc[idx] = 1
        elif prev_dir == 1 and cur_close < st_lower.iloc[idx - 1]:
            st_dir.iloc[idx] = -1
        else:
            st_dir.iloc[idx] = prev_dir

    data['ST_dir']  = st_dir
    data['ST_line'] = np.where(st_dir == 1, st_lower, st_upper)

    # ── 15m SuperTrend for Path F HTF alignment ──────────────────────────────
    # Resample 5m data to 15m, compute SuperTrend(7, 2.5), then join back.
    # Each 5m bar gets the 15m ST value for its 15m period (forward-filled).
    # Same params as paper_bot.py get_htf_context() for consistency.
    try:
        _15m = data[['High', 'Low', 'Close']].resample('15min').agg(
            {'High': 'max', 'Low': 'min', 'Close': 'last'}
        ).dropna()
        if len(_15m) >= 15:
            _st15_period, _st15_mult = 7, 2.5
            _atr15  = pd.concat([
                _15m['High'] - _15m['Low'],
                (_15m['High'] - _15m['Close'].shift(1)).abs(),
                (_15m['Low']  - _15m['Close'].shift(1)).abs(),
            ], axis=1).max(axis=1).rolling(_st15_period).mean()
            _hl2_15 = (_15m['High'] + _15m['Low']) / 2
            _ru15   = _hl2_15 + _st15_mult * _atr15
            _rl15   = _hl2_15 - _st15_mult * _atr15
            _su15   = _ru15.copy()
            _sl15   = _rl15.copy()
            _sd15   = pd.Series(1, index=_15m.index, dtype=float)
            for _k in range(1, len(_15m)):
                _pc = _15m['Close'].iloc[_k - 1]
                _su15.iloc[_k] = (min(_ru15.iloc[_k], _su15.iloc[_k - 1])
                                   if _pc <= _su15.iloc[_k - 1] else _ru15.iloc[_k])
                _sl15.iloc[_k] = (max(_rl15.iloc[_k], _sl15.iloc[_k - 1])
                                   if _pc >= _sl15.iloc[_k - 1] else _rl15.iloc[_k])
                _pd15 = _sd15.iloc[_k - 1]
                _cc   = _15m['Close'].iloc[_k]
                if   _pd15 == -1 and _cc > _su15.iloc[_k - 1]: _sd15.iloc[_k] = 1
                elif _pd15 ==  1 and _cc < _sl15.iloc[_k - 1]: _sd15.iloc[_k] = -1
                else:                                            _sd15.iloc[_k] = _pd15
            # Merge back: reindex to 5m, forward-fill within each 15m period
            _st15_series = _sd15.reindex(data.index, method='ffill')
            data['ST_15m'] = _st15_series
    except Exception:
        pass   # ST_15m missing → check_path_g_signal skips the filter (safe fallback)

    data = data.dropna(subset=['EMA_fast', 'EMA_slow', 'ADX', 'RSI'])
    return data


print("\nComputing indicators...")
data_nifty  = add_indicators(df_nifty)  if len(df_nifty)  > 0 else pd.DataFrame()
data_bnf    = add_indicators(df_bnf)    if len(df_bnf)    > 0 else pd.DataFrame()
data_sensex = add_indicators(df_sensex) if len(df_sensex) > 0 else pd.DataFrame()

if len(data_nifty) > 0:
    print(f"  NIFTY   : {len(data_nifty):,} candles with indicators")
if len(data_bnf) > 0:
    print(f"  BANKNIFTY: {len(data_bnf):,} candles with indicators")
if len(data_sensex) > 0:
    print(f"  SENSEX  : {len(data_sensex):,} candles with indicators")


# ─── 2b. SuperTrend ───────────────────────────────────────────────────────────

def compute_supertrend(df, period=10, multiplier=3.0):
    hl2   = (df['High'] + df['Low']) / 2
    tr    = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift(1)).abs(),
        (df['Low']  - df['Close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr   = tr.rolling(period).mean()

    up    = (hl2 - multiplier * atr).values.copy()
    dn    = (hl2 + multiplier * atr).values.copy()
    close = df['Close'].values.copy()
    trend = np.ones(len(df))

    for i in range(1, len(df)):
        if close[i - 1] > up[i - 1]:
            up[i] = max(up[i], up[i - 1])
        if close[i - 1] < dn[i - 1]:
            dn[i] = min(dn[i], dn[i - 1])
        if trend[i - 1] == 1:
            trend[i] = -1 if close[i] < up[i] else 1
        else:
            trend[i] = 1 if close[i] > dn[i] else -1

    return pd.Series(trend, index=df.index, name='SuperTrend')


# ─── 2c. Strategy C: 5-8 Day Rolling S/R Levels ──────────────────────────────

def add_rolling_sr(data: pd.DataFrame, lookback_days: int = 6) -> pd.DataFrame:
    """
    Compute rolling support/resistance from the last N trading days.

    For each 5-min bar, SR_High = max(High) and SR_Low = min(Low) over the
    previous `lookback_days` full trading days (no look-ahead: today's data
    is excluded). These form the breakout thresholds.

    Also computes a 20-bar rolling volume baseline for volume-spike detection.
    """
    data = data.copy()
    # Build daily OHLC (previous day shifted)
    daily = data.resample('D').agg({'High': 'max', 'Low': 'min'}).dropna()
    daily['SR_High'] = daily['High'].shift(1).rolling(lookback_days).max()
    daily['SR_Low']  = daily['Low'].shift(1).rolling(lookback_days).min()
    data['SR_High']  = daily['SR_High'].reindex(data.index, method='ffill')
    data['SR_Low']   = daily['SR_Low'].reindex(data.index, method='ffill')
    return data


# ─── 2d. India VIX (optional) ─────────────────────────────────────────────────

def load_vix(data_index):
    """Load VIX data and align to index. Returns Series or None."""
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    vix_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'vix_5min'
    )
    if not (config.USE_VIX_FILTER and os.path.exists(vix_dir)):
        return None
    vfiles = sorted(f for f in os.listdir(vix_dir) if f.endswith('.csv'))
    if not vfiles:
        return None
    vdfs = []
    for vf in vfiles:
        try:
            tmp = pd.read_csv(os.path.join(vix_dir, vf), index_col=0, parse_dates=True)
            if len(tmp) > 0:
                vdfs.append(tmp)
        except Exception:
            pass
    if not vdfs:
        return None
    vix = pd.concat(vdfs).sort_index()
    vix = vix[~vix.index.duplicated(keep='first')]
    if vix.index.tzinfo is None:
        vix.index = vix.index.tz_localize(IST)
    else:
        vix.index = vix.index.tz_convert(IST)
    return vix['Close'].reindex(data_index, method='ffill')


# ─── 3. Entry Window ──────────────────────────────────────────────────────────

_open  = dtime(9, 15)
_close = dtime(15, 30)
ENTRY_START = (dt.datetime.combine(dt.date.today(), _open) +
               dt.timedelta(minutes=config.AVOID_FIRST_MINUTES)).time()
ENTRY_END   = (dt.datetime.combine(dt.date.today(), _close) -
               dt.timedelta(minutes=config.AVOID_LAST_MINUTES)).time()
print(f"\n  Entry window : {ENTRY_START.strftime('%H:%M')} – {ENTRY_END.strftime('%H:%M')} IST")


# ─── 4. Black-Scholes Pricing ─────────────────────────────────────────────────

def bs_call(S, K, T, r, sigma):
    if T <= 0:
        return max(S - K, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0:
        return max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_price(opt_type, S, K, T, r, sigma):
    return bs_call(S, K, T, r, sigma) if opt_type == 'CALL' else bs_put(S, K, T, r, sigma)


def transaction_costs(entry_px, exit_px, lot_size):
    buy  = entry_px * lot_size
    sell = exit_px  * lot_size
    brok = config.BROKERAGE_PER_ORDER * 2
    exch = (buy + sell) * config.NSE_EXCHANGE_CHARGE_RATE
    sebi = (buy + sell) * config.SEBI_CHARGES_RATE
    gst  = (brok + exch + sebi) * config.GST_RATE
    stt  = sell * config.STT_RATE
    stmp = buy  * config.STAMP_DUTY_RATE
    return brok + exch + sebi + gst + stt + stmp


# ─── 5. Signal Functions ───────────────────────────────────────────────────────

def check_trend_momentum(data, i,
                         ema_fast_col='EMA_fast', ema_slow_col='EMA_slow',
                         adx_threshold=None):
    """
    EMA crossover + ADX + VWAP trend momentum signal.
    ema_fast_col / ema_slow_col : which indicator columns to use (default 9/21)
    adx_threshold               : override ADX threshold (default: from config)
    """
    if not config.USE_TREND_MOMENTUM:
        return None

    adx_thr = adx_threshold if adx_threshold is not None else config.MOMENTUM_ADX_THRESHOLD

    latest = data.iloc[i]
    if pd.isna(latest.get(ema_fast_col)) or pd.isna(latest.get(ema_slow_col)) or pd.isna(latest['ADX']):
        return None

    if latest['ADX'] <= adx_thr:
        return None

    if config.USE_VOLUME_FILTER:
        vol_ok = (not pd.isna(latest['Volume_MA'])) and latest['Volume_MA'] > 0
        if vol_ok and latest['Volume'] < latest['Volume_MA'] * config.VOLUME_MULTIPLIER:
            return None

    lb     = min(config.EMA_CROSSOVER_LOOKBACK, i)
    window = data.iloc[i - lb: i + 1]

    bull_x = any(
        window[ema_fast_col].iloc[j - 1] <= window[ema_slow_col].iloc[j - 1] and
        window[ema_fast_col].iloc[j]     >  window[ema_slow_col].iloc[j]
        for j in range(1, len(window))
    )
    bear_x = any(
        window[ema_fast_col].iloc[j - 1] >= window[ema_slow_col].iloc[j - 1] and
        window[ema_fast_col].iloc[j]     <  window[ema_slow_col].iloc[j]
        for j in range(1, len(window))
    )

    sig = None
    ef = latest[ema_fast_col]
    es = latest[ema_slow_col]
    if bull_x and ef > es and latest['Close'] > ef:
        sig = 'CALL'
    elif bear_x and ef < es and latest['Close'] < ef:
        sig = 'PUT'

    if sig is None:
        return None

    if config.USE_VWAP_FILTER and 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        if sig == 'CALL' and latest['Close'] < latest['VWAP']:
            return None
        if sig == 'PUT'  and latest['Close'] > latest['VWAP']:
            return None

    # MTF gates (disabled by default — v8 experiments showed over-filtering)
    if config.USE_SUPERTREND_FILTER and 'SuperTrend_15m' in data.columns:
        st = latest.get('SuperTrend_15m', np.nan)
        if not pd.isna(st):
            if sig == 'CALL' and st < 0:
                return None
            if sig == 'PUT'  and st > 0:
                return None

    if config.USE_HTF_EMA_FILTER and 'EMA_fast_15m' in data.columns:
        ef15 = latest.get('EMA_fast_15m', np.nan)
        es15 = latest.get('EMA_slow_15m', np.nan)
        if not pd.isna(ef15) and not pd.isna(es15):
            if sig == 'CALL' and ef15 < es15:
                return None
            if sig == 'PUT'  and ef15 > es15:
                return None

    if config.USE_VIX_FILTER and 'VIX' in data.columns and not pd.isna(latest.get('VIX', np.nan)):
        vix = latest['VIX']
        if vix > config.VIX_MAX or vix < config.VIX_MIN:
            return None

    # ── Signal strength score (0–3) ──────────────────────────────────────────
    # Used by v9c for dynamic lot sizing.  Each factor adds 1 point:
    #  1. ADX ≥ 35            — very strong trend (well above 25/30 entry minimum)
    #  2. EMA spread ≥ 0.04%  — notably wide fast/slow separation (top ~30%)
    #  3. VWAP distance ≥ 0.10% — clear directional price action
    #
    # Thresholds calibrated on 14-month NIFTY backtest data:
    #   EMA spread at entries: mean 0.03%, 75th pct 0.05% → 0.04% catches top ~40%
    #   VWAP distance at entries: varies 0–0.36% → 0.10% catches top ~35%
    strength = 0
    if latest['ADX'] >= 35:
        strength += 1
    ema_spread_pct = abs(ef - es) / es if es != 0 else 0.0
    if ema_spread_pct >= 0.0004:   # 0.04% of slow EMA
        strength += 1
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        vwap_dist_pct = abs(latest['Close'] - latest['VWAP']) / latest['VWAP']
        if vwap_dist_pct >= 0.001:   # 0.10% from VWAP
            strength += 1

    return {'type': sig, 'strategy': 'Trend Momentum', 'price': latest['Close'],
            'signal_strength': strength, 'adx': float(latest['ADX'])}


def check_pre_window_trend(data: pd.DataFrame, i: int, entry_start: dtime) -> str | None:
    """
    Detect if a directional trend was established BEFORE the entry window
    and has been maintained continuously since today's open.
    Returns 'CALL', 'PUT', or None.

    Conditions (all must hold):
      1. EMA_fast/slow aligned in same direction across ALL pre-window bars today
      2. VWAP aligned in same direction for >= 60% of pre-window bars
      3. ADX at bar i is higher than it was 4 bars ago (rising trend strength)
      4. No fresh EMA crossover in the last EMA_CROSSOVER_LOOKBACK bars
         (if there is one, check_trend_momentum already handles it)

    Used by vPW: if all conditions met, the ADX threshold is relaxed by 4 pts
    because a trend held for 90+ minutes is more confirmed than a fresh cross.

    Performance: O(bars_per_day) per call — uses backward day-start search
    rather than scanning the full dataset, keeping the main loop O(n).
    """
    today = data.index[i].date()

    # Walk backwards from i to find the first bar of today (O(bars/day) ≈ O(78))
    first_today = i
    while first_today > 0 and data.index[first_today - 1].date() == today:
        first_today -= 1

    # today_slice: all of today's bars up to and including bar i
    today_slice = data.iloc[first_today: i + 1]
    if len(today_slice) < 5:
        return None

    # Pre-window bars: today's bars whose time is strictly before entry_start
    pre_mask = today_slice.index.time < entry_start
    pre = today_slice[pre_mask]
    if len(pre) < 3:
        return None   # not enough pre-window history

    # Determine current EMA direction at bar i
    curr = data.iloc[i]
    if pd.isna(curr.get('EMA_fast')) or pd.isna(curr.get('EMA_slow')):
        return None
    ef, es = curr['EMA_fast'], curr['EMA_slow']

    if ef > es:
        direction = 'CALL'
        ema_pre_ok  = (pre['EMA_fast'] > pre['EMA_slow']).all()
        vwap_pre_ok = ('VWAP' not in pre.columns or
                       (pre['Close'] > pre['VWAP']).mean() >= 0.60)
    elif ef < es:
        direction = 'PUT'
        ema_pre_ok  = (pre['EMA_fast'] < pre['EMA_slow']).all()
        vwap_pre_ok = ('VWAP' not in pre.columns or
                       (pre['Close'] < pre['VWAP']).mean() >= 0.60)
    else:
        return None

    if not (ema_pre_ok and vwap_pre_ok):
        return None

    # ADX must be rising (bar i higher than 4 bars ago)
    adx_now = curr.get('ADX', float('nan'))
    adx_ref = data['ADX'].iloc[max(0, i - 4)]
    if pd.isna(adx_now) or pd.isna(adx_ref) or adx_now <= adx_ref:
        return None

    # Guard: no fresh EMA crossover in the last EMA_CROSSOVER_LOOKBACK bars
    # (if there is, check_trend_momentum already catches it as primary signal)
    lb     = min(config.EMA_CROSSOVER_LOOKBACK, i)
    window = data.iloc[i - lb: i + 1]
    ef_w = window['EMA_fast'].values
    es_w = window['EMA_slow'].values
    if direction == 'CALL':
        has_fresh_x = any(
            ef_w[j - 1] <= es_w[j - 1] and ef_w[j] > es_w[j]
            for j in range(1, len(window))
        )
    else:
        has_fresh_x = any(
            ef_w[j - 1] >= es_w[j - 1] and ef_w[j] < es_w[j]
            for j in range(1, len(window))
        )
    if has_fresh_x:
        return None  # check_trend_momentum handles fresh crosses

    return direction


def check_pre_window_momentum(data: pd.DataFrame, i: int, direction: str,
                               adx_threshold: float) -> dict | None:
    """
    Like check_trend_momentum but without the fresh EMA crossover requirement.
    Used by vPW when a pre-window trend is confirmed: the crossover already
    happened before the entry window, so we just need EMA alignment + VWAP +
    lenient ADX (threshold - 4 pts) to enter.
    """
    latest = data.iloc[i]
    if (pd.isna(latest.get('EMA_fast')) or pd.isna(latest.get('EMA_slow'))
            or pd.isna(latest.get('ADX'))):
        return None

    ef  = latest['EMA_fast']
    es  = latest['EMA_slow']
    adx = latest['ADX']

    if adx <= adx_threshold:
        return None

    # EMA aligned + price on right side of fast EMA
    if direction == 'CALL' and not (ef > es and latest['Close'] > ef):
        return None
    if direction == 'PUT'  and not (ef < es and latest['Close'] < ef):
        return None

    # VWAP filter (same logic as check_trend_momentum)
    if (config.USE_VWAP_FILTER and 'VWAP' in data.columns
            and not pd.isna(latest.get('VWAP', float('nan')))):
        if direction == 'CALL' and latest['Close'] < latest['VWAP']:
            return None
        if direction == 'PUT'  and latest['Close'] > latest['VWAP']:
            return None

    # Signal strength (same thresholds as check_trend_momentum)
    strength = 0
    if adx >= 35:
        strength += 1
    ema_spread_pct = abs(ef - es) / es if es != 0 else 0.0
    if ema_spread_pct >= 0.0004:
        strength += 1
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        vwap_dist_pct = abs(latest['Close'] - latest['VWAP']) / latest['VWAP']
        if vwap_dist_pct >= 0.001:
            strength += 1

    return {
        'type'           : direction,
        'strategy'       : 'Pre-Window Trend',
        'price'          : latest['Close'],
        'signal_strength': strength,
        'adx'            : float(adx),
    }


def check_rsi_entry(data: pd.DataFrame, i: int) -> dict | None:
    """
    v18 secondary signal: RSI-14 crosses the 50 level inside an established EMA trend.

    Logic
    -----
    When the EMA trend is already set (fast > slow for bullish, fast < slow for bearish)
    but no fresh EMA *crossover* was detected by check_trend_momentum, look for RSI-14
    crossing 50 from below (CALL) or above (PUT) within the last 3 bars.

    This catches mid-trend momentum re-entries after a brief pullback — e.g., after a
    trailing stop exit at ~75%, the market consolidates (RSI dips to 48-50), then
    resumes (RSI re-crosses 50). v9c sits out this continuation; v18 re-enters on it.

    All standard filters still apply:
      - EMA_fast/slow must be directionally aligned
      - Close must be on the correct side of EMA_fast
      - VWAP filter (same as check_trend_momentum)
      - ADX checked by caller (≥30 CALL, ≥25 PUT)
    """
    LOOKBACK = 3
    if i < LOOKBACK or 'RSI' not in data.columns:
        return None

    window = data.iloc[max(0, i - LOOKBACK + 1): i + 1]
    latest = window.iloc[-1]

    ef  = latest.get('EMA_fast', float('nan'))
    es  = latest.get('EMA_slow', float('nan'))
    adx = latest.get('ADX',      float('nan'))
    if pd.isna(ef) or pd.isna(es) or pd.isna(adx) or adx < config.MOMENTUM_ADX_THRESHOLD:
        return None

    # Determine EMA trend direction
    if ef > es:
        direction = 'CALL'
        rsi_cross = any(
            not pd.isna(window['RSI'].iloc[j - 1]) and
            not pd.isna(window['RSI'].iloc[j])     and
            window['RSI'].iloc[j - 1] <= 50 and window['RSI'].iloc[j] > 50
            for j in range(1, len(window))
        )
    elif ef < es:
        direction = 'PUT'
        rsi_cross = any(
            not pd.isna(window['RSI'].iloc[j - 1]) and
            not pd.isna(window['RSI'].iloc[j])     and
            window['RSI'].iloc[j - 1] >= 50 and window['RSI'].iloc[j] < 50
            for j in range(1, len(window))
        )
    else:
        return None

    if not rsi_cross:
        return None

    # Price must be on the correct side of EMA_fast (trend confirmation)
    close = latest['Close']
    if direction == 'CALL' and close <= ef:
        return None
    if direction == 'PUT'  and close >= ef:
        return None

    # VWAP filter (same as check_trend_momentum)
    if config.USE_VWAP_FILTER and 'VWAP' in data.columns:
        vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(vwap):
            if direction == 'CALL' and close < vwap:
                return None
            if direction == 'PUT'  and close > vwap:
                return None

    # Signal strength score (same scoring as check_trend_momentum)
    strength = 0
    if adx >= 35:
        strength += 1
    ema_spread_pct = abs(ef - es) / es if es != 0 else 0.0
    if ema_spread_pct >= 0.0004:
        strength += 1
    if 'VWAP' in data.columns:
        vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(vwap):
            vwap_dist_pct = abs(close - vwap) / vwap
            if vwap_dist_pct >= 0.001:
                strength += 1

    return {
        'type'            : direction,
        'strategy'        : 'RSI-50 Crossover',
        'price'           : close,
        'signal_strength' : strength,
        'adx'             : float(adx),
    }


def check_supertrend_signal(data, i, call_adx_min=30, put_adx_min=25):
    """
    Supertrend direction flip + ADX + VWAP signal.

    Replaces EMA 9/21 crossover with Supertrend flip as the trend trigger.
    Uses same ADX threshold and VWAP filter as vX.

    CALL: ST_dir flipped from -1 → +1 within last 3 bars  AND  ADX >= call_adx_min
          AND Close > VWAP
    PUT:  ST_dir flipped from +1 → -1 within last 3 bars  AND  ADX >= put_adx_min
          AND Close < VWAP
    """
    if 'ST_dir' not in data.columns:
        return None

    latest = data.iloc[i]
    if pd.isna(latest.get('ST_dir')) or pd.isna(latest.get('ADX')):
        return None

    adx = latest['ADX']
    lb  = min(3, i)
    window = data.iloc[i - lb: i + 1]

    # Detect flip in lookback window
    bull_flip = any(
        window['ST_dir'].iloc[j - 1] == -1 and window['ST_dir'].iloc[j] == 1
        for j in range(1, len(window))
    )
    bear_flip = any(
        window['ST_dir'].iloc[j - 1] == 1 and window['ST_dir'].iloc[j] == -1
        for j in range(1, len(window))
    )

    sig = None
    if bull_flip and latest['ST_dir'] == 1 and adx >= call_adx_min:
        sig = 'CALL'
    elif bear_flip and latest['ST_dir'] == -1 and adx >= put_adx_min:
        sig = 'PUT'

    if sig is None:
        return None

    # VWAP filter
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        if sig == 'CALL' and latest['Close'] < latest['VWAP']:
            return None
        if sig == 'PUT'  and latest['Close'] > latest['VWAP']:
            return None

    # Signal strength (same scoring as check_trend_momentum)
    strength = 0
    if adx >= 35:
        strength += 1
    st_dist_pct = abs(latest['Close'] - latest['ST_line']) / latest['ST_line'] if latest['ST_line'] != 0 else 0.0
    if st_dist_pct >= 0.0004:
        strength += 1
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        vwap_dist_pct = abs(latest['Close'] - latest['VWAP']) / latest['VWAP']
        if vwap_dist_pct >= 0.001:
            strength += 1

    lots = 2 if strength >= 2 else 1

    return {
        'type'           : sig,
        'strategy'       : 'Supertrend',
        'price'          : latest['Close'],
        'signal_strength': strength,
        'lots'           : lots,
        'path'           : 'vST',
    }


def check_di_momentum(data, i, call_adx_min=30, put_adx_min=25):
    """
    vXDI — DI+/DI- crossover replaces EMA 9/21 as the trend trigger.

    +DI/-DI react 3-5 bars faster than EMA 9/21 crossover on the same data
    because they are computed directly from each bar's directional movement
    (no smoothing lag of a moving average of a moving average).

    CALL: +DI crossed above -DI within last 3 bars  AND  ADX >= call_adx_min
          AND Close > VWAP  AND  DI_plus > DI_minus (currently)
    PUT:  -DI crossed above +DI within last 3 bars  AND  ADX >= put_adx_min
          AND Close < VWAP  AND  DI_minus > DI_plus (currently)

    Strength score (0-3):
      1. ADX >= 35
      2. DI spread |DI+ - DI-| >= 5pts  (was: EMA spread >= 0.04%)
      3. VWAP distance >= 0.10%
    """
    if 'DI_plus' not in data.columns or 'DI_minus' not in data.columns:
        return None

    latest = data.iloc[i]
    if any(pd.isna(latest.get(c)) for c in ['DI_plus', 'DI_minus', 'ADX']):
        return None

    adx = latest['ADX']
    lb  = min(config.EMA_CROSSOVER_LOOKBACK, i)
    window   = data.iloc[i - lb: i + 1]
    dip_w    = window['DI_plus'].values
    dim_w    = window['DI_minus'].values

    bull_di = any(
        dip_w[j-1] <= dim_w[j-1] and dip_w[j] > dim_w[j]
        for j in range(1, len(dip_w))
    )
    bear_di = any(
        dip_w[j-1] >= dim_w[j-1] and dip_w[j] < dim_w[j]
        for j in range(1, len(dip_w))
    )

    sig = None
    if bull_di and latest['DI_plus'] > latest['DI_minus'] and adx >= call_adx_min:
        sig = 'CALL'
    elif bear_di and latest['DI_minus'] > latest['DI_plus'] and adx >= put_adx_min:
        sig = 'PUT'

    if sig is None:
        return None

    if config.USE_VWAP_FILTER and 'VWAP' in data.columns:
        vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(vwap):
            if sig == 'CALL' and latest['Close'] < vwap:
                return None
            if sig == 'PUT'  and latest['Close'] > vwap:
                return None

    # Signal strength score
    strength = 0
    if adx >= 35:
        strength += 1
    if abs(latest['DI_plus'] - latest['DI_minus']) >= 5.0:
        strength += 1
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        if abs(latest['Close'] - latest['VWAP']) / latest['VWAP'] >= 0.001:
            strength += 1

    return {
        'type'           : sig,
        'strategy'       : 'DI Momentum',
        'price'          : latest['Close'],
        'signal_strength': strength,
        'lots'           : 2 if strength >= 2 else 1,
        'path'           : 'vXDI',
    }


def check_path_e_signal(data, i, adx_min=None, di_bars=None):
    """
    vXF / Path E: HTF Trend Continuation (no fresh crossover required).

    Catches slow-grind trend days where ADX builds gradually but no fresh
    EMA or DI crossover fires within the entry window (e.g. Apr 6 2026).

    Conditions:
      - DI+ > DI- (or DI- > DI+) for di_bars consecutive bars
      - ADX >= adx_min (lower bar than vX — grind days build slowly)
      - 15m SuperTrend agrees (ST_15m column)
      - Price on correct side of VWAP
      - Only fires once per day (controlled by the caller via `_path_e_last_day` flag)

    Strength score (0-3):
      1. ADX >= 30
      2. DI spread >= 10pts (sustained dominance)
      3. VWAP distance >= 0.10%
    """
    if adx_min is None:
        adx_min = config.PATH_E_ADX_MIN
    if di_bars is None:
        di_bars = config.PATH_E_DI_BARS

    if 'DI_plus' not in data.columns or 'DI_minus' not in data.columns:
        return None
    if i < di_bars:
        return None

    latest = data.iloc[i]
    if any(pd.isna(latest.get(c)) for c in ['DI_plus', 'DI_minus', 'ADX']):
        return None

    adx = float(latest['ADX'])
    if adx < adx_min:
        return None

    dip = float(latest['DI_plus'])
    dim = float(latest['DI_minus'])
    if dip == dim:
        return None

    # DI sustained for di_bars consecutive bars
    recent = data.iloc[i - di_bars + 1: i + 1]
    sig = None
    if dip > dim and (recent['DI_plus'] > recent['DI_minus']).all():
        sig = 'CALL'
    elif dim > dip and (recent['DI_minus'] > recent['DI_plus']).all():
        sig = 'PUT'

    if sig is None:
        return None

    # DI spread gate: require meaningful directional dominance (not just barely positive)
    if abs(dip - dim) < 12:
        return None

    # 5m SuperTrend must agree with signal direction
    if 'ST_dir' in data.columns:
        st5 = latest.get('ST_dir', 0)
        if not pd.isna(st5):
            if sig == 'CALL' and int(st5) != 1:
                return None
            if sig == 'PUT'  and int(st5) != -1:
                return None

    # 15m SuperTrend alignment (if column exists — backtest computes it)
    if 'ST_15m' in data.columns:
        st15 = latest.get('ST_15m', 0)
        if not pd.isna(st15):
            if sig == 'CALL' and int(st15) != 1:
                return None
            if sig == 'PUT'  and int(st15) != -1:
                return None

    # VWAP filter
    if config.USE_VWAP_FILTER and 'VWAP' in data.columns:
        vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(vwap) and vwap > 0:
            if sig == 'CALL' and latest['Close'] < vwap:
                return None
            if sig == 'PUT'  and latest['Close'] > vwap:
                return None

    # Strength scoring
    strength = 0
    if adx >= 30:
        strength += 1
    if abs(dip - dim) >= 10:
        strength += 1
    if 'VWAP' in data.columns:
        v = latest.get('VWAP', float('nan'))
        if not pd.isna(v) and v > 0:
            if abs(latest['Close'] - v) / v >= 0.001:
                strength += 1

    return {
        'type'           : sig,
        'strategy'       : 'Path E Trend Continuation',
        'price'          : latest['Close'],
        'signal_strength': strength,
        'lots'           : 2 if strength >= 2 else 1,
        'path'           : 'E',
    }


def check_ensemble_signal(data, i, call_adx_min=30, put_adx_min=25):
    """
    vXST Ensemble: EMA 9/21 crossover (primary) + Supertrend(7,2.5) direction (secondary).

    Rationale
    ---------
    vX uses EMA crossover alone → lag: entry is 1-3 bars AFTER trend starts.
    vST uses Supertrend flip alone → noise: 145 trades vs vX's 63; too many false signals.
    vXST combines both:
      - EMA alignment is a MANDATORY GATE for any entry.
      - Supertrend flip can trigger EARLY entries before the EMA crossover fires.
      - Both firing together gives maximum conviction → 2 lots.

    Trigger logic
    -------------
    CALL entry fires if:
      A) Fresh EMA crossover (last N bars) + EMA aligned (fast > slow, close > fast)
         + Supertrend currently bullish (ST_dir == +1)
      B) Fresh ST bullish flip (last 3 bars) + EMA already directionally aligned
         (fast > slow, close > fast) — even without a fresh EMA crossover

    PUT entry fires: mirror logic with bear side.

    Blocked vs vX  : EMA cross when ST direction opposes → BLOCKED (prevents divergence)
    Blocked vs vST : ST flip without EMA alignment         → BLOCKED (prevents whipsaws)

    Lot sizing
    ----------
    Both fresh EMA cross AND fresh ST flip match direction → 2 lots (maximum conviction)
    Only one trigger fires (other provides alignment only)  → 1 lot

    ADX + VWAP filters: identical to vX per-instrument thresholds.
    """
    if 'ST_dir' not in data.columns or i < 2:
        return None

    latest = data.iloc[i]
    ef     = latest.get('EMA_fast', float('nan'))
    es     = latest.get('EMA_slow', float('nan'))
    adx    = latest.get('ADX',      float('nan'))
    st_dir = latest.get('ST_dir',   float('nan'))
    close  = latest['Close']

    if any(pd.isna(v) for v in [ef, es, adx, st_dir]):
        return None

    # ── EMA crossover detection ───────────────────────────────────────────────
    lb     = min(config.EMA_CROSSOVER_LOOKBACK, i)
    window = data.iloc[i - lb: i + 1]
    ef_w   = window['EMA_fast'].values
    es_w   = window['EMA_slow'].values
    ema_bull_cross = any(ef_w[j-1] <= es_w[j-1] and ef_w[j] > es_w[j]
                         for j in range(1, len(window)))
    ema_bear_cross = any(ef_w[j-1] >= es_w[j-1] and ef_w[j] < es_w[j]
                         for j in range(1, len(window)))

    # EMA direction alignment (price on right side of fast EMA)
    ema_bull_aligned = bool(ef > es and close > ef)
    ema_bear_aligned = bool(ef < es and close < ef)

    # ── Supertrend flip detection (last 3 bars) ───────────────────────────────
    st_lb     = min(3, i)
    st_window = data.iloc[i - st_lb: i + 1]
    st_bull_flip = any(
        st_window['ST_dir'].iloc[j-1] == -1 and st_window['ST_dir'].iloc[j] == 1
        for j in range(1, len(st_window))
    )
    st_bear_flip = any(
        st_window['ST_dir'].iloc[j-1] == 1  and st_window['ST_dir'].iloc[j] == -1
        for j in range(1, len(st_window))
    )

    st_bull_dir = (int(st_dir) == 1)
    st_bear_dir = (int(st_dir) == -1)

    # ── Determine entry signal ─────────────────────────────────────────────────
    # CALL: (fresh EMA cross + EMA aligned + ST bullish)
    #       OR (fresh ST bull flip + EMA bull aligned — early entry before EMA cross)
    call_via_ema = ema_bull_cross and ema_bull_aligned and st_bull_dir
    call_via_st  = st_bull_flip   and st_bull_dir      and ema_bull_aligned

    # PUT: mirror
    put_via_ema  = ema_bear_cross and ema_bear_aligned and st_bear_dir
    put_via_st   = st_bear_flip   and st_bear_dir      and ema_bear_aligned

    sig      = None
    ens_lots = 1   # default: 1 lot

    if call_via_ema or call_via_st:
        sig = 'CALL'
        if call_via_ema and call_via_st:   # both triggers agree → 2 lots
            ens_lots = 2
    elif put_via_ema or put_via_st:
        sig = 'PUT'
        if put_via_ema and put_via_st:
            ens_lots = 2

    if sig is None:
        return None

    # ── ADX filter ────────────────────────────────────────────────────────────
    if sig == 'CALL' and adx < call_adx_min:
        return None
    if sig == 'PUT'  and adx < put_adx_min:
        return None

    # ── VWAP filter ───────────────────────────────────────────────────────────
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        vwap = latest['VWAP']
        if sig == 'CALL' and close < vwap:
            return None
        if sig == 'PUT'  and close > vwap:
            return None

    # ── Signal strength (0-3) for reporting ───────────────────────────────────
    strength = 0
    if adx >= 35:
        strength += 1
    ema_spread_pct = abs(ef - es) / es if es != 0 else 0.0
    if ema_spread_pct >= 0.0004:
        strength += 1
    if 'VWAP' in data.columns and not pd.isna(latest.get('VWAP', float('nan'))):
        vwap_dist_pct = abs(close - latest['VWAP']) / latest['VWAP']
        if vwap_dist_pct >= 0.001:
            strength += 1

    # Determine path for diagnostics
    if sig == 'CALL':
        path = ('ema+st' if call_via_ema and call_via_st else
                'ema'    if call_via_ema else 'st-early')
    else:
        path = ('ema+st' if put_via_ema and put_via_st else
                'ema'    if put_via_ema  else 'st-early')

    return {
        'type'           : sig,
        'strategy'       : 'EMA+ST Ensemble',
        'price'          : close,
        'signal_strength': strength,
        'ens_lots'       : ens_lots,   # 1 or 2 (ensemble lot logic)
        'path'           : path,
    }


def check_vwap_breach(data, i, call_adx_min=30, put_adx_min=25):
    """
    Secondary signal: EMA trend already established + price just breaches VWAP.

    Fires when check_trend_momentum misses because:
      (a) EMA crossover happened but VWAP hadn't been breached yet (VWAP filter blocked it),
          and price subsequently crosses VWAP without a new EMA crossover, OR
      (b) EMA crossover happened before the entry window (e.g. SENSEX EMA trends overnight,
          crossover never fires during 12:00+ window — but VWAP breach at 12:15 is valid).

    PUT signal conditions:
      - EMA_fast < EMA_slow (bearish trend established)
      - No fresh EMA crossdown in last EMA_CROSSOVER_LOOKBACK bars
        (if there were, check_trend_momentum would have caught it)
      - Current bar: Close < VWAP  AND  previous bar: Close >= VWAP  (fresh breach)
      - Close < EMA_fast  (price below fast average — confirms direction)
      - ADX >= put_adx_min

    CALL signal conditions: mirror of PUT (EMA bullish, price just above VWAP).
    """
    if 'VWAP' not in data.columns or i < 2:
        return None

    latest    = data.iloc[i]
    prev      = data.iloc[i - 1]
    ef        = latest['EMA_fast'];  es   = latest['EMA_slow']
    adx       = latest['ADX'];       px   = latest['Close']
    vwap      = latest['VWAP'];      prev_px   = prev['Close']
    prev_vwap = prev['VWAP']

    if any(pd.isna(v) for v in [ef, es, adx, vwap, prev_vwap]):
        return None

    # Guard: no fresh EMA crossover in lookback window
    # (if there is, check_trend_momentum already handles it)
    lb     = min(config.EMA_CROSSOVER_LOOKBACK, i)
    window = data.iloc[i - lb: i + 1]
    fresh_cross_dn = any(
        window['EMA_fast'].iloc[j - 1] >= window['EMA_slow'].iloc[j - 1] and
        window['EMA_fast'].iloc[j]     <  window['EMA_slow'].iloc[j]
        for j in range(1, len(window))
    )
    fresh_cross_up = any(
        window['EMA_fast'].iloc[j - 1] <= window['EMA_slow'].iloc[j - 1] and
        window['EMA_fast'].iloc[j]     >  window['EMA_slow'].iloc[j]
        for j in range(1, len(window))
    )

    sig = None
    # ── PUT: EMA bearish + price just crossed below VWAP ──────────────────────
    if (ef < es                          # trend established bearish
            and not fresh_cross_dn       # not a crossover bar (primary would catch it)
            and px < vwap                # now below VWAP
            and prev_px >= prev_vwap     # was at/above VWAP previous bar
            and px < ef                  # price below fast EMA (direction confirmed)
            and adx >= put_adx_min):     # trend strong enough
        sig = 'PUT'

    # ── CALL: EMA bullish + price just crossed above VWAP ─────────────────────
    elif (ef > es
              and not fresh_cross_up
              and px > vwap
              and prev_px <= prev_vwap
              and px > ef               # price above fast EMA
              and adx >= call_adx_min):
        sig = 'CALL'

    if sig is None:
        return None

    # Signal strength (same scoring as check_trend_momentum)
    strength = 0
    if adx >= 35:
        strength += 1
    ema_spread_pct = abs(ef - es) / es if es != 0 else 0.0
    if ema_spread_pct >= 0.0004:
        strength += 1
    vwap_dist_pct = abs(px - vwap) / vwap
    if vwap_dist_pct >= 0.001:
        strength += 1

    return {'type': sig, 'strategy': 'VWAP Breach', 'price': px,
            'signal_strength': strength, 'adx': float(adx)}


def check_path_b_signal(data: pd.DataFrame, i: int,
                     mr_high: float, mr_low: float) -> dict | None:
    """
    vB — Path B: Morning Range Breakout (backtest variant).

    Fires when bar i's Close breaks above MR_high or below MR_low with
    momentum confirmation: ADX ≥ PATH_B_ADX_MIN + VWAP + 15m SuperTrend.

    mr_high / mr_low: pre-computed per-day morning range (09:15–10:55).
    No OI snap or PCR gate here (no historical OI available in backtest).
    """
    latest = data.iloc[i]
    _px    = latest['Close']
    _adx   = latest.get('ADX', float('nan'))
    if pd.isna(_adx) or pd.isna(_px):
        return None

    buf         = config.PATH_B_BUFFER
    _call_break = _px > mr_high * (1.0 + buf)
    _put_break  = _px < mr_low  * (1.0 - buf)

    if not _call_break and not _put_break:
        return None

    sig_type = 'CALL' if _call_break else 'PUT'

    # ADX filter (uniform — no CALL/PUT asymmetry for 12-DTE)
    if _adx < config.PATH_B_ADX_MIN:
        return None

    # VWAP filter
    if config.USE_VWAP_FILTER and 'VWAP' in data.columns:
        _vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(_vwap) and _vwap > 0:
            if sig_type == 'CALL' and _px < _vwap:
                return None
            if sig_type == 'PUT'  and _px > _vwap:
                return None

    # 15m SuperTrend alignment
    if config.PATH_B_HTF_REQUIRED and 'ST_15m' in data.columns:
        _st15 = latest.get('ST_15m', float('nan'))
        if not pd.isna(_st15):
            if sig_type == 'CALL' and _st15 != 1:
                return None
            if sig_type == 'PUT'  and _st15 != -1:
                return None

    # Strength scoring
    strength = 0
    if _adx >= 35:
        strength += 1
    _break_pct = ((_px - mr_high) / mr_high if sig_type == 'CALL'
                  else (mr_low - _px) / mr_low)
    if _break_pct > 0.002:
        strength += 1
    if 'VWAP' in data.columns:
        _vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(_vwap) and _vwap > 0 and abs(_px - _vwap) / _vwap >= 0.001:
            strength += 1

    return {
        'type'           : sig_type,
        'strategy'       : 'MRB',
        'price'          : _px,
        'signal_strength': strength,
        'adx'            : float(_adx),
        'lots'           : 2 if strength >= 2 else 1,
    }


def check_vb1_signal(data: pd.DataFrame, i: int) -> dict | None:
    """
    vB1 — VWAP Pullback Re-entry.

    Fires when:
      - EMA trend is established (EMA_fast > EMA_slow for CALL; reversed for PUT)
      - ADX >= 25
      - In the last 3 bars, price pulled back to within 0.3% of VWAP
        (i.e., at least one bar's Close was in the VWAP±0.3% zone)
      - Current bar: resuming the trend away from VWAP
        CALL: Close > VWAP AND Close > Open (bullish bar)
        PUT:  Close < VWAP AND Close < Open (bearish bar)
      - 15m SuperTrend agrees (when available)
    Window: 11:00–14:00 (enforced by caller).
    """
    if i < 4:
        return None

    latest   = data.iloc[i]
    _px      = latest['Close']
    _open    = latest['Open']
    _adx     = latest.get('ADX', float('nan'))
    _ef      = latest.get('EMA_fast', float('nan'))
    _es_col  = latest.get('EMA_slow', float('nan'))
    _vwap    = latest.get('VWAP', float('nan'))

    if any(pd.isna(v) for v in [_px, _adx, _ef, _es_col, _vwap]) or _vwap <= 0:
        return None
    if _adx < 25:
        return None

    # Determine trend direction
    if _ef > _es_col:
        sig_type = 'CALL'
    elif _ef < _es_col:
        sig_type = 'PUT'
    else:
        return None

    # Current bar must resume trend away from VWAP
    if sig_type == 'CALL':
        if not (_px > _vwap and _px > _open):
            return None
    else:
        if not (_px < _vwap and _px < _open):
            return None

    # Check that at least one of the last 3 bars pulled back to VWAP zone (±0.3%)
    pullback_zone = 0.003
    touched_vwap = False
    for j in range(1, 4):
        if i - j < 0:
            break
        prev_close = data['Close'].iloc[i - j]
        prev_vwap  = data['VWAP'].iloc[i - j] if 'VWAP' in data.columns else float('nan')
        if pd.isna(prev_vwap) or prev_vwap <= 0:
            continue
        if abs(prev_close - prev_vwap) / prev_vwap <= pullback_zone:
            touched_vwap = True
            break

    if not touched_vwap:
        return None

    # 15m SuperTrend alignment (optional — skip if column missing)
    if 'ST_15m' in data.columns:
        _st15 = latest.get('ST_15m', float('nan'))
        if not pd.isna(_st15):
            if sig_type == 'CALL' and _st15 != 1:
                return None
            if sig_type == 'PUT' and _st15 != -1:
                return None

    # Strength scoring
    strength = 0
    if _adx >= 35:
        strength += 1
    vwap_dist = abs(_px - _vwap) / _vwap
    if vwap_dist >= 0.001:
        strength += 1
    if abs(_px - _open) / _open >= 0.001:   # meaningful candle body
        strength += 1

    return {
        'type'           : sig_type,
        'strategy'       : 'vB1_VWAP_PULL',
        'price'          : _px,
        'signal_strength': strength,
        'adx'            : float(_adx),
        'lots'           : 2 if strength >= 2 else 1,
    }


def check_vb2_signal(data: pd.DataFrame, i: int,
                     mini_high: float, mini_low: float) -> dict | None:
    """
    vB2 — 30-minute Micro Range Breakout.

    Mini-range = High/Low of the first 3 × 5-min bars after 11:00
    (i.e., 11:00, 11:05, 11:10 bars, computed once per day).
    Entry fires when price breaks above mini_high or below mini_low.

    Filters: ADX >= 28, VWAP, 15m SuperTrend.
    Window: 11:15–14:00 (enforced by caller; range needs 3 bars to form first).
    """
    latest = data.iloc[i]
    _px    = latest['Close']
    _adx   = latest.get('ADX', float('nan'))

    if pd.isna(_adx) or pd.isna(_px):
        return None

    buf         = 0.0005   # 0.05% buffer (tighter than MRB — smaller range)
    _call_break = _px > mini_high * (1.0 + buf)
    _put_break  = _px < mini_low  * (1.0 - buf)

    if not _call_break and not _put_break:
        return None

    sig_type = 'CALL' if _call_break else 'PUT'

    if _adx < 28:
        return None

    # VWAP filter
    if 'VWAP' in data.columns:
        _vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(_vwap) and _vwap > 0:
            if sig_type == 'CALL' and _px < _vwap:
                return None
            if sig_type == 'PUT' and _px > _vwap:
                return None

    # 15m SuperTrend alignment
    if 'ST_15m' in data.columns:
        _st15 = latest.get('ST_15m', float('nan'))
        if not pd.isna(_st15):
            if sig_type == 'CALL' and _st15 != 1:
                return None
            if sig_type == 'PUT' and _st15 != -1:
                return None

    # Strength scoring
    strength = 0
    if _adx >= 35:
        strength += 1
    _break_pct = ((_px - mini_high) / mini_high if sig_type == 'CALL'
                  else (mini_low - _px) / mini_low)
    if _break_pct > 0.001:
        strength += 1
    if 'VWAP' in data.columns:
        _vwap = latest.get('VWAP', float('nan'))
        if not pd.isna(_vwap) and _vwap > 0 and abs(_px - _vwap) / _vwap >= 0.001:
            strength += 1

    return {
        'type'           : sig_type,
        'strategy'       : 'vB2_MICRO_RANGE',
        'price'          : _px,
        'signal_strength': strength,
        'adx'            : float(_adx),
        'lots'           : 2 if strength >= 2 else 1,
    }


def check_sr_breakout(data, i):
    """
    Strategy C: 5-8 day S/R breakout with volume confirmation.

    Entry logic:
    - CALL: Close breaks above SR_High with volume > 1.5× 20-bar MA
    - PUT : Close breaks below SR_Low  with volume > 1.5× 20-bar MA

    The S/R levels come from the rolling high/low of the previous 6 trading
    days (computed by add_rolling_sr — no look-ahead bias).
    """
    latest = data.iloc[i]

    sr_high = latest.get('SR_High', np.nan)
    sr_low  = latest.get('SR_Low',  np.nan)

    if pd.isna(sr_high) or pd.isna(sr_low):
        return None

    # Volume spike required for breakout conviction
    vol_ma  = latest.get('Volume_MA', np.nan)
    vol     = latest.get('Volume', 0)
    vol_ok  = (not pd.isna(vol_ma)) and vol_ma > 0 and vol >= vol_ma * 1.5

    # Index symbols often have 0 volume — skip volume gate when unavailable
    if (not pd.isna(vol_ma)) and vol_ma == 0:
        vol_ok = True   # auto-pass when no volume data

    close = latest['Close']
    sig   = None

    if close > sr_high and vol_ok:
        sig = 'CALL'
    elif close < sr_low and vol_ok:
        sig = 'PUT'

    if sig is None:
        return None

    # ADX confirms trend is active (don't trade range breakouts in low-ADX env)
    if not pd.isna(latest['ADX']) and latest['ADX'] <= config.MOMENTUM_ADX_THRESHOLD:
        return None

    return {'type': sig, 'strategy': 'SR Breakout', 'price': close}


# ─── 5b. vG: Greeks-Informed Entry Quality Filters ───────────────────────────────

def check_vg_filters(data: pd.DataFrame, i: int,
                     instrument: str, signal_type: str) -> tuple:
    """
    vG (Greeks-Informed) entry quality gate — applied AFTER all vX conditions pass.

    Returns (pass_bool, reason_str).  All four filters must pass for a vG entry.

    The filters translate insights from options-market dynamics into rules that
    are computable from OHLCV data alone (no live option chain needed):

    Filter 1 — HV Rank (3-93%): volatility regime awareness
        HV_rank < 0.03 → extreme low-vol outlier: underlying nearly frozen,
            options won't deliver the required 130% move → skip.
        HV_rank > 0.93 → extreme high-vol outlier: options are very expensive
            (IV spike already priced in) AND mean reversion dominates → skip.
        Sweet spot (3-93%): covers normal market conditions including intraday
            vol expansions.  Only the true extremes (crisis calm / vol spike)
            are blocked.  Calibrated to match the empirical HV_rank distribution
            at vX signal bars: P50=0.10, P95=0.42 — thresholds sit at the tails.

    Filter 2 — ATR Expansion (≥ 0.75): active momentum required
        ATR_ratio = ATR14 / 20-day-avg-ATR.  A reading < 0.75 means current
        intrabar range is meaningfully below the 20-day norm — compressing.
        In this regime, EMA crossovers frequently stall before reaching target.
        v11 uses 0.70 (original); vG raises to 0.75 (slightly tighter without
        over-filtering — calibrated to signal-bar P25=0.756).

    Filter 3 — RSI Quality Gate: no exhausted entries
        Buying a CALL when RSI > 78 means momentum has already run hard;
        the option is expensive at a point where a pullback is probable.
        Buying a PUT when RSI < 22 is the mirror case.
        These thresholds are softer than reversal_guard (which uses 55/45)
        — they only block extreme exhaustion, not normal momentum.

    Filter 4 — Round-Number Clearance: proxy for OI concentration walls
        Option writers cluster their strikes at round numbers (e.g. NIFTY
        22500, 22550; BANKNIFTY 50000, 50100; SENSEX 74000, 74200).
        When price hugs the ceiling just below a round number (for a CALL)
        or the floor just above one (for a PUT), the position is fighting
        the gamma of those OI walls — the probability of being pinned or
        reversed at that level is elevated.
        Threshold: skip if price is within 0.08% of the nearest wall in
        the adverse direction.  Calibrated from OI zone analysis: BREAK_PCT
        (just-broke) = 0.08% — same distance used to flag gamma-squeeze entries.
    """
    latest = data.iloc[i]

    # ── Filter 1: HV Rank ─────────────────────────────────────────────────────
    hv_rank = latest.get('HV_rank', float('nan'))
    if not pd.isna(hv_rank):
        if hv_rank < 0.03:
            return False, f"HV rank extreme low ({hv_rank:.2f}) — frozen-vol regime, options won't move"
        if hv_rank > 0.93:
            return False, f"HV rank extreme high ({hv_rank:.2f}) — vol spike, mean-reversion risk"

    # ── Filter 2: ATR Expansion ───────────────────────────────────────────────
    atr_r = latest.get('ATR_ratio', float('nan'))
    if not pd.isna(atr_r) and atr_r < 0.75:
        return False, f"ATR contracting ({atr_r:.2f} < 0.75) — momentum compressing"

    # ── Filter 3: RSI Quality Gate ────────────────────────────────────────────
    rsi = latest.get('RSI', float('nan'))
    if not pd.isna(rsi):
        if signal_type == 'CALL' and rsi > 78:
            return False, f"RSI overbought ({rsi:.1f} > 78) — CALL entry is late"
        if signal_type == 'PUT' and rsi < 22:
            return False, f"RSI oversold ({rsi:.1f} < 22) — PUT entry is late"

    # ── Filter 4: Round-Number Clearance (OI wall proxy) ─────────────────────
    step_map   = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 200}
    step       = step_map.get(instrument, 50)
    price      = latest['Close']

    upper_wall = math.ceil(price / step) * step    # nearest resistance above
    lower_wall = math.floor(price / step) * step   # nearest support below

    # Handle case where price lands exactly on a round number
    if upper_wall == lower_wall:
        upper_wall += step

    dist_to_upper = (upper_wall - price) / price  # fraction below resistance
    dist_to_lower = (price - lower_wall) / price  # fraction above support

    if signal_type == 'CALL' and dist_to_upper < 0.0008:
        return False, (f"Near round-number resistance {upper_wall:.0f} "
                       f"({dist_to_upper*100:.2f}% away) — OI wall risk")
    if signal_type == 'PUT' and dist_to_lower < 0.0008:
        return False, (f"Near round-number support {lower_wall:.0f} "
                       f"({dist_to_lower*100:.2f}% away) — OI wall risk")

    return True, ""


# ─── 6. Backtest Engine ─────────────────────────────────────────────────────────
#
# variant='v7' : original 80%/40% config
# variant='A'  : 200% target, exit at 150% floor if trailing fires before 200%
# variant='B'  : partial exit — 50% closed at 80%, remaining 50% runs to 200%
# variant='C'  : S/R breakout signal (same A-style targets for comparability)

def run_backtest(data: pd.DataFrame, instrument: str, variant: str = 'v7') -> tuple:
    """
    Run the backtest for one instrument.

    Returns (trades_df, equity_df, final_capital).
    """
    inst_cfg = config.INSTRUMENTS[instrument]
    lot_size = inst_cfg['lot_size']
    strike_gap = inst_cfg['strike_gap']
    capital_alloc = inst_cfg['capital']   # per-instrument capital

    # ── Variant-specific parameters ───────────────────────────────────────────
    if variant == 'v7':
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None    # no floor — plain trailing stop
        partial_exit    = False

    elif variant == 'A':
        # 200% target; if price reverses after 150% but before 200%, exit at 150%
        stop_pct        = 0.40
        target_pct      = 2.00
        trail_act       = 1.50   # trailing activates at 150%
        trail_dist      = 0.30   # 30% trail distance (wider for bigger target)
        floor_pct       = 1.50   # minimum exit at 150% once activated
        partial_exit    = False

    elif variant in ('B', 'C'):
        # Partial exit at 80% (half lot), remaining targets 200%
        stop_pct        = 0.40
        target_pct      = 2.00
        trail_act       = 1.50
        trail_dist      = 0.30
        floor_pct       = 1.50
        partial_exit    = True   # close 50% at 80%

    elif variant == 'D':
        # User-specified: EMA 6/12, ADX>18, Stop 50%, Target 180%
        # Trailing: activates at 100% (half of 180% target), 30% distance
        stop_pct        = 0.50
        target_pct      = 1.80
        trail_act       = 1.00   # trail kicks in after 100% gain
        trail_dist      = 0.30
        floor_pct       = None
        partial_exit    = False

    elif variant == 'E':
        # ── Monday + Tuesday ONLY ──────────────────────────────────────────────
        # Hypothesis: entries on Mon/Tue always land in a true 2-day expiry
        # (next Thursday is 2-3 days away), maximising gamma.
        # On Wed-Fri the "2-day" option actually has 6-8 days left (lower gamma).
        # NOTE: Backtest data shows Tuesday is the worst day (31% WR), so this
        # experiment is expected to prove the gamma theory wrong in practice.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v9':
        # ── Smart Loss Avoidance (derived from 58-trade v7 analysis) ──────────
        # Pattern analysis found two dominant loss drivers:
        #   1. Tuesday entries: 31.2% WR, -₹3,472 net (11 of 16 trades are losses)
        #   2. CALL entries with ADX 25–29: 10 of 14 stops are CALLs; weak-trend
        #      CALLs whipsaw and rarely recover before EOD force-close.
        # Filters applied (same base params as v7):
        #   - Skip Tuesday (weekday 1) — biggest single gain
        #   - CALL signals require ADX ≥ 30 (vs standard 25 for PUTs)
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v9b':
        # ── v9 + 2 Lots + No Daily Trade Cap ──────────────────────────────────
        # Removes the MAX_TRADES_PER_DAY ceiling so the strategy re-enters as
        # many times as the signal permits on any given day. Lot size doubled to
        # 2× (50 shares / 2 NIFTY lots) to capture more P&L on strong trend days
        # like today (2026-03-02) where 3 valid re-entries existed.
        #
        # Rationale for removal:
        #   The 2-trade cap was a conservative filter from early testing when WR
        #   was ~47% (v7). At v9's 64% WR and high-ADX entry gating, additional
        #   same-day re-entries are more likely to be genuine trend continuations
        #   than noise. The CALL ADX≥30 and Tuesday-skip filters already act as
        #   quality gates; the daily count cap is an extra layer that simply
        #   leaves money on the table on trending days.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v9c':
        # ── v9b + Dynamic Position Sizing (scale up on high-conviction signals) ─
        # Same filters as v9b (skip Tuesday, CALL ADX≥30, no daily cap).
        # Lot size scales up when multiple confluence factors are aligned:
        #
        # signal_strength score (0–3) — each factor adds 1 point:
        #  1. ADX ≥ 35            : very strong trend
        #  2. EMA spread ≥ 0.5%   : wide fast/slow separation = strong momentum
        #  3. VWAP distance ≥ 0.3% : clearly directional price action
        #
        # Score ≥ 2 → 3 lots  (high conviction — take the bigger bet)
        # Score  < 2 → 2 lots  (normal signal  — same size as v9b)
        #
        # Max single-trade risk: 3 lots × 25 shares × ~₹100 ATM × 40% stop = ₹3,000
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v9d':
        # ── v9c + Trend Continuation Re-Entry ─────────────────────────────────
        # Same as v9c but adds immediate re-entry after a target/trailing exit
        # when the EMA trend is still intact (no fresh crossover required).
        #
        # When to re-enter (within 5 bars = 25 min of last exit):
        #   - Last exit was Target(80%), Trailing Stop, or EOD Close
        #   - EMA_fast still > EMA_slow (CALL) OR < EMA_slow (PUT)
        #   - ADX still above threshold (30 for CALL, 25 for PUT)
        #   - VWAP still confirms direction
        #   - NOT re-entering after a plain Stop(40%) — trend already broken
        #
        # Rationale: after a trailing stop exit at ~75% gain, the market often
        # pulls back 10-15 points and then resumes the trend. Currently the bot
        # sits out the rest of the move waiting for a fresh EMA cross. v9d
        # captures this continuation leg without doubling down on a reversal
        # (protected by the no-re-entry-after-stop rule).
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v13':
        # ── v9c + Vertical Spread Hedge ────────────────────────────────────────
        # Instead of buying a naked option, trade a bull call spread / bear put spread:
        #   CALL signal: Buy ATM CE (strike K) + Sell OTM CE (K + 3×strike_gap)
        #   PUT  signal: Buy ATM PE (strike K) + Sell OTM PE (K - 3×strike_gap)
        #
        # Economics for NIFTY (strike_gap=50, spread_width=150 points):
        #   Buy ATM CE  ≈ ₹100  |  Sell CE+150 ≈ ₹20  |  Net debit ≈ ₹80
        #   Max gain    = 150 - 80 = ₹70/share = +87.5% (above 80% target ✓)
        #   Max loss    = ₹80/share (net debit, vs ₹100 naked)
        #   80% target  = ₹64/share gain (spread value ₹144 < max ₹150 ✓)
        #
        # Advantages over naked option:
        #   - 20% cheaper entry → can buy 25% more lots with same capital
        #   - Hard cap on max loss = net debit (sold leg absorbs extra theta)
        #   - Lower vega exposure — less IV crush risk on correction days
        #
        # Disadvantage: capped upside at spread_width - net_debit per share.
        # With 3×strike_gap spread, the 80% target remains reachable.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v14':
        # ── v9c + Tuesday PUT at ADX ≥ 35 ─────────────────────────────────────
        # Tuesday has the lowest WR (31.2%) across all weekdays in v7 data. The
        # Tuesday skip (v9+) removed -₹3,472 of losses. However, that data includes
        # CALLs (worse performers) mixed with PUTs (53.6% WR overall).
        #
        # Hypothesis: A PUT signal on Tuesday with ADX ≥ 35 (very strong downtrend)
        # is worth trading. The Nifty-heavy-down days (Mon expiry sell-off, budget
        # shocks) often fall on Tuesdays and show strong sustained put moves.
        #
        # Rules:
        #  - Tuesday CALL entries: always blocked (unchanged from v9c)
        #  - Tuesday PUT entries: only allowed when ADX ≥ 35 (high conviction)
        #  - All other days: identical to v9c (CALL ADX ≥ 30, dynamic lots)
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v15':
        # ── v9c + CALL ADX relaxed 30 → 27 ───────────────────────────────────
        # v9c raised the CALL ADX threshold to 30 because at ADX 25-29, CALL
        # entries whipsawed (10 of 14 stops were CALLs in v7 data). However,
        # v7 data included Tuesdays and other now-filtered conditions. With the
        # Tuesday skip already in place, ADX 27-29 CALLs might be viable.
        #
        # ADX 27-29 still represents a meaningful trend (vs the base 25 floor).
        # The VWAP and EMA filters remain as quality gatekeepers.
        # This test recovers 2-4 CALL entries per year that were blocked by 30.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v16':
        # ── v9c + Earlier Entry: 10:30 IST (AVOID_FIRST_MINUTES = 75) ─────────
        # The 11:00 start was chosen because 10:xx entries had 11% WR in v7 data.
        # But 10:xx spans 45 bars (9:15–10:59). The worst sub-period is 9:15–10:15
        # (opening volatility, fake breakouts). By 10:30 the initial range is
        # usually set and trend momentum is more reliable.
        #
        # Opening 75 min = 9:15–10:29 still blocked (worst sub-period).
        # New window: 10:30–14:30 IST (+6 bars/day = ~330 extra bars over 14mo).
        # EMA 9/21 is also more meaningful by 10:30 (enough bars to compute).
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v17':
        # ── v14 + v16 combined (Tue PUT ≥35 + 10:30 start) ────────────────────
        # Maximum trade-count relaxation while keeping v9c quality discipline.
        # Both unlocks applied simultaneously:
        #   1. Tuesday PUT allowed at ADX ≥ 35 (from v14)
        #   2. Entry window starts at 10:30 instead of 11:00 (from v16)
        # Intent: see if the combined effect adds enough high-quality trades to
        # push P&L above v9c without inflating drawdown.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v20':
        # ── v14 + v19: Tuesday PUT ≥35 + 3 Lots Always ───────────────────────
        # Combines the two best individual improvements:
        #   v14: opens Tuesday PUT entries when ADX ≥ 35 (+1 high-conviction trade)
        #   v19: raises baseline lot size to 3 (scale every winning trade harder)
        # Expected: ~26 trades, 72%+ WR, Net P&L > ₹38,000
        # Risk: 3-lot worst case = 3×25×₹100×40% = ₹3,000 (6% of ₹50k capital)
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'vX':
        # ── Per-Instrument Optimised Strategy ─────────────────────────────────
        # Reads per-instrument parameters from config.INSTRUMENT_STRATEGY.
        # Each index has individually tuned ADX thresholds, entry windows,
        # weekday filters, and concurrent position limits derived from 14-month
        # Fyers backtest data (Jan 2025–Mar 2026).
        #
        # NIFTY    : CALL ADX≥30, PUT ADX≥25, skip Tue, entry 11:00–14:00, conc=1
        # BANKNIFTY: CALL ADX≥25, PUT ADX≥35, skip Tue, entry 11:00–14:45, conc=2
        # SENSEX   : CALL ADX≥25, PUT ADX≥25, skip Tue+Thu, entry 12:00–14:45, conc=1
        #
        # Lot sizing: same dynamic scheme as v9c (1 base lot, 2 on strength≥2)
        # Exit params: per-instrument PATH-A params (may 2026). Fall back to
        # global PATH_A_* if not set per-instrument (e.g. older config).
        _inst_strat  = config.INSTRUMENT_STRATEGY.get(instrument, {})
        stop_pct     = _inst_strat.get('path_a_stop',       config.PATH_A_STOP)
        target_pct   = _inst_strat.get('path_a_target',     config.PATH_A_TARGET)
        trail_act    = _inst_strat.get('path_a_trail_act',   config.PATH_A_TRAIL_ACT)
        trail_dist   = _inst_strat.get('path_a_trail_dist',  config.PATH_A_TRAIL_DIST)
        floor_pct    = None
        partial_exit = False

    elif variant == 'vX2':
        # ── vX + VWAP-Breach Secondary Signal + Tuesday CALL-Only Block ────────
        # Extends vX with a second entry trigger that catches moves missed when:
        #   (a) The EMA crossover fired but VWAP wasn't yet breached → signal blocked,
        #       and price later crosses VWAP without a new EMA crossover.
        #   (b) EMA crossover occurred before the entry window (e.g. SENSEX overnight
        #       pre-market), so the primary check_trend_momentum never fires in-window
        #       yet all conditions are valid mid-session once VWAP is breached.
        #
        # Secondary trigger (check_vwap_breach):
        #   PUT: EMA_fast < EMA_slow (established bearish) + no fresh crossdown in
        #        lookback + Close just crossed below VWAP + ADX ≥ put_adx_min
        #   CALL: mirror of PUT (EMA bullish + Close just crossed above VWAP)
        #
        # Tuesday fix: Tuesday CALL entries always blocked (31% WR danger zone).
        # PUT entries on Tuesday are now ALLOWED (PUTs were never shown to be bad
        # on Tuesdays — the danger zone data was CALL-specific).
        #
        # Observed real-world miss (Mar 10 2026 — Tuesday):
        #   NIFTY:  xDn 11:50 blocked (px above VWAP). Then VWAP breached 12:20
        #           with EMA BEAR + ADX=28–33. Secondary would have entered a PUT.
        #   SENSEX: EMA already BEAR before 12:00 start. VWAP breach at 12:15
        #           with ADX=26–34. Primary never fires; secondary catches it.
        #
        # Lot sizing: same as vX (1 base lot, 2 on strength≥2)
        stop_pct     = 0.40
        target_pct   = 0.80
        trail_act    = 0.55
        trail_dist   = 0.20
        floor_pct    = None
        partial_exit = False

    elif variant == 'vPW':
        # ── vX + Pre-Window Setup Discount ─────────────────────────────────────
        # Same per-instrument settings as vX, but adds a secondary entry trigger
        # that fires when the EMA crossover happened BEFORE the entry window.
        #
        # Problem vPW solves:
        #   On fast trend days (e.g. NIFTY -260 pts on Mar 13 2026), the EMA
        #   crossover happens at 9:30-10:00 and holds all morning. At 11:00,
        #   the standard vX primary (check_trend_momentum) REJECTS the signal
        #   because there is no fresh crossover in the last 3 bars — it already
        #   happened pre-window. The trend is real and well-established, but the
        #   freshness guard correctly misses it.
        #
        # vPW secondary trigger (check_pre_window_trend + check_pre_window_momentum):
        #   1. EMA aligned in one direction across ALL pre-window bars today
        #   2. VWAP aligned same direction for >= 60% of pre-window bars
        #   3. ADX rising (bar i > 4 bars ago) — trend strengthening toward threshold
        #   4. No fresh EMA crossover in last EMA_CROSSOVER_LOOKBACK bars
        #      (avoids double-firing with primary check_trend_momentum)
        #   5. ADX >= adx_min - 4 (e.g. NIFTY PUT: 25 - 4 = 21)
        #      Justified: a trend held 90+ min is MORE confirmed than a fresh cross.
        #      ADX structurally lags EMA by 2-4 bars — a rising ADX at 22 on a
        #      90-min-old trend is equivalent conviction to ADX 25 on a fresh cross.
        #
        # Per-instrument settings: identical to vX (same INSTRUMENT_STRATEGY config)
        # Lot sizing: same as vX (1 base lot, 2 on strength>=2)
        # Targets: uses config.BASE_TARGET (130%) to match live bot
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vST':
        # ── Supertrend: replaces EMA 9/21 crossover with ST(7, 2.5) flip ──────
        # Identical exit params to vX; per-instrument ADX + VWAP filters kept.
        # Entry trigger: Supertrend direction flip (-1→+1 = CALL, +1→-1 = PUT)
        # within last 3 bars, confirmed by ADX threshold and VWAP side.
        # Clean A/B vs vX: only the entry trigger changes (ST flip vs EMA cross).
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vXST':
        # ── EMA + Supertrend Ensemble ──────────────────────────────────────────
        # Combines EMA 9/21 crossover (primary) with Supertrend(7,2.5) (secondary).
        #
        # Problem addressed: EMA crossover fires 1-3 bars AFTER the trend starts.
        # Solution: if ST already flips direction AND EMA is directionally aligned
        # (fast/slow separated correctly), enter BEFORE the EMA crossover fires.
        # The EMA alignment gate prevents the vST noise problem (ST alone = bad).
        #
        # Entry rules (see check_ensemble_signal for full detail):
        #   Case A: Fresh EMA cross + EMA aligned + ST bullish/bearish  → 1 lot
        #   Case B: Fresh ST flip + EMA directionally aligned            → 1 lot (early)
        #   Case C: Both fresh EMA cross AND fresh ST flip in same dir   → 2 lots
        #
        # Blocked entries:
        #   EMA cross when ST direction opposes → divergence, skip
        #   ST flip without EMA alignment       → prevents vST whipsaws
        #
        # Same exit parameters and per-instrument config as vX.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vXS':
        # ── vX + Scale-In + Mean-Reversion Gate ─────────────────────────────────
        # Two key improvements over the vX baseline:
        #
        # 1. SCALE-IN SIZING (pyramid into winners):
        #    Always enter 1 lot on signal. A 2nd lot is added automatically
        #    when the position reaches TRAILING_ACTIVATION (55% gain) —
        #    directional conviction is confirmed by the position's own P&L.
        #
        #    Risk profile vs vX 2-lot upfront:
        #      vX  : up to 2 lots risk from bar 1 (signal_strength >= 2)
        #      vXS : 1 lot risk until 55% confirmed; scale-in at 55%, so:
        #            Worst case after scale-in → trail fires at 35% on lot1
        #            and -20% on lot2 → net still positive combined P&L
        #
        # 2. MEAN-REVERSION GATE:
        #    Skips entries when price is >3× ATR above VWAP (CALL) or
        #    >3× ATR below VWAP (PUT). Statistically overextended moves are
        #    more likely to revert than continue — avoid chasing late entries.
        #    Gate: VWAP_ATR_Z = (Close - VWAP) / ATR14  >3.0 or <-3.0
        #
        # Per-instrument settings: identical to vX (config.INSTRUMENT_STRATEGY).
        # Target: 80% (same as vX) — scale-in is designed to capture the last 25%
        # of the move from 55% to 80%. Using 130% target means scale-in rarely pays off
        # (the option almost never travels 75% more after scale-in point).
        stop_pct     = 0.40
        target_pct   = 0.80
        trail_act    = config.TRAILING_ACTIVATION   # 0.55 — scale-in trigger
        trail_dist   = config.TRAILING_DISTANCE     # 0.20
        floor_pct    = None
        partial_exit = False

    elif variant == 'vG':
        # ── Greeks-Informed: vX base + 4 options-market quality filters ────
        # Identical to vX in every parameter (same EMA/ADX/VWAP/time gates,
        # same 130%/40%/55%/20% exits, same per-instrument settings).
        # On top of vX, four additional filters screen for favourable options
        # conditions BEFORE entering (see check_vg_filters for full detail):
        #   1. HV Rank 3-93%   — volatility regime (only extreme frozen/spike outliers blocked)
        #   2. ATR_ratio ≥ 0.75 — momentum actively expanding (not compressing)
        #   3. RSI < 78 (CALL) or RSI > 22 (PUT) — no exhausted entries
        #   4. Round-number clearance — not hugging an OI wall strike zone
        # Hypothesis: options-market conditions filter improves entry quality
        # independent of underlying trend signal quality → higher WR, lower DD.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vCH':
        # ── Challenger: vX + OTM+1 Strike ─────────────────────────────────
        # Identical to vX in every way except strike selection:
        #   CALL signal → buy strike = ATM + 1×strike_gap  (1 step OTM)
        #   PUT  signal → buy strike = ATM - 1×strike_gap  (1 step OTM)
        #
        # Hypothesis: OTM options have a cheaper entry price → the same 130%
        # P&L target requires less underlying movement to hit (higher leverage).
        # The 40% stop-loss is also hit with less adverse movement, but each ₹
        # of option movement represents a larger % return.
        #
        # This is a clean A/B vs vX: same signal gate, same BS model, only K changes.
        # If vCH consistently outperforms vX, the live Challenger OI selection
        # (which also favours OTM via IV-discount scoring) is validated.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vXDI':
        # ── DI Momentum: same exits as vX, DI+/DI- crossover trigger ──────────
        # Identical exit params to vX — only the entry signal differs.
        # Entry: fresh +DI/-DI cross (last 3 bars) + ADX + VWAP.
        # Strength: ADX≥35 (+1) + DI spread≥5pts (+1) + VWAP dist≥0.1% (+1).
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vXF':
        # ── Path F: HTF Trend Continuation — same exits as vX ────────────────
        # Lower entry bar (ADX≥20, DI sustained 8 bars) to catch grind days.
        # Exit parameters identical to vX.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vB':
        # ── Path B: Morning Range Breakout (backtest variant) ────────────────
        # Signal: price breaks above Max(High) or below Min(Low) of the
        # 09:15–10:55 morning range with ADX ≥ 25 + VWAP + 15m ST alignment.
        # No OI snap / PCR in backtest (no historical OI data available).
        # Once-per-day entry to prevent chasing stale breakouts.
        # Exits: same parameters as vX for clean A/B comparison.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vB1':
        # ── vB1: VWAP Pullback Re-entry — same exits as vX ───────────────────
        # Enters on pullback-to-VWAP resumption in an established EMA trend.
        # Same exit parameters as vX for clean A/B comparison.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'vB2':
        # ── vB2: 30-min Micro Range Breakout — same exits as vX ──────────────
        # Mini-range = high/low of first 3 bars after 11:00 (11:00–11:10).
        # Entry on breakout of that tighter range + ADX≥28 + VWAP + 15m ST.
        stop_pct     = config.STOP_LOSS
        target_pct   = config.BASE_TARGET
        trail_act    = config.TRAILING_ACTIVATION
        trail_dist   = config.TRAILING_DISTANCE
        floor_pct    = None
        partial_exit = False

    elif variant == 'v19':
        # ── v9c + 3 Lots Always (Aggressive Sizing) ───────────────────────────
        # Same filters as v9c (skip Tuesday, CALL ADX≥30, dynamic sizing logic).
        # The only change: baseline lot size is 3 (not 2), so every trade uses
        # either 3 or 4 lots (strength≥2 → 4 lots, else 3 lots).
        #
        # Rationale: v9c's 72% WR over 25 trades is high enough that the
        # expected loss per trade is well-contained. Raising the minimum from
        # 2→3 lots extracts more P&L from each of the 25 already-filtered trades
        # without introducing any new signals or relaxing any filter.
        #
        # Risk scaling:
        #   2-lot worst case (v9c): 2 × 25 × ₹100 × 40% = ₹2,000/trade
        #   3-lot worst case (v19): 3 × 25 × ₹100 × 40% = ₹3,000/trade  (+50%)
        #   Max 4-lot (strength≥2): 4 × 25 × ₹100 × 40% = ₹4,000/trade
        #   ₹50k capital → max worst single trade = 6.0% loss (manageable)
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v18':
        # ── v9c + RSI-50 Crossover Secondary Trigger ──────────────────────────
        # Extends v9c with a second entry signal type: RSI-14 crossing the 50
        # level inside an already-established EMA trend.
        #
        # Primary signal (v9c):  EMA 9/21 crossover + ADX ≥ 25/30 + VWAP
        # Secondary signal (v18): RSI-50 crossover inside EMA trend (no EMA X needed)
        #
        # Rationale:
        #   After a target or trailing-stop exit, the EMA remains in the correct
        #   direction but a fresh crossover won't fire until the next trend leg
        #   starts. During this "post-exit trend continuation" phase, the RSI often
        #   dips toward 50 (pullback) and then re-crosses 50 upward (CALL) or
        #   downward (PUT), signalling that momentum has resumed. v9c misses this
        #   leg entirely. v18 catches it with check_rsi_entry().
        #
        # The RSI-50 signal is mutually exclusive with the EMA crossover:
        #   check_trend_momentum fires first — if it finds a signal, we enter on it.
        #   Only if check_trend_momentum returns None does check_rsi_entry run.
        #
        # Same quality gates: Tuesday skip, CALL ADX ≥ 30, VWAP, dynamic lots.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v10':
        # ── v9 + Heavyweight Stock Alignment ──────────────────────────────────
        # Adds a conviction filter on top of v9: before entering any trade,
        # checks that ≥2 of the top-3 Nifty stocks by weight agree.
        #
        # Top-3 (Feb 2026):
        #   Reliance Industries  — 9.33% of Nifty
        #   HDFC Bank            — 6.75% of Nifty
        #   Bharti Airtel        — 5.67% of Nifty    (combined: ~21.75%)
        #
        # Alignment rule:
        #   CALL → ≥2 stocks with Close > their own daily VWAP
        #   PUT  → ≥2 stocks with Close < their own daily VWAP
        #
        # Rationale: these stocks drive ~22% of NIFTY's movement. If they are
        # trending with the signal direction, the EMA crossover has broader
        # market support and is less likely to be a false breakout.
        #
        # Requires stock data: python data_collector.py stocks_backfill 400
        # Gracefully bypasses filter if stock data is unavailable.
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v11':
        # ── v9 + ATR Volatility Regime Filter ─────────────────────────────────
        # ATR_ratio = ATR14 / 20-day-avg-ATR.
        # When the ratio is < 0.70, the market is in a choppy, compressed
        # volatility regime. EMA crossovers in these periods produce false
        # breakouts that stop out quickly. Waiting for expanding volatility
        # (ratio ≥ 0.70) significantly improves signal quality.
        # Source: practitioner consensus — ATR normalisation is the #1 intraday
        # trend filter for index derivatives (confirmed by multiple quant blogs).
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    elif variant == 'v12':
        # ── v9 + ATR Regime + VWAP 2σ Stretch Filter ──────────────────────────
        # Adds a VWAP band filter on top of v11:
        # If price is already > 2σ above the daily VWAP (for CALLs) or
        # < 2σ below (for PUTs), the move is over-extended and a reversion
        # is more likely than a continuation. Skipping these "stretched" entries
        # avoids the scenario where we chase a move that is about to reverse.
        # VWAP_upper2 = VWAP + 2×(daily intraday std of TP-VWAP deviation)
        # VWAP_lower2 = VWAP - 2×(daily intraday std of TP-VWAP deviation)
        stop_pct        = 0.40
        target_pct      = 0.80
        trail_act       = 0.55
        trail_dist      = 0.20
        floor_pct       = None
        partial_exit    = False

    # Lot sizing (lot_size=75 corrected, NIFTY 50 per SEBI Nov 2024 circular)
    # Risk/trade: 1 lot × 75 shares × ~₹100 ATM × 40% stop = ₹3,000 = 6% of ₹50k
    #             2 lot × 75 × ~₹100 × 40% = ₹6,000 = 12% of ₹50k (v19 aggressive mode)
    # v9b/v9c family: 1 lot baseline, no daily cap (signal quality acts as gate)
    # v19/v20: 2 lots always (aggressive — use only after live trade validation)
    _v9c_family = ('v9b', 'v9c', 'v9d', 'v13', 'v14', 'v15', 'v16', 'v17', 'v18', 'v19', 'v20', 'vX', 'vX2', 'vPW', 'vCH', 'vST', 'vXS', 'vXST', 'vG', 'vXDI', 'vXF', 'vB', 'vB1', 'vB2')
    trade_lots  = 2 if variant in ('v19', 'v20') else 1
    max_trades  = 999 if variant in _v9c_family else config.MAX_TRADES_PER_DAY

    # v16/v17: earlier entry window (10:30 instead of 11:00)
    _entry_start = (dtime(10, 30) if variant in ('v16', 'v17')
                    else ENTRY_START)
    _entry_end   = ENTRY_END

    # ── vX: load per-instrument overrides from config.INSTRUMENT_STRATEGY ──────
    _vX_cfg           = {}
    _vX_call_adx_min  = 30      # default (v9 family value)
    _vX_put_adx_min   = 25      # default
    _vX_max_concurrent = config.MAX_CONCURRENT_POSITIONS
    _vX_skip_thursday  = False
    if variant in ('vX', 'vX2', 'vPW', 'vCH', 'vST', 'vXS', 'vXST', 'vG', 'vXDI', 'vXF', 'vB', 'vB1', 'vB2'):
        _vX_cfg = config.INSTRUMENT_STRATEGY.get(instrument, {})
        _vX_call_adx_min   = _vX_cfg.get('call_adx_min',   30)
        _vX_put_adx_min    = _vX_cfg.get('put_adx_min',    25)
        _vX_max_concurrent = _vX_cfg.get('max_concurrent', config.MAX_CONCURRENT_POSITIONS)
        _vX_skip_thursday  = _vX_cfg.get('skip_thursday',  False)
        # Override entry window from instrument config
        _es = _vX_cfg.get('entry_start', None)
        _ee = _vX_cfg.get('entry_end',   None)
        if _es:
            h, m = map(int, _es.split(':'))
            _entry_start = dtime(h, m)
        if _ee:
            h, m = map(int, _ee.split(':'))
            _entry_end = dtime(h, m)

    capital           = float(capital_alloc)
    positions         = []
    trades            = []
    equity_curve      = []
    daily_trade_count = {}
    daily_pnl         = {}
    last_exit         = None  # v9d: tracks last exit for trend continuation re-entry
    _path_e_last_day  = None  # vXF: date Path E last fired (once per day)
    _path_b_last_day  = None  # vB: date Path B last fired (once per day)
    _vb1_last_day     = None  # vB1: VWAP Pullback — once per day
    _vb2_last_day     = None  # vB2: Micro Range — once per day

    # ── vB: pre-compute morning ranges per trading day ───────────────────────
    # Morning range = max(High) / min(Low) of 09:15–10:55 bars.
    # Computed once per day before the main bar loop — no look-ahead because
    # the range closes at 10:55 and the entry window only opens at 11:00+.
    _path_b_ranges: dict = {}
    if variant == 'vB':
        _path_b_end_t = dtime(10, 55)
        for _day, _day_data in data.groupby(data.index.date):
            _morning = _day_data[_day_data.index.time <= _path_b_end_t]
            if len(_morning) >= 3:   # need at least 3 bars for a meaningful range
                _path_b_ranges[_day] = {
                    'mr_high': float(_morning['High'].max()),
                    'mr_low' : float(_morning['Low'].min()),
                }

    # ── vB2: pre-compute 30-min mini-ranges per trading day ──────────────────
    # Mini-range = high/low of the first 3 × 5-min bars after 11:00
    # (bars at 11:00, 11:05, 11:10). No look-ahead: range closes at 11:14,
    # entry window opens at 11:15.
    _vb2_ranges: dict = {}
    if variant == 'vB2':
        _vb2_range_start = dtime(11, 0)
        _vb2_range_end   = dtime(11, 14)
        for _day, _day_data in data.groupby(data.index.date):
            _mini = _day_data[
                (_day_data.index.time >= _vb2_range_start) &
                (_day_data.index.time <= _vb2_range_end)
            ]
            if len(_mini) >= 2:   # need at least 2 bars
                _vb2_ranges[_day] = {
                    'mr_high': float(_mini['High'].max()),
                    'mr_low' : float(_mini['Low'].min()),
                }

    force_t = dtime(*[int(x) for x in config.FORCE_CLOSE_TIME.split(':')])

    for i in range(1, len(data)):
        today    = data.index[i].date()
        now      = data.index[i]
        bar_time = now.time()
        spot     = data['Close'].iloc[i]
        hv       = data['HV'].iloc[i]

        daily_trade_count.setdefault(today, 0)
        daily_pnl.setdefault(today, 0.0)

        # ── Manage open positions ──────────────────────────────────────────────
        to_remove        = []
        scale_ins_to_add = []   # vXS: collect scale-in positions to add after the loop
        for idx, pos in enumerate(positions):
            hrs_held  = (now - pos['entry_time']).total_seconds() / 3600
            days_held = (now - pos['entry_time']).days
            T         = max(config.DAYS_TO_EXPIRY - days_held, 0.1) / 365

            # Use real options price if available, else fall back to Black-Scholes
            real_px = _get_real_option_price(pos.get('expiry'), pos['strike'],
                                             pos['type'], now) \
                      if pos.get('expiry') else None
            opt_px  = real_px if (real_px is not None and real_px > 0) \
                      else bs_price(pos['type'], spot, pos['strike'], T,
                                    config.RISK_FREE_RATE, hv)
            pos['price_source'] = 'real' if real_px else 'bs'

            # v13: convert single-leg price to spread value
            # spread_value = buy_leg_price - sell_leg_price, clamped to [0, spread_width]
            if variant == 'v13' and pos.get('sell_strike') is not None:
                sell_real = _get_real_option_price(pos.get('expiry'), pos['sell_strike'],
                                                   pos['type'], now) \
                            if pos.get('expiry') else None
                sell_px = sell_real if (sell_real is not None and sell_real > 0) \
                          else bs_price(pos['type'], spot, pos['sell_strike'], T,
                                        config.RISK_FREE_RATE, hv)
                spread_val = opt_px - sell_px
                opt_px = float(np.clip(spread_val, 0, pos['spread_width']))

            pnl_pct = (opt_px - pos['entry_price']) / pos['entry_price']

            if pnl_pct > pos['peak_pnl']:
                pos['peak_pnl'] = pnl_pct

            # ── vXS: Scale-in at TRAILING_ACTIVATION (55% gain) ──────────────
            # Add 1 more lot when the position has confirmed directional momentum
            # by reaching 55% profit. This avoids doubling risk upfront and only
            # adds capital when the trade is already "working."
            # Conditions: variant=vXS, not already scaled, not a scale-in itself,
            #             profit ≥ 55%, option still liquid, not near force-close.
            if (variant == 'vXS'
                    and not pos.get('scaled_in', False)
                    and not pos.get('is_scale_in', False)
                    and pnl_pct >= trail_act           # 55% gain trigger
                    and opt_px >= config.MIN_OPTION_PRICE
                    and bar_time < force_t):           # not at EOD close
                scale_pos = {
                    'type'         : pos['type'],
                    'strategy'     : pos['strategy'] + ' [Scale-In]',
                    'entry_time'   : now,
                    'entry_price'  : opt_px,           # enter at current price
                    'strike'       : pos['strike'],
                    'expiry'       : pos.get('expiry'),
                    'peak_pnl'     : 0.0,
                    'partial_done' : False,
                    'remaining_lot': lot_size,         # 1 base lot (shares)
                    'price_source' : pos.get('price_source', 'bs'),
                    'lots'         : 1,
                    'is_scale_in'  : True,
                }
                scale_ins_to_add.append(scale_pos)
                pos['scaled_in'] = True
                # Don't increment daily_trade_count — not a new signal entry

            exit_sig, exit_why = False, ""

            # ── Partial exit (Strategy B/C): book 50% at 80% gain ────────────
            if partial_exit and not pos.get('partial_done', False):
                if pnl_pct >= 0.80:
                    half_lot  = max(lot_size // 2, 1)
                    gross_p   = (opt_px - pos['entry_price']) * half_lot
                    cost_p    = transaction_costs(pos['entry_price'], opt_px, half_lot)
                    net_p     = gross_p - cost_p
                    capital  += net_p
                    daily_pnl[today] += net_p
                    # Record partial trade
                    trades.append({
                        'Entry Date'        : pos['entry_time'],
                        'Exit Date'         : now,
                        'Instrument'        : instrument,
                        'Variant'           : variant,
                        'Strategy'          : pos['strategy'],
                        'Type'              : pos['type'],
                        'Entry Price'       : round(pos['entry_price'], 2),
                        'Exit Price'        : round(opt_px, 2),
                        'Strike'            : pos['strike'],
                        'Lot Fraction'      : '50%',
                        'Hours Held'        : round(hrs_held, 2),
                        'P&L Gross'         : round(gross_p, 2),
                        'Transaction Costs' : round(cost_p, 2),
                        'P&L Net'           : round(net_p, 2),
                        'P&L %'             : round(pnl_pct * 100, 2),
                        'Peak P&L %'        : round(pos['peak_pnl'] * 100, 2),
                        'Exit Reason'       : 'Partial 80%',
                        'Capital'           : round(capital, 2),
                    })
                    pos['partial_done'] = True
                    pos['remaining_lot'] = lot_size - half_lot

            # ── Full/remaining exit logic ─────────────────────────────────────
            if pnl_pct <= -stop_pct:
                exit_sig, exit_why = True, f"Stop ({stop_pct*100:.0f}%)"

            elif pnl_pct >= target_pct:
                exit_sig, exit_why = True, f"Target ({target_pct*100:.0f}%)"

            elif (pos['peak_pnl'] >= trail_act and
                  pnl_pct < pos['peak_pnl'] - trail_dist):
                # Floor check: if we have a floor and pnl is above it, honour floor
                if floor_pct is not None and pos['peak_pnl'] >= floor_pct:
                    exit_sig, exit_why = True, f"Trailing (floor {floor_pct*100:.0f}%)"
                elif floor_pct is None:
                    exit_sig, exit_why = True, "Trailing Stop"
                # If peak never reached floor, keep normal trailing
                elif floor_pct is not None and pos['peak_pnl'] < floor_pct:
                    exit_sig, exit_why = True, "Trailing Stop (pre-floor)"

            elif (now - pos['entry_time']).days >= config.MAX_HOLDING_DAYS:
                exit_sig, exit_why = True, "Max Hold Period"

            if not exit_sig and config.INTRADAY_FORCE_CLOSE and bar_time >= force_t:
                exit_sig, exit_why = True, f"EOD Close ({config.FORCE_CLOSE_TIME})"

            if exit_sig:
                rem_lot = pos.get('remaining_lot', lot_size)
                gross   = (opt_px - pos['entry_price']) * rem_lot
                costs   = transaction_costs(pos['entry_price'], opt_px, rem_lot)
                net     = gross - costs
                capital += net
                daily_pnl[today] += net

                trades.append({
                    'Entry Date'        : pos['entry_time'],
                    'Exit Date'         : now,
                    'Instrument'        : instrument,
                    'Variant'           : variant,
                    'Strategy'          : pos['strategy'],
                    'Type'              : pos['type'],
                    'Entry Price'       : round(pos['entry_price'], 2),
                    'Exit Price'        : round(opt_px, 2),
                    'Strike'            : pos['strike'],
                    'Expiry'            : str(pos.get('expiry', '')),
                    'Price Source'      : pos.get('price_source', 'bs'),
                    'Lot Fraction'      : '50%' if pos.get('partial_done') else '100%',
                    'Hours Held'        : round(hrs_held, 2),
                    'P&L Gross'         : round(gross, 2),
                    'Transaction Costs' : round(costs, 2),
                    'P&L Net'           : round(net, 2),
                    'P&L %'             : round(pnl_pct * 100, 2),
                    'Peak P&L %'        : round(pos['peak_pnl'] * 100, 2),
                    'Exit Reason'       : exit_why,
                    'Capital'           : round(capital, 2),
                    'Lots'              : pos.get('lots', trade_lots),
                })
                # v9d: record exit context for potential trend continuation re-entry
                if variant == 'v9d':
                    last_exit = {
                        'direction' : pos['type'],
                        'reason'    : exit_why,
                        'bar'       : i,
                    }
                to_remove.append(idx)

        for idx in sorted(to_remove, reverse=True):
            positions.pop(idx)

        # vXS: add scale-in positions after all exits are processed
        for scale_pos in scale_ins_to_add:
            positions.append(scale_pos)

        # ── New entry ─────────────────────────────────────────────────────────
        _max_conc = _vX_max_concurrent if variant in ('vX', 'vX2', 'vPW', 'vST', 'vXS', 'vXST', 'vG', 'vXDI', 'vXF', 'vB') else config.MAX_CONCURRENT_POSITIONS
        can_enter = (
            len(positions) < _max_conc and
            daily_trade_count[today] < max_trades and
            daily_pnl.get(today, 0.0) > -config.MAX_DAILY_LOSS and
            _entry_start <= bar_time <= _entry_end
        )

        # ── Weekday gate ───────────────────────────────────────────────────────
        # variant E: high-gamma window — only Monday (0) and Tuesday (1)
        if can_enter and variant == 'E':
            can_enter = today.weekday() in {0, 1}

        # variant v9 family: skip Tuesday (weekday 1) — 31% WR danger zone
        # Analysis of 58 v7 trades: Tue = 16 trades, 5 wins, 11 losses, -Rs 3,472 net
        # v14/v17: don't block Tuesday here — PUT at ADX≥35 is allowed (filtered post-signal)
        if can_enter and variant in ('v9', 'v9b', 'v9c', 'v9d', 'v10', 'v11', 'v12', 'v13', 'v15', 'v16', 'v18', 'v19'):
            can_enter = (today.weekday() != 1)
        # v14/v17/v20: Tuesday handled post-signal (PUT at ADX≥35 allowed)

        # vX/vPW/vST/vXS/vG/vXDI/vXF/vB: per-instrument weekday gates
        if can_enter and variant in ('vX', 'vPW', 'vST', 'vXS', 'vXST', 'vG', 'vXDI', 'vXF', 'vB'):
            if _vX_cfg.get('skip_tuesday', True) and today.weekday() == 1:
                can_enter = False
            if _vX_skip_thursday and today.weekday() == 3:
                can_enter = False
        # vX2: Thursday same as vX; Tuesday NOT blocked here — CALL filtered post-signal
        if can_enter and variant == 'vX2':
            if _vX_skip_thursday and today.weekday() == 3:
                can_enter = False

        if can_enter:
            if variant == 'C':
                signal = check_sr_breakout(data, i)
            elif variant == 'D':
                signal = check_trend_momentum(data, i,
                                              ema_fast_col='EMA_fast_D',
                                              ema_slow_col='EMA_slow_D',
                                              adx_threshold=18)
            elif variant == 'vST':
                signal = check_supertrend_signal(data, i,
                                                 call_adx_min=_vX_call_adx_min,
                                                 put_adx_min=_vX_put_adx_min)
            elif variant == 'vXST':
                signal = check_ensemble_signal(data, i,
                                               call_adx_min=_vX_call_adx_min,
                                               put_adx_min=_vX_put_adx_min)
            elif variant == 'vXDI':
                signal = check_di_momentum(data, i,
                                           call_adx_min=_vX_call_adx_min,
                                           put_adx_min=_vX_put_adx_min)
            elif variant == 'vXF':
                # Path E (standalone): fires once per day in 12:30–13:45 window
                signal = None
                _pf_start = dtime(*map(int, config.PATH_E_START.split(':')))
                _pf_end   = dtime(*map(int, config.PATH_E_END.split(':')))
                if _path_e_last_day != today and _pf_start <= bar_time <= _pf_end:
                    signal = check_path_e_signal(data, i)
                    if signal:
                        _path_e_last_day = today
            elif variant == 'vB':
                # Path B: once per day — fires on first qualified breakout of morning range
                signal = None
                if _path_b_last_day != today:
                    _mr = _path_b_ranges.get(today)
                    if _mr is not None:
                        signal = check_path_b_signal(
                            data, i, _mr['mr_high'], _mr['mr_low']
                        )
                        if signal:
                            _path_b_last_day = today
            elif variant == 'vB1':
                # vB1: VWAP Pullback — once per day
                signal = None
                if _vb1_last_day != today:
                    signal = check_vb1_signal(data, i)
                    if signal:
                        _vb1_last_day = today
            elif variant == 'vB2':
                # vB2: 30-min Micro Range Breakout — once per day, entry from 11:15
                signal = None
                _vb2_start = dtime(11, 15)
                if _vb2_last_day != today and bar_time >= _vb2_start:
                    _mr2 = _vb2_ranges.get(today)
                    if _mr2 is not None:
                        signal = check_vb2_signal(
                            data, i, _mr2['mr_high'], _mr2['mr_low']
                        )
                        if signal:
                            _vb2_last_day = today
            else:
                signal = check_trend_momentum(data, i)

            # ── v9 family extra: CALL entries need ADX >= 30 ──────────────────
            # 10 of 14 stop-loss exits (all losses) are CALL entries.
            # At ADX 25-29 (marginal trend), CALL entries whipsaw and stop out.
            # PUTs are more resilient — keep the standard ADX > 25 threshold.
            # v15 uses a relaxed threshold of 27 (experiment: recover near-30 entries).
            if signal and signal['type'] == 'CALL':
                adx_now = data['ADX'].iloc[i]
                if variant in ('v9', 'v9b', 'v9c', 'v9d', 'v10', 'v11', 'v12', 'v13', 'v14', 'v16', 'v17', 'v18', 'v19', 'v20'):
                    if not pd.isna(adx_now) and adx_now < 30.0:
                        signal = None   # skip weak-ADX CALL entry (threshold 30)
                elif variant == 'v15':
                    if not pd.isna(adx_now) and adx_now < 27.0:
                        signal = None   # relaxed threshold: allow CALL at ADX 27–29

            # ── vX / vX2 / vXS / vG: per-instrument ADX gates ────────────────
            # Apply instrument-specific CALL and PUT ADX minimums from
            # config.INSTRUMENT_STRATEGY. Replaces the hardcoded v9 family
            # CALL≥30/PUT≥25 thresholds with tuned per-instrument values.
            if signal and variant in ('vX', 'vX2', 'vPW', 'vXS', 'vG'):
                adx_now = data['ADX'].iloc[i]
                if not pd.isna(adx_now):
                    if signal['type'] == 'CALL' and adx_now < _vX_call_adx_min:
                        signal = None
                    elif signal['type'] == 'PUT' and adx_now < _vX_put_adx_min:
                        signal = None

            # ── vX2: Tuesday CALL block (PUT remains valid) ───────────────────
            # vX blocks all Tuesday entries in can_enter.
            # vX2 instead filters only CALL signals post-detection, leaving PUT
            # signals free to enter — Tuesday PUTs have no backtest evidence of
            # being bad (the 31% WR finding was CALL-specific from v9 analysis).
            if signal and variant == 'vX2':
                if _vX_cfg.get('skip_tuesday', True) and today.weekday() == 1:
                    if signal['type'] == 'CALL':
                        signal = None   # Tuesday CALL: blocked (31% WR danger zone)

            # ── vX2: VWAP-breach secondary signal ─────────────────────────────
            # Fires when check_trend_momentum found nothing (no fresh EMA crossover
            # in the lookback window) but EMA is already directional AND price
            # just crossed the VWAP from the EMA-direction side.
            # Catches two real missed scenarios:
            #   (a) EMA crossover was blocked by VWAP at crossover bar → VWAP
            #       subsequently breached without new crossover (e.g. NIFTY 11:50 xDn
            #       above VWAP, VWAP breach at 12:20 — 6 bars later)
            #   (b) EMA crossover happened before entry window (SENSEX overnight)
            #       so primary never fires in-window despite valid conditions
            if signal is None and variant == 'vX2':
                signal = check_vwap_breach(data, i,
                                           call_adx_min=_vX_call_adx_min,
                                           put_adx_min=_vX_put_adx_min)
                # Apply same Tuesday CALL block to secondary signal
                if signal and _vX_cfg.get('skip_tuesday', True) and today.weekday() == 1:
                    if signal['type'] == 'CALL':
                        signal = None

            # ── vPW: pre-window trend discount ────────────────────────────────
            # Fires when primary check_trend_momentum found nothing (no fresh EMA
            # crossover in last 3 bars), but a directional trend has been running
            # since before the entry window (EMA aligned all pre-window bars today,
            # VWAP aligned 60%+, ADX rising). Entry uses a lenient ADX threshold
            # (adx_min - 4, floor 20) because 90+ min of trend = more confirmed.
            if signal is None and variant == 'vPW':
                pw_dir = check_pre_window_trend(data, i, _entry_start)
                if pw_dir is not None:
                    _adx_min  = _vX_call_adx_min if pw_dir == 'CALL' else _vX_put_adx_min
                    _lenient  = max(_adx_min - 4, 20)
                    signal    = check_pre_window_momentum(data, i, pw_dir, _lenient)
                    # Apply Tuesday CALL block (same as vX)
                    if signal and _vX_cfg.get('skip_tuesday', True) and today.weekday() == 1:
                        if signal['type'] == 'CALL':
                            signal = None

            # ── vXS: Mean-reversion gate (VWAP-ATR Z-score) ──────────────────
            # Skip entry when price is already over-stretched from intraday VWAP.
            # Z = (Close - VWAP) / ATR14 in current volatility units.
            # CALL at Z > 3.0 = price >3× ATR above VWAP → reversal more likely
            # PUT  at Z < -3.0 = price >3× ATR below VWAP → bounce more likely
            if signal and variant == 'vXS' and 'VWAP_ATR_Z' in data.columns:
                vwap_z = data['VWAP_ATR_Z'].iloc[i]
                if not pd.isna(vwap_z):
                    if signal['type'] == 'CALL' and vwap_z > 3.0:
                        signal = None   # over-extended upside — mean reversion risk
                    elif signal['type'] == 'PUT' and vwap_z < -3.0:
                        signal = None   # over-extended downside — bounce risk

            # ── vG: Greeks-Informed quality filters ──────────────────────────
            # Applied AFTER all vX conditions pass.  Screens for favourable
            # options-market microstructure conditions at entry.
            # Four filters: HV rank, ATR expansion, RSI quality, OI wall proxy.
            if signal and variant == 'vG':
                _vg_pass, _vg_reason = check_vg_filters(data, i, instrument,
                                                         signal['type'])
                if not _vg_pass:
                    signal = None

            # ── v14/v17 extra: Tuesday CALL always skip; PUT needs ADX >= 35 ──
            # Allow Tuesday entries but only for very high-conviction PUT signals.
            if signal and variant in ('v14', 'v17', 'v20') and today.weekday() == 1:
                if signal['type'] == 'CALL':
                    signal = None   # Tuesday CALL: always blocked
                else:
                    adx_now = data['ADX'].iloc[i]
                    if pd.isna(adx_now) or adx_now < 35.0:
                        signal = None   # Tuesday PUT: only at ADX ≥ 35

            # ── v10 extra: heavyweight stock alignment ───────────────────────
            # Require ≥2 of top-3 Nifty stocks (Reliance, HDFC Bank,
            # Bharti Airtel — ~21.75% of index) to confirm signal direction.
            # CALL: stocks above their own daily VWAP
            # PUT:  stocks below their own daily VWAP
            # Bypasses gracefully when stock data is not yet collected.
            if signal and variant == 'v10':
                if not _heavyweight_alignment(now, signal['type']):
                    signal = None   # heavyweights disagree — skip trade

            # ── v11/v12 extra: ATR volatility regime filter ──────────────────
            # ATR_ratio = ATR14 / 20-day-rolling-avg-ATR.
            # Skip entry when current ATR < 70% of 20-day avg — choppy market.
            # False signals spike in compressed-volatility periods (EMA crossovers
            # in narrow ranges whipsaw back immediately).
            if signal and variant in ('v11', 'v12'):
                atr_r = data['ATR_ratio'].iloc[i]
                if not pd.isna(atr_r) and atr_r < 0.70:
                    signal = None   # choppy/compressed regime — skip entry

            # ── v12 extra: VWAP 2σ stretch filter ────────────────────────────
            # If price is already > 2σ above daily VWAP for a CALL signal, or
            # > 2σ below for a PUT signal, the move is over-extended and likely
            # to revert before hitting the 80% target. Skip these entries.
            if signal and variant == 'v12':
                vwap_u2 = data['VWAP_upper2'].iloc[i]
                vwap_l2 = data['VWAP_lower2'].iloc[i]
                close_i = data['Close'].iloc[i]
                if signal['type'] == 'CALL' and not pd.isna(vwap_u2) and close_i > vwap_u2:
                    signal = None   # CALL but price already 2σ above VWAP (stretched)
                elif signal['type'] == 'PUT' and not pd.isna(vwap_l2) and close_i < vwap_l2:
                    signal = None   # PUT but price already 2σ below VWAP (stretched)

            # ── v9d: trend continuation re-entry ─────────────────────────────
            # After a target/trailing exit, if EMA trend is still intact within
            # 5 bars (25 min), re-enter without waiting for a fresh crossover.
            # Guards:
            #  - Must be non-stop exit (market reversed on a stop = broken trend)
            #  - EMA still directional (fast > slow for CALL, fast < slow for PUT)
            #  - ADX still strong (≥30 CALL, ≥25 PUT — same v9 thresholds)
            #  - VWAP still confirms direction
            if signal is None and variant == 'v9d' and last_exit is not None:
                bars_since = i - last_exit['bar']
                # Do not re-enter after: hard stop (trend broken) or EOD close (day done)
                no_reentry = (last_exit['reason'].startswith('Stop (')
                              or 'EOD' in last_exit['reason'])
                if bars_since <= 5 and not no_reentry:
                    ef_c  = data['EMA_fast'].iloc[i]
                    es_c  = data['EMA_slow'].iloc[i]
                    adx_c = data['ADX'].iloc[i]
                    vwap_c = data['VWAP'].iloc[i] if 'VWAP' in data.columns else float('nan')
                    if last_exit['direction'] == 'CALL':
                        dir_ok  = not pd.isna(ef_c) and ef_c > es_c and spot > ef_c
                        adx_ok  = not pd.isna(adx_c) and adx_c >= 30.0
                        vwap_ok = pd.isna(vwap_c) or spot > vwap_c
                    else:  # PUT
                        dir_ok  = not pd.isna(ef_c) and ef_c < es_c and spot < ef_c
                        adx_ok  = not pd.isna(adx_c) and adx_c >= config.MOMENTUM_ADX_THRESHOLD
                        vwap_ok = pd.isna(vwap_c) or spot < vwap_c
                    if dir_ok and adx_ok and vwap_ok:
                        signal = {
                            'type'           : last_exit['direction'],
                            'strategy'       : 'Trend Continuation',
                            'price'          : spot,
                            'signal_strength': 1,  # moderate conviction
                            'adx'            : float(adx_c),
                        }
                        last_exit = None  # consume — one re-entry per exit event

            # ── v18: RSI-50 crossover secondary trigger ───────────────────────
            # When no EMA crossover signal was found, check for RSI-14 crossing
            # the 50 level while the EMA trend is already in the right direction.
            # This fires mid-trend (after a pullback to RSI ~50) and catches legs
            # that v9c misses because no fresh EMA crossover is present.
            if signal is None and variant == 'v18':
                signal = check_rsi_entry(data, i)
                # Apply same CALL ADX ≥ 30 quality gate as other v9 family variants
                if signal and signal['type'] == 'CALL':
                    adx_now = data['ADX'].iloc[i]
                    if not pd.isna(adx_now) and adx_now < 30.0:
                        signal = None
                # Require signal_strength ≥ 2: at least TWO strong factors must align
                # (ADX≥35, EMA spread≥0.04%, VWAP dist≥0.10%) for an RSI entry to fire.
                # This is the same bar as the 3-lot dynamic sizing threshold in v9c,
                # ensuring RSI re-entries only occur in genuinely high-momentum conditions.
                if signal and signal.get('signal_strength', 0) < 2:
                    signal = None

            if signal:
                atm     = round(spot / strike_gap) * strike_gap
                # vCH Challenger: 1 strike OTM from ATM
                # CALL → higher strike (ATM+gap) | PUT → lower strike (ATM-gap)
                if variant == 'vCH':
                    strike = (atm + strike_gap if signal['type'] == 'CALL'
                              else atm - strike_gap)
                else:
                    strike = atm
                T_entry = config.DAYS_TO_EXPIRY / 365
                expiry  = _expiry_for_date(today)

                # Use real options price if available
                real_entry = _get_real_option_price(expiry, strike,
                                                    signal['type'], now) \
                             if expiry else None
                px          = real_entry if (real_entry is not None and real_entry > 0) \
                              else bs_price(signal['type'], spot, strike,
                                           T_entry, config.RISK_FREE_RATE, hv)
                price_src   = 'real' if real_entry else 'bs'

                # Dynamic lot scaling (lot_size=75 corrected):
                #   Normal (strength < 2): 1 lot = 75 shares → ₹3,000 max risk (6% of ₹50k)
                #   Strong (strength ≥ 2): 2 lots = 150 shares → ₹6,000 max risk (12% of ₹50k)
                #   v19/v20 aggressive: 2 lots normal, 3 lots on strength≥2 (12-18% risk)
                lots_this_trade = trade_lots
                if variant in ('v9c', 'v9d', 'v14', 'v15', 'v16', 'v17', 'v18', 'vX', 'vX2', 'vPW', 'vCH', 'vST', 'vG', 'vXDI', 'vXF', 'vB'):
                    lots_this_trade = 2 if signal.get('signal_strength', 0) >= 2 else 1
                # vXST: lot size driven by ensemble logic (ens_lots):
                #   2 lots when both EMA cross AND ST flip fire (max conviction)
                #   1 lot when only one trigger fires (EMA-only or ST-early)
                elif variant == 'vXST':
                    lots_this_trade = signal.get('ens_lots', 1)
                # vXS: always 1 lot at entry — scale-in adds the 2nd lot when position
                # reaches TRAILING_ACTIVATION (55% profit), handled in position loop below
                elif variant == 'vXS':
                    lots_this_trade = 1
                elif variant in ('v19', 'v20'):
                    lots_this_trade = 3 if signal.get('signal_strength', 0) >= 2 else 2

                # v13: vertical spread — buy ATM option, sell OTM option 3 strikes away
                sell_strike_v13  = None
                spread_width_v13 = None
                if variant == 'v13':
                    sw = 3 * strike_gap   # spread width in index points (150 for NIFTY)
                    sell_strike_v13  = strike + sw if signal['type'] == 'CALL' else strike - sw
                    spread_width_v13 = sw
                    # Price the sold (OTM) leg at entry
                    sell_real_v13 = _get_real_option_price(expiry, sell_strike_v13,
                                                           signal['type'], now) \
                                    if expiry else None
                    sell_px_v13 = sell_real_v13 if (sell_real_v13 and sell_real_v13 > 0) \
                                  else bs_price(signal['type'], spot, sell_strike_v13,
                                               T_entry, config.RISK_FREE_RATE, hv)
                    px = max(px - sell_px_v13, 1.0)  # net debit (floor at ₹1)

                if px >= config.MIN_OPTION_PRICE:
                    pos_entry = {
                        'type'          : signal['type'],
                        'strategy'      : signal['strategy'],
                        'entry_time'    : now,
                        'entry_price'   : px,
                        'strike'        : strike,
                        'expiry'        : expiry,
                        'peak_pnl'      : 0.0,
                        'partial_done'  : False,
                        'remaining_lot' : lot_size * lots_this_trade,
                        'price_source'  : price_src,
                        'lots'          : lots_this_trade,
                    }
                    if variant == 'v13':
                        pos_entry['sell_strike']   = sell_strike_v13
                        pos_entry['spread_width']  = spread_width_v13
                    positions.append(pos_entry)
                    daily_trade_count[today] += 1

        equity_curve.append({
            'Date'           : now,
            'Capital'        : capital,
            'Open Positions' : len(positions),
            'Daily Trades'   : daily_trade_count[today],
        })

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_curve)
    return trades_df, equity_df, capital


# ─── 7. Metrics Helper ────────────────────────────────────────────────────────

def compute_metrics(trades: pd.DataFrame, equity: pd.DataFrame,
                    initial_capital: float) -> dict:
    if len(trades) == 0:
        return {'trades': 0}

    # For partial-exit strategies, aggregate entry→full-exit pairs
    # Use only full-exit records for win-rate (partial is always a win at 80%+)
    full_exits = trades[trades['Exit Reason'] != 'Partial 80%']
    wins       = full_exits[full_exits['P&L Net'] > 0]
    losses     = full_exits[full_exits['P&L Net'] <= 0]
    win_rate   = len(wins) / len(full_exits) * 100 if len(full_exits) > 0 else 0

    avg_win  = wins['P&L Net'].mean()   if len(wins)   > 0 else 0
    avg_loss = losses['P&L Net'].mean() if len(losses) > 0 else 0
    wl_ratio = abs(avg_win / avg_loss)  if avg_loss    else float('inf')
    pf       = (wins['P&L Net'].sum() / abs(losses['P&L Net'].sum())
                if len(losses) > 0 else float('inf'))

    gross_pnl = trades['P&L Gross'].sum()
    cost_sum  = trades['Transaction Costs'].sum()
    net_pnl   = trades['P&L Net'].sum()

    final_cap = initial_capital + net_pnl
    equity['Peak']     = equity['Capital'].cummax()
    equity['Drawdown'] = (equity['Capital'] - equity['Peak']) / equity['Peak'] * 100
    max_dd             = equity['Drawdown'].min()

    eq_daily = equity.set_index('Date')['Capital'].resample('D').last().dropna()
    dr       = eq_daily.pct_change().dropna()
    sharpe   = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0

    return {
        'trades'    : len(full_exits),
        'total_rows': len(trades),
        'win_rate'  : win_rate,
        'wl_ratio'  : wl_ratio,
        'pf'        : pf,
        'gross'     : gross_pnl,
        'costs'     : cost_sum,
        'net'       : net_pnl,
        'max_dd'    : max_dd,
        'sharpe'    : sharpe,
        'avg_win'   : avg_win,
        'avg_loss'  : avg_loss,
    }


# ─── 8. Single Variant Run ─────────────────────────────────────────────────────

def run_variant(variant: str, instruments=None):
    """Run backtest for one variant across all instruments. Returns summary dict."""
    if instruments is None:
        instruments = []
        if len(data_nifty) > 0:
            instruments.append(('NIFTY', data_nifty))
        if len(data_bnf) > 0:
            instruments.append(('BANKNIFTY', data_bnf))
        if len(data_sensex) > 0:
            instruments.append(('SENSEX', data_sensex))

    all_trades  = []
    all_equity  = []
    total_init  = 0

    for inst_name, inst_data in instruments:
        # Strategy C needs S/R columns added
        if variant == 'C':
            inst_data = add_rolling_sr(inst_data, lookback_days=6)

        t, e, final = run_backtest(inst_data, inst_name, variant)
        all_trades.append(t)
        all_equity.append(e)
        total_init += config.INSTRUMENTS[inst_name]['capital']

    trades_combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    equity_combined = pd.concat(all_equity, ignore_index=True) if all_equity else pd.DataFrame()

    if len(equity_combined) == 0:
        return {'variant': variant, 'trades': 0}

    # Combine equity by summing capitals per timestamp
    equity_combined = (
        equity_combined.groupby('Date')['Capital']
        .sum()
        .reset_index()
        .rename(columns={'Capital': 'Capital'})
    )

    m = compute_metrics(trades_combined, equity_combined, total_init)
    m['variant'] = variant
    m['trades_df'] = trades_combined
    m['equity_df'] = equity_combined
    m['initial']   = total_init
    return m


# ─── 9. Print Results ─────────────────────────────────────────────────────────

def print_best_day(trades_df: pd.DataFrame, lot_size_map: dict | None = None):
    """
    Print the most profitable single day's trade breakdown.
    Shows entry/exit times, lots, premium cost, costs, and net P&L for each trade.
    Also prints worst day for reference.
    """
    if len(trades_df) == 0:
        return

    df = trades_df.copy()
    df['_date'] = pd.to_datetime(df['Entry Date']).dt.date
    daily = df.groupby('_date')['P&L Net'].sum()

    def _day_block(date, label):
        day_trades = df[df['_date'] == date]
        total_pnl  = daily[date]
        total_cost = day_trades['Transaction Costs'].sum()
        pnl_str    = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):,.0f}"
        print(f"\n  {'─'*65}")
        print(f"  {label}: {date}   Total P&L: {pnl_str}   Costs: ₹{total_cost:,.0f}")
        print(f"  {'─'*65}")
        hdr = f"  {'Time':<7} {'Dir':<5} {'Lots':>4} {'Strike':>7} {'Entry':>7} {'Exit':>7} {'Premium₹':>9} {'Gross':>8} {'Net':>8} {'Exit Reason'}"
        print(hdr)
        for _, t in day_trades.sort_values('Entry Date').iterrows():
            entry_dt = pd.to_datetime(t['Entry Date'])
            lots     = int(t.get('Lots', 2)) if not pd.isna(t.get('Lots', float('nan'))) else 2
            # Lot size: derive from remaining_lot — fall back to NIFTY default (25)
            ls       = lot_size_map.get(t.get('Instrument', 'NIFTY'), 25) if lot_size_map else 25
            premium  = round(t['Entry Price'] * lots * ls, 0)  # total premium paid
            gross    = round(t['P&L Gross'], 0)
            net      = round(t['P&L Net'], 0)
            net_s    = f"+₹{net:,.0f}" if net >= 0 else f"-₹{abs(net):,.0f}"
            print(f"  {entry_dt.strftime('%H:%M'):<7} {t['Type']:<5} {lots:>4} {int(t.get('Strike',0)):>7} "
                  f"₹{t['Entry Price']:>5.1f} ₹{t['Exit Price']:>5.1f} "
                  f"₹{premium:>8,.0f} {'+₹' if gross>=0 else '-₹'}{abs(gross):>6,.0f}  {net_s:>8}  {t['Exit Reason']}")
        print(f"  {'─'*65}")

    best_date  = daily.idxmax()
    worst_date = daily.idxmin()
    _day_block(best_date,  "BEST DAY ")
    if worst_date != best_date:
        _day_block(worst_date, "WORST DAY")


def print_results(m: dict):
    W = 70
    v = m.get('variant', '?')
    print(f"\n{'=' * W}")
    print(f"  {config.BOT_NAME} — BACKTEST  [variant={v}]")
    print(f"{'=' * W}")

    if m.get('trades', 0) == 0:
        print("  ⚠ No trades generated.")
        return

    net = m['net']
    print(f"\n  TRADES          : {m['trades']}  (full exits); {m['total_rows']} total rows")
    print(f"  Win Rate        : {m['win_rate']:.1f}%  {'✓' if m['win_rate'] >= 45 else '↑ aim 45%+'}")
    print(f"  Win/Loss Ratio  : {m['wl_ratio']:.2f}x")
    print(f"  Profit Factor   : {m['pf']:.2f}  {'✓' if m['pf'] >= 1.2 else '↑ aim 1.2+'}")
    print(f"\n  Gross P&L       : ₹{m['gross']:>10,.0f}")
    print(f"  Costs           : ₹{m['costs']:>10,.0f}")
    print(f"  Net P&L         : ₹{net:>10,.0f}  {'▲ PROFIT' if net >= 0 else '▼ LOSS'}")
    print(f"\n  Max Drawdown    : {m['max_dd']:.2f}%  {'✓' if m['max_dd'] > -15 else '⚠'}")
    print(f"  Sharpe          : {m['sharpe']:.2f}")

    td = m['trades_df']
    if len(td) > 0:
        print(f"\n  EXIT BREAKDOWN")
        print(f"  {'─'*55}")
        for reason, cnt in td['Exit Reason'].value_counts().items():
            print(f"  {reason:<40}: {cnt:>4}  ({cnt/len(td)*100:>5.1f}%)")

        lsmap = {k: v['lot_size'] for k, v in config.INSTRUMENTS.items()}
        print_best_day(td, lsmap)


# ─── 10. Comparison Mode ──────────────────────────────────────────────────────

def print_comparison(results: list):
    W = 70
    print(f"\n{'=' * W}")
    print(f"  STRATEGY COMPARISON  (NIFTY only, real Fyers CSV data, ~13 months)")
    print(f"{'=' * W}")
    hdr = f"  {'Variant':<8} {'Trades':>7} {'WR%':>6} {'W/L':>5} {'PF':>5} {'Net P&L':>10} {'MaxDD%':>8} {'Sharpe':>7}"
    print(hdr)
    print(f"  {'─'*65}")
    for m in results:
        v = m.get('variant', '?')
        if m.get('trades', 0) == 0:
            print(f"  {v:<8}  no trades")
            continue
        net_str = f"{'▲' if m['net'] >= 0 else '▼'}₹{abs(m['net']):,.0f}"
        best    = ' ◀' if m == max(results, key=lambda x: x.get('net', -99999)) else ''
        print(f"  {v:<8} {m['trades']:>7} {m['win_rate']:>5.1f}% {m['wl_ratio']:>5.2f} "
              f"{m['pf']:>5.2f} {net_str:>10} {m['max_dd']:>7.2f}% {m['sharpe']:>7.2f}{best}")
    print(f"  {'─'*65}")
    print(f"\n  Notes:")
    print(f"  v7 = EMA 9/21 | ADX>25 | Stop 40% | Target 80% | Trail@55% (BASELINE)")
    print(f"  A  = EMA 9/21 | ADX>25 | Stop 40% | Target 200% | Floor 150%")
    print(f"  B  = EMA 9/21 | ADX>25 | Partial 50% at 80%, remainder to 200%")
    print(f"  C  = 5-8 day S/R breakout + volume (200% target)")
    print(f"  D  = EMA 6/12 | ADX>18 | Stop 50% | Target 180% | Trail@100%")
    print(f"  E  = v7 params | Mon+Tue ONLY (high-gamma experiment)")
    print(f"  v9  = v7 params | Skip Tuesday + CALL ADX>=30 (smart loss avoidance)")
    print(f"  v9b = v9 params | 2 lots per trade + no daily trade cap (max re-entries)")
    print(f"  v9c = v9b params | dynamic lots: 3 lots when signal_strength>=2, else 2")
    print(f"  v9d = v9c params | + trend continuation re-entry after target/trail exit")
    print(f"  v13 = v9c params | + vertical spread (buy ATM + sell OTM 3 strike_gaps)")
    print(f"  v14 = v9c params | + Tuesday PUT allowed when ADX>=35 (high conviction)")
    print(f"  v15 = v9c params | + CALL ADX threshold relaxed 30->27")
    print(f"  v16 = v9c params | + entry window starts 10:30 IST (AVOID_FIRST=75)")
    print(f"  v17 = v14+v16    | Tuesday PUT >=35 + 10:30 entry start (combined)")
    print(f"  v10 = v9 params | >=2 of Reliance/HDFC/Airtel confirm via daily VWAP")
    print(f"  v11 = v9 params | ATR_ratio >= 0.70 (skip choppy low-vol regime entries)")
    print(f"  v12 = v9 params | ATR_ratio >= 0.70 + skip 2-sigma VWAP stretch entries")
    print(f"{'=' * W}")


# ─── 11. Main Execution ───────────────────────────────────────────────────────

if VARIANT == 'compare':
    print(f"\nRunning all 23 variants (NIFTY only — real Fyers CSV data)...")
    results = []
    for v in ('v7', 'A', 'B', 'C', 'D', 'E', 'v9', 'v9b', 'v9c', 'v9d', 'v10', 'v11', 'v12', 'v13', 'v14', 'v15', 'v16', 'v17', 'v18', 'v19', 'v20', 'vB1', 'vB2'):
        print(f"  Running variant {v}...")
        m = run_variant(v)
        results.append(m)
        print(f"    → {m.get('trades', 0)} trades | net ₹{m.get('net', 0):,.0f} | "
              f"WR {m.get('win_rate', 0):.1f}%")

    print_comparison(results)

    # Save combined trade log from v7 (for mistake_analyzer)
    v7_trades = next((r['trades_df'] for r in results if r['variant'] == 'v7'), pd.DataFrame())
    if len(v7_trades) > 0:
        v7_trades['Entry Hour']    = v7_trades['Entry Date'].dt.hour
        v7_trades['Entry Minute']  = v7_trades['Entry Date'].dt.minute
        v7_trades['Entry Weekday'] = v7_trades['Entry Date'].dt.day_name()
        v7_trades['Exit Hour']     = v7_trades['Exit Date'].dt.hour
        v7_trades['Win']           = (v7_trades['P&L Net'] > 0).astype(int)
        csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'backtest_trades.csv'
        )
        v7_trades.to_csv(csv_path, index=False)
        print(f"\nv7 trade log saved → {os.path.abspath(csv_path)}")

    # Plot equity curves for all variants side-by-side (7×3 grid, 20 used)
    fig, axes = plt.subplots(7, 3, figsize=(24, 42))
    fig.suptitle(f"{config.BOT_NAME} — Strategy Comparison (NIFTY only, real Fyers data)",
                 fontsize=15, fontweight='bold')
    variant_names = {
        'v7' : 'v7  — EMA 9/21 | ADX>25 | 80%/40% (BASELINE)',
        'A'  : 'A   — EMA 9/21 | ADX>25 | 200%/40% floor150%',
        'B'  : 'B   — EMA 9/21 | Partial 50% at 80%',
        'C'  : 'C   — 5-8 day S/R Breakout',
        'D'  : 'D   — EMA 6/12 | ADX>18 | 180%/50%',
        'E'  : 'E   — Mon+Tue only (high-gamma window)',
        'v9' : 'v9  — Skip Tue + CALL ADX>=30 (smart filter)',
        'v9b': 'v9b — v9 + 2 lots + no daily trade cap',
        'v9c': 'v9c — v9b + 3 lots on strength≥2 (dynamic sizing)',
        'v9d': 'v9d — v9c + trend continuation re-entry after exit',
        'v10': 'v10 — v9 + Heavyweight alignment (Reliance/HDFC/Airtel)',
        'v13': 'v13 — v9c + vertical spread (buy ATM + sell OTM 3 gaps)',
        'v11': 'v11 — v9 + ATR regime filter (skip choppy days)',
        'v12': 'v12 — v9 + ATR regime + VWAP 2sigma stretch filter',
        'v14': 'v14 — v9c + Tuesday PUT at ADX>=35 (high-conviction Tue)',
        'v15': 'v15 — v9c + CALL ADX threshold 30→27 (recover near-30)',
        'v16': 'v16 — v9c + entry starts 10:30 IST (75-min skip)',
        'v17': 'v17 — v14+v16 (Tue PUT + 10:30 start)',
        'v18': 'v18 — v9c + RSI-50 crossover secondary trigger',
        'v19': 'v19 — v9c + 3 lots always (4 on strength>=2)',
        'v20': 'v20 — v14+v19: Tue PUT>=35 + 3 lots always',
        'vXS': 'vXS — vX + scale-in at 55% + MR gate (1-lot entry)',
    }
    for ax, m in zip(axes.flat, results):
        if m.get('trades', 0) == 0:
            ax.text(0.5, 0.5, 'No trades', ha='center', va='center')
            continue
        eq = m['equity_df']
        init = m['initial']
        colour = '#27ae60' if m['net'] >= 0 else '#e74c3c'
        ax.plot(eq['Date'], eq['Capital'], lw=2, color=colour)
        ax.axhline(init, color='gray', ls='--', alpha=0.6)
        ax.fill_between(eq['Date'], init, eq['Capital'],
                        where=eq['Capital'] >= init, alpha=0.15, color='#27ae60')
        ax.fill_between(eq['Date'], init, eq['Capital'],
                        where=eq['Capital'] <  init, alpha=0.15, color='#e74c3c')
        v = m['variant']
        net_str = f"+₹{m['net']:,.0f}" if m['net'] >= 0 else f"-₹{abs(m['net']):,.0f}"
        ax.set_title(f"{variant_names.get(v, v)}\n"
                     f"Trades: {m['trades']} | WR: {m['win_rate']:.1f}% | Net: {net_str}",
                     fontsize=10)
        ax.set_ylabel('Capital (₹)')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
        ax.tick_params(axis='x', rotation=30)
        ax.grid(alpha=0.3)

    plt.tight_layout()
    cmp_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'strategy_comparison.png'
    )
    plt.savefig(cmp_path, dpi=120, bbox_inches='tight')
    plt.close()
    print(f"Comparison chart saved → {os.path.abspath(cmp_path)}")

elif VARIANT == 'blind':
    # ─── Out-of-sample / blind backtest ───────────────────────────────────────
    # Simulates deploying to live: calibrate on the first ~8 months of data,
    # then test on the remaining ~6 months the strategy has never seen.
    #
    # Split:
    #   In-sample  : Jan 2025 – Sep 30 2025  (~168 trading days, used for tuning)
    #   Out-of-sample: Oct 01 2025 – Apr 2026  (~126 trading days, blind window)
    #
    # The OOS period is the most regime-relevant: it includes the monthly-only
    # expiry transition (SEBI Oct 2024), the tariff war spike (Apr 2025 / Jan-Apr
    # 2026), and is closest to the current live environment.
    #
    # Variants: vX (production), vB (old MRB), vB1 (VWAP Pullback), vB2 (Micro Range)
    # Instruments: all three (NIFTY, BANKNIFTY, SENSEX combined)
    import pytz as _pytz
    _IST     = _pytz.timezone('Asia/Kolkata')
    SPLIT_DT = pd.Timestamp('2025-10-01', tz=_IST)
    BLIND_VARIANTS = ('vX', 'vB', 'vB1', 'vB2')
    BLIND_INSTRUMENTS = list(config.INSTRUMENTS.keys())   # NIFTY, BANKNIFTY, SENSEX

    _inst_data = {
        'NIFTY'    : data_nifty,
        'BANKNIFTY': data_bnf,
        'SENSEX'   : data_sensex,
    }
    # Check all data is available
    _missing = [i for i in BLIND_INSTRUMENTS if len(_inst_data.get(i, [])) == 0]
    if _missing:
        print(f"  ⚠ Missing data for: {_missing}. Run data_collector.py backfill first.")
    else:
        W = 80
        # ── Print header ─────────────────────────────────────────────────────
        _sample_data = _inst_data['NIFTY']
        _is_data  = _sample_data[_sample_data.index <  SPLIT_DT]
        _oos_data = _sample_data[_sample_data.index >= SPLIT_DT]
        is_start  = _is_data.index[0].strftime('%d %b %Y')   if len(_is_data)  > 0 else '?'
        is_end    = _is_data.index[-1].strftime('%d %b %Y')  if len(_is_data)  > 0 else '?'
        oos_start = _oos_data.index[0].strftime('%d %b %Y')  if len(_oos_data) > 0 else '?'
        oos_end   = _oos_data.index[-1].strftime('%d %b %Y') if len(_oos_data) > 0 else '?'
        is_days   = _is_data.index.normalize().nunique()
        oos_days  = _oos_data.index.normalize().nunique()

        print(f"\n{'=' * W}")
        print(f"  BLIND / OUT-OF-SAMPLE BACKTEST  (simulating live deployment)")
        print(f"{'=' * W}")
        print(f"  IN-SAMPLE  (calibration window) : {is_start} → {is_end}  ({is_days} trading days)")
        print(f"  OUT-OF-SAMPLE (blind window)    : {oos_start} → {oos_end}  ({oos_days} trading days)")
        print(f"  Variants  : {', '.join(BLIND_VARIANTS)}")
        print(f"  Instruments: {', '.join(BLIND_INSTRUMENTS)}")
        print(f"{'─' * W}")

        # ── Run all variants × all instruments ───────────────────────────────
        # results[var][inst] = {'is': metrics, 'oos': metrics}
        results = {var: {} for var in BLIND_VARIANTS}

        for var in BLIND_VARIANTS:
            for inst in BLIND_INSTRUMENTS:
                raw     = _inst_data[inst]
                d_is    = raw[raw.index <  SPLIT_DT]
                d_oos   = raw[raw.index >= SPLIT_DT]
                cap     = config.INSTRUMENTS[inst]['capital']

                t_is,  e_is,  _ = run_backtest(d_is,  inst, var)
                t_oos, e_oos, _ = run_backtest(d_oos, inst, var)

                results[var][inst] = {
                    'is'     : compute_metrics(t_is,  e_is,  cap),
                    'oos'    : compute_metrics(t_oos, e_oos, cap),
                    'is_trades' : t_is,
                    'oos_trades': t_oos,
                }

        # ── Helper: aggregate metrics across instruments ──────────────────────
        def _agg(var, split):
            """Sum trades/P&L, average WR/PF/Sharpe across instruments."""
            all_m   = [results[var][inst][split] for inst in BLIND_INSTRUMENTS]
            trades  = sum(m.get('trades', 0) for m in all_m)
            net     = sum(m.get('net', 0)    for m in all_m)
            max_dd  = min(m.get('max_dd', 0) for m in all_m)   # worst single instrument
            wrs     = [m.get('win_rate', 0) for m in all_m if m.get('trades', 0) > 0]
            pfs     = [m.get('pf', 0)       for m in all_m if m.get('trades', 0) > 0]
            sharpes = [m.get('sharpe', 0)   for m in all_m if m.get('trades', 0) > 0]
            return {
                'trades'  : trades,
                'net'     : net,
                'max_dd'  : max_dd,
                'win_rate': (sum(wrs)     / len(wrs))     if wrs     else 0,
                'pf'      : (sum(pfs)     / len(pfs))     if pfs     else 0,
                'sharpe'  : (sum(sharpes) / len(sharpes)) if sharpes else 0,
            }

        # ── Per-variant comparison table ─────────────────────────────────────
        for var in BLIND_VARIANTS:
            mis  = _agg(var, 'is')
            moos = _agg(var, 'oos')
            print(f"\n  {'─' * W}")
            print(f"  Variant: {var}  (all 3 instruments combined)")
            print(f"  {'─' * W}")
            print(f"  {'Metric':<22} {'In-Sample (8m)':>16} {'OOS / Blind (6m)':>18}  {'Δ':>10}")
            print(f"  {'─' * W}")

            def _row(label, key, fmt='.1f', pct=False, higher_better=True):
                iv  = mis.get(key, 0)
                ov  = moos.get(key, 0)
                suf = '%' if pct else ''
                if fmt == ',.0f':
                    is_str  = f"₹{iv:,.0f}"
                    oos_str = f"₹{ov:,.0f}"
                    delta   = ov - iv
                    d_str   = f"{'▲' if delta>=0 else '▼'}₹{abs(delta):,.0f}"
                else:
                    is_str  = f"{iv:{fmt}}{suf}"
                    oos_str = f"{ov:{fmt}}{suf}"
                    delta   = ov - iv
                    d_str   = f"{delta:+{fmt}}{suf}"
                good = (delta >= 0) if higher_better else (delta <= 0)
                flag = '✓' if good else '⚠'
                print(f"  {label:<22} {is_str:>16} {oos_str:>18}  {d_str:>10}  {flag}")

            _row('Trades (combined)', 'trades',  '.0f')
            _row('Win Rate',          'win_rate', '.1f', pct=True)
            _row('Profit Factor',     'pf',       '.2f')
            _row('Net P&L',           'net',      ',.0f')
            _row('Worst DD',          'max_dd',   '.1f', pct=True, higher_better=False)
            _row('Sharpe (avg)',       'sharpe',   '.2f')

            # Per-instrument OOS breakdown
            print(f"\n  OOS breakdown by instrument:")
            for inst in BLIND_INSTRUMENTS:
                m = results[var][inst]['oos']
                t = m.get('trades', 0)
                wr = m.get('win_rate', 0)
                net = m.get('net', 0)
                sign = '▲' if net >= 0 else '▼'
                print(f"    {inst:<12} {t:>3} trades  WR {wr:.1f}%  "
                      f"Net {sign}₹{abs(net):,.0f}")

        # ── Summary scorecard ─────────────────────────────────────────────────
        print(f"\n{'=' * W}")
        print(f"  SCORECARD  (OOS blind window: {oos_start} → {oos_end})")
        print(f"{'─' * W}")
        print(f"  {'Variant':<8} {'OOS Trades':>11} {'OOS WR':>8} {'OOS Net P&L':>13} {'OOS DD':>9}  Verdict")
        print(f"  {'─' * W}")
        for var in BLIND_VARIANTS:
            moos = _agg(var, 'oos')
            mis  = _agg(var, 'is')
            wr_ok  = moos.get('win_rate', 0) >= 55.0              # must clear 55% live bar
            pnl_ok = moos.get('net', 0) > 0
            wr_stable = moos.get('win_rate', 0) >= mis.get('win_rate', 0) * 0.80
            grade = ('PASS ✓✓'    if (wr_ok and pnl_ok and wr_stable)
                     else 'MARGINAL ⚠' if pnl_ok
                     else 'FAIL ✗')
            net   = moos.get('net', 0)
            sign  = '▲' if net >= 0 else '▼'
            print(f"  {var:<8} {moos.get('trades',0):>11.0f} "
                  f"{moos.get('win_rate',0):>7.1f}% "
                  f"{sign}₹{abs(net):>10,.0f} "
                  f"{moos.get('max_dd',0):>8.1f}%  {grade}")
        print(f"{'=' * W}")

elif VARIANT == 'vG':
    # ─── vG: Greeks-Informed — side-by-side vs vX benchmark ──────────────────
    # Runs vG and vX across all 3 instruments, then prints a head-to-head
    # comparison broken down by instrument and combined, showing which filters
    # blocked signals (debug level), trade quality metrics, and equity curves.
    import pytz as _pytz
    _IST = _pytz.timezone('Asia/Kolkata')

    W = 74
    print(f"\n{'=' * W}")
    print(f"  {config.BOT_NAME} — vG (Greeks-Informed) vs vX (Current Best)")
    print(f"{'=' * W}")
    print(f"\n  vG filters applied on top of all vX conditions:")
    print(f"    1. HV Rank 3-93%   — volatility regime (IV rank proxy, 3-month window; only extreme outliers blocked)")
    print(f"    2. ATR_ratio ≥ 0.75 — momentum expanding, not compressing (P25=0.756 at vX signal bars)")
    print(f"    3. RSI quality gate — CALL RSI<78 | PUT RSI>22 (no exhausted entries)")
    print(f"    4. Round-number clearance — 0.08% threshold | NIFTY 50-pt | BANKNIFTY 100-pt | SENSEX 200-pt")

    print(f"\nRunning vX (benchmark)...")
    m_vx = run_variant('vX')
    print(f"  vX  : {m_vx.get('trades',0)} trades | WR {m_vx.get('win_rate',0):.1f}% | "
          f"Net ₹{m_vx.get('net',0):,.0f} | DD {m_vx.get('max_dd',0):.2f}%")

    print(f"Running vG (Greeks-informed)...")
    m_vg = run_variant('vG')
    print(f"  vG  : {m_vg.get('trades',0)} trades | WR {m_vg.get('win_rate',0):.1f}% | "
          f"Net ₹{m_vg.get('net',0):,.0f} | DD {m_vg.get('max_dd',0):.2f}%")

    # ── Head-to-head table ─────────────────────────────────────────────────────
    print(f"\n{'─' * W}")
    print(f"  {'Metric':<25} {'vX (baseline)':>16} {'vG (new)':>16}  {'Δ (vG–vX)':>12}  {'Better?':>8}")
    print(f"  {'─' * 70}")

    def _cmp_row(label, key, fmt='.1f', pct=False, higher_better=True):
        vx_v = m_vx.get(key, 0)
        vg_v = m_vg.get(key, 0)
        suf  = '%' if pct else ''
        if fmt == ',.0f':
            vx_s  = f"₹{vx_v:,.0f}"
            vg_s  = f"₹{vg_v:,.0f}"
            d_s   = f"{'▲' if (vg_v-vx_v)>=0 else '▼'}₹{abs(vg_v-vx_v):,.0f}"
        else:
            vx_s  = f"{vx_v:{fmt}}{suf}"
            vg_s  = f"{vg_v:{fmt}}{suf}"
            delta = vg_v - vx_v
            d_s   = f"{delta:+{fmt}}{suf}"
        delta = vg_v - vx_v
        good  = (delta >= 0) if higher_better else (delta <= 0)
        flag  = '✓  vG' if good else '✗  vX'
        print(f"  {label:<25} {vx_s:>16} {vg_s:>16}  {d_s:>12}  {flag:>8}")

    _cmp_row('Trades',             'trades',   '.0f')
    _cmp_row('Win Rate',           'win_rate',  '.1f', pct=True)
    _cmp_row('Win / Loss Ratio',   'wl_ratio',  '.2f')
    _cmp_row('Profit Factor',      'pf',        '.2f')
    _cmp_row('Net P&L',            'net',       ',.0f')
    _cmp_row('Max Drawdown',       'max_dd',    '.2f', pct=True,  higher_better=False)
    _cmp_row('Sharpe',             'sharpe',    '.2f')
    _cmp_row('Avg Win',            'avg_win',   ',.0f')
    _cmp_row('Avg Loss',           'avg_loss',  ',.0f', higher_better=False)

    print(f"  {'─' * 70}")

    # Trades filtered by vG (vX trades - vG trades)
    _vx_n = m_vx.get('trades', 0)
    _vg_n = m_vg.get('trades', 0)
    _filtered = _vx_n - _vg_n
    if _vx_n > 0:
        print(f"\n  Trades filtered by vG: {_filtered} of {_vx_n} vX signals "
              f"({_filtered/_vx_n*100:.1f}% blocked)")

    # Per-instrument breakdown
    print(f"\n  PER-INSTRUMENT BREAKDOWN")
    print(f"  {'─' * 70}")
    hdr = f"  {'Instrument':<12} {'vX Trades':>9} {'vX WR%':>7} {'vX Net':>10}  {'vG Trades':>9} {'vG WR%':>7} {'vG Net':>10}"
    print(hdr)
    print(f"  {'─' * 70}")

    inst_list = []
    if len(data_nifty)  > 0: inst_list.append(('NIFTY',     data_nifty))
    if len(data_bnf)    > 0: inst_list.append(('BANKNIFTY', data_bnf))
    if len(data_sensex) > 0: inst_list.append(('SENSEX',    data_sensex))

    for _inst, _idata in inst_list:
        _t_vx, _e_vx, _ = run_backtest(_idata, _inst, 'vX')
        _t_vg, _e_vg, _ = run_backtest(_idata, _inst, 'vG')
        _cap = config.INSTRUMENTS[_inst]['capital']
        _mx = compute_metrics(_t_vx, _e_vx, _cap)
        _mg = compute_metrics(_t_vg, _e_vg, _cap)
        _vx_wr  = f"{_mx.get('win_rate',0):.1f}%"
        _vg_wr  = f"{_mg.get('win_rate',0):.1f}%"
        _vx_net = f"₹{_mx.get('net',0):,.0f}"
        _vg_net = f"₹{_mg.get('net',0):,.0f}"
        print(f"  {_inst:<12} {_mx.get('trades',0):>9} {_vx_wr:>7} {_vx_net:>10}  "
              f"{_mg.get('trades',0):>9} {_vg_wr:>7} {_vg_net:>10}")
    print(f"  {'─' * 70}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n  VERDICT")
    print(f"  {'─' * 70}")
    wr_better  = m_vg.get('win_rate', 0) > m_vx.get('win_rate', 0)
    net_better = m_vg.get('net', 0)      > m_vx.get('net', 0)
    dd_better  = m_vg.get('max_dd', -99) > m_vx.get('max_dd', -99)
    sh_better  = m_vg.get('sharpe', 0)   > m_vx.get('sharpe', 0)

    wins_vg = sum([wr_better, net_better, dd_better, sh_better])
    if wins_vg >= 3:
        verdict = "vG WINS ✓✓ — superior on 3+ metrics; consider adopting vG"
    elif wins_vg == 2:
        verdict = "MIXED ≈ — roughly equivalent; vG offers different risk profile"
    else:
        verdict = "vX WINS — extra filters hurt more than they help (overfiltering)"

    print(f"  WR better  : {'✓ vG' if wr_better  else '✗ vX'}")
    print(f"  Net better : {'✓ vG' if net_better else '✗ vX'}")
    print(f"  DD better  : {'✓ vG' if dd_better  else '✗ vX'}")
    print(f"  Sharpe     : {'✓ vG' if sh_better  else '✗ vX'}")
    print(f"\n  {verdict}")
    print(f"{'=' * W}")

    # ── Save trade log for vG ─────────────────────────────────────────────────
    trades_vg = m_vg.get('trades_df', pd.DataFrame())
    if len(trades_vg) > 0:
        trades_vg = trades_vg.copy()
        trades_vg['Entry Hour']    = trades_vg['Entry Date'].dt.hour
        trades_vg['Entry Weekday'] = trades_vg['Entry Date'].dt.day_name()
        trades_vg['Win']           = (trades_vg['P&L Net'] > 0).astype(int)
        csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'backtest_trades_vG.csv'
        )
        trades_vg.to_csv(csv_path, index=False)
        print(f"\nvG trade log saved → {os.path.abspath(csv_path)}")

    # ── Overlay equity chart: vX vs vG ───────────────────────────────────────
    eq_vx = m_vx.get('equity_df', pd.DataFrame())
    eq_vg = m_vg.get('equity_df', pd.DataFrame())
    if len(eq_vx) > 0 and len(eq_vg) > 0:
        fig, axes = plt.subplots(2, 1, figsize=(16, 10))
        fig.suptitle(f"{config.BOT_NAME} — vG (Greeks-Informed) vs vX (Baseline)\n"
                     f"vX: {m_vx.get('trades',0)} trades | WR {m_vx.get('win_rate',0):.1f}% | Net ₹{m_vx.get('net',0):,.0f}   "
                     f"vG: {m_vg.get('trades',0)} trades | WR {m_vg.get('win_rate',0):.1f}% | Net ₹{m_vg.get('net',0):,.0f}",
                     fontsize=12, fontweight='bold')
        _init = m_vx['initial']

        # Equity overlay
        ax1 = axes[0]
        ax1.plot(eq_vx['Date'], eq_vx['Capital'], lw=2, color='#3498db', label='vX (baseline)', alpha=0.9)
        ax1.plot(eq_vg['Date'], eq_vg['Capital'], lw=2, color='#e67e22', label='vG (Greeks-informed)', alpha=0.9)
        ax1.axhline(_init, color='gray', ls='--', alpha=0.5, label='Initial capital')
        ax1.set_ylabel('Capital (₹)')
        ax1.legend()
        ax1.set_title('Equity Curves: vX vs vG')
        ax1.grid(alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))

        # Drawdown comparison
        ax2 = axes[1]
        for _eqdf, _col, _lbl in [(eq_vx, '#3498db', 'vX'), (eq_vg, '#e67e22', 'vG')]:
            _eqdf = _eqdf.copy()
            _eqdf['Peak']     = _eqdf['Capital'].cummax()
            _eqdf['Drawdown'] = (_eqdf['Capital'] - _eqdf['Peak']) / _eqdf['Peak'] * 100
            ax2.fill_between(_eqdf['Date'], _eqdf['Drawdown'], 0,
                             alpha=0.3, color=_col, label=_lbl)
        ax2.set_ylabel('Drawdown (%)')
        ax2.set_title('Drawdown: vX vs vG')
        ax2.legend()
        ax2.grid(alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))

        plt.tight_layout()
        cmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'vG_vs_vX.png'
        )
        plt.savefig(cmp_path, dpi=120, bbox_inches='tight')
        plt.close()
        print(f"vG vs vX equity chart saved → {os.path.abspath(cmp_path)}")

else:
    # Single variant run
    print(f"\nRunning variant {VARIANT}...")
    m = run_variant(VARIANT)
    print_results(m)

    trades = m.get('trades_df', pd.DataFrame())
    equity = m.get('equity_df', pd.DataFrame())

    if len(trades) > 0:
        # Save trade log
        trades_export = trades.copy()
        trades_export['Entry Hour']    = trades_export['Entry Date'].dt.hour
        trades_export['Entry Minute']  = trades_export['Entry Date'].dt.minute
        trades_export['Entry Weekday'] = trades_export['Entry Date'].dt.day_name()
        trades_export['Exit Hour']     = trades_export['Exit Date'].dt.hour
        trades_export['Win']           = (trades_export['P&L Net'] > 0).astype(int)
        csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'backtest_trades.csv'
        )
        trades_export.to_csv(csv_path, index=False)
        print(f"\nTrade log saved → {os.path.abspath(csv_path)}")

        # Equity + drawdown chart
        if len(equity) > 0:
            fig, axes = plt.subplots(3, 1, figsize=(16, 15))
            fig.suptitle(f"{config.BOT_NAME} [variant={VARIANT}] — NIFTY + BANKNIFTY",
                         fontsize=14, fontweight='bold')

            eq = equity
            init = m['initial']
            ax = axes[0]
            ax.plot(eq['Date'], eq['Capital'], lw=2, color='#27ae60')
            ax.axhline(init, color='#e74c3c', ls='--', alpha=0.6, label='Initial capital')
            ax.fill_between(eq['Date'], init, eq['Capital'],
                            where=eq['Capital'] >= init, alpha=0.2, color='#27ae60')
            ax.fill_between(eq['Date'], init, eq['Capital'],
                            where=eq['Capital'] <  init, alpha=0.2, color='#e74c3c')
            ax.set_title('Equity Curve', fontsize=12)
            ax.set_ylabel('Capital (₹)')
            ax.legend()
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
            ax.grid(alpha=0.3)

            eq['Peak']     = eq['Capital'].cummax()
            eq['Drawdown'] = (eq['Capital'] - eq['Peak']) / eq['Peak'] * 100
            ax = axes[1]
            ax.plot(eq['Date'], eq['Drawdown'], lw=1.5, color='#c0392b')
            ax.fill_between(eq['Date'], eq['Drawdown'], 0, alpha=0.3, color='#c0392b')
            ax.set_title('Drawdown', fontsize=12)
            ax.set_ylabel('Drawdown (%)')
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %y'))
            ax.grid(alpha=0.3)

            ax = axes[2]
            full_exits = trades[trades['Exit Reason'] != 'Partial 80%']
            ax.hist(full_exits['P&L Net'], bins=40, edgecolor='black', alpha=0.7, color='#2980b9')
            ax.axvline(0, color='#e74c3c', ls='--', lw=2, label='Break-even')
            ax.set_title('P&L Distribution (Net, full exits)', fontsize=12)
            ax.set_xlabel('P&L per trade (₹)')
            ax.set_ylabel('Frequency')
            ax.legend()
            ax.grid(alpha=0.3)

            plt.tight_layout()
            out_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), '..', 'backtest_results.png'
            )
            plt.savefig(out_path, dpi=120, bbox_inches='tight')
            plt.close()
            print(f"Chart saved → {os.path.abspath(out_path)}")
