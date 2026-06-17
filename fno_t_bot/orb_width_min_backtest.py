"""
orb_width_min_backtest.py — Minimum OR-width sweep backtest.

Tests whether requiring a minimum opening-range width before allowing an ORB
entry improves win-rate and net P&L vs the current baseline (no min width).

Current live config (from config.PATH_A_DAY_CONFIG, May 2026):
  Mon : 4 bars  | Tue : 5 bars  | Wed : 3 bars
  Thu : 5 bars  | Fri : 5 bars
  No max-width gate (removed May 2026; all or_width_max = None)
  Min-extension gate: 0.10% beyond OR boundary (PATH_A_MIN_OR_EXTENSION)

What this backtest adds:
  OR_WIDTH_MIN — price must span at least X% high-to-low during the opening bars.
  Below this threshold: day is skipped (too compressed, noise > signal).

Sweep: 0.00% (baseline) → 0.15 → 0.20 → 0.25 → 0.30 → 0.35 → 0.40 → 0.45%

Trade parameters (matched to live config May 2026):
  Stop     : 25%  | Target  : 28%  | Trail activation: 18%  | Trail dist: 10%
  DTE      : 2    | Brokerage: Rs40 / round-trip
  Checkpoint: 12:00 — loss → hard close | profit → trail to 14:30
  Entry window per day:
    Mon/Tue/Thu/Fri : 09:30–12:00 (after OR bars)
    Wed             : 09:30–10:55 (BNF expiry day early close)

Instruments: NIFTY + BANKNIFTY + SENSEX

Usage:
  python orb_width_min_backtest.py              # all instruments
  python orb_width_min_backtest.py NIFTY        # single instrument
"""
from __future__ import annotations

import glob
import io
import math
import os
import sys
from datetime import time as dtime

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── CLI ───────────────────────────────────────────────────────────────────────
_args       = [a for a in sys.argv[1:] if not a.startswith('--')]
INSTRUMENTS = _args if _args else ['NIFTY', 'BANKNIFTY', 'SENSEX']

# ── Data paths ────────────────────────────────────────────────────────────────
DATA_ROOTS = {
    'NIFTY'    : r'C:\quant_trading\data\nifty_5min',
    'BANKNIFTY': r'C:\quant_trading\data\banknifty_5min',
    'SENSEX'   : r'C:\quant_trading\data\sensex_5min',
}
FILE_PREFIXES = {
    'NIFTY'    : 'nifty_5min_',
    'BANKNIFTY': 'banknifty_5min_',
    'SENSEX'   : 'sensex_5min_',
}

# ── Instrument constants ──────────────────────────────────────────────────────
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
IVS       = {'NIFTY': 0.14, 'BANKNIFTY': 0.17, 'SENSEX': 0.16}
STRIKES   = {'NIFTY': 50,  'BANKNIFTY': 100,   'SENSEX': 200}

# ── Trade parameters (live config May 2026) ───────────────────────────────────
BROKERAGE  = 40
STOP_PCT   = 0.25     # 25% stop
TARGET_PCT = 0.28     # 28% target
TRAIL_ACT  = 0.18     # trailing activates at 18% gain
TRAIL_DIST = 0.10     # 10% trail distance
DTE        = 2
R          = 0.065
ADX_PERIOD = 14
OR_BUFFER  = 0.001    # 0.10% extension beyond OR (PATH_A_MIN_OR_EXTENSION)
GAP_THRESH = 0.003    # 0.30% gap threshold

# ── Per-day config (live PATH_A_DAY_CONFIG May 2026) ─────────────────────────
DAY_CONFIG = {
    'Mon': {'or_bars': 4, 'adx_min': 25, 'entry_end': dtime(12, 0),
            'no_call': False, 'no_put': False, 'skip': False},
    'Tue': {'or_bars': 5, 'adx_min': 25, 'entry_end': dtime(12, 0),
            'no_call': False, 'no_put': False, 'skip': False},
    'Wed': {'or_bars': 3, 'adx_min': 25, 'entry_end': dtime(10, 55),
            'no_call': False, 'no_put': False, 'skip': False},
    'Thu': {'or_bars': 5, 'adx_min': 25, 'entry_end': dtime(12, 0),
            'no_call': False, 'no_put': False, 'skip': False},
    'Fri': {'or_bars': 5, 'adx_min': 20, 'entry_end': dtime(12, 0),
            'no_call': False, 'no_put': False, 'skip': False},
}

