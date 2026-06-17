"""
daily_debrief.py — Post-Market Learning Logger  (enhanced v2)
Runs at 15:45 IST Mon–Fri via fno_t_bot_debrief.timer

Computes and appends a rich JSON record to logs/market_learnings.jsonl for
each instrument. The record combines:
  • Market structure (OR, gap, ADX profile, regime)
  • Trade outcomes (from JSONL trade log)
  • 11:30 option snapshot (Black-Scholes — what the option was worth at checkpoint)
  • Volatility rank (today's range vs recent history)
  • Regime label (from market_regime.py)

Fields in each JSONL record:
  date, instrument, day_of_week,
  or_high, or_low, or_width_pct,
  breakout_direction, adx_at_entry, adx_at_1130, adx_at_close, adx_peak,
  ema_aligned_1130,
  gap_pct, gap_type,
  path_a_fired, path_a_reentry,
  entry_price_used, entry_underlying, lots,
  option_pct_at_1130,   ← Black-Scholes at 11:30 (key for adaptive_params)
  option_pct_peak,      ← max option gain during session (BS walk)
  option_pct_final,     ← actual exit pct from JSONL
  hold_decision,        ← HOLD / CLOSE / NA (from exit_reason + exit_time)
  exit_reason,
  total_pnl_net, total_pnl_pct,
  num_trades,
  vol_rank,             ← today's range percentile vs last 20 days (0–100)
  consecutive_losses,   ← loss streak at time of today's trade
  market_regime,        ← from market_regime.py
  posture,              ← AGGRESSIVE / NORMAL / CAUTIOUS
  regime_at_open,       ← same as market_regime (from bot's JSONL record if present)
  index_open, index_close, index_change_pct

Usage:
  python daily_debrief.py              # NIFTY + BANKNIFTY
  python daily_debrief.py NIFTY        # single instrument
  python daily_debrief.py --date 2026-04-24 NIFTY   # backfill a specific date
"""

from __future__ import annotations

import json
import math
import os
import sys
import logging
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path

import pytz

_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)

import config

IST = pytz.timezone('Asia/Kolkata')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [DEBRIEF] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('daily_debrief')

LOG_DIR    = os.path.join(_DIR, 'logs')
JSONL_PATH = os.path.join(LOG_DIR, 'market_learnings.jsonl')
os.makedirs(LOG_DIR, exist_ok=True)


# ── Indicator helpers ─────────────────────────────────────────────────────────

def compute_adx(df, period: int = 14):
    """Return (adx, DI_plus, DI_minus) series."""
    import pandas as pd
    high, low, close = df['High'], df['Low'], df['Close']
    tr   = pd.concat([high - low, (high - close.shift()).abs(),
                      (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1/period, min_periods=period).mean()
    up   = high.diff()
    dn   = -low.diff()
    dip  = up.where(up > dn, 0).clip(lower=0)
    dim  = dn.where(dn > up, 0).clip(lower=0)
    dip_s = dip.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, float('nan')) * 100
    dim_s = dim.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, float('nan')) * 100
    dx    = (dip_s - dim_s).abs() / (dip_s + dim_s).replace(0, float('nan')) * 100
    adx   = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx, dip_s, dim_s


def compute_ema(series, span: int):
    return series.ewm(span=span, adjust=False).mean()


def bs_price(option_type: str, S: float, K: float, T: float, sigma: float,
             r: float = 0.065) -> float:
    """Black-Scholes European option price. Returns 0 on error."""
    try:
        from math import log, sqrt, exp
        from statistics import NormalDist
        nd = NormalDist()
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.0
        d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)
        if option_type.upper() in ('CALL', 'CE'):
            return S * nd.cdf(d1) - K * exp(-r * T) * nd.cdf(d2)
        else:
            return K * exp(-r * T) * nd.cdf(-d2) - S * nd.cdf(-d1)
    except Exception:
        return 0.0


