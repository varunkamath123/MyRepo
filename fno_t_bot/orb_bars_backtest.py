"""
orb_bars_backtest.py — 3 / 4 / 5 / 6-bar Opening Range: per-day DOW × BARS matrix.

Tests OR_BARS in {3, 4, 5, 6} against Jan 2025–Apr 2026 historical data
(NIFTY + BANKNIFTY + SENSEX) to find the optimal ORB window per day of week.

Key model features vs the original orb_3v6_backtest.py:
  • 5-bar tested alongside 3 / 4 / 6
  • 12PM conditional checkpoint (loss_stop=True by default):
      – Position at a loss at 12:00  → hard close  (CKPT_LOSS)
      – Position profitable at 12:00 → run to 14:30 (target / stop / EOD)
  • Same OR width gates for all bar counts (Mon 0.25% / Tue 0.30% / Wed 0.25% / Thu 0.35%)
    A wider OR naturally breaks the gate more → fewer, cleaner signals for larger BARS.
  • ADX per-day floors : Mon 30 / Tue 25 / Wed 20 / Thu 25 / Fri 20
  • Entry window : from 1st bar after OR established → 11:30 (same for all BARS)
  • GAP-fade filter | VWAP filter | Thu CALL-suppressed (NIFTY/BNF)
  • Stop 50% | Target 80% | DTE=2 | Black-Scholes option pricing

Usage:
    python orb_bars_backtest.py                    # all instruments, loss stop ON
    python orb_bars_backtest.py NIFTY              # single instrument
    python orb_bars_backtest.py --no-loss-stop     # compare without 12PM loss stop
    python orb_bars_backtest.py NIFTY --no-loss-stop
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

# UTF-8 safe output (avoids charmap errors on Windows)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── CLI flags ─────────────────────────────────────────────────────────────────
LOSS_STOP = '--no-loss-stop' not in sys.argv
_args     = [a for a in sys.argv[1:] if not a.startswith('--')]

# ── Instrument constants ──────────────────────────────────────────────────────
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
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
IVS       = {'NIFTY': 0.14, 'BANKNIFTY': 0.17, 'SENSEX': 0.16}
STRIKES   = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 200}

# ── Trade parameters ──────────────────────────────────────────────────────────
BROKERAGE     = 40       # Rs per round-trip
OR_BUFFER     = 0.0005   # 0.05% breakout buffer (matches PATH_A_BUFFER)
STOP_PCT      = 0.50     # PATH_A_STOP
TARGET_PCT    = 0.80     # PATH_A_TARGET
DTE           = 2        # days to expiry for BS pricing
R             = 0.065    # risk-free rate
ADX_PERIOD    = 14
GAP_THRESHOLD = 0.003    # 0.30% gap threshold

# ── Session times ─────────────────────────────────────────────────────────────
ENTRY_CLOSE_T = dtime(11, 30)  # no new entries after 11:30 (all bar counts)
CHECKPOINT_T  = dtime(12,  0)  # 12PM loss-stop checkpoint
EOD_CLOSE_T   = dtime(14, 30)  # EOD force-close (profitable held positions)

# ── Per-day config ────────────────────────────────────────────────────────────
ADX_BY_DAY = {'Mon': 30, 'Tue': 25, 'Wed': 20, 'Thu': 25, 'Fri': 20}

# Same width gates for ALL bar counts:
#   A wider OR (more bars) hits the gate more often → auto-filters noise days.
#   Relaxing gates for more bars defeats the purpose and worsens WR.
WIDTH_MAX_BY_DAY = {
    'Mon': 0.0025,   # 0.25% — strict; only calmest Mon opens
    'Tue': 0.0030,   # 0.30%
    'Wed': 0.0025,   # 0.25% — matches PATH_A_DAY_CONFIG['Wed']
    'Thu': 0.0035,   # 0.35% — PUT-only day
    'Fri': None,     # no restriction — best ORB day
}

# Per-instrument direction/day rules
NO_CALL = {
    'NIFTY'    : {'Thu'},       # Thu CALL WR historically <32%
    'BANKNIFTY': {'Thu'},
    'SENSEX'   : set(),
}
SKIP_DAYS = {
    'NIFTY'    : set(),
    'BANKNIFTY': set(),
    'SENSEX'   : {'Thu'},       # SENSEX has no Thu ORB in live config
}

BAR_COUNTS = [3, 4, 5, 6]
DOW_ORDER  = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if call else max(K - S, 0)
        return max(float(intrinsic), 0.05)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    p  = (S * norm.cdf(d1)  - K * math.exp(-r * T) * norm.cdf(d2)  if call else
          K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return max(float(p), 0.05)


# ── Data loading ──────────────────────────────────────────────────────────────
def load_data(instrument: str) -> pd.DataFrame | None:
    root   = DATA_ROOTS[instrument]
    prefix = FILE_PREFIXES[instrument]
    files  = sorted(glob.glob(os.path.join(root, f'{prefix}*.csv')))
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
    df['ADX']    = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    df['tp']     = (hi + lo + cl) / 3
    df['VWAP']   = df.groupby('date')['tp'].transform(lambda x: x.expanding().mean())
    return df


def classify_gap(prev_close: float, curr_open: float, ref_close: float) -> str:
    pct = (curr_open - prev_close) / prev_close
    if abs(pct) < GAP_THRESHOLD:
        return 'INSIDE_OPEN'
    if pct < 0:
        return 'GAP_AND_GO_DN' if ref_close < curr_open else 'GAP_FADE_DN'
    return 'GAP_AND_GO_UP' if ref_close > curr_open else 'GAP_FADE_UP'


# ── Trade simulation ──────────────────────────────────────────────────────────
def simulate_trade(day_df: pd.DataFrame,
                   direction: str,
                   entry_idx: int,
                   S_entry: float,
                   iv: float,
                   lot: int,
                   instrument: str,
                   loss_stop: bool) -> tuple[float, str]:
    """
    Simulate one trade from entry_idx with conditional 12PM checkpoint.

    Timeline:
      entry_idx+1 → ENTRY_CLOSE_T : normal trade monitoring (stop / target)
      12:00 (CHECKPOINT_T)         : if pnl < 0 → CKPT_LOSS (hard close)
                                     if pnl > 0 → run on to EOD_CLOSE_T
      EOD_CLOSE_T (14:30)          : force-close whatever remains

    If loss_stop=False, ALL positions are closed at CHECKPOINT_T regardless of pnl
    (equivalent to the old model used in orb_3v6_backtest.py).
    """
    is_call = (direction == 'CALL')
    stride  = STRIKES[instrument]
    K       = round(S_entry / stride) * stride
    T_entry = max((DTE - entry_idx / 75) / 252, 1 / 252 / 75)
    opt_e   = bs_price(S_entry, K, T_entry, R, iv, call=is_call)
    stop_px = opt_e * (1 - STOP_PCT)
    tgt_px  = opt_e * (1 + TARGET_PCT)

    post_checkpoint = False   # True once we've passed the 12PM checkpoint

    for j in range(entry_idx + 1, len(day_df)):
        row   = day_df.iloc[j]
        t     = row['datetime'].time()
        T_j   = max((DTE - j / 75) / 252, 1 / 252 / 75)
        opt_j = bs_price(row['Close'], K, T_j, R, iv, call=is_call)

        # ── 12PM checkpoint ───────────────────────────────────────────────────
        if not post_checkpoint and t >= CHECKPOINT_T:
            post_checkpoint = True
            pnl_pct = (opt_j - opt_e) / opt_e
            if not loss_stop:
                # Old model: close everything at checkpoint
                return (opt_j - opt_e) * lot - BROKERAGE, 'CHECKPOINT'
            if pnl_pct < 0:
                # New model: loss → hard close
                return (opt_j - opt_e) * lot - BROKERAGE, 'CKPT_LOSS'
            # pnl >= 0 → let it run to 14:30

        # ── EOD force-close (only reached if profitable past checkpoint) ──────
        if t >= EOD_CLOSE_T:
            return (opt_j - opt_e) * lot - BROKERAGE, 'EOD'

        # ── Stop / target (fires any time in the day) ─────────────────────────
        if opt_j <= stop_px:
            return (stop_px - opt_e) * lot - BROKERAGE, 'STOP'
        if opt_j >= tgt_px:
            return (tgt_px - opt_e) * lot - BROKERAGE, 'TARGET'

    # Ran out of bars — use last available price
    last  = day_df.iloc[-1]
    T_l   = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_l = bs_price(last['Close'], K, T_l, R, iv, call=is_call)
    return (opt_l - opt_e) * lot - BROKERAGE, 'EOD'


# ── Day simulation ────────────────────────────────────────────────────────────
def simulate_day(day_df: pd.DataFrame,
                 prev_close: float,
                 instrument: str,
                 or_bars: int,
                 loss_stop: bool) -> dict | None:
    """Simulate one trading day with a given OR bar count."""
    if len(day_df) < or_bars + 2:
        return None

    lot = LOT_SIZES[instrument]
    iv  = IVS[instrument]
    dow = day_df.iloc[0]['datetime'].strftime('%a')

    # Instrument-level day skip (e.g. SENSEX skips Thu)
    if dow in SKIP_DAYS[instrument]:
        return None

    # Build OR (first or_bars × 5-min bars: 09:15 … 09:15+(or_bars-1)*5)
    or_hi    = day_df.iloc[:or_bars]['High'].max()
    or_lo    = day_df.iloc[:or_bars]['Low'].min()
    or_mid   = (or_hi + or_lo) / 2
    or_width = (or_hi - or_lo) / or_mid

    # OR width gate (same % threshold for all bar counts)
    width_max = WIDTH_MAX_BY_DAY.get(dow)
    if width_max is not None and or_width > width_max:
        return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
                'dow': dow, 'or_bars': or_bars,
                'result': 'WIDTH_BLOCKED', 'pnl': 0, 'win': None,
                'direction': '-', 'or_width_pct': round(or_width * 100, 3)}

    # Bar right after OR is established (used for gap classification)
    ref_bar   = day_df.iloc[or_bars]
    curr_open = day_df.iloc[0]['Open']
    gap_type  = classify_gap(prev_close, curr_open, ref_bar['Close'])
    adx_floor = ADX_BY_DAY.get(dow, 20)

    # Scan for first breakout signal within entry window
    for i in range(or_bars, len(day_df)):
        row = day_df.iloc[i]
        t   = row['datetime'].time()

        # Entry window closes at 11:30 (no new entries after that)
        if t >= ENTRY_CLOSE_T:
            break

        if row['ADX'] < adx_floor:
            continue

        px    = row['Close']
        vwap  = row['VWAP']
        no_c  = dow in NO_CALL[instrument]

        call_ok = (not no_c and
                   gap_type != 'GAP_FADE_UP' and
                   px > or_hi * (1 + OR_BUFFER) and
                   px > vwap)
        put_ok  = (gap_type != 'GAP_FADE_DN' and
                   px < or_lo * (1 - OR_BUFFER) and
                   px < vwap)

        if call_ok:
            direction = 'CALL'
        elif put_ok:
            direction = 'PUT'
        else:
            continue

        pnl, why = simulate_trade(day_df, direction, i, px,
                                  iv, lot, instrument, loss_stop)
        return {
            'date'        : day_df.iloc[0]['date'],
            'instrument'  : instrument,
            'dow'         : dow,
            'or_bars'     : or_bars,
            'result'      : why,
            'pnl'         : round(pnl),
            'win'         : pnl > 0,
            'direction'   : direction,
            'or_width_pct': round(or_width * 100, 3),
            'adx'         : round(float(row['ADX']), 1),
            'gap_type'    : gap_type,
            'entry_time'  : str(t),
        }

    # No signal fired
    return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
            'dow': dow, 'or_bars': or_bars,
            'result': 'NO_BREAK', 'pnl': 0, 'win': None,
            'direction': '-', 'or_width_pct': round(or_width * 100, 3)}


# ── Run one instrument ────────────────────────────────────────────────────────
def run_instrument(instrument: str, df: pd.DataFrame,
                   loss_stop: bool) -> list[dict]:
    """Run all bar counts for one instrument; return flat list of day results."""
    dates   = sorted(df['date'].unique())
    results = []
    for n_bars in BAR_COUNTS:
        for i, d in enumerate(dates):
            day_df = df[df['date'] == d].reset_index(drop=True)
            prev_close = (df[df['date'] == dates[i - 1]]['Close'].iloc[-1]
                          if i > 0 else day_df.iloc[0]['Open'])
            r = simulate_day(day_df, prev_close, instrument, n_bars, loss_stop)
            if r:
                results.append(r)
    return results


# ── Aggregate stats ───────────────────────────────────────────────────────────
def agg(rows: list[dict]) -> dict:
    """Compute stats from a list of trade rows (win is not None and pnl != 0)."""
    trades = [r for r in rows if r.get('win') is not None and r['pnl'] != 0]
    blocked  = sum(1 for r in rows if r.get('result') == 'WIDTH_BLOCKED')
    no_break = sum(1 for r in rows if r.get('result') == 'NO_BREAK')
    if not trades:
        return {'n': 0, 'wr': 0.0, 'pnl': 0, 'avg': 0.0,
                'best': 0, 'worst': 0, 'blocked': blocked, 'no_break': no_break}
    pnls = [t['pnl'] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        'n'       : len(trades),
        'wr'      : wins / len(trades) * 100,
        'pnl'     : sum(pnls),
        'avg'     : sum(pnls) / len(trades),
        'best'    : max(pnls),
        'worst'   : min(pnls),
        'blocked' : blocked,
        'no_break': no_break,
    }


def score(s: dict) -> float:
    """Composite ranking score: P&L weighted by WR."""
    if s['n'] == 0:
        return float('-inf')
    return s['pnl'] * (s['wr'] / 100) ** 0.5


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    target_instruments = ([a for a in _args if a in DATA_ROOTS]
                          or list(DATA_ROOTS.keys()))

    print()
    print('=' * 90)
    print('  ORB Bar-Count Backtest  |  3 / 4 / 5 / 6 bars  |  per Day-of-Week')
    print(f'  12PM loss stop : {"ON  (losses close at 12:00; profits run to 14:30)" if LOSS_STOP else "OFF (all positions close at 12:00)"}')
    print('=' * 90)

    # Load & prepare data
    dfs: dict[str, pd.DataFrame] = {}
    for inst in target_instruments:
        print(f'  Loading {inst} ...', end=' ', flush=True)
        df = load_data(inst)
        if df is None:
            print('NO DATA — skipped')
            continue
        df = add_indicators(df)
        dfs[inst] = df
        print(f'{len(df["date"].unique())} days')

    print()
    if not dfs:
        print('  No data found. Check DATA_ROOTS paths.')
        return

    # Collect all results
    all_rows: list[dict] = []
    for inst, df in dfs.items():
        print(f'  Simulating {inst} ({", ".join(str(b)+"-bar" for b in BAR_COUNTS)}) ...')
        all_rows.extend(run_instrument(inst, df, LOSS_STOP))

    df_all = pd.DataFrame(all_rows)

    # ── Per-instrument summary ────────────────────────────────────────────────
    print()
    for inst in target_instruments:
        if inst not in dfs:
            continue
        print(f'  {"─"*86}')
        print(f'  {inst}')
        print(f'  {"─"*86}')
        hdr = (f'  {"BARS":<6} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
               f'{"Avg/trade":>10} {"Best":>9} {"Worst":>9} '
               f'{"Blocked":>8} {"NoBreak":>8}')
        print(hdr)
        print(f'  {"─"*86}')
        inst_rows = [r for r in all_rows if r['instrument'] == inst]
        for nb in BAR_COUNTS:
            rows = [r for r in inst_rows if r['or_bars'] == nb]
            s    = agg(rows)
            tag  = f'{nb}-bar'
            print(f'  {tag:<6} {s["n"]:>7} {s["wr"]:>5.1f}% '
                  f'Rs{s["pnl"]:>10,.0f} Rs{s["avg"]:>9,.0f} '
                  f'Rs{s["best"]:>8,.0f} Rs{s["worst"]:>8,.0f} '
                  f'{s["blocked"]:>8} {s["no_break"]:>8}')
        print()

    # ── Combined summary ──────────────────────────────────────────────────────
    if len(dfs) > 1:
        print(f'  {"─"*86}')
        print(f'  COMBINED (all instruments)')
        print(f'  {"─"*86}')
        hdr = (f'  {"BARS":<6} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
               f'{"Avg/trade":>10} {"Best":>9} {"Worst":>9}')
        print(hdr)
        print(f'  {"─"*86}')
        for nb in BAR_COUNTS:
            rows = [r for r in all_rows if r['or_bars'] == nb]
            s    = agg(rows)
            tag  = f'{nb}-bar'
            print(f'  {tag:<6} {s["n"]:>7} {s["wr"]:>5.1f}% '
                  f'Rs{s["pnl"]:>10,.0f} Rs{s["avg"]:>9,.0f} '
                  f'Rs{s["best"]:>8,.0f} Rs{s["worst"]:>8,.0f}')
        print()

    # ── DOW × BARS matrix (all instruments combined) ──────────────────────────
    print()
    print('  ' + '═' * 86)
    print('  DOW × BARS MATRIX  (all instruments combined)')
    print('  ' + '═' * 86)

    # Header row
    col_w = 23
    hdr = f'  {"DOW":<5}'
    for nb in BAR_COUNTS:
        lbl = f'{nb}-bar'
        hdr += f'  {lbl:^{col_w}}'
    hdr += f'  {"BEST BAR":<10}'
    print(hdr)
    sub = f'  {"":5}'
    for _ in BAR_COUNTS:
        sub += f'  {"Tr":>3} {"WR%":>5} {"P&L":>10}'
    print(sub)
    print('  ' + '─' * 86)

    recommendation: dict[str, tuple[int, dict]] = {}

    for dow in DOW_ORDER:
        row_str = f'  {dow:<5}'
        best_bars  = None
        best_score = float('-inf')
        for nb in BAR_COUNTS:
            rows = [r for r in all_rows if r['dow'] == dow and r['or_bars'] == nb]
            s    = agg(rows)
            if s['n'] > 0:
                row_str += f'  {s["n"]:>3} {s["wr"]:>4.0f}% Rs{s["pnl"]:>7,.0f}'
                sc = score(s)
                if sc > best_score:
                    best_score = sc
                    best_bars  = nb
            else:
                row_str += f'  {"–":>3} {"–":>5} {"–":>10}'
        if best_bars and best_score > 0:
            row_str += f'  {best_bars}-bar ✓'
            recommendation[dow] = (best_bars, agg([r for r in all_rows
                                                   if r['dow'] == dow
                                                   and r['or_bars'] == best_bars]))
        elif best_bars:
            row_str += f'  {best_bars}-bar (neg)'
            recommendation[dow] = (best_bars, agg([r for r in all_rows
                                                   if r['dow'] == dow
                                                   and r['or_bars'] == best_bars]))
        else:
            row_str += f'  {"–":<10}'
        print(row_str)

    print()

    # ── Per-instrument DOW × BARS breakdown ──────────────────────────────────
    for inst in target_instruments:
        if inst not in dfs:
            continue
        print(f'  {"─"*86}')
        print(f'  {inst} — DOW breakdown')
        print(f'  {"─"*86}')
        sub = f'  {"DOW":<5}'
        for _ in BAR_COUNTS:
            sub += f'  {"Tr":>3} {"WR%":>5} {"P&L":>10}'
        print(sub)
        for dow in DOW_ORDER:
            row_str = f'  {dow:<5}'
            for nb in BAR_COUNTS:
                rows = [r for r in all_rows
                        if r['instrument'] == inst
                        and r['dow'] == dow
                        and r['or_bars'] == nb]
                s = agg(rows)
                if s['n'] > 0:
                    row_str += f'  {s["n"]:>3} {s["wr"]:>4.0f}% Rs{s["pnl"]:>7,.0f}'
                else:
                    row_str += f'  {"–":>3} {"–":>5} {"–":>10}'
            print(row_str)
        print()

    # ── Exit reason breakdown ─────────────────────────────────────────────────
    print()
    print(f'  {"─"*86}')
    print(f'  Exit reason breakdown (all instruments, trades only)')
    print(f'  {"─"*86}')
    trade_rows = [r for r in all_rows if r.get('win') is not None and r['pnl'] != 0]
    for nb in BAR_COUNTS:
        t_rows = [r for r in trade_rows if r['or_bars'] == nb]
        if not t_rows:
            continue
        reasons: dict[str, tuple[int, int]] = {}
        for r in t_rows:
            rsn = r.get('result', '?')
            prev = reasons.get(rsn, (0, 0))
            reasons[rsn] = (prev[0] + 1, prev[1] + r['pnl'])
        print(f'  {nb}-bar:')
        for rsn, (cnt, pnl) in sorted(reasons.items(), key=lambda x: -x[1][0]):
            print(f'    {rsn:<18} {cnt:>4} trades  Rs{pnl:>+10,.0f}')

    # ── Recommendation ────────────────────────────────────────────────────────
    print()
    print('  ' + '═' * 86)
    print('  RECOMMENDATION: optimal BARS per day (highest composite score = P&L × WR½)')
    print('  ' + '═' * 86)
    print(f'  {"DOW":<6} {"Bars":<6} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
          f'{"Avg/trade":>10}  Status')
    print(f'  {"─"*86}')
    for dow in DOW_ORDER:
        if dow not in recommendation:
            print(f'  {dow:<6} {"–":<6} {"–":>7} {"–":>6} {"–":>11} {"–":>10}  no data')
            continue
        nb, s = recommendation[dow]
        status = 'PROFITABLE ✓' if s['pnl'] > 0 else 'negative  ✗'
        print(f'  {dow:<6} {nb}-bar  {s["n"]:>7} {s["wr"]:>5.1f}%  '
              f'Rs{s["pnl"]:>10,.0f} Rs{s["avg"]:>9,.0f}  {status}')
    print()

    model_note = ('12PM loss-stop ON  → losses closed at 12:00; profits run to 14:30'
                  if LOSS_STOP else
                  '12PM loss-stop OFF → all positions closed at 12:00 (old model)')
    print(f'  Model: {model_note}')
    print()


if __name__ == '__main__':
    main()
