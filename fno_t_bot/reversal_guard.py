# -*- coding: utf-8 -*-
"""
Reversal Guard — Trend Exhaustion Filter
=========================================
Detects when a vX signal is entering an EXHAUSTED move rather than a fresh
trend, and suppresses or reduces those entries.

Problem this solves
-------------------
The EMA 9/21 crossover fires 1-3 bars AFTER a trend starts.  On large,
fast moves (e.g. a sharp 0.6% drop in 30 min), the crossover may fire just
as the move is running out of steam.  The option's intrinsic premium is
already priced in, and a reversal stops the position out.

How it works
------------
For every bar where a vX signal fires, compute a Reversal Risk Score (0–100).
Five independent components measure exhaustion from different angles:

  1. RSI Extreme         (0–25 pts)  RSI deviating from 50 midline at entry
                                      Scores from RSI<55/PUT or RSI>45/CALL
                                      Full 25pts at RSI<25 / RSI>75
  2. RSI Divergence      (0–25 pts)  Price making new extreme but RSI diverging
  3. VWAP Overextension  (0–20 pts)  Price already >0.1% from daily VWAP anchor
                                      Full 20pts at >0.5% extension
  4. ADX Declining       (0–15 pts)  ADX falling over last 5 bars (trend fading)
  5. Consecutive Candles (0–15 pts)  3+ candles in same direction (climax move)
                                      Full 15pts at 5+ consecutive

  Optional bonus:
  6. VIX Spike           (0–15 pts)  India VIX rose >8% intraday (fear peak)
                                      — added automatically when VIX data present

Risk levels:
  Score 0–29   LOW      → take signal normally (full lots)
  Score 30–49  MODERATE → take signal at 1 lot only (reduce size)
  Score 50+    HIGH     → skip signal entirely

Usage
-----
  # Import into paper_bot.py:
  from reversal_guard import compute_reversal_risk
  risk = compute_reversal_risk(data, i, 'PUT')
  if risk['score'] >= 60: skip()

  # Standalone backtest (all instruments):
  python reversal_guard.py

  # Focus on a single instrument:
  python reversal_guard.py NIFTY

  # Show only recent days (last N trading days):
  python reversal_guard.py NIFTY --recent 20
"""

from __future__ import annotations

import os, sys, math, warnings
import datetime as dt
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from scipy.stats import norm
warnings.filterwarnings('ignore')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE     = Path(__file__).parent
_DATA_DIR = _HERE.parent / 'data'
sys.path.insert(0, str(_HERE))
import config

BARS_PER_DAY = 75


# ─────────────────────────────────────────────────────────────────────────────
# 1.  CORE MODULE: compute_reversal_risk()
#     (importable by paper_bot.py — no side effects)
# ─────────────────────────────────────────────────────────────────────────────