# Skip rules (live config)
SKIP_DAYS = {
    'NIFTY'    : set(),
    'BANKNIFTY': set(),
    'SENSEX'   : {'Thu'},
}
NO_CALL_DAYS = {      # per-instrument (Thursday CALL suppression active)
    'NIFTY'    : {'Thu'},
    'BANKNIFTY': {'Thu'},
    'SENSEX'   : set(),
}

# ── Width sweep thresholds ────────────────────────────────────────────────────
WIDTH_MIN_SWEEP = [0.000, 0.0015, 0.002, 0.0025, 0.003, 0.0035, 0.004, 0.0045]

# ── Session times ─────────────────────────────────────────────────────────────
CHECKPOINT_T = dtime(12,  0)
EOD_T        = dtime(14, 30)


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if call else max(K - S, 0)
        return max(float(intrinsic), 0.05)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    p  = (S * norm.cdf(d1)  - K * math.exp(-r * T) * norm.cdf(d2)  if call else
          K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return max(float(p), 0.05)


# ── Data loading ──────────────────────────────────────────────────────────────
def load_data(instrument: str) -> pd.DataFrame | None:
    root, prefix = DATA_ROOTS[instrument], FILE_PREFIXES[instrument]
    files = sorted(glob.glob(os.path.join(root, f'{prefix}*.csv')))
    if not files:
        return None
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df['datetime'] = pd.to_datetime(df['ts']).dt.tz_localize(None)
            dfs.append(df.sort_values('datetime'))
        except Exception:
            continue
    if not dfs:
        return None
    out = pd.concat(dfs, ignore_index=True).sort_values('datetime').reset_index(drop=True)
    out['date'] = out['datetime'].dt.date
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    hi, lo, cl = df['High'], df['Low'], df['Close']
    pdm = hi.diff().clip(lower=0)
    ndm = (-lo.diff()).clip(lower=0)
    pdm = pdm.where(pdm > ndm, 0.0)
    ndm = ndm.where(ndm > pdm, 0.0)
    tr  = pd.concat([hi - lo, (hi - cl.shift()).abs(),
                     (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    df['ADX']  = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    df['DI_p'] = pdi
    df['DI_m'] = ndi
    df['tp']   = (hi + lo + cl) / 3
    df['VWAP'] = df.groupby('date')['tp'].transform(lambda x: x.expanding().mean())
    return df


# ── Trade simulation (matches live exit logic) ────────────────────────────────
def simulate_trade(day_df: pd.DataFrame, direction: str, entry_idx: int,
                   S_entry: float, iv: float, lot: int,
                   instrument: str) -> tuple[float, str]:
    is_call = (direction == 'CALL')
    stride  = STRIKES[instrument]
    K       = round(S_entry / stride) * stride
    T_entry = max((DTE - entry_idx / 75) / 252, 1 / 252 / 75)
    opt_e   = bs_price(S_entry, K, T_entry, R, iv, call=is_call)

    stop_px    = opt_e * (1 - STOP_PCT)
    target_px  = opt_e * (1 + TARGET_PCT)
    trail_high = opt_e   # peak option price seen (for trailing stop)
    trailing   = False
    post_ckpt  = False

    for j in range(entry_idx + 1, len(day_df)):
        row   = day_df.iloc[j]
        t     = row['datetime'].time()
        T_j   = max((DTE - j / 75) / 252, 1 / 252 / 75)
        opt_j = bs_price(row['Close'], K, T_j, R, iv, call=is_call)

        # ── Trailing stop update ──────────────────────────────────────────────
        if opt_j > trail_high:
            trail_high = opt_j
        if not trailing and opt_j >= opt_e * (1 + TRAIL_ACT):
            trailing = True
        if trailing:
            trail_stop = trail_high * (1 - TRAIL_DIST)
            if opt_j <= trail_stop:
                return (opt_j - opt_e) * lot - BROKERAGE, 'TRAIL'

        # ── 12:00 checkpoint ──────────────────────────────────────────────────
        if not post_ckpt and t >= CHECKPOINT_T:
            post_ckpt = True
            pnl_pct   = (opt_j - opt_e) / opt_e
            if pnl_pct < 0:
                return (opt_j - opt_e) * lot - BROKERAGE, 'CKPT_LOSS'
            # Profitable → continue to EOD

        # ── EOD force-close ───────────────────────────────────────────────────
        if t >= EOD_T:
            return (opt_j - opt_e) * lot - BROKERAGE, 'EOD'

        # ── Hard stop / target ────────────────────────────────────────────────
        if opt_j <= stop_px:
            return (stop_px - opt_e) * lot - BROKERAGE, 'STOP'
        if opt_j >= target_px:
            return (target_px - opt_e) * lot - BROKERAGE, 'TARGET'

    last  = day_df.iloc[-1]
    T_l   = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_l = bs_price(last['Close'], K, T_l, R, iv, call=is_call)
    return (opt_l - opt_e) * lot - BROKERAGE, 'EOD'


# ── Single-day simulation ─────────────────────────────────────────────────────
def simulate_day(day_df: pd.DataFrame, prev_close: float,
                 instrument: str, width_min: float) -> dict | None:
    if len(day_df) < 4:
        return None

    dow = day_df.iloc[0]['datetime'].strftime('%a')
    cfg = DAY_CONFIG.get(dow)
    if cfg is None:
        return None
    if cfg['skip'] or dow in SKIP_DAYS[instrument]:
        return None

    or_bars   = cfg['or_bars']
    adx_floor = cfg['adx_min']
    entry_end = cfg['entry_end']
    no_call   = cfg['no_call'] or dow in NO_CALL_DAYS[instrument]

    if len(day_df) < or_bars + 1:
        return None

    # ── OR construction ───────────────────────────────────────────────────────
    or_hi    = day_df.iloc[:or_bars]['High'].max()
    or_lo    = day_df.iloc[:or_bars]['Low'].min()
    or_mid   = (or_hi + or_lo) / 2
    or_width = (or_hi - or_lo) / or_mid   # fraction

    # ── Minimum width gate (the filter we're testing) ─────────────────────────
    if or_width < width_min:
        return {
            'or_width_pct': round(or_width * 100, 3),
            'filtered'    : True,
            'dow'         : dow,
            'instrument'  : instrument,
        }

    # ── Gap type ──────────────────────────────────────────────────────────────
    curr_open = day_df.iloc[0]['Open']
    ref_bar   = day_df.iloc[or_bars]
    gap_pct   = (curr_open - prev_close) / prev_close
    if abs(gap_pct) < GAP_THRESH:
        gap_type = 'INSIDE_OPEN'
    elif gap_pct < 0:
        gap_type = 'GAP_AND_GO_DN' if ref_bar['Close'] < curr_open else 'GAP_FADE_DN'
    else:
        gap_type = 'GAP_AND_GO_UP' if ref_bar['Close'] > curr_open else 'GAP_FADE_UP'

    iv  = IVS[instrument]
    lot = LOT_SIZES[instrument]

    # ── Scan for breakout within entry window ─────────────────────────────────
    for i in range(or_bars, len(day_df)):
        row = day_df.iloc[i]
        t   = row['datetime'].time()
        if t >= entry_end:
            break
        if row['ADX'] < adx_floor:
            continue

        px   = row['Close']
        vwap = row['VWAP']

        call_ok = (not no_call and
                   gap_type != 'GAP_FADE_UP' and
                   px > or_hi * (1 + OR_BUFFER) and
                   px > vwap)
        put_ok  = (not cfg['no_put'] and
                   gap_type != 'GAP_FADE_DN' and
                   px < or_lo * (1 - OR_BUFFER) and
                   px < vwap)

        if call_ok:
            direction = 'CALL'
        elif put_ok:
            direction = 'PUT'
        else:
            continue

        pnl, why = simulate_trade(day_df, direction, i, px, iv, lot, instrument)
        return {
            'or_width_pct': round(or_width * 100, 3),
            'filtered'    : False,
            'dow'         : dow,
            'instrument'  : instrument,
            'direction'   : direction,
            'pnl'         : round(pnl),
            'win'         : pnl > 0,
            'result'      : why,
            'adx'         : round(float(row['ADX']), 1),
            'entry_time'  : str(t),
        }

    return {
        'or_width_pct': round(or_width * 100, 3),
        'filtered'    : False,
        'dow'         : dow,
        'instrument'  : instrument,
        'direction'   : None,
        'pnl'         : 0,
        'win'         : None,
        'result'      : 'NO_BREAK',
    }


# ── Run one instrument across all width_min values ────────────────────────────
def run_instrument(instrument: str, df: pd.DataFrame) -> dict[float, list[dict]]:
    """Returns {width_min: [day_results]} for all sweep values."""
    dates  = sorted(df['date'].unique())
    # Pre-compute all day results with full data (no width filter yet) —
    # then apply width filter in post-processing to avoid repeated simulation.
    day_results = []
    for i, d in enumerate(dates):
        day_df = df[df['date'] == d].reset_index(drop=True)
        prev_close = (df[df['date'] == dates[i - 1]]['Close'].iloc[-1]
                      if i > 0 else day_df.iloc[0]['Open'])
        # Simulate with width_min=0 (no filter) to get trade data + or_width
        r = simulate_day(day_df, prev_close, instrument, width_min=0.0)
        if r:
            day_results.append(r)

    # Now apply each width_min threshold in post-processing
    results_by_thresh: dict[float, list[dict]] = {}
    for wm in WIDTH_MIN_SWEEP:
        kept = []
        for r in day_results:
            if r.get('or_width_pct', 0) / 100 < wm:
                kept.append({**r, 'filtered': True})
            else:
                kept.append(r)
        results_by_thresh[wm] = kept
    return results_by_thresh


# ── Aggregate stats for one threshold ────────────────────────────────────────
def agg(rows: list[dict]) -> dict:
    filtered = sum(1 for r in rows if r.get('filtered'))
    trades   = [r for r in rows if not r.get('filtered') and r.get('win') is not None]
    no_break = sum(1 for r in rows if not r.get('filtered') and r.get('result') == 'NO_BREAK')
    if not trades:
        return {'n': 0, 'wr': 0.0, 'pnl': 0, 'avg': 0,
                'filtered': filtered, 'no_break': no_break}
    pnls = [t['pnl'] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        'n'        : len(trades),
        'wr'       : wins / len(trades) * 100,
        'pnl'      : sum(pnls),
        'avg'      : round(sum(pnls) / len(trades)),
        'filtered' : filtered,
        'no_break' : no_break,
        'pf'       : (sum(p for p in pnls if p > 0) /
                      abs(sum(p for p in pnls if p < 0)) if any(p < 0 for p in pnls) else 999),
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"\n{'='*72}")
    print(f"  OR Minimum Width Sweep Backtest — {', '.join(INSTRUMENTS)}")
    print(f"  OR bars: Mon=4 Tue=5 Wed=3 Thu=5 Fri=5  (live config May 2026)")
    print(f"  Exits : Stop 25% | Target 28% | Trail 18%/10% | Checkpoint 12:00")
    print(f"  No max-width gate (removed May 2026)")
    print(f"{'='*72}\n")

    # Collect all_results[instrument][width_min] = [rows]
    all_results: dict[str, dict[float, list[dict]]] = {}
    for inst in INSTRUMENTS:
        print(f"  Loading {inst}... ", end='', flush=True)
        df = load_data(inst)
        if df is None:
            print("NO DATA")
            continue
        df = add_indicators(df)
        print(f"{df['date'].nunique()} days")
        all_results[inst] = run_instrument(inst, df)

    if not all_results:
        print("No data loaded.")
        return

    # ── Per-instrument tables ─────────────────────────────────────────────────
    for inst, by_thresh in all_results.items():
        print(f"\n{'─'*72}")
        print(f"  {inst}")
        print(f"{'─'*72}")
        print(f"  {'Width Min':>10} {'Trades':>7} {'WR%':>7} {'Net P&L':>10} "
              f"{'Avg/trade':>10} {'PF':>6} {'Filtered':>9} {'Δ vs 0%':>10}")
        print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*10} {'-'*10} {'-'*6} {'-'*9} {'-'*10}")
        base = None
        for wm in WIDTH_MIN_SWEEP:
            s = agg(by_thresh[wm])
            if base is None:
                base = s
            delta = f"{'':>10}" if base is None or wm == 0.0 else f"Rs{s['pnl'] - base['pnl']:>+8,}"
            print(f"  {wm*100:>9.2f}% {s['n']:>7} {s['wr']:>7.1f} "
                  f"Rs{s['pnl']:>9,} Rs{s['avg']:>9,} {s['pf']:>6.2f} "
                  f"{s['filtered']:>9} {delta:>10}")

    # ── Combined (all instruments) ────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(f"  COMBINED ({', '.join(all_results.keys())})")
    print(f"{'─'*72}")
    print(f"  {'Width Min':>10} {'Trades':>7} {'WR%':>7} {'Net P&L':>10} "
          f"{'Avg/trade':>10} {'PF':>6} {'Filtered':>9} {'Δ vs 0%':>10}")
    print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*10} {'-'*10} {'-'*6} {'-'*9} {'-'*10}")
    base_combined = None
    for wm in WIDTH_MIN_SWEEP:
        all_rows = []
        for by_thresh in all_results.values():
            all_rows.extend(by_thresh.get(wm, []))
        s = agg(all_rows)
        if base_combined is None:
            base_combined = s
        delta = '' if wm == 0.0 else f"Rs{s['pnl'] - base_combined['pnl']:>+8,}"
        print(f"  {wm*100:>9.2f}% {s['n']:>7} {s['wr']:>7.1f} "
              f"Rs{s['pnl']:>9,} Rs{s['avg']:>9,} {s['pf']:>6.2f} "
              f"{s['filtered']:>9} {delta:>10}")

    # ── Per-DOW breakdown at each threshold ───────────────────────────────────
    print(f"\n{'─'*72}")
    print(f"  PER-DAY breakdown at key thresholds")
    print(f"{'─'*72}")
    key_thresholds = [0.000, 0.002, 0.0025, 0.003]
    dow_order      = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    for wm in key_thresholds:
        print(f"\n  Width min = {wm*100:.2f}%:")
        all_rows = []
        for by_thresh in all_results.values():
            all_rows.extend(by_thresh.get(wm, []))
        print(f"  {'DOW':>5} {'N':>5} {'WR%':>7} {'Net P&L':>10} {'Filtered':>9}")
        for dow in dow_order:
            rows = [r for r in all_rows if r.get('dow') == dow]
            s    = agg(rows)
            print(f"  {dow:>5} {s['n']:>5} {s['wr']:>7.1f} Rs{s['pnl']:>9,} {s['filtered']:>9}")

    # ── Width distribution of actual trades (baseline) ────────────────────────
    print(f"\n{'─'*72}")
    print(f"  OR WIDTH DISTRIBUTION of actual trades (baseline, no filter)")
    print(f"{'─'*72}")
    all_trades_0 = []
    for by_thresh in all_results.values():
        all_trades_0.extend([r for r in by_thresh.get(0.0, [])
                             if not r.get('filtered') and r.get('win') is not None])
    if all_trades_0:
        widths = pd.Series([r['or_width_pct'] for r in all_trades_0])
        wins_w = pd.Series([r['or_width_pct'] for r in all_trades_0 if r['win']])
        loss_w = pd.Series([r['or_width_pct'] for r in all_trades_0 if not r['win']])
        print(f"  All trades  — mean: {widths.mean():.3f}%  "
              f"median: {widths.median():.3f}%  "
              f"p25: {widths.quantile(0.25):.3f}%  "
              f"p75: {widths.quantile(0.75):.3f}%")
        print(f"  Winners     — mean: {wins_w.mean():.3f}%  median: {wins_w.median():.3f}%")
        print(f"  Losers      — mean: {loss_w.mean():.3f}%  median: {loss_w.median():.3f}%")

        buckets = [0.0, 0.15, 0.25, 0.35, 0.45, 0.60, 1.0]
        print(f"\n  {'Width bucket':>20} {'N':>5} {'WR%':>7} {'Net P&L':>10}")
        for lo, hi in zip(buckets, buckets[1:]):
            bucket = [r for r in all_trades_0
                      if lo <= r['or_width_pct'] < hi]
            if not bucket:
                continue
            bpnl = sum(r['pnl'] for r in bucket)
            bwr  = sum(1 for r in bucket if r['win']) / len(bucket) * 100
            print(f"  {lo:.2f}%–{hi:.2f}%           {len(bucket):>5} {bwr:>7.1f} Rs{bpnl:>9,}")

    print(f"\n{'='*72}")
    print(f"  Recommendation: choose the width_min where WR and P&L both improve")
    print(f"  over baseline with ≤15% trade reduction. Deploy as PATH_A_OR_WIDTH_MIN")
    print(f"  in config.py (checked in options_bot.py get_path_a_signal).")
    print(f"{'='*72}\n")


if __name__ == '__main__':
    main()