# ── Data loader ───────────────────────────────────────────────────────────────

def _data_dir(instrument: str) -> str | None:
    dirs = {
        'NIFTY'    : os.path.join(_DIR, '..', 'data', 'nifty_5min'),
        'BANKNIFTY': os.path.join(_DIR, '..', 'data', 'banknifty_5min'),
        'SENSEX'   : os.path.join(_DIR, '..', 'data', 'sensex_5min'),
    }
    return dirs.get(instrument)


def load_5min(instrument: str, days_back: int = 25) -> 'pd.DataFrame | None':
    import pandas as pd
    ddir = _data_dir(instrument)
    if not ddir or not os.path.isdir(ddir):
        return None
    csvs = sorted(Path(ddir).glob('*.csv'))
    if not csvs:
        return None
    recent = csvs[-max(days_back * 2, 30):]
    frames = []
    for p in recent:
        try:
            # Support both 'Datetime' (local dev) and 'ts' (EC2 / Fyers format)
            try:
                df = pd.read_csv(p, parse_dates=['Datetime'], index_col='Datetime')
            except (KeyError, ValueError):
                df = pd.read_csv(p, parse_dates=['ts'], index_col='ts')
            frames.append(df)
        except Exception:
            pass
    if not frames:
        return None
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep='first')]


def add_indicators(df):
    import pandas as pd
    df = df.copy()
    adx, dip, dim = compute_adx(df)
    df['ADX']      = adx
    df['DI_plus']  = dip
    df['DI_minus'] = dim
    df['EMA_fast'] = compute_ema(df['Close'], config.MOMENTUM_EMA_FAST)
    df['EMA_slow'] = compute_ema(df['Close'], config.MOMENTUM_EMA_SLOW)
    return df


# ── Trade log reader ──────────────────────────────────────────────────────────

def read_trades(instrument: str, for_date: date) -> list[dict]:
    """Read today's JSONL trade records for an instrument."""
    pattern = f'FnO_T_Bot_{instrument}_trades_{for_date.strftime("%Y-%m-%d")}.jsonl'
    trade_path = os.path.join(LOG_DIR, pattern)

    # Also check EC2 path for running from EC2
    ec2_path = f'/opt/trading_bot/live_bot/logs/{pattern}'

    trades = []
    for path in [trade_path, ec2_path]:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                trades.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except OSError:
                pass

    # Deduplicate by entry_time
    seen = set()
    unique = []
    for t in trades:
        k = (t.get('entry_time'), t.get('type'), t.get('strike'))
        if k not in seen:
            seen.add(k)
            unique.append(t)
    return unique


# ── Volatility rank ───────────────────────────────────────────────────────────

def compute_vol_rank(df_all, today: date, lookback: int = 20) -> float | None:
    """
    Percentile rank of today's intraday range vs last `lookback` trading days.
    Returns 0–100 (100 = highest volatility in lookback window).
    """
    import pandas as pd

    all_dates = sorted(set(df_all.index.date))
    past_dates = [d for d in all_dates if d < today][-lookback:]
    if len(past_dates) < 5:
        return None

    ranges = []
    for d in past_dates:
        df_d = df_all[df_all.index.date == d]
        if len(df_d) < 5:
            continue
        o = float(df_d['Open'].iloc[0])
        h = float(df_d['High'].max())
        l = float(df_d['Low'].min())
        if o > 0:
            ranges.append((h - l) / o * 100)

    if not ranges:
        return None

    df_today = df_all[df_all.index.date == today]
    if df_today.empty:
        return None

    o = float(df_today['Open'].iloc[0])
    h = float(df_today['High'].max())
    l = float(df_today['Low'].min())
    today_range = (h - l) / o * 100 if o > 0 else 0

    below = sum(1 for r in ranges if r < today_range)
    return round(below / len(ranges) * 100, 1)


# ── Consecutive loss streak ───────────────────────────────────────────────────