def compute_reversal_risk(data: pd.DataFrame, i: int,
                          signal_type: str) -> dict:
    """
    Compute exhaustion/reversal risk for a potential signal at bar i.

    Parameters
    ----------
    data        : DataFrame with columns EMA_fast, EMA_slow, ADX, RSI,
                  VWAP, ATR14, Close, High, Low, Open.
                  Optionally: VIX (India VIX aligned to same index).
    i           : integer bar index (iloc position)
    signal_type : 'CALL' or 'PUT'

    Returns
    -------
    dict with keys:
      score      : int 0-100 (higher = more exhaustion risk)
      risk_level : 'LOW' | 'MODERATE' | 'HIGH'
      skip       : bool   (True when score >= 50)
      reduce_lots: bool   (True when score >= 30 — use 1 lot regardless)
      components : dict of sub-scores
      reason     : human-readable summary of dominant signals
    """
    if i < 10:
        return _safe_result(0, {})

    row   = data.iloc[i]
    close = float(row['Close'])
    rsi   = float(row.get('RSI',  50.0))
    adx   = float(row.get('ADX',  25.0))
    vwap  = float(row.get('VWAP', close))
    is_put = (signal_type == 'PUT')

    components = {}

    # ── 1. RSI Extreme (0–25 pts) ──────────────────────────────────────────
    # PUT entered when RSI is already low → risk of bounce back up
    # CALL entered when RSI is already high → risk of fade
    # Soft curve: starts scoring from RSI<55/PUT, RSI>45/CALL (midline ±5)
    # Full 25pts at RSI<25 for PUT, RSI>75 for CALL (extreme zones)
    if is_put:
        # 0pts at RSI=55, 25pts at RSI=25 (range of 30 pts)
        components['rsi_extreme'] = int(min(25, max(0, (55 - rsi) / 30 * 25)))
    else:
        # 0pts at RSI=45, 25pts at RSI=75 (range of 30 pts)
        components['rsi_extreme'] = int(min(25, max(0, (rsi - 45) / 30 * 25)))

    # ── 2. RSI Divergence (0–25 pts) ──────────────────────────────────────
    # Price makes a new directional extreme but RSI does NOT confirm →
    # classic exhaustion divergence (bullish div = risk for PUT; bearish for CALL)
    lb5 = max(0, i - 5)
    lb10 = max(0, i - 10)
    c5   = float(data['Close'].iloc[lb5])
    c10  = float(data['Close'].iloc[lb10])
    r5   = float(data['RSI'].iloc[lb5])
    r10  = float(data['RSI'].iloc[lb10])

    if is_put:
        # Price fell vs 5 bars ago, but RSI rose → bullish divergence → reversal risk
        price_fell   = close < c5
        rsi_rose     = rsi > r5
        # Stronger signal: divergence over 10 bars too
        price_fell10 = close < c10
        rsi_rose10   = rsi > r10
        div_score    = 15 if (price_fell and rsi_rose) else 0
        div_score   += 10 if (price_fell10 and rsi_rose10) else 0
    else:
        # Price rose vs 5 bars ago, but RSI fell → bearish divergence → reversal risk
        price_rose   = close > c5
        rsi_fell     = rsi < r5
        price_rose10 = close > c10
        rsi_fell10   = rsi < r10
        div_score    = 15 if (price_rose and rsi_fell) else 0
        div_score   += 10 if (price_rose10 and rsi_fell10) else 0
    components['rsi_divergence'] = int(min(25, div_score))

    # ── 3. VWAP Overextension (0–20 pts) ──────────────────────────────────
    # Price already stretched far from intraday VWAP anchor →
    # mean-reversion probability increases sharply beyond 0.1%
    if not pd.isna(vwap) and vwap > 0:
        vwap_dist_pct = abs(close - vwap) / vwap * 100
        # For PUT: only score when price is BELOW vwap (overextended down)
        # For CALL: only score when price is ABOVE vwap (overextended up)
        in_right_direction = (is_put and close < vwap) or (not is_put and close > vwap)
        if in_right_direction:
            # 0 at 0.1%, full 20 at 0.5% extension
            components['vwap_extension'] = int(min(20, max(0, (vwap_dist_pct - 0.1) / 0.4 * 20)))
        else:
            components['vwap_extension'] = 0
    else:
        components['vwap_extension'] = 0

    # ── 4. ADX Declining (0–15 pts) ────────────────────────────────────────
    # ADX peaked and is now falling → trend strength is fading
    # Use 5-bar lookback; score scales with the drop magnitude
    adx_5ago = float(data['ADX'].iloc[max(0, i - 5)])
    if adx < adx_5ago and adx_5ago > 0:
        drop_pct = (adx_5ago - adx) / adx_5ago
        components['adx_declining'] = int(min(15, drop_pct * 90))
    else:
        components['adx_declining'] = 0

    # ── 5. Consecutive Same-Direction Candles (0–15 pts) ──────────────────
    # 3+ candles all moving in signal direction = building exhaustion
    # (lowered from 5+ to 3+ to catch climax moves earlier)
    lb = min(12, i)
    closes = data['Close'].iloc[i - lb: i + 1].values
    consecutive = 0
    for k in range(len(closes) - 1, 0, -1):
        if is_put and closes[k] < closes[k - 1]:   # bear candle
            consecutive += 1
        elif not is_put and closes[k] > closes[k - 1]:  # bull candle
            consecutive += 1
        else:
            break
    # Score starts at 3+ consecutive; full 15pts at 5+
    components['consecutive_bars'] = int(min(15, max(0, (consecutive - 2) * 5)))

    # ── 6. VIX Spike (0–15 pts, optional) ─────────────────────────────────
    # If India VIX data is available: a sharp intraday VIX spike >8%
    # signals peak fear, often right at a market bottom (reversal risk for PUT)
    vix_score = 0
    if 'VIX' in data.columns:
        vix_now  = float(row.get('VIX', float('nan')))
        vix_open = float(data['VIX'].iloc[max(0, i - 15)])   # ~75 min ago
        if not (pd.isna(vix_now) or pd.isna(vix_open)) and vix_open > 0:
            vix_change_pct = (vix_now - vix_open) / vix_open * 100
            if is_put and vix_change_pct > 8:    # fear spike while entering PUT
                vix_score = int(min(15, (vix_change_pct - 8) / 7 * 15))
            elif not is_put and vix_change_pct < -8:  # complacency while entering CALL
                vix_score = int(min(15, (-vix_change_pct - 8) / 7 * 15))
    components['vix_spike'] = vix_score

    # ── Total ──────────────────────────────────────────────────────────────
    total = sum(components.values())
    return _safe_result(total, components)


