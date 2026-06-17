"""
March 30 2026: Path B (ORB 9:15-10:55) + Path A (vX EMA 11:00-14:00)
with handover coordination between them.
"""
from __future__ import annotations
import sys, math
from datetime import time
import pandas as pd
import numpy as np
import pytz
from scipy.stats import norm

sys.path.insert(0, '.')
import config

IST = pytz.timezone('Asia/Kolkata')

# ── helpers ────────────────────────────────────────────────────────────────
def bs_price(opt_type, S, K, T, sigma, r=0.065):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == 'CALL':
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def round_strike(spot, gap=50):
    return round(spot / gap) * gap

def compute_hv(close_series):
    rets = np.log(close_series / close_series.shift(1)).dropna()
    if len(rets) < 2:
        return 0.18
    return float(rets.std() * math.sqrt(252 * 75))

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def compute_adx(df_sub, period=14):
    high, low, close = df_sub['High'], df_sub['Low'], df_sub['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    dm_plus  = (high - high.shift(1)).clip(lower=0)
    dm_minus = (low.shift(1) - low).clip(lower=0)
    dm_plus  = dm_plus.where(dm_plus > dm_minus, 0)
    dm_minus = dm_minus.where(dm_minus > dm_plus, 0)
    atr  = tr.ewm(alpha=1/period, adjust=False).mean()
    dip  = 100 * dm_plus.ewm(alpha=1/period, adjust=False).mean() / atr
    dim  = 100 * dm_minus.ewm(alpha=1/period, adjust=False).mean() / atr
    dx   = (100 * (dip - dim).abs() / (dip + dim)).replace([np.inf, -np.inf], 0)
    adx  = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx, dip, dim

def compute_vwap(df_sub):
    tp = (df_sub['High'] + df_sub['Low'] + df_sub['Close']) / 3
    cumvol = df_sub['Volume'].cumsum().replace(0, np.nan)
    return (tp * df_sub['Volume']).cumsum() / cumvol


# ── load data ──────────────────────────────────────────────────────────────
df = pd.read_csv(
    r'C:\quant_trading\data\nifty_5min\nifty_5min_20260330.csv',
    index_col=0, parse_dates=True
)
df.index = df.index.tz_convert(IST)

print("=== March 30 2026 — NIFTY 5-min ===")
print(f"Open 9:15 : {df.iloc[0]['Close']:.2f}")
print(f"Close 15:25: {df.iloc[-1]['Close']:.2f}")
print(f"Day range  : {df['Low'].min():.2f} – {df['High'].max():.2f}")
print()

# pre-compute indicators
df['ema9']  = ema(df['Close'], 9)
df['ema21'] = ema(df['Close'], 21)
adx_s, dip_s, dim_s = compute_adx(df)
df['adx']  = adx_s
df['dip']  = dip_s
df['dim']  = dim_s
df['vwap'] = compute_vwap(df)
df['hv']   = pd.Series(
    [compute_hv(df['Close'].iloc[max(0, i-20):i+1]) for i in range(len(df))],
    index=df.index
)

# ── CONFIG ─────────────────────────────────────────────────────────────────
LOT       = config.INSTRUMENTS['NIFTY']['lot_size']   # 65
BROKERAGE = config.BROKERAGE_PER_ORDER

# Path B
OR_BARS      = config.EARLY_SESSION_ORB_BARS      # 3
OR_BUF       = config.EARLY_SESSION_ORB_BUFFER    # 0.0005
STOP_B       = config.EARLY_SESSION_STOP          # 0.5
TARGET_B     = config.EARLY_SESSION_TARGET        # 1.5
TRAIL_B_ACT  = config.EARLY_SESSION_TRAIL_ACT     # 0.8
TRAIL_B_DST  = config.EARLY_SESSION_TRAIL_DIST    # 0.2
DTE_B        = config.EARLY_SESSION_DAYS_TO_EXP   # 2
B_START      = time(9, 30)
B_FORCE      = time(10, 55)

# Path A
STRAT        = config.INSTRUMENT_STRATEGY['NIFTY']
CALL_ADX_A   = STRAT['call_adx_min']   # 30
PUT_ADX_A    = STRAT['put_adx_min']    # 25
A_START      = time(11, 0)
A_END        = time(14, 0)
A_FORCE      = time(14, 30)
STOP_A       = config.STOP_LOSS        # 0.40
TARGET_A     = config.BASE_TARGET      # 1.30
TRAIL_A_ACT  = config.TRAILING_ACTIVATION  # 0.55
TRAIL_A_DST  = config.TRAILING_DISTANCE    # 0.20
DTE_A        = config.DAYS_TO_EXPIRY   # 2
LOOKBACK     = config.EMA_CROSSOVER_LOOKBACK

# ────────────────────────────────────────────────────────────────────────────
# PATH B SIMULATION
# ────────────────────────────────────────────────────────────────────────────
print("=" * 62)
print("PATH B — ORB (9:15–10:55)")
print("NOTE: config restricts to Wed/Fri. March 30 = Monday.")
print("      Running hypothetically (relaxed day constraint).")
print("=" * 62)

or_bars  = df.iloc[:OR_BARS]
or_high  = float(or_bars['High'].max())
or_low   = float(or_bars['Low'].min())
call_trig = or_high * (1 + OR_BUF)
put_trig  = or_low  * (1 - OR_BUF)

print(f"OR (9:15–9:25): High={or_high:.2f}  Low={or_low:.2f}")
print(f"Triggers      : CALL>{call_trig:.2f}  PUT<{put_trig:.2f}")
print()

path_b_pos    = None
path_b_result = None

for ts, row in df.iterrows():
    t  = ts.time()
    px = float(row['Close'])
    hv = float(row['hv']) if not math.isnan(row['hv']) else 0.18

    # manage open position
    if path_b_pos:
        ep  = path_b_pos['entry_px']
        T   = max(DTE_B / 365, 1e-4)
        opt = bs_price(path_b_pos['type'], px, path_b_pos['strike'], T, hv)
        pnl_pct = (opt - ep) / ep * 100

        if pnl_pct >= TRAIL_B_ACT * 100:
            path_b_pos['trail_hwm'] = max(path_b_pos.get('trail_hwm', opt), opt)
        hwm = path_b_pos.get('trail_hwm')

        reason = None
        if pnl_pct <= -STOP_B * 100:
            reason = f'STOP {pnl_pct:+.1f}%'
        elif pnl_pct >= TARGET_B * 100:
            reason = f'TARGET {pnl_pct:+.1f}%'
        elif hwm and opt < hwm * (1 - TRAIL_B_DST):
            reason = f'TRAIL {pnl_pct:+.1f}%'
        elif t >= B_FORCE:
            reason = f'FORCE-CLOSE {pnl_pct:+.1f}%'

        if reason:
            gross = (opt - ep) * LOT
            net   = gross - 2 * BROKERAGE
            path_b_result = {
                'type': path_b_pos['type'], 'strike': path_b_pos['strike'],
                'entry_px': ep, 'exit_px': opt,
                'entry_time': path_b_pos['entry_time'], 'exit_time': ts,
                'pnl_pct': pnl_pct, 'gross_pnl': gross, 'net_pnl': net,
                'reason': reason, 'spot_entry': path_b_pos['spot_entry'],
            }
            print(f"  EXIT  {ts.strftime('%H:%M')} | {path_b_pos['type']} "
                  f"| {reason} | opt ₹{ep:.2f}→₹{opt:.2f}")
            print(f"         Gross ₹{gross:+,.0f}  Net ₹{net:+,.0f}")
            path_b_pos = None
        continue

    # look for entry
    if path_b_result is not None:
        continue  # already traded
    if not (B_START <= t < B_FORCE):
        continue

    T  = max(DTE_B / 365, 1e-4)
    if float(row['High']) >= call_trig:
        strike = round_strike(px)
        opt_px = bs_price('CALL', px, strike, T, hv)
        path_b_pos = {
            'type': 'CALL', 'strike': strike, 'entry_px': opt_px,
            'entry_time': ts, 'spot_entry': px,
        }
        print(f"  ENTRY {ts.strftime('%H:%M')} | CALL {strike} @ ₹{opt_px:.2f} "
              f"spot={px:.2f} ADX={row['adx']:.1f}")
    elif float(row['Low']) <= put_trig:
        strike = round_strike(px)
        opt_px = bs_price('PUT', px, strike, T, hv)
        path_b_pos = {
            'type': 'PUT', 'strike': strike, 'entry_px': opt_px,
            'entry_time': ts, 'spot_entry': px,
        }
        print(f"  ENTRY {ts.strftime('%H:%M')} | PUT {strike} @ ₹{opt_px:.2f} "
              f"spot={px:.2f} ADX={row['adx']:.1f}")

if path_b_pos:
    # force close at last available bar before force-close time
    row = df.between_time('10:55', '10:55').iloc[0]
    px  = float(row['Close'])
    hv  = float(row['hv']) if not math.isnan(row['hv']) else 0.18
    T   = max(DTE_B / 365, 1e-4)
    opt = bs_price(path_b_pos['type'], px, path_b_pos['strike'], T, hv)
    pnl_pct = (opt - path_b_pos['entry_px']) / path_b_pos['entry_px'] * 100
    gross = (opt - path_b_pos['entry_px']) * LOT
    net   = gross - 2 * BROKERAGE
    path_b_result = {
        'type': path_b_pos['type'], 'strike': path_b_pos['strike'],
        'entry_px': path_b_pos['entry_px'], 'exit_px': opt,
        'entry_time': path_b_pos['entry_time'], 'exit_time': row.name,
        'pnl_pct': pnl_pct, 'gross_pnl': gross, 'net_pnl': net,
        'reason': f'FORCE-CLOSE {pnl_pct:+.1f}%', 'spot_entry': path_b_pos['spot_entry'],
    }
    print(f"  FORCE-CLOSE 10:55 | {path_b_pos['type']} | ₹{path_b_pos['entry_px']:.2f}→₹{opt:.2f}"
          f" | Gross ₹{gross:+,.0f}  Net ₹{net:+,.0f}")
    path_b_pos = None

if path_b_result is None:
    window = df.between_time('09:30', '10:55')
    print(f"  No trade — OR never broken in 9:30–10:55")
    print(f"  9:30-10:55 range: Low={window['Low'].min():.2f}  High={window['High'].max():.2f}")
    print(f"  Needed: High>{call_trig:.2f} or Low<{put_trig:.2f}")

# ────────────────────────────────────────────────────────────────────────────
# PATH A SIMULATION
# ────────────────────────────────────────────────────────────────────────────
print()
print("=" * 62)
print("PATH A (vX) — EMA crossover 11:00–14:00")
print("=" * 62)
print(f"ADX thresholds: CALL≥{CALL_ADX_A}  PUT≥{PUT_ADX_A}  |  Lookback: {LOOKBACK} bars")
print()

# Handover logic:
# Path B force-closes at 10:55 — so at 11:00 it is FLAT.
# No live position to inherit. But we note if Path B's direction
# aligns with the first Path A signal.
b_dir = path_b_result['type'] if path_b_result else None
print(f"Path B exit direction: {b_dir or 'No trade'}")
print(f"Handover: Path B is flat at 11:00 → Path A enters fresh (no inheritance possible)")
print()

path_a_pos    = None
trades_a      = []
bars          = list(df.iterrows())
n             = len(bars)

for idx in range(n):
    ts, row = bars[idx]
    t  = ts.time()
    px = float(row['Close'])
    hv = float(row['hv']) if not math.isnan(row['hv']) else 0.18

    if t < A_START:
        continue

    # manage open position
    if path_a_pos:
        ep  = path_a_pos['entry_px']
        T   = max(DTE_A / 365, 1e-4)
        opt = bs_price(path_a_pos['type'], px, path_a_pos['strike'], T, hv)
        pnl_pct = (opt - ep) / ep * 100

        if pnl_pct >= TRAIL_A_ACT * 100:
            path_a_pos['trail_hwm'] = max(path_a_pos.get('trail_hwm', opt), opt)
        hwm = path_a_pos.get('trail_hwm')

        reason = None
        if pnl_pct <= -STOP_A * 100:
            reason = f'STOP {pnl_pct:+.1f}%'
        elif pnl_pct >= TARGET_A * 100:
            reason = f'TARGET {pnl_pct:+.1f}%'
        elif hwm and opt < hwm * (1 - TRAIL_A_DST):
            reason = f'TRAIL {pnl_pct:+.1f}%'
        elif t >= A_FORCE:
            reason = f'FORCE-CLOSE {pnl_pct:+.1f}%'

        if reason:
            gross = (opt - ep) * LOT
            net   = gross - 2 * BROKERAGE
            trades_a.append({
                **path_a_pos,
                'exit_time': ts, 'exit_px': opt,
                'pnl_pct': pnl_pct, 'gross_pnl': gross, 'net_pnl': net,
                'reason': reason, 'spot_exit': px,
            })
            print(f"  EXIT  {ts.strftime('%H:%M')} | {path_a_pos['type']} "
                  f"{path_a_pos['strike']} | {reason}")
            print(f"         opt ₹{ep:.2f}→₹{opt:.2f}  Gross ₹{gross:+,.0f}  Net ₹{net:+,.0f}")
            path_a_pos = None
        continue

    if t >= A_END:
        continue

    # EMA crossover scan in last LOOKBACK bars
    w_start = max(0, idx - LOOKBACK + 1)
    w       = df.iloc[w_start:idx + 1]

    signal_type = None
    for k in range(1, len(w)):
        bar_time = w.index[k - 1].time()
        if bar_time < A_START:
            continue  # only crossovers within window
        p9, p21 = float(w['ema9'].iloc[k-1]), float(w['ema21'].iloc[k-1])
        c9, c21 = float(w['ema9'].iloc[k]),   float(w['ema21'].iloc[k])
        if p9 <= p21 and c9 > c21:
            signal_type = 'CALL'; break
        if p9 >= p21 and c9 < c21:
            signal_type = 'PUT';  break

    if signal_type is None:
        continue

    adx_val  = float(row['adx'])
    vwap_val = float(row['vwap'])

    # ADX filter
    if signal_type == 'CALL' and adx_val < CALL_ADX_A:
        print(f"  SKIP  {ts.strftime('%H:%M')} | {signal_type} signal ADX={adx_val:.1f} < {CALL_ADX_A}")
        continue
    if signal_type == 'PUT'  and adx_val < PUT_ADX_A:
        print(f"  SKIP  {ts.strftime('%H:%M')} | {signal_type} signal ADX={adx_val:.1f} < {PUT_ADX_A}")
        continue

    # VWAP filter
    if signal_type == 'CALL' and px < vwap_val:
        print(f"  SKIP  {ts.strftime('%H:%M')} | CALL but spot {px:.2f} < VWAP {vwap_val:.2f}")
        continue
    if signal_type == 'PUT'  and px > vwap_val:
        print(f"  SKIP  {ts.strftime('%H:%M')} | PUT but spot {px:.2f} > VWAP {vwap_val:.2f}")
        continue

    # alignment note
    align = f'  [ALIGNS with Path-B {b_dir}]' if b_dir == signal_type else ''

    T      = max(DTE_A / 365, 1e-4)
    strike = round_strike(px)
    opt_px = bs_price(signal_type, px, strike, T, hv)

    path_a_pos = {
        'type': signal_type, 'strike': strike, 'entry_px': opt_px,
        'entry_time': ts, 'spot_entry': px, 'adx': adx_val, 'vwap': vwap_val,
    }
    print(f"  ENTRY {ts.strftime('%H:%M')} | {signal_type} {strike} @ ₹{opt_px:.2f} "
          f"spot={px:.2f} ADX={adx_val:.1f} VWAP={vwap_val:.2f}{align}")

# force-close anything at 14:30
if path_a_pos:
    try:
        row_fc = df.between_time('14:30', '14:30').iloc[0]
    except IndexError:
        row_fc = df.iloc[-1]
    px  = float(row_fc['Close'])
    hv  = float(row_fc['hv']) if not math.isnan(row_fc['hv']) else 0.18
    T   = max(DTE_A / 365, 1e-4)
    opt = bs_price(path_a_pos['type'], px, path_a_pos['strike'], T, hv)
    pnl_pct = (opt - path_a_pos['entry_px']) / path_a_pos['entry_px'] * 100
    gross = (opt - path_a_pos['entry_px']) * LOT
    net   = gross - 2 * BROKERAGE
    trades_a.append({
        **path_a_pos,
        'exit_time': row_fc.name, 'exit_px': opt,
        'pnl_pct': pnl_pct, 'gross_pnl': gross, 'net_pnl': net,
        'reason': f'FORCE-CLOSE {pnl_pct:+.1f}%', 'spot_exit': px,
    })
    print(f"  FORCE-CLOSE 14:30 | {path_a_pos['type']} {path_a_pos['strike']} "
          f"| ₹{path_a_pos['entry_px']:.2f}→₹{opt:.2f}  Gross ₹{gross:+,.0f}  Net ₹{net:+,.0f}")
    path_a_pos = None

# ── SUMMARY ────────────────────────────────────────────────────────────────
print()
print("=" * 62)
print("COMBINED SUMMARY — March 30 2026")
print("=" * 62)

pb_net = path_b_result['net_pnl'] if path_b_result else 0.0
pa_net = sum(r['net_pnl'] for r in trades_a)
total  = pb_net + pa_net

print(f"\nPath B (ORB 9:15–10:55, hypothetical Monday run):")
if path_b_result:
    r = path_b_result
    print(f"  {r['type']} {r['strike']}  |  "
          f"Entry {r['entry_time'].strftime('%H:%M')} ₹{r['entry_px']:.2f}  "
          f"→  Exit {r['exit_time'].strftime('%H:%M')} ₹{r['exit_px']:.2f}")
    print(f"  P&L: {r['pnl_pct']:+.1f}%  |  Net ₹{r['net_pnl']:+,.0f}  ({r['reason']})")
else:
    print("  No trade — OR not broken")

print(f"\nPath A (vX EMA 11:00–14:00):")
if trades_a:
    for r in trades_a:
        print(f"  {r['type']} {r['strike']}  |  "
              f"Entry {r['entry_time'].strftime('%H:%M')} ₹{r['entry_px']:.2f}  "
              f"→  Exit {r['exit_time'].strftime('%H:%M')} ₹{r['exit_px']:.2f}")
        print(f"  P&L: {r['pnl_pct']:+.1f}%  |  Net ₹{r['net_pnl']:+,.0f}  ({r['reason']})")
else:
    print("  No trade — all signals blocked by ADX/VWAP filters")

print()
print(f"  Path B net  :  ₹{pb_net:+,.0f}")
print(f"  Path A net  :  ₹{pa_net:+,.0f}")
print(f"  {'─'*35}")
print(f"  COMBINED    :  ₹{total:+,.0f}")
print()

# ── intraday context ───────────────────────────────────────────────────────
print("─" * 62)
print("Intraday context (key bars):")
print(f"{'Time':<7} {'Close':>8} {'EMA9':>8} {'EMA21':>8} {'ADX':>6} {'VWAP':>8}  Cross")
for kts in ['09:15','09:20','09:25','09:30','10:00','10:30','10:55',
            '11:00','11:30','12:00','12:30','13:00','13:30','14:00','14:30','15:25']:
    try:
        row = df.between_time(kts, kts).iloc[0]
        cross = 'BEAR' if row['ema9'] < row['ema21'] else 'BULL'
        print(f"{kts:<7} {row['Close']:>8.2f} {row['ema9']:>8.2f} {row['ema21']:>8.2f} "
              f"{row['adx']:>6.1f} {row['vwap']:>8.2f}  {cross}")
    except Exception:
        pass