def consecutive_losses(instrument: str, before_date: date) -> int:
    """Count consecutive losing PATH-A trading days before today."""
    if not os.path.exists(JSONL_PATH):
        return 0

    records = []
    try:
        with open(JSONL_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if (rec.get('instrument') == instrument and
                            rec.get('path_a_fired') and
                            rec.get('date', '') < before_date.isoformat()):
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except OSError:
        return 0

    records.sort(key=lambda r: r.get('date', ''))
    streak = 0
    for r in reversed(records):
        if (r.get('total_pnl_net') or 0) <= 0:
            streak += 1
        else:
            break
    return streak


# ── 11:30 option price via Black-Scholes ──────────────────────────────────────

def option_pct_at_time(trade: dict, df_today, target_time: dtime,
                       hv_today: float) -> float | None:
    """
    Estimate option P&L % at `target_time` using Black-Scholes.
    Uses the 5-min bar at or just before target_time for index price.
    """
    entry_price = trade.get('entry_price')
    strike      = trade.get('strike')
    opt_type    = trade.get('type')
    entry_time_str = trade.get('entry_time', '')

    if not all([entry_price, strike, opt_type, entry_time_str]):
        return None

    # Index price at target_time
    bars_at = df_today[df_today.index.time <= target_time]
    if bars_at.empty:
        return None
    index_px = float(bars_at['Close'].iloc[-1])

    # Time remaining at target_time (rough approximation)
    T_rem = max(config.DAYS_TO_EXPIRY - 0.5, 0.01) / 365   # ~half-day consumed

    opt_px_at = bs_price(opt_type, index_px, strike, T_rem, hv_today)
    if opt_px_at <= 0 or entry_price <= 0:
        return None

    return round((opt_px_at - entry_price) / entry_price * 100, 2)


def option_pct_peak(trade: dict, df_today, hv_today: float) -> float | None:
    """
    Estimate peak option P&L % by walking through each 5-min bar.
    Uses Black-Scholes — approximate but directionally correct.
    """
    entry_price = trade.get('entry_price')
    strike      = trade.get('strike')
    opt_type    = trade.get('type')
    entry_time_str = trade.get('entry_time', '')

    if not all([entry_price, strike, opt_type, entry_time_str]):
        return None

    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
        entry_time_only = entry_dt.astimezone(IST).time()
    except Exception:
        return None

    # Only look at bars after entry
    bars_after = df_today[df_today.index.time >= entry_time_only]
    if bars_after.empty:
        return None

    peak_pct = None
    for i, (ts, row) in enumerate(bars_after.iterrows()):
        T_rem = max(config.DAYS_TO_EXPIRY - i * 5 / (6.25 * 60), 0.01) / 365
        opt_px = bs_price(opt_type, float(row['Close']), strike, T_rem, hv_today)
        if opt_px > 0 and entry_price > 0:
            pct = (opt_px - entry_price) / entry_price * 100
            if peak_pct is None or pct > peak_pct:
                peak_pct = pct

    return round(peak_pct, 2) if peak_pct is not None else None


# ── Hold decision classifier ──────────────────────────────────────────────────

def classify_hold_decision(trade: dict) -> str:
    """
    Determine if the position was HELD past 11:30 or CLOSED at 11:30.
    Returns: 'HOLD' | 'CLOSE' | 'NA' (no PATH-A trade today)
    """
    exit_reason = trade.get('exit_reason', '')
    exit_time_str = trade.get('exit_time', '')

    if not exit_time_str:
        return 'NA'

    try:
        exit_dt   = datetime.fromisoformat(exit_time_str)
        exit_time = exit_dt.astimezone(IST).time()
    except Exception:
        return 'NA'

    # If exit was at or very near 11:30 → CLOSE decision
    if dtime(11, 28) <= exit_time <= dtime(11, 35):
        return 'CLOSE'

    # If exit was significantly after 11:30 (main session) → HOLD
    if exit_time > dtime(11, 35):
        return 'HOLD'

    # Exit before 11:30 = stop-loss / target — no checkpoint needed
    return 'NA'


# ── Regime classifier (fallback if market_regime unavailable) ─────────────────

def classify_regime_simple(df_today) -> str:
    """Simple single-day regime from OHLCV (no external module needed)."""
    if len(df_today) < 10:
        return 'UNKNOWN'
    main = df_today[df_today.index.time >= dtime(11, 0)]
    try:
        from market_regime import _compute_adx_series
        adx_series = _compute_adx_series(df_today)
        adx_main   = adx_series[main.index]
        adx_avg    = float(adx_main.mean())
    except Exception:
        adx_avg = 0

    o = float(df_today['Open'].iloc[0])
    c = float(df_today['Close'].iloc[-1])
    h = float(df_today['High'].max())
    l = float(df_today['Low'].min())
    change_pct = (c - o) / o * 100
    range_pct  = (h - l) / o * 100

    if adx_avg >= 25:
        return 'TRENDING_BULL' if change_pct > 0.3 else ('TRENDING_BEAR' if change_pct < -0.3 else 'CHOPPY')
    if range_pct >= 1.5 and abs(change_pct) < 0.3:
        return 'HIGH_VOL_CHOPPY'
    return 'CHOPPY'


def get_regime_and_posture(instrument: str, for_date: date) -> tuple[str, str]:
    """Return (regime, posture) using market_regime.py if available."""
    try:
        from market_regime import RegimeAnalyzer
        snap = RegimeAnalyzer(instrument, lookback=12).get_snapshot(snap_lookback=5)
        return snap.regime, snap.posture
    except Exception:
        return 'UNKNOWN', 'NORMAL'


# ── Core debrief function ─────────────────────────────────────────────────────

def debrief_instrument(instrument: str, for_date: date) -> dict | None:
    logger.info(f'Debriefing {instrument} for {for_date}...')

    df_all = load_5min(instrument, days_back=25)
    if df_all is None or df_all.empty:
        logger.warning(f'No 5-min data for {instrument}')
        return None

    df_all = add_indicators(df_all)

    df_today = df_all[df_all.index.date == for_date]
    if len(df_today) < 5:
        logger.warning(f'Not enough bars for {instrument} on {for_date}')
        return None

    # ── Opening Range ────────────────────────────────────────────────────────
    or_bars  = df_today[df_today.index.time < dtime(9, 30)]
    or_high  = float(or_bars['High'].max())  if len(or_bars) >= 2 else None
    or_low   = float(or_bars['Low'].min())   if len(or_bars) >= 2 else None
    or_width = (round((or_high - or_low) / or_high * 100, 3)
                if or_high and or_high > 0 else None)

    # ── Price levels ─────────────────────────────────────────────────────────
    open_px   = float(df_today['Open'].iloc[0])
    close_px  = float(df_today['Close'].iloc[-1])
    high_px   = float(df_today['High'].max())
    low_px    = float(df_today['Low'].min())

    # Gap vs previous day
    past_days = [d for d in sorted(set(df_all.index.date)) if d < for_date]
    if past_days:
        prev_close = float(df_all[df_all.index.date == past_days[-1]]['Close'].iloc[-1])
    else:
        prev_close = open_px

    gap_pct  = round((open_px - prev_close) / prev_close * 100, 3)
    change_pct = round((close_px - prev_close) / prev_close * 100, 3)

    gap_type = None
    if abs(gap_pct) >= 0.3:
        if gap_pct > 0:
            gap_type = 'GAP_AND_GO_UP' if close_px > open_px else 'GAP_FADE_UP'
        else:
            gap_type = 'GAP_AND_GO_DN' if close_px < open_px else 'GAP_FADE_DN'

    # ── ADX at key times ─────────────────────────────────────────────────────
    def _adx_at(t: dtime) -> float | None:
        bars = df_today[df_today.index.time <= t]
        return round(float(bars['ADX'].iloc[-1]), 2) if len(bars) > 0 else None

    adx_at_entry = _adx_at(dtime(9, 45))
    adx_at_1130  = _adx_at(dtime(11, 30))
    adx_at_close = _adx_at(dtime(14, 30))
    adx_peak     = round(float(df_today['ADX'].max()), 2)

    # ── EMA alignment at 11:30 ───────────────────────────────────────────────
    bar_1130 = df_today[df_today.index.time <= dtime(11, 30)]
    ema_aligned_1130 = None
    if len(bar_1130) > 0:
        ef = float(bar_1130['EMA_fast'].iloc[-1])
        es = float(bar_1130['EMA_slow'].iloc[-1])
        ema_aligned_1130 = 'CALL' if ef > es else 'PUT'

    # ── Breakout direction ───────────────────────────────────────────────────
    breakout_direction = None
    if or_high and or_low:
        bar_945 = df_today[df_today.index.time <= dtime(9, 45)]
        if len(bar_945) > 0:
            px = float(bar_945['Close'].iloc[-1])
            buf = config.PATH_A_BUFFER
            if px > or_high * (1 + buf):
                breakout_direction = 'CALL'
            elif px < or_low * (1 - buf):
                breakout_direction = 'PUT'

    # ── Volatility rank ──────────────────────────────────────────────────────
    vol_rank = compute_vol_rank(df_all, for_date, lookback=20)

    # ── Historical volatility (for BS pricing) ───────────────────────────────
    import pandas as pd
    returns = df_all['Close'].pct_change().dropna()
    hv_today = float(returns.std() * (252 * 75) ** 0.5)  # annualised from 5-min

    # ── Trade outcomes from JSONL ────────────────────────────────────────────
    trades = read_trades(instrument, for_date)

    # Find PATH-A trade (path == 'A' or 'A_HELD' or entry_time 09:30–11:00)
    path_a_trade = None
    for t in trades:
        path = t.get('path', '')
        if path in ('A', 'A_HELD'):
            path_a_trade = t
            break
        # Fallback: entry between 09:30 and 11:05
        try:
            et = datetime.fromisoformat(t.get('entry_time', '')).astimezone(IST)
            if dtime(9, 30) <= et.time() <= dtime(11, 5):
                path_a_trade = t
                break
        except Exception:
            pass

    path_a_reentry_trade = next(
        (t for t in trades if t.get('path') == 'A_REENTRY'), None
    )

    total_pnl_net = round(sum(t.get('pnl_net', 0)  for t in trades), 2)
    total_pnl_pct = round(sum(t.get('pnl_pct', 0)  for t in trades), 2)
    exit_reason   = path_a_trade.get('exit_reason') if path_a_trade else None

    # ── 11:30 option snapshot + peak ─────────────────────────────────────────
    opt_pct_1130 = None
    opt_pct_peak = None
    entry_price_used = None
    entry_underlying = None
    lots_used = None
    hold_decision = 'NA'

    if path_a_trade:
        entry_price_used = path_a_trade.get('entry_price')
        entry_underlying = path_a_trade.get('entry_underlying')
        lots_used        = path_a_trade.get('lots', 1)
        hold_decision    = classify_hold_decision(path_a_trade)

        opt_pct_1130 = option_pct_at_time(path_a_trade, df_today, dtime(11, 30), hv_today)
        opt_pct_peak = option_pct_peak(path_a_trade, df_today, hv_today)

    option_pct_final = path_a_trade.get('pnl_pct') if path_a_trade else None

    # ── Consecutive losses ────────────────────────────────────────────────────
    consec_losses = consecutive_losses(instrument, for_date)

    # ── Regime and posture ────────────────────────────────────────────────────
    market_regime, posture = get_regime_and_posture(instrument, for_date)
    # Override with single-day computation if no module
    if market_regime == 'UNKNOWN':
        market_regime = classify_regime_simple(df_today)

    # ── Assemble record ───────────────────────────────────────────────────────
    record = {
        'date'               : for_date.isoformat(),
        'instrument'         : instrument,
        'day_of_week'        : for_date.strftime('%a'),
        'or_high'            : or_high,
        'or_low'             : or_low,
        'or_width_pct'       : or_width,
        'breakout_direction' : breakout_direction,
        'adx_at_entry'       : adx_at_entry,
        'adx_at_1130'        : adx_at_1130,
        'adx_at_close'       : adx_at_close,
        'adx_peak'           : adx_peak,
        'ema_aligned_1130'   : ema_aligned_1130,
        'gap_pct'            : gap_pct,
        'gap_type'           : gap_type,
        'path_a_fired'       : path_a_trade is not None,
        'path_a_reentry'     : path_a_reentry_trade is not None,
        'entry_price_used'   : entry_price_used,
        'entry_underlying'   : entry_underlying,
        'lots'               : lots_used,
        'option_pct_at_1130' : opt_pct_1130,
        'option_pct_peak'    : opt_pct_peak,
        'option_pct_final'   : option_pct_final,
        'hold_decision'      : hold_decision,
        'exit_reason'        : exit_reason,
        'total_pnl_net'      : total_pnl_net,
        'total_pnl_pct'      : total_pnl_pct,
        'num_trades'         : len(trades),
        'vol_rank'           : vol_rank,
        'consecutive_losses' : consec_losses,
        'market_regime'      : market_regime,
        'posture'            : posture,
        'regime_at_open'     : market_regime,  # alias for adaptive_params.py
        'index_open'         : round(open_px,  2),
        'index_close'        : round(close_px, 2),
        'index_change_pct'   : change_pct,
    }

    logger.info(
        f'  {instrument}: regime={market_regime} | posture={posture} | '
        f'PATH-A={path_a_trade is not None} | '
        f'hold={hold_decision} | P&L=₹{total_pnl_net:+,.0f} | '
        f'vol_rank={vol_rank}'
    )
    return record


# ── Duplicate guard ───────────────────────────────────────────────────────────

def record_exists(instrument: str, for_date: str) -> bool:
    """Check if we already have a record for this instrument+date."""
    if not os.path.exists(JSONL_PATH):
        return False
    target_date = for_date if isinstance(for_date, str) else for_date.isoformat()
    try:
        with open(JSONL_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get('instrument') == instrument and rec.get('date') == target_date:
                        return True
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return False


def append_record(record: dict) -> None:
    with open(JSONL_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')
    logger.info(f'  → Appended to {JSONL_PATH}')


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # Parse --date flag
    for_date = datetime.now(IST).date()
    if '--date' in args:
        idx = args.index('--date')
        if idx + 1 < len(args):
            try:
                for_date = date.fromisoformat(args[idx + 1])
                args = [a for a in args if a not in ('--date', args[idx + 1])]
            except ValueError:
                logger.error(f'Invalid date format: {args[idx + 1]}. Use YYYY-MM-DD.')
                sys.exit(1)

    instruments = [a for a in args if not a.startswith('-')] or ['NIFTY', 'BANKNIFTY']

    if for_date.weekday() >= 5 and '--date' not in sys.argv:
        logger.info(f'Weekend ({for_date.strftime("%a")}) — no debrief needed.')
        return

    logger.info(f'=== Daily Debrief {for_date} ===')

    for inst in instruments:
        if inst not in config.INSTRUMENTS:
            logger.error(f'Unknown instrument: {inst}')
            continue

        if record_exists(inst, for_date) and '--force' not in sys.argv:
            logger.info(f'{inst}: record already exists for {for_date}. '
                        f'Use --force to overwrite.')
            continue

        record = debrief_instrument(inst, for_date)
        if record:
            append_record(record)

    logger.info('=== Debrief complete ===')


if __name__ == '__main__':
    main()