def _safe_result(total: int, components: dict) -> dict:
    score = min(100, total)
    risk  = ('HIGH' if score >= 50 else 'MODERATE' if score >= 30 else 'LOW')

    # Build human-readable dominant reason
    if not components:
        reason = 'insufficient data'
    else:
        top = sorted(components.items(), key=lambda x: -x[1])
        dominant = [f"{k.replace('_',' ')} ({v}pts)" for k, v in top if v > 0]
        reason = ' + '.join(dominant[:3]) if dominant else 'no exhaustion signals'

    return {
        'score'      : score,
        'risk_level' : risk,
        'skip'       : score >= 50,
        'reduce_lots': score >= 30,
        'components' : components,
        'reason'     : reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2.  STANDALONE BACKTEST INFRASTRUCTURE
# ─────────────────────────────────────────────────────────────────────────────

def _load_data(instrument: str) -> pd.DataFrame:
    """Load + compute indicators for one instrument.  Same logic as bot.py."""
    import pytz
    from ta.trend import ADXIndicator, EMAIndicator
    from ta.momentum import RSIIndicator
    from ta.volatility import AverageTrueRange

    IST = pytz.timezone('Asia/Kolkata')
    folder = {'NIFTY':'nifty_5min','BANKNIFTY':'banknifty_5min',
               'SENSEX':'sensex_5min'}[instrument]
    data_dir = _DATA_DIR / folder
    if not data_dir.exists():
        raise FileNotFoundError(data_dir)

    frames = []
    for f in sorted(data_dir.glob('*.csv')):
        df = pd.read_csv(f)
        if 'ts' not in df.columns and 'Datetime' in df.columns:
            df = df.rename(columns={'Datetime':'ts'})
        frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)
    all_df['ts'] = pd.to_datetime(all_df['ts'], utc=True).dt.tz_convert('Asia/Kolkata')
    all_df = all_df.sort_values('ts').set_index('ts')

    # EMA 9/21
    all_df['EMA_fast'] = EMAIndicator(all_df['Close'], 9).ema_indicator()
    all_df['EMA_slow'] = EMAIndicator(all_df['Close'], 21).ema_indicator()

    # ADX, RSI, ATR
    adx_i = ADXIndicator(all_df['High'], all_df['Low'], all_df['Close'], 14)
    all_df['ADX']   = adx_i.adx()
    all_df['RSI']   = RSIIndicator(all_df['Close'], 14).rsi()
    all_df['ATR14'] = AverageTrueRange(
        all_df['High'], all_df['Low'], all_df['Close'], 14
    ).average_true_range()

    # VWAP (daily reset; volume=0 fallback to TP mean)
    all_df['_date'] = all_df.index.date
    all_df['_tp']   = (all_df['High'] + all_df['Low'] + all_df['Close']) / 3
    all_df['_cv']   = all_df.groupby('_date')['Volume'].cumsum()
    all_df['_ctpv'] = (all_df.groupby('_date')
                        .apply(lambda g: (g['_tp'] * g['Volume']).cumsum())
                        .reset_index(level=0, drop=True))
    m = all_df['_cv'] > 0
    all_df['VWAP'] = np.where(m, all_df['_ctpv'] / all_df['_cv'], all_df['_tp'])

    # Historical Volatility
    all_df['Returns'] = all_df['Close'].pct_change()
    all_df['HV'] = (all_df['Returns'].rolling(30).std()
                    * np.sqrt(252 * BARS_PER_DAY)).bfill().fillna(0.18)

    # Bar-of-day rank
    all_df['_bar_rank'] = all_df.groupby('_date').cumcount()

    # Optionally load VIX
    vix_dir = _DATA_DIR / 'vix_5min'
    if vix_dir.exists():
        vix_frames = []
        for f in sorted(vix_dir.glob('*.csv')):
            vf = pd.read_csv(f)
            if 'ts' not in vf.columns and 'Datetime' in vf.columns:
                vf = vf.rename(columns={'Datetime':'ts'})
            vix_frames.append(vf)
        if vix_frames:
            vix_df = pd.concat(vix_frames, ignore_index=True)
            vix_df['ts'] = pd.to_datetime(vix_df['ts'], utc=True).dt.tz_convert('Asia/Kolkata')
            vix_df = vix_df.sort_values('ts').set_index('ts')
            all_df['VIX'] = vix_df['Close'].reindex(all_df.index, method='ffill')

    all_df = all_df.drop(columns=['_date','_tp','_cv','_ctpv'], errors='ignore')
    all_df = all_df.dropna(subset=['EMA_fast','EMA_slow','ADX','RSI'])
    return all_df


