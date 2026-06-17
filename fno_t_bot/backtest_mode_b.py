"""
Path A Mode B — EMA Spread Widening Continuation
Backtest on 275-day NIFTY dataset

Compares:
  vX        = Mode A only (fresh crossover, current bot)
  vX+ModeB  = Mode A + Mode B (continuation sub-mode)
  ModeB     = Mode B only (to see its standalone contribution)
"""
from __future__ import annotations
import math, glob, os
from datetime import time, datetime
import pandas as pd
import numpy as np
import pytz
from scipy.stats import norm

IST = pytz.timezone('Asia/Kolkata')

# ── helpers ────────────────────────────────────────────────────────────────
def bs_price(opt_type, S, K, T, sigma, r=0.065):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if opt_type == 'CALL' else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == 'CALL':
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def round_strike(spot, gap=50):
    return round(spot / gap) * gap

def compute_adx(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    prev_c = close.shift(1)
    tr = pd.concat([high - low, (high-prev_c).abs(), (low-prev_c).abs()], axis=1).max(axis=1)
    dmp = (high - high.shift(1)).clip(lower=0)
    dmm = (low.shift(1) - low).clip(lower=0)
    dmp = dmp.where(dmp > dmm, 0)
    dmm = dmm.where(dmm > dmp, 0)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    dip = 100 * dmp.ewm(alpha=1/period, adjust=False).mean() / atr
    dim = 100 * dmm.ewm(alpha=1/period, adjust=False).mean() / atr
    dx  = (100 * (dip-dim).abs() / (dip+dim)).replace([np.inf,-np.inf], 0)
    return dx.ewm(alpha=1/period, adjust=False).mean()

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def compute_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    cv = df['Volume'].cumsum().replace(0, np.nan)
    return (tp * df['Volume']).cumsum() / cv

def compute_hv(close, window=20):
    rets = np.log(close / close.shift(1))
    return rets.rolling(window).std() * math.sqrt(252 * 75)

# ── config ─────────────────────────────────────────────────────────────────
LOT        = 65
BROKERAGE  = 20
DTE        = 2
STOP       = 0.40
TARGET     = 1.30
TRAIL_ACT  = 0.55
TRAIL_DST  = 0.20
CALL_ADX   = 30    # Mode A CALL threshold
PUT_ADX    = 25    # Mode A PUT threshold
CONT_ADX   = 35    # Mode B threshold
LOOKBACK   = 3
A_START    = time(11, 0)
B_START    = time(12, 0)   # Mode B delayed start
A_END      = time(14, 0)
FORCE      = time(14, 30)
SKIP_TUE_CALL = True

# ── single-day simulation ──────────────────────────────────────────────────
def simulate_day(df, allow_mode_a=True, allow_mode_b=True):
    """Returns list of trade dicts for one day."""
    df = df.copy()
    df['ema9']  = ema(df['Close'], 9)
    df['ema21'] = ema(df['Close'], 21)
    df['adx']   = compute_adx(df)
    df['vwap']  = compute_vwap(df)
    df['hv']    = compute_hv(df['Close'])

    date   = df.index[0].date()
    is_tue = df.index[0].day_of_week == 1   # Monday=0

    bars   = list(df.iterrows())
    n      = len(bars)
    trades = []
    pos    = None
    traded = False   # one trade per day (max_trades=1 for simplicity matching vX)

    for idx in range(n):
        ts, row = bars[idx]
        t  = ts.time()
        px = float(row['Close'])
        hv = float(row['hv']) if not math.isnan(row['hv']) else 0.18

        # ── manage open position ──────────────────────────────────────────
        if pos:
            ep  = pos['entry_px']
            T   = max(DTE / 365, 1e-4)
            opt = bs_price(pos['type'], px, pos['strike'], T, hv)
            pct = (opt - ep) / ep * 100

            if pct >= TRAIL_ACT * 100:
                pos['hwm'] = max(pos.get('hwm', opt), opt)
            hwm = pos.get('hwm')

            reason = None
            if pct <= -STOP * 100:
                reason = f'STOP'
            elif pct >= TARGET * 100:
                reason = f'TARGET'
            elif hwm and opt < hwm * (1 - TRAIL_DST):
                reason = f'TRAIL'
            elif t >= FORCE:
                reason = f'FORCE'

            if reason:
                gross = (opt - ep) * LOT
                net   = gross - 2 * BROKERAGE
                trades.append({
                    'date': str(date), 'type': pos['type'],
                    'mode': pos['mode'],
                    'entry_time': pos['entry_time'].strftime('%H:%M'),
                    'exit_time': ts.strftime('%H:%M'),
                    'entry_px': ep, 'exit_px': opt,
                    'pct': pct, 'gross': gross, 'net': net, 'reason': reason,
                })
                pos = None
            continue

        if traded or t >= A_END:
            continue

        adx_val  = float(row['adx'])
        vwap_val = float(row['vwap'])
        e9       = float(row['ema9'])
        e21      = float(row['ema21'])

        signal = None

        # ── Mode A: fresh crossover ───────────────────────────────────────
        if allow_mode_a and t >= A_START:
            w_start = max(0, idx - LOOKBACK + 1)
            w = df.iloc[w_start:idx + 1]
            for k in range(1, len(w)):
                bt = w.index[k-1].time()
                if bt < A_START:
                    continue
                p9, p21 = float(w['ema9'].iloc[k-1]), float(w['ema21'].iloc[k-1])
                c9, c21 = float(w['ema9'].iloc[k]),   float(w['ema21'].iloc[k])
                if p9 <= p21 and c9 > c21:
                    sig = 'CALL'
                    if is_tue and SKIP_TUE_CALL:
                        sig = None
                    if sig and adx_val >= CALL_ADX and px > vwap_val:
                        signal = {'type': 'CALL', 'mode': 'A-cross'}
                    break
                if p9 >= p21 and c9 < c21:
                    if adx_val >= PUT_ADX and px < vwap_val:
                        signal = {'type': 'PUT', 'mode': 'A-cross'}
                    break

        # ── Mode B: spread widening continuation ──────────────────────────
        if signal is None and allow_mode_b and t >= B_START:
            # direction from current EMA
            if e9 > e21:
                direction = 'CALL'
                if is_tue and SKIP_TUE_CALL:
                    direction = None
            elif e9 < e21:
                direction = 'PUT'
            else:
                direction = None

            if direction and adx_val >= CONT_ADX:
                # VWAP filter
                vwap_ok = (direction == 'CALL' and px > vwap_val) or \
                          (direction == 'PUT'  and px < vwap_val)
                if vwap_ok and idx >= 3:
                    # spread widening: last 3 bars all widening
                    spreads = []
                    for k in range(idx - 2, idx + 1):
                        e9k  = float(df['ema9'].iloc[k])
                        e21k = float(df['ema21'].iloc[k])
                        spreads.append(abs(e9k - e21k))
                    widening = all(spreads[i] < spreads[i+1]
                                   for i in range(len(spreads)-1))
                    if widening:
                        # ensure crossover was PRE-window
                        # (if fresh cross just happened, Mode A should have caught it)
                        w_start = max(0, idx - LOOKBACK + 1)
                        w = df.iloc[w_start:idx + 1]
                        fresh = False
                        for k in range(1, len(w)):
                            bt = w.index[k-1].time()
                            if bt < A_START:
                                continue
                            p9, p21 = float(w['ema9'].iloc[k-1]), float(w['ema21'].iloc[k-1])
                            c9, c21 = float(w['ema9'].iloc[k]),   float(w['ema21'].iloc[k])
                            if (p9 <= p21 and c9 > c21) or (p9 >= p21 and c9 < c21):
                                fresh = True
                                break
                        if not fresh:  # genuinely pre-window, continuation
                            signal = {'type': direction, 'mode': 'B-cont'}

        # ── enter ─────────────────────────────────────────────────────────
        if signal:
            T      = max(DTE / 365, 1e-4)
            strike = round_strike(px)
            opt_px = bs_price(signal['type'], px, strike, T, hv)
            pos = {
                'type': signal['type'], 'mode': signal['mode'],
                'strike': strike, 'entry_px': opt_px,
                'entry_time': ts,
            }
            traded = True

    return trades


# ── run backtest ───────────────────────────────────────────────────────────
files = sorted(glob.glob('../data/nifty_5min/*.csv'))
print(f"Running on {len(files)} days...")

all_a    = []   # Mode A only
all_b    = []   # Mode B only
all_ab   = []   # Mode A + B combined

for f in files:
    try:
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        if len(df) < 30:
            continue

        all_a  += simulate_day(df, allow_mode_a=True,  allow_mode_b=False)
        all_b  += simulate_day(df, allow_mode_a=False, allow_mode_b=True)
        all_ab += simulate_day(df, allow_mode_a=True,  allow_mode_b=True)
    except Exception as e:
        pass


# ── stats ──────────────────────────────────────────────────────────────────
def stats(trades, label):
    if not trades:
        print(f"\n{label}: 0 trades")
        return
    df = pd.DataFrame(trades)
    wins = df[df['net'] > 0]
    loss = df[df['net'] <= 0]
    total_net = df['net'].sum()
    wr = len(wins) / len(df) * 100
    avg_w = wins['net'].mean() if len(wins) else 0
    avg_l = loss['net'].mean() if len(loss) else 0
    wl = abs(avg_w / avg_l) if avg_l else 0

    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")
    print(f"  Trades  : {len(df)}")
    print(f"  Win Rate: {wr:.1f}%  ({len(wins)}W / {len(loss)}L)")
    print(f"  Net P&L : ₹{total_net:+,.0f}")
    print(f"  Avg Win : ₹{avg_w:+,.0f}  |  Avg Loss: ₹{avg_l:+,.0f}")
    print(f"  W/L     : {wl:.2f}x")

    # max drawdown
    cumnet = df['net'].cumsum()
    roll_max = cumnet.cummax()
    dd = (cumnet - roll_max).min()
    print(f"  Max DD  : ₹{dd:,.0f}  ({dd/50000*100:.1f}% of ₹50k capital)")

    # by mode
    if 'mode' in df.columns:
        for mode in df['mode'].unique():
            sub = df[df['mode'] == mode]
            sw  = sub[sub['net'] > 0]
            print(f"  [{mode}] {len(sub)} trades | "
                  f"WR={len(sw)/len(sub)*100:.0f}% | "
                  f"Net ₹{sub['net'].sum():+,.0f}")

    # by reason
    print(f"  Exits   :", dict(df['reason'].value_counts()))

    # new Mode B trades not in Mode A (additive days)
    return df

print("\n" + "="*55)
print("  PATH A MODE B — BACKTEST (275 days NIFTY)")
print("="*55)

df_a  = stats(all_a,  "Mode A only  (vX current)")
df_b  = stats(all_b,  "Mode B only  (spread widening standalone)")
df_ab = stats(all_ab, "Mode A + B   (combined vX + continuation)")

# overlap analysis
if df_a is not None and df_b is not None:
    a_dates = set(df_a['date'])
    b_dates = set(df_b['date'])
    both    = a_dates & b_dates
    b_only  = b_dates - a_dates
    print(f"\n{'─'*55}")
    print(f"  Overlap analysis")
    print(f"{'─'*55}")
    print(f"  Days Mode A traded         : {len(a_dates)}")
    print(f"  Days Mode B traded         : {len(b_dates)}")
    print(f"  Days BOTH would fire       : {len(both)}  (Mode B blocked in combined)")
    print(f"  Days Mode B adds new trade : {len(b_only)}  (Mode A didn't fire)")
    if b_only:
        b_only_trades = df_b[df_b['date'].isin(b_only)]
        print(f"  Mode B additive P&L        : ₹{b_only_trades['net'].sum():+,.0f}")
        bw = b_only_trades[b_only_trades['net'] > 0]
        print(f"  Mode B additive WR         : {len(bw)/len(b_only_trades)*100:.0f}%")

print()
