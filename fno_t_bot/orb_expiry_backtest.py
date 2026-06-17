"""
orb_expiry_backtest.py — Early-session ORB with trail-from-80% exit model.

Recreates the old early_bot.py mechanics to validate the BNF Wednesday+Friday
strategy and find the optimal parameters for a new Tue/Wed approach.

Old model (early_bot.py era — BNF Wed+Fri profitable):
  • 3-bar OR (09:15–09:25)
  • Entry window : 09:30–10:55
  • Stop         : 50%
  • Trail        : activates at TRAIL_ACT gain, TRAIL_DIST distance from peak
  • Force-close  : 10:55 (no 12PM checkpoint)

This script tests variations of that model across all days to identify:
  1. Which days are profitable with the early-exit trail model
  2. Whether a tighter entry window (09:30–10:00) improves Tue/Wed specifically
  3. Whether the Wednesday BNF edge is robust enough to re-enable

Usage:
    python orb_expiry_backtest.py              # all instruments, default params
    python orb_expiry_backtest.py NIFTY        # single instrument
    python orb_expiry_backtest.py --tight      # 09:30–10:00 entry window only
    python orb_expiry_backtest.py --wed-fri    # Wednesday + Friday only
    python orb_expiry_backtest.py --no-trail   # hard 80% target (compare with trail)
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

# ── CLI flags ─────────────────────────────────────────────────────────────────
TIGHT_WINDOW = '--tight'    in sys.argv   # 09:30–10:00 entry only
WED_FRI_ONLY = '--wed-fri'  in sys.argv   # only Wed + Fri
NO_TRAIL     = '--no-trail' in sys.argv   # hard 80% target (no trail)
_args        = [a for a in sys.argv[1:] if not a.startswith('--')]

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

# ── Trade parameters (old early_bot model) ────────────────────────────────────
BROKERAGE     = 40
OR_BUFFER     = 0.0005      # 0.05%
OR_BARS       = 3           # 09:15, 09:20, 09:25 — tight 15-min OR
STOP_PCT      = 0.50        # 50% stop-loss on option premium
HARD_TARGET   = 0.80        # 80% hard target — exit immediately (same as PATH_A_TARGET)
TRAIL_ACT     = 0.12        # trail activates at +12% gain (PATH_A_TRAIL_ACT)
TRAIL_DIST    = 0.10        # trail distance: 10% pullback from peak (PATH_A_TRAIL_DIST)
# With use_trail=True: check trail AFTER stop, stop hard target from firing.
# With use_trail=False (--no-trail): uses hard target at HARD_TARGET, no trail.

DTE           = 2
R             = 0.065
ADX_PERIOD    = 14
GAP_THRESHOLD = 0.003

# ── Session times ─────────────────────────────────────────────────────────────
ENTRY_OPEN_T  = dtime(9, 30)   # first possible entry (bar after 3-bar OR)
ENTRY_TIGHT_T = dtime(10,  0)  # tight-window close (--tight)
ENTRY_WIDE_T  = dtime(10, 55)  # standard early ORB close
FORCE_CLOSE_T = dtime(10, 55)  # hard close for all positions

# ── Per-day ADX and width gates ───────────────────────────────────────────────
# ADX floors — same as the old early_bot.py
ADX_BY_DAY = {'Mon': 30, 'Tue': 25, 'Wed': 25, 'Thu': 25, 'Fri': 20}

# Width gates — same as PATH_A_OR_WIDTH_MAX (keep tight, higher bar for BNF)
WIDTH_MAX_BY_DAY = {
    'Mon': 0.0025,
    'Tue': 0.0030,
    'Wed': None,     # no width gate on Wed — old early_bot config (orb_handover_backtest.py)
                     # BNF expiry day: wider gap opens are normal; gate would block all trades
    'Thu': 0.0035,
    'Fri': None,     # no restriction — best ORB day
}

# CALL suppression
NO_CALL = {
    'NIFTY'    : {'Thu'},
    'BANKNIFTY': {'Thu'},
    'SENSEX'   : set(),
}

# Instrument+day skips
SKIP_DAYS = {
    'NIFTY'    : set(),
    'BANKNIFTY': set(),
    'SENSEX'   : {'Thu'},
}

DOW_ORDER = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']


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


# ── Trade simulation — trail-from-80% model ───────────────────────────────────
def simulate_trade(day_df: pd.DataFrame,
                   direction: str,
                   entry_idx: int,
                   S_entry: float,
                   iv: float,
                   lot: int,
                   instrument: str,
                   use_trail: bool) -> tuple[float, str]:
    """
    Simulate one trade using the early-exit model (mirrors early_backtest.py).

    Exit priority:
      1. Stop-loss     : option premium <= STOP_PCT loss
      2a. Hard target  : (use_trail=False) pnl_pct >= HARD_TARGET → exit immediately
      2b. Trailing stop: (use_trail=True)  once peak >= TRAIL_ACT, exit if
                         pnl falls TRAIL_DIST below peak. Target still fires at HARD_TARGET.
      3. Force-close   : hard close at FORCE_CLOSE_T regardless

    use_trail=True (default): trail from 12% + hard 80% cap — matches early_backtest.py
    use_trail=False (--no-trail): hard 80% target only — compare baseline
    """
    is_call = (direction == 'CALL')
    stride  = STRIKES[instrument]
    K       = round(S_entry / stride) * stride
    T_entry = max((DTE - entry_idx / 75) / 252, 1 / 252 / 75)
    opt_e   = bs_price(S_entry, K, T_entry, R, iv, call=is_call)
    stop_px = opt_e * (1 - STOP_PCT)
    tgt_px  = opt_e * (1 + HARD_TARGET)

    peak_pct     = 0.0
    trail_active = False

    for j in range(entry_idx + 1, len(day_df)):
        row     = day_df.iloc[j]
        t       = row['datetime'].time()
        T_j     = max((DTE - j / 75) / 252, 1 / 252 / 75)
        opt_j   = bs_price(row['Close'], K, T_j, R, iv, call=is_call)
        pnl_pct = (opt_j - opt_e) / opt_e

        # ── Stop ──────────────────────────────────────────────────────────────
        if opt_j <= stop_px:
            return (stop_px - opt_e) * lot - BROKERAGE, 'STOP'

        # ── Hard target (always fires at 80%) ─────────────────────────────────
        if opt_j >= tgt_px:
            return (tgt_px - opt_e) * lot - BROKERAGE, 'TARGET'

        # ── Trailing stop (use_trail=True only) ───────────────────────────────
        if use_trail:
            if pnl_pct > peak_pct:
                peak_pct = pnl_pct
            if peak_pct >= TRAIL_ACT:
                trail_active = True
            if trail_active and pnl_pct <= peak_pct - TRAIL_DIST:
                return (opt_j - opt_e) * lot - BROKERAGE, 'TRAIL'

        # ── Force-close ───────────────────────────────────────────────────────
        if t >= FORCE_CLOSE_T:
            return (opt_j - opt_e) * lot - BROKERAGE, 'FORCE_CLOSE'

    # Ran out of bars
    last  = day_df.iloc[-1]
    T_l   = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_l = bs_price(last['Close'], K, T_l, R, iv, call=is_call)
    return (opt_l - opt_e) * lot - BROKERAGE, 'EOD'


# ── Day simulation ────────────────────────────────────────────────────────────
def simulate_day(day_df: pd.DataFrame,
                 prev_close: float,
                 instrument: str,
                 use_trail: bool,
                 tight: bool,
                 wed_fri_only: bool) -> dict | None:
    if len(day_df) < OR_BARS + 2:
        return None

    lot = LOT_SIZES[instrument]
    iv  = IVS[instrument]
    dow = day_df.iloc[0]['datetime'].strftime('%a')

    if dow in SKIP_DAYS[instrument]:
        return None
    if wed_fri_only and dow not in ('Wed', 'Fri'):
        return None

    # 3-bar Opening Range (09:15, 09:20, 09:25)
    or_hi    = day_df.iloc[:OR_BARS]['High'].max()
    or_lo    = day_df.iloc[:OR_BARS]['Low'].min()
    or_mid   = (or_hi + or_lo) / 2
    or_width = (or_hi - or_lo) / or_mid

    width_max = WIDTH_MAX_BY_DAY.get(dow)
    if width_max is not None and or_width > width_max:
        return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
                'dow': dow, 'result': 'WIDTH_BLOCKED', 'pnl': 0, 'win': None,
                'direction': '-', 'or_width_pct': round(or_width * 100, 3)}

    ref_bar   = day_df.iloc[OR_BARS]
    curr_open = day_df.iloc[0]['Open']
    gap_type  = classify_gap(prev_close, curr_open, ref_bar['Close'])
    adx_floor = ADX_BY_DAY.get(dow, 20)
    entry_end = ENTRY_TIGHT_T if tight else ENTRY_WIDE_T

    for i in range(OR_BARS, len(day_df)):
        row = day_df.iloc[i]
        t   = row['datetime'].time()

        if t < ENTRY_OPEN_T:
            continue
        if t >= entry_end:
            break

        if row['ADX'] < adx_floor:
            continue

        px   = row['Close']
        vwap = row['VWAP']
        no_c = dow in NO_CALL[instrument]

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
                                  iv, lot, instrument, use_trail)
        return {
            'date'        : day_df.iloc[0]['date'],
            'instrument'  : instrument,
            'dow'         : dow,
            'result'      : why,
            'pnl'         : round(pnl),
            'win'         : pnl > 0,
            'direction'   : direction,
            'or_width_pct': round(or_width * 100, 3),
            'adx'         : round(float(row['ADX']), 1),
            'gap_type'    : gap_type,
            'entry_time'  : str(t),
        }

    return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
            'dow': dow, 'result': 'NO_BREAK', 'pnl': 0, 'win': None,
            'direction': '-', 'or_width_pct': round(or_width * 100, 3)}


def run_instrument(instrument: str, df: pd.DataFrame,
                   use_trail: bool, tight: bool,
                   wed_fri_only: bool) -> list[dict]:
    dates   = sorted(df['date'].unique())
    results = []
    for i, d in enumerate(dates):
        day_df = df[df['date'] == d].reset_index(drop=True)
        prev_close = (df[df['date'] == dates[i - 1]]['Close'].iloc[-1]
                      if i > 0 else day_df.iloc[0]['Open'])
        r = simulate_day(day_df, prev_close, instrument,
                         use_trail, tight, wed_fri_only)
        if r:
            results.append(r)
    return results


def agg(rows: list[dict]) -> dict:
    trades   = [r for r in rows if r.get('win') is not None and r['pnl'] != 0]
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


def print_dow_table(all_rows: list[dict],
                    target_instruments: list[str]) -> None:
    """Print a combined DOW × instrument breakdown."""
    print(f'\n  {"DOW":<5}', end='')
    for inst in target_instruments:
        print(f'  {inst:^36}', end='')
    print()
    print(f'  {"":5}', end='')
    for _ in target_instruments:
        print(f'  {"Trades":>6} {"WR%":>5} {"P&L":>10} {"Avg":>7}', end='')
    print()
    print('  ' + '─' * (5 + len(target_instruments) * 38))

    for dow in DOW_ORDER:
        print(f'  {dow:<5}', end='')
        for inst in target_instruments:
            rows = [r for r in all_rows
                    if r['dow'] == dow and r['instrument'] == inst]
            s    = agg(rows)
            if s['n'] > 0:
                print(f'  {s["n"]:>6} {s["wr"]:>4.0f}% '
                      f'Rs{s["pnl"]:>8,.0f} Rs{s["avg"]:>5,.0f}', end='')
            else:
                print(f'  {"–":>6} {"–":>5} {"–":>10} {"–":>7}', end='')
        print()


def main() -> None:
    target_instruments = ([a for a in _args if a in DATA_ROOTS]
                          or list(DATA_ROOTS.keys()))
    use_trail  = not NO_TRAIL
    tight      = TIGHT_WINDOW
    wed_fri    = WED_FRI_ONLY

    entry_desc = '09:30–10:00 (tight)' if tight else '09:30–10:55 (standard)'
    exit_desc  = f'Hard target {HARD_TARGET*100:.0f}%' if not use_trail else \
                 f'Trail from {TRAIL_ACT*100:.0f}% gain, {TRAIL_DIST*100:.0f}% dist'
    days_desc  = 'Wed + Fri only' if wed_fri else 'All days'

    print()
    print('=' * 90)
    print('  Early-Session ORB Backtest  |  3-bar OR  |  Old early_bot model')
    print('=' * 90)
    print(f'  OR window  : 09:15–09:25  (3 bars)')
    print(f'  Entry      : {entry_desc}')
    print(f'  Exit model : {exit_desc}')
    print(f'  Force-close: 10:55')
    print(f'  Stop       : {STOP_PCT*100:.0f}%')
    print(f'  Days       : {days_desc}')
    print()

    # Load data
    dfs: dict[str, pd.DataFrame] = {}
    for inst in target_instruments:
        print(f'  Loading {inst} ...', end=' ', flush=True)
        df = load_data(inst)
        if df is None:
            print('NO DATA'); continue
        df = add_indicators(df)
        dfs[inst] = df
        print(f'{len(df["date"].unique())} days')

    if not dfs:
        print('  No data.'); return

    # Run simulations
    all_rows: list[dict] = []
    for inst, df in dfs.items():
        print(f'  Simulating {inst} ...')
        all_rows.extend(run_instrument(inst, df, use_trail, tight, wed_fri))

    inst_list = [i for i in target_instruments if i in dfs]

    # ── Per-instrument overall ────────────────────────────────────────────────
    print()
    print('  ' + '─' * 86)
    print(f'  {"Instrument":<12} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
          f'{"Avg/trade":>10} {"Best":>9} {"Worst":>9}')
    print('  ' + '─' * 86)
    for inst in inst_list:
        rows = [r for r in all_rows if r['instrument'] == inst]
        s    = agg(rows)
        print(f'  {inst:<12} {s["n"]:>7} {s["wr"]:>5.1f}%  '
              f'Rs{s["pnl"]:>9,.0f} Rs{s["avg"]:>9,.0f} '
              f'Rs{s["best"]:>8,.0f} Rs{s["worst"]:>8,.0f}')

    if len(inst_list) > 1:
        comb = agg(all_rows)
        print('  ' + '─' * 86)
        print(f'  {"COMBINED":<12} {comb["n"]:>7} {comb["wr"]:>5.1f}%  '
              f'Rs{comb["pnl"]:>9,.0f} Rs{comb["avg"]:>9,.0f} '
              f'Rs{comb["best"]:>8,.0f} Rs{comb["worst"]:>8,.0f}')

    # ── DOW × Instrument breakdown ────────────────────────────────────────────
    print()
    print('  ' + '═' * 86)
    print(f'  DOW breakdown by instrument')
    print('  ' + '═' * 86)
    print_dow_table(all_rows, inst_list)

    # ── DOW combined ──────────────────────────────────────────────────────────
    print()
    print('  ' + '─' * 86)
    print(f'  {"DOW":<6} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} {"Avg/trade":>10}  {"Status"}')
    print('  ' + '─' * 86)
    for dow in DOW_ORDER:
        rows = [r for r in all_rows if r['dow'] == dow]
        s    = agg(rows)
        if s['n'] == 0:
            print(f'  {dow:<6} {"–":>7} {"–":>6} {"–":>11} {"–":>10}')
            continue
        status = 'PROFITABLE ✓' if s['pnl'] > 0 else 'negative  ✗'
        print(f'  {dow:<6} {s["n"]:>7} {s["wr"]:>5.1f}%  '
              f'Rs{s["pnl"]:>9,.0f} Rs{s["avg"]:>9,.0f}  {status}')

    # ── Exit reason breakdown ─────────────────────────────────────────────────
    print()
    print('  ' + '─' * 86)
    print('  Exit reasons (all instruments, trades only)')
    print('  ' + '─' * 86)
    trade_rows = [r for r in all_rows if r.get('win') is not None and r['pnl'] != 0]
    reasons: dict[str, tuple[int, int]] = {}
    for r in trade_rows:
        rsn = r.get('result', '?')
        c, p = reasons.get(rsn, (0, 0))
        reasons[rsn] = (c + 1, p + r['pnl'])
    for rsn, (cnt, pnl) in sorted(reasons.items(), key=lambda x: -x[1][0]):
        wins = sum(1 for r in trade_rows
                   if r.get('result') == rsn and r['pnl'] > 0)
        wr   = wins / cnt * 100 if cnt else 0
        print(f'  {rsn:<18} {cnt:>4} trades  WR {wr:>4.0f}%  Rs{pnl:>+10,.0f}')

    # ── Wed vs Fri breakdown (key insight) ───────────────────────────────────
    print()
    print('  ' + '═' * 86)
    print('  WED vs FRI focus (the old Wed+Fri early ORB)')
    print('  ' + '═' * 86)
    for dow in ('Wed', 'Fri'):
        print(f'\n  {dow}:')
        for inst in inst_list:
            rows = [r for r in all_rows
                    if r['dow'] == dow and r['instrument'] == inst]
            s    = agg(rows)
            if s['n'] == 0:
                print(f'    {inst:<12} — no trades')
                continue
            call_rows = [r for r in rows if r.get('direction') == 'CALL'
                         and r.get('win') is not None and r['pnl'] != 0]
            put_rows  = [r for r in rows if r.get('direction') == 'PUT'
                         and r.get('win') is not None and r['pnl'] != 0]
            call_s, put_s = agg(call_rows), agg(put_rows)
            print(f'    {inst:<12} {s["n"]:>3} trades  '
                  f'WR {s["wr"]:>4.0f}%  Rs{s["pnl"]:>+8,.0f}  '
                  f'| CALL {call_s["n"]}t {call_s["wr"]:.0f}% Rs{call_s["pnl"]:+,.0f}'
                  f'  PUT {put_s["n"]}t {put_s["wr"]:.0f}% Rs{put_s["pnl"]:+,.0f}')

    # ── Tight window comparison hint ─────────────────────────────────────────
    if not tight:
        print()
        print('  Tip: run with --tight to see 09:30-10:00-only entry results')
        print('       run with --no-trail to compare hard-target vs trail model')
        print('       run with --wed-fri to isolate Wed+Fri performance')
    print()


if __name__ == '__main__':
    main()