def _bs_price(opt_type, S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S-K, 0) if opt_type == 'CALL' else max(K-S, 0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if opt_type == 'CALL':
        return S*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)
    return K*math.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)


def _round_trip_cost(entry_px, exit_px, lot):
    bv = entry_px * lot; sv = exit_px * lot
    br = config.BROKERAGE_PER_ORDER * 2
    ex = (bv + sv) * config.NSE_EXCHANGE_CHARGE_RATE
    se = (bv + sv) * config.SEBI_CHARGES_RATE
    gt = (br + ex + se) * config.GST_RATE
    st = sv * config.STT_RATE
    sd = bv * config.STAMP_DUTY_RATE
    return br + ex + se + gt + st + sd


def _detect_ema_signal(data: pd.DataFrame, i: int,
                        call_adx_min: float, put_adx_min: float,
                        lookback: int = 3) -> str | None:
    """
    Simplified vX primary signal: EMA crossover + ADX + VWAP.
    Returns 'CALL', 'PUT', or None.
    """
    row = data.iloc[i]
    ef  = row.get('EMA_fast', float('nan'))
    es  = row.get('EMA_slow', float('nan'))
    adx = row.get('ADX',     float('nan'))
    vwap= row.get('VWAP',    float('nan'))
    cls = row['Close']
    if any(pd.isna(v) for v in [ef, es, adx]):
        return None

    lb = min(lookback, i)
    w  = data.iloc[i - lb: i + 1]
    ef_w = w['EMA_fast'].values
    es_w = w['EMA_slow'].values

    bull_x = any(ef_w[j-1] <= es_w[j-1] and ef_w[j] > es_w[j] for j in range(1, len(w)))
    bear_x = any(ef_w[j-1] >= es_w[j-1] and ef_w[j] < es_w[j] for j in range(1, len(w)))

    sig = None
    if bull_x and ef > es and cls > ef and adx >= call_adx_min:
        sig = 'CALL'
    elif bear_x and ef < es and cls < ef and adx >= put_adx_min:
        sig = 'PUT'

    if sig is None:
        return None

    # VWAP filter
    if not pd.isna(vwap):
        if sig == 'CALL' and cls < vwap:
            return None
        if sig == 'PUT'  and cls > vwap:
            return None

    return sig


def _simulate_trade(data: pd.DataFrame, entry_bar: int, sig_type: str,
                    inst_cfg: dict, stop: float, target: float,
                    trail_act: float, trail_dist: float) -> dict:
    """Simulate option trade from entry_bar to SL / Target / Trail / EOD."""
    lot      = inst_cfg['lot_size']
    gap      = inst_cfg['strike_gap']
    dte = getattr(config, 'DAYS_TO_EXPIRY', 2)

    row0    = data.iloc[entry_bar]
    spot0   = float(row0['Close'])
    hv0     = float(row0.get('HV', 0.18))
    strike  = int(round(spot0 / gap) * gap)
    entry_px= _bs_price(sig_type, spot0, strike,
                         dte/365, config.RISK_FREE_RATE, hv0)

    if entry_px < config.MIN_OPTION_PRICE:
        return None

    highest = 0.0
    exit_px = entry_px
    exit_bar = entry_bar
    exit_reason = 'EOD'

    force_close_time = dtime(*[int(x) for x in config.FORCE_CLOSE_TIME.split(':')])

    for j in range(entry_bar + 1, len(data)):
        rj   = data.iloc[j]
        spot = float(rj['Close'])
        hv   = float(rj.get('HV', hv0))
        bar_time = data.index[j].time()
        elapsed  = (j - entry_bar) * 5
        T_rem    = max(dte - elapsed / (24*60), 0.001) / 365
        cur_px   = _bs_price(sig_type, spot, strike, T_rem, config.RISK_FREE_RATE, hv)
        pnl_pct  = (cur_px - entry_px) / entry_px

        highest = max(highest, pnl_pct)

        # Force-close
        if bar_time >= force_close_time:
            exit_px = cur_px; exit_bar = j; exit_reason = 'Force-Close'; break

        if pnl_pct <= -stop:
            exit_px = cur_px; exit_bar = j; exit_reason = 'Stop'; break
        elif pnl_pct >= target:
            exit_px = cur_px; exit_bar = j; exit_reason = 'Target'; break
        elif highest >= trail_act and pnl_pct < highest - trail_dist:
            exit_px = cur_px; exit_bar = j; exit_reason = 'Trail'; break

    cost = _round_trip_cost(entry_px, exit_px, lot)
    pnl  = (exit_px - entry_px) * lot - cost

    return {
        'sig_type'   : sig_type,
        'entry_bar'  : entry_bar,
        'exit_bar'   : exit_bar,
        'entry_time' : data.index[entry_bar].strftime('%H:%M'),
        'exit_time'  : data.index[exit_bar].strftime('%H:%M'),
        'entry_px'   : round(entry_px, 2),
        'exit_px'    : round(exit_px, 2),
        'pnl'        : round(pnl, 2),
        'exit_reason': exit_reason,
        'strike'     : strike,
        'lots'       : 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3.  BACKTEST RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(instrument: str, recent_days: int | None = None) -> dict:
    """
    Run vX signal detection + reversal guard on full history.
    Returns comparison dict: baseline vs guarded P&L.
    """
    print(f"\n{'='*60}")
    print(f"  {instrument} — Reversal Guard Backtest")
    print(f"{'='*60}")

    data = _load_data(instrument)
    print(f"  {len(data):,} bars  ({data.index[0].date()} to {data.index[-1].date()})")

    inst_cfg = config.INSTRUMENTS[instrument]
    vX_cfg   = config.INSTRUMENT_STRATEGY.get(instrument, {})
    call_adx = vX_cfg.get('call_adx_min', 30)
    put_adx  = vX_cfg.get('put_adx_min',  25)

    # Entry window
    es = vX_cfg.get('entry_start', '11:00')
    ee = vX_cfg.get('entry_end',   '14:45')
    entry_start = dtime(*map(int, es.split(':')))
    entry_end   = dtime(*map(int, ee.split(':')))

    skip_tuesday  = vX_cfg.get('skip_tuesday',  True)
    skip_thursday = vX_cfg.get('skip_thursday', False)

    stop       = config.STOP_LOSS
    target     = config.BASE_TARGET
    trail_act  = config.TRAILING_ACTIVATION
    trail_dist = config.TRAILING_DISTANCE

    baseline_trades  = []   # all signals taken regardless
    guarded_trades   = []   # signals filtered by reversal guard
    signal_log       = []   # full signal log with risk scores

    in_trade_base  = False
    in_trade_guard = False
    daily_loss_b   = {}
    daily_loss_g   = {}

    i = 1
    while i < len(data):
        today    = data.index[i].date()
        bar_time = data.index[i].time()

        daily_loss_b.setdefault(today, 0.0)
        daily_loss_g.setdefault(today, 0.0)

        # Weekday gates
        wd = data.index[i].weekday()   # 0=Mon, 4=Fri
        # skip_thursday: blocks ALL entries on that day (SENSEX expiry day)
        if skip_thursday and wd == 3: i += 1; continue
        if not (entry_start <= bar_time <= entry_end):
            i += 1; continue
        if daily_loss_b[today] < -config.MAX_DAILY_LOSS and \
           daily_loss_g[today] < -config.MAX_DAILY_LOSS:
            i += 1; continue

        sig_type = _detect_ema_signal(data, i, call_adx, put_adx)
        if sig_type is None:
            i += 1; continue

        # skip_tuesday: only blocks CALL entries (PUT signals still valid on Tue)
        # Matches paper_bot.py fix applied Mar 10 2026
        if skip_tuesday and wd == 1 and sig_type == 'CALL':
            i += 1; continue

        # Compute reversal risk
        risk = compute_reversal_risk(data, i, sig_type)

        # Simulate both baseline and guarded
        trade = _simulate_trade(data, i, sig_type, inst_cfg,
                                stop, target, trail_act, trail_dist)
        if trade is None:
            i += 1; continue

        # ── Baseline (no guard) ────────────────────────────────────────
        if daily_loss_b[today] > -config.MAX_DAILY_LOSS:
            daily_loss_b[today] += trade['pnl']
            baseline_trades.append({**trade, 'date': today})

        # ── Guarded ───────────────────────────────────────────────────
        if not risk['skip'] and daily_loss_g[today] > -config.MAX_DAILY_LOSS:
            lots = 1 if risk['reduce_lots'] else trade['lots']
            guarded_pnl = trade['pnl'] * lots   # scale if needed
            daily_loss_g[today] += guarded_pnl
            guarded_trades.append({**trade, 'date': today,
                                   'pnl': round(guarded_pnl, 2),
                                   'risk_score': risk['score'],
                                   'risk_level': risk['risk_level']})

        signal_log.append({
            'date'       : today,
            'time'       : data.index[i].strftime('%H:%M'),
            'sig_type'   : sig_type,
            'adx'        : round(float(data['ADX'].iloc[i]), 1),
            'rsi'        : round(float(data['RSI'].iloc[i]), 1),
            'vwap_dist'  : round(abs(data['Close'].iloc[i] - data['VWAP'].iloc[i]) /
                                  data['Close'].iloc[i] * 100, 3),
            'risk_score' : risk['score'],
            'risk_level' : risk['risk_level'],
            'skip'       : risk['skip'],
            'reason'     : risk['reason'],
            'pnl_base'   : trade['pnl'],
            'pnl_guard'  : round(trade['pnl'] * (1 if not risk['skip'] else 0), 2),
            'exit_reason': trade['exit_reason'],
            'components' : risk['components'],
        })

        # Skip to next day after a trade (max_concurrent=1)
        i = trade['exit_bar'] + 1

    # ── Summarise ─────────────────────────────────────────────────────────────
    df_base  = pd.DataFrame(baseline_trades)
    df_guard = pd.DataFrame(guarded_trades)
    df_log   = pd.DataFrame(signal_log)

    if df_base.empty:
        print("  No signals detected.")
        return {}

    def _stats(df):
        if df.empty: return dict(n=0, wr=0, net=0, avg_w=0, avg_l=0)
        n  = len(df); wins = (df['pnl'] > 0).sum()
        return dict(
            n    = n,
            wr   = wins / n * 100,
            net  = df['pnl'].sum(),
            avg_w= df.loc[df['pnl']>0,'pnl'].mean() if wins else 0,
            avg_l= df.loc[df['pnl']<=0,'pnl'].mean() if n-wins else 0,
        )

    sb = _stats(df_base)
    sg = _stats(df_guard)

    skipped   = len(df_log[df_log['skip']])
    kept      = len(df_log[~df_log['skip']])
    reduced   = len(df_log[(~df_log['skip']) & (df_log['risk_level'] == 'MODERATE')])
    skipped_l = df_log[df_log['skip']]['pnl_base'].sum()   # what skipped signals made/lost
    reduced_l = df_log[(~df_log['skip']) & (df_log['risk_level'] == 'MODERATE')]['pnl_base'].sum()

    print(f"\n  {'Metric':<28} {'Baseline vX':>12} {'+ Guard':>12}  {'Delta':>10}")
    print(f"  {'-'*66}")
    print(f"  {'Signals taken':<28} {sb['n']:>12} {sg['n']:>12}")
    print(f"  {'Skipped (HIGH>=50)':<28} {'—':>12} {skipped:>12}")
    print(f"  {'Reduced lots (MOD>=30)':<28} {'—':>12} {reduced:>12}")
    print(f"  {'Win Rate':<28} {sb['wr']:>11.1f}% {sg['wr']:>11.1f}%  "
          f"{sg['wr']-sb['wr']:>+9.1f}pp")
    print(f"  {'Net P&L':<28} Rs.{sb['net']:>9,.0f} Rs.{sg['net']:>9,.0f}  "
          f"Rs.{sg['net']-sb['net']:>+8,.0f}")
    print(f"  {'Avg Win':<28} Rs.{sb['avg_w']:>9,.0f} Rs.{sg['avg_w']:>9,.0f}")
    print(f"  {'Avg Loss':<28} Rs.{sb['avg_l']:>9,.0f} Rs.{sg['avg_l']:>9,.0f}")
    print(f"\n  Skipped signals P&L (would have been): Rs.{skipped_l:+,.0f}"
          f"  ({'loss avoided' if skipped_l < 0 else 'gain foregone'})")
    print(f"  Reduced-lot signals P&L (at full lot): Rs.{reduced_l:+,.0f}"
          f"  ({'partial loss avoided' if reduced_l < 0 else 'gains at reduced size'})")

    # ── Risk score distribution of LOSERS vs WINNERS ──────────────────────
    if not df_log.empty and 'risk_score' in df_log.columns:
        winners_score = df_log[df_log['pnl_base'] > 0]['risk_score'].mean()
        losers_score  = df_log[df_log['pnl_base'] <= 0]['risk_score'].mean()
        print(f"\n  Avg risk score — Winners: {winners_score:.1f}  "
              f"Losers: {losers_score:.1f}  "
              f"(higher losers score = guard is working)")

    # ── Recent days view ─────────────────────────────────────────────────────
    if recent_days and not df_log.empty:
        max_date    = df_log['date'].max()
        cutoff_date = max_date - dt.timedelta(days=recent_days)
        df_recent   = df_log[df_log['date'] >= cutoff_date]
        _print_recent_signals(instrument, df_recent)

    # ── Chart ─────────────────────────────────────────────────────────────────
    _plot_guard_impact(instrument, df_base, df_guard, df_log)

    return {
        'instrument'   : instrument,
        'baseline'     : sb,
        'guarded'      : sg,
        'skipped'      : skipped,
        'skipped_pnl'  : skipped_l,
        'signal_log'   : df_log,
    }


def _print_recent_signals(instrument: str, df: pd.DataFrame):
    if df.empty:
        return
    print(f"\n  ── Recent Signals ({instrument}) ──────────────────────────────────")
    print(f"  {'Date':<12} {'Time':<6} {'Type':<5} {'ADX':>5} {'RSI':>5} "
          f"{'VWAP%':>6} {'Score':>6} {'Risk':<9} {'Action':<8} "
          f"{'P&L':>8} {'Exit'}")
    print(f"  {'-'*88}")
    for _, r in df.sort_values('date').iterrows():
        action = 'SKIP  ' if r['skip'] else ('1-lot ' if r.get('risk_level')=='MODERATE' else 'TAKE  ')
        pnl_s  = f"Rs.{r['pnl_base']:+,.0f}"
        print(f"  {str(r['date']):<12} {r['time']:<6} {r['sig_type']:<5} "
              f"{r['adx']:>5.1f} {r['rsi']:>5.1f} "
              f"{r['vwap_dist']:>5.3f}% {r['risk_score']:>6} "
              f"{r['risk_level']:<9} {action:<8} "
              f"{pnl_s:>10}  {r['exit_reason']}")
        # Show component breakdown for flagged signals (MODERATE or HIGH)
        if r.get('risk_level') in ('MODERATE', 'HIGH') and isinstance(r.get('components'), dict):
            comp = r['components']
            parts = [f"{k.replace('_',' ')}={v}" for k, v in comp.items() if v > 0]
            tag = '^ SKIP  ' if r['skip'] else '^ 1-LOT '
            print(f"  {'':>66} {tag} [{', '.join(parts)}]")


def _plot_guard_impact(instrument: str,
                        df_base: pd.DataFrame,
                        df_guard: pd.DataFrame,
                        df_log: pd.DataFrame):
    """2-panel chart: cumulative P&L baseline vs guarded + risk score histogram."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 5))
    fig.suptitle(f'{instrument} — Reversal Guard Impact', fontsize=13, fontweight='bold')

    # Panel 1: Cumulative P&L
    if not df_base.empty:
        ax1.plot(range(len(df_base)), df_base['pnl'].cumsum(),
                 color='#ef5350', lw=2, label=f'Baseline vX  ({len(df_base)} trades)')
    if not df_guard.empty:
        ax1.plot(range(len(df_guard)), df_guard['pnl'].cumsum(),
                 color='#1976d2', lw=2, label=f'+ Reversal Guard  ({len(df_guard)} trades)')
    ax1.axhline(0, color='#455a64', lw=0.8, linestyle='--')
    ax1.set_title('Cumulative Net P&L')
    ax1.set_xlabel('Trade #')
    ax1.set_ylabel('Net P&L (Rs.)')
    ax1.legend(fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'Rs.{x:,.0f}'))

    # Panel 2: Risk score distribution — winners vs losers
    if not df_log.empty:
        winners = df_log[df_log['pnl_base'] > 0]['risk_score']
        losers  = df_log[df_log['pnl_base'] <= 0]['risk_score']
        bins = range(0, 105, 10)
        ax2.hist(winners, bins=bins, alpha=0.65, color='#26a69a',
                 label=f'Winners (n={len(winners)})', density=True)
        ax2.hist(losers,  bins=bins, alpha=0.65, color='#ef5350',
                 label=f'Losers  (n={len(losers)})',  density=True)
        ax2.axvline(40, color='#ff8f00', lw=1.5, linestyle='--', label='Moderate (40)')
        ax2.axvline(60, color='#b71c1c', lw=1.5, linestyle='--', label='Skip (60)')
        ax2.set_title('Risk Score Distribution: Winners vs Losers')
        ax2.set_xlabel('Reversal Risk Score')
        ax2.set_ylabel('Density')
        ax2.legend(fontsize=9)
        ax2.set_xlim(0, 100)

    plt.tight_layout()
    out = _HERE.parent / f'reversal_guard_{instrument}.png'
    plt.savefig(out, dpi=130, bbox_inches='tight')
    plt.close()
    print(f"\n  Chart saved -> {out}")


# ─────────────────────────────────────────────────────────────────────────────
# 4.  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args         = sys.argv[1:]
    recent_days  = None
    instruments  = ['NIFTY', 'BANKNIFTY', 'SENSEX']

    # Parse args
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == '--recent' and i + 1 < len(args):
            recent_days = int(args[i + 1]); i += 2
        elif args[i].upper() in instruments:
            instruments = [args[i].upper()]; i += 1
        else:
            i += 1

    if recent_days is None:
        recent_days = 30   # default: show last 30 calendar days of signals

    print("\nReversal Guard — Trend Exhaustion Filter")
    print("Based on: RSI extreme | RSI divergence | VWAP overextension |"
          " ADX declining | Consecutive candles")
    print(f"Thresholds: Skip >= 50pts | Reduce lots >= 30pts")
    print(f"Showing recent {recent_days} days of signals")

    all_results = []
    for inst in instruments:
        try:
            r = run_backtest(inst, recent_days=recent_days)
            if r:
                all_results.append(r)
        except Exception as e:
            print(f"\nERROR on {inst}: {e}")
            import traceback; traceback.print_exc()

    # Combined summary
    if len(all_results) > 1:
        print(f"\n{'='*60}")
        print(f"  COMBINED — Reversal Guard Summary")
        print(f"{'='*60}")
        base_net  = sum(r['baseline']['net']  for r in all_results)
        guard_net = sum(r['guarded']['net']   for r in all_results)
        base_n    = sum(r['baseline']['n']    for r in all_results)
        guard_n   = sum(r['guarded']['n']     for r in all_results)
        skip_pnl  = sum(r['skipped_pnl']      for r in all_results)
        print(f"  Baseline : {base_n} trades | Rs.{base_net:+,.0f}")
        print(f"  Guarded  : {guard_n} trades | Rs.{guard_net:+,.0f}  "
              f"(delta Rs.{guard_net-base_net:+,.0f})")
        print(f"  Skipped signals total P&L: Rs.{skip_pnl:+,.0f}"
              f"  ({'losses avoided' if skip_pnl < 0 else 'gains foregone'})")

    print(f"\n{'='*60}")
    print(f"  ON OPTION PRICE DATA")
    print(f"{'='*60}")
    print("""
  The reversal guard above uses only index OHLCV.  Option price data
  adds two powerful additional signals:

  1. India VIX (already supported — run: python data_collector.py vix_backfill 400)
     VIX spike >8% intraday when entering a PUT = peak fear = reversal risk.
     Example: PUT entered at 11:25 when VIX rose 12% from open → score +15pts.
     This is already wired into component 6 (VIX Spike) and activates
     automatically once vix_5min/ data is present.

  2. ATM IV of the option you're about to buy
     If IV is in its top-20% for that expiry (IV rank > 80), the option is
     very expensive AND fear is at a peak → reversal risk.
     Requires options chain data (TrueData/Global Datafeeds, ~Rs.500/mo).
     Worth adding once live and you have a 30-day IV baseline.

  3. Put/Call ratio (NSE daily)
     Extreme P/C ratio (>1.4) = too many bears → contrarian bullish signal.
     Free from NSE website; would take ~30 min to add as a daily CSV.

  Recommendation: collect VIX first (free, already have infrastructure).
  Run: python data_collector.py vix_backfill 400
  Then rerun this script — component 6 activates automatically.
""")


if __name__ == '__main__':
    main()
