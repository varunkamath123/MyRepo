"""
second_half_sim.py — Simulate hypothetical second-half entries (11:00–14:00)
on historical 5-min index data to find optimal conviction parameters.

Motivation:
  PATH-A (ORB) fires 09:30–11:00.  After that, only ONE mechanism exists for
  the second half: the re-entry after a stop-loss (ADX>=35, before 13:00).
  But on many days the ORB either never fired OR was stopped early, and the
  underlying trend continues through the afternoon.

  Both the re-entry case and the "fresh entry in the second half" case require
  elevated conviction — they are structurally the same gate.  This sim tests
  what ADX / EMA / time-window combination produces the best risk-adjusted
  outcome for any hypothetical second-half entry.

London opening correlation (13:30 IST):
  London opens 09:00 BST = 13:30 IST.  The London session often adds
  directional volume to the Indian afternoon.  The sim explicitly separates the
  13:30–14:00 sub-window to test whether London-open entries outperform the
  earlier 11:00–13:30 window.

Signal definition (NOT ORB-dependent — fresh signal):
  CALL  :  EMA9 > EMA21  AND  Close > VWAP  AND  ADX >= threshold
  PUT   :  EMA9 < EMA21  AND  Close < VWAP  AND  ADX >= threshold
  Thursday CALL suppression applied.

Exit rules (mirror PATH-A live config):
  Stop       : -50% from entry option price
  Target     : +150%
  Trail act  : +12% gain  |  Trail dist : 15%
  EOD close  : 14:30

Usage:
  python second_half_sim.py                  # NIFTY + BANKNIFTY
  python second_half_sim.py NIFTY            # single instrument
  python second_half_sim.py --from 2025-10-01
"""
from __future__ import annotations

import os
import sys
import math
from datetime import date, datetime, time as dtime
from typing import Optional

import pandas as pd
import numpy as np

# ── Instrument config ────────────────────────────────────────────────────────
DATA_ROOTS = {
    'NIFTY'    : r'C:\quant_trading\data\nifty_5min',
    'BANKNIFTY': r'C:\quant_trading\data\banknifty_5min',
}
FILE_PREFIXES = {
    'NIFTY'    : 'nifty_5min_',
    'BANKNIFTY': 'banknifty_5min_',
}
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30}
BASE_IV   = {'NIFTY': 0.14, 'BANKNIFTY': 0.17}   # approx HV used for BS
DAYS_TO_EXP = 2   # 2-DTE options

# ── Exit parameters (match live config as of Apr 2026) ───────────────────────
STOP       = 0.50   # 50% stop
TARGET     = 1.50   # 150% target
TRAIL_ACT  = 0.12   # trail activates at +12%
TRAIL_DIST = 0.15   # 15% from peak
EOD_CLOSE  = dtime(14, 30)
NO_CALL_DAYS = {'Thu'}

# ── Second-half windows ───────────────────────────────────────────────────────
SECOND_HALF_START = dtime(11, 0)
LONDON_OPEN_IST   = dtime(13, 30)   # London 09:00 BST
ENTRY_CUTOFF      = dtime(14, 0)    # no entries in last 30 min (need runway)

# ── Parameter grid ────────────────────────────────────────────────────────────
ADX_THRESHOLDS   = [25, 30, 35, 40]
TIME_WINDOWS     = [
    ('Full 11-14',   SECOND_HALF_START, ENTRY_CUTOFF),
    ('Pre-London',   SECOND_HALF_START, LONDON_OPEN_IST),
    ('London+ 13:30',LONDON_OPEN_IST,   ENTRY_CUTOFF),
    ('11:00-12:00',  dtime(11, 0),      dtime(12, 0)),
    ('12:00-13:00',  dtime(12, 0),      dtime(13, 0)),
    ('13:00-14:00',  dtime(13, 0),      ENTRY_CUTOFF),
]
EMA_REQUIRED = [True, False]   # test with and without EMA alignment gate


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def bs_opt(S: float, K: float, opt_type: str, T: float, iv: float,
           r: float = 0.065) -> float:
    """Black-Scholes option price.  T in years."""
    if T <= 0:
        return max(S - K, 0.0) if opt_type == 'CALL' else max(K - S, 0.0)
    d1 = (math.log(max(S / K, 1e-9)) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if opt_type == 'CALL':
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    else:
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def est_opt(S: float, K: float, opt_type: str, bars_from_entry: int,
            iv: float) -> float:
    """Option price at bars_from_entry × 5min after entry."""
    T = max((DAYS_TO_EXP * 390 - bars_from_entry * 5) / (252 * 390), 1e-6)
    px = bs_opt(S, K, opt_type, T, iv)
    return max(px, 0.01)


# ── Indicator helpers ────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ADX, EMA9, EMA21, VWAP to the dataframe."""
    # ADX (14-period)
    period = 14
    hi, lo, cl = df['High'], df['Low'], df['Close']
    tr   = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()],
                     axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    dmp  = (hi - hi.shift()).clip(lower=0)
    dmm  = (lo.shift() - lo).clip(lower=0)
    dmp[dmp <= dmm] = 0
    dmm[dmm <= dmp] = 0
    dip  = 100 * dmp.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr
    dim  = 100 * dmm.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr
    dx   = 100 * (dip - dim).abs() / (dip + dim).clip(lower=1e-9)
    df['ADX']      = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    df['DI_plus']  = dip
    df['DI_minus'] = dim

    # EMA 9 / EMA 21
    df['EMA9']  = cl.ewm(span=9,  adjust=False).mean()
    df['EMA21'] = cl.ewm(span=21, adjust=False).mean()

    # VWAP (intraday reset)
    df['_date'] = df.index.date
    df['_tp']   = (hi + lo + cl) / 3
    df['_tpv']  = df['_tp'] * df['Volume'].clip(lower=1)
    df['VWAP']  = (df.groupby('_date')['_tpv'].cumsum()
                   / df.groupby('_date')['Volume'].transform('cumsum').clip(lower=1))
    df.drop(columns=['_date', '_tp', '_tpv'], inplace=True)
    return df


# ── Data loading ─────────────────────────────────────────────────────────────
def load_data(instrument: str,
              date_from: Optional[date] = None,
              date_to:   Optional[date] = None) -> pd.DataFrame:
    root   = DATA_ROOTS[instrument]
    prefix = FILE_PREFIXES[instrument]
    files  = sorted(f for f in os.listdir(root)
                    if f.startswith(prefix) and f.endswith('.csv'))
    frames = []
    for fn in files:
        ds = fn.replace(prefix, '').replace('.csv', '')
        try:
            d = datetime.strptime(ds, '%Y%m%d').date()
        except ValueError:
            continue
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        fp = os.path.join(root, fn)
        try:
            frames.append(pd.read_csv(fp))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df['Datetime'] = pd.to_datetime(df['ts'], utc=False).dt.tz_localize(None)
    df = df.drop(columns=['ts']).sort_values('Datetime').set_index('Datetime')
    df = df[df.index.notna()]   # drop any NaT rows that cause sort comparison errors
    if 'Volume' not in df.columns:
        df['Volume'] = 1_000_000
    df = add_indicators(df)
    return df


# ── Day-level simulation ──────────────────────────────────────────────────────
def simulate_day_window(day_df: pd.DataFrame, instrument: str, day_date: date,
                        adx_min: float, win_start: dtime, win_end: dtime,
                        ema_required: bool) -> Optional[dict]:
    """
    Scan [win_start, win_end) for a CALL or PUT signal.
    Return first-signal trade result, or None if no signal fires.
    """
    iv  = BASE_IV[instrument]
    lot = LOT_SIZES[instrument]
    dow = day_date.strftime('%a')
    bars = list(day_df.itertuples())

    # Pre-sort: find bars inside the window
    window_bars = [(i, b) for i, b in enumerate(bars)
                   if win_start <= b.Index.time() < win_end]
    if not window_bars:
        return None

    # Find first qualifying signal in window
    entry = None
    entry_bar_idx = None
    for i, bar in window_bars:
        adx  = getattr(bar, 'ADX',    float('nan'))
        ema9 = getattr(bar, 'EMA9',   float('nan'))
        e21  = getattr(bar, 'EMA21',  float('nan'))
        vwap = getattr(bar, 'VWAP',   float('nan'))
        px   = bar.Close

        if pd.isna(adx) or adx < adx_min:
            continue
        if pd.isna(ema9) or pd.isna(e21) or pd.isna(vwap):
            continue

        # VWAP alignment (always required)
        vwap_call = px > vwap
        vwap_put  = px < vwap

        if ema_required:
            # Both VWAP and EMA 9/21 must agree on direction
            call_aligned = vwap_call and ema9 > e21
            put_aligned  = vwap_put  and ema9 < e21
        else:
            # VWAP only — EMA direction not required (faster, noisier signal)
            call_aligned = vwap_call
            put_aligned  = vwap_put

        if not call_aligned and not put_aligned:
            continue
        # If both somehow align (crossover day), prefer the one matching DI
        if call_aligned and put_aligned:
            di_p = getattr(bar, 'DI_plus', 0)
            di_m = getattr(bar, 'DI_minus', 0)
            call_aligned = di_p >= di_m
            put_aligned  = di_m > di_p
        sig = 'CALL' if call_aligned else 'PUT'

        if sig == 'CALL' and dow in NO_CALL_DAYS:
            continue

        # Entry found
        K = round(px / 100) * 100
        entry_opt = est_opt(px, K, sig, 0, iv)
        entry_opt = max(entry_opt, 5.0)   # floor for deep OTM

        entry = {
            'bar_idx'   : i,
            'time'      : bar.Index.time(),
            'sig'       : sig,
            'px_idx'    : px,
            'strike'    : K,
            'entry_opt' : entry_opt,
            'adx'       : adx,
            'ema9'      : ema9,
            'ema21'     : e21,
        }
        entry_bar_idx = i
        break

    if entry is None:
        return None

    # ── Simulate exit ────────────────────────────────────────────────────────
    peak_opt    = entry['entry_opt']
    trailing    = False
    trail_floor = None
    exit_reason = None
    exit_opt    = None
    exit_t      = None

    for j in range(entry_bar_idx + 1, len(bars)):
        bar = bars[j]
        t   = bar.Index.time()

        if t >= EOD_CLOSE:
            opt = est_opt(bar.Close, entry['strike'], entry['sig'],
                          j - entry_bar_idx, iv)
            exit_opt = opt; exit_t = t; exit_reason = 'EOD'
            break

        bars_el = j - entry_bar_idx
        opt = est_opt(bar.Close, entry['strike'], entry['sig'], bars_el, iv)

        if opt > peak_opt:
            peak_opt = opt

        gain_pct = (opt - entry['entry_opt']) / entry['entry_opt']

        if not trailing and gain_pct >= TRAIL_ACT:
            trailing = True
        if trailing:
            trail_floor = peak_opt * (1 - TRAIL_DIST)
            if opt <= trail_floor:
                exit_opt = opt; exit_t = t; exit_reason = 'Trail'
                break

        if opt <= entry['entry_opt'] * (1 - STOP):
            exit_opt = opt; exit_t = t; exit_reason = 'Stop'
            break
        if opt >= entry['entry_opt'] * (1 + TARGET):
            exit_opt = opt; exit_t = t; exit_reason = 'Target'
            break

    if exit_opt is None:
        bars_el = len(bars) - 1 - entry_bar_idx
        exit_opt = est_opt(bars[-1].Close, entry['strike'], entry['sig'],
                           bars_el, iv)
        exit_t = bars[-1].Index.time()
        exit_reason = 'EOD'

    pnl_pct = (exit_opt - entry['entry_opt']) / entry['entry_opt']
    pnl_rs  = pnl_pct * entry['entry_opt'] * lot

    return {
        'date'       : str(day_date),
        'dow'        : day_date.strftime('%a'),
        'instrument' : instrument,
        'sig'        : entry['sig'],
        'entry_t'    : str(entry['time']),
        'entry_opt'  : round(entry['entry_opt'], 2),
        'exit_opt'   : round(exit_opt, 2),
        'exit_t'     : str(exit_t),
        'exit_reason': exit_reason,
        'adx'        : round(entry['adx'], 1),
        'pnl_pct'    : round(pnl_pct * 100, 1),
        'pnl_rs'     : round(pnl_rs, 0),
        'win'        : pnl_rs > 0,
        # London flag: was entry in the London session?
        'london_session': entry['time'] >= LONDON_OPEN_IST,
    }


# ── Parameter grid runner ─────────────────────────────────────────────────────
def run_grid(df: pd.DataFrame, instrument: str) -> list[dict]:
    """
    Run the full parameter grid across the instrument's history.
    Returns a list of (params, trades[]) row dicts for analysis.
    """
    results = []
    days = sorted(set(df.index.date))

    for adx_min in ADX_THRESHOLDS:
        for win_label, win_start, win_end in TIME_WINDOWS:
            for ema_req in EMA_REQUIRED:
                trades = []
                for d in days:
                    day_df = df[df.index.date == d].copy()
                    if len(day_df) < 10:
                        continue
                    t = simulate_day_window(day_df, instrument, d,
                                           adx_min, win_start, win_end, ema_req)
                    if t:
                        trades.append(t)

                n = len(trades)
                if n == 0:
                    results.append({
                        'instrument': instrument,
                        'adx_min'   : adx_min,
                        'window'    : win_label,
                        'ema_gate'  : ema_req,
                        'n'         : 0,
                        'wr'        : 0.0,
                        'pnl_rs'    : 0.0,
                        'best'      : 0.0,
                        'worst'     : 0.0,
                        'exits'     : {},
                    })
                    continue

                wins   = sum(1 for t in trades if t['win'])
                pnl    = sum(t['pnl_rs'] for t in trades)
                exits  = {}
                for t in trades:
                    exits[t['exit_reason']] = exits.get(t['exit_reason'], 0) + 1
                best   = max(t['pnl_rs'] for t in trades)
                worst  = min(t['pnl_rs'] for t in trades)

                # London split (only meaningful for full/pre-London windows)
                london = [t for t in trades if t['london_session']]
                pre_lo = [t for t in trades if not t['london_session']]
                lo_wr  = (sum(1 for t in london if t['win']) / len(london) * 100
                          if london else None)
                pr_wr  = (sum(1 for t in pre_lo if t['win']) / len(pre_lo) * 100
                          if pre_lo else None)

                results.append({
                    'instrument' : instrument,
                    'adx_min'    : adx_min,
                    'window'     : win_label,
                    'ema_gate'   : ema_req,
                    'n'          : n,
                    'wr'         : round(wins / n * 100, 1),
                    'pnl_rs'     : round(pnl, 0),
                    'avg_rs'     : round(pnl / n, 0),
                    'best'       : round(best, 0),
                    'worst'      : round(worst, 0),
                    'exits'      : exits,
                    'london_n'   : len(london),
                    'london_wr'  : round(lo_wr, 1) if lo_wr is not None else '-',
                    'prelondon_n': len(pre_lo),
                    'prelondon_wr': round(pr_wr, 1) if pr_wr is not None else '-',
                })
    return results


# ── Print analysis ────────────────────────────────────────────────────────────
def print_results(results: list[dict], instrument: str) -> None:
    inst_r = [r for r in results if r['instrument'] == instrument]
    if not inst_r:
        print(f"\n{instrument}: No results.")
        return

    print(f"\n{'=' * 72}")
    print(f"  {instrument}  Second-Half Entry Simulation (11:00–14:00)")
    print(f"{'=' * 72}")
    print(f"  {'ADX':>4}  {'Window':<18}  {'EMA':>3}  {'N':>4}  {'WR%':>5}  "
          f"{'Net Rs':>9}  {'Avg/tr':>7}  {'Best':>7}  {'Worst':>7}")
    print(f"  {'-'*68}")

    # Sort by P&L descending for easy reading
    inst_r_sorted = sorted(inst_r, key=lambda x: -x['pnl_rs'])
    for r in inst_r_sorted:
        if r['n'] == 0:
            continue
        ema_tag = 'Y' if r['ema_gate'] else 'N'
        print(f"  {r['adx_min']:>4}  {r['window']:<18}  {ema_tag:>3}  "
              f"{r['n']:>4}  {r['wr']:>5.1f}  "
              f"{r['pnl_rs']:>9,.0f}  {r['avg_rs']:>7,.0f}  "
              f"{r['best']:>7,.0f}  {r['worst']:>7,.0f}")

    # ── London correlation summary ────────────────────────────────────────────
    print(f"\n  -- London Opening Correlation (13:30 IST = London 09:00 BST) --")
    full_windows = [r for r in inst_r if r['window'] == 'Full 11-14' and r['n'] > 0]
    if full_windows:
        for r in sorted(full_windows, key=lambda x: x['adx_min']):
            lo_n  = r.get('london_n', 0)
            pre_n = r.get('prelondon_n', 0)
            lo_wr = r.get('london_wr', '-')
            pr_wr = r.get('prelondon_wr', '-')
            ema_tag = 'EMA+' if r['ema_gate'] else 'EMA-'
            print(f"  ADX>={r['adx_min']} {ema_tag}: "
                  f"Pre-London ({pre_n} tr, WR {pr_wr}%)  "
                  f"London+ ({lo_n} tr, WR {lo_wr}%)")

    # ── Best parameter set (top 3 by P&L, min 5 trades) ─────────────────────
    top = [r for r in inst_r_sorted if r['n'] >= 5][:3]
    if top:
        print(f"\n  -- Top 3 parameter sets (min 5 trades, ranked by Net P&L) --")
        for i, r in enumerate(top, 1):
            ema_tag = 'EMA-required' if r['ema_gate'] else 'no EMA gate'
            exits   = ', '.join(f"{k}:{v}" for k, v in
                                sorted(r['exits'].items(), key=lambda x: -x[1]))
            print(f"  #{i}  ADX>={r['adx_min']}  {r['window']}  {ema_tag}")
            print(f"       {r['n']} trades | {r['wr']:.1f}% WR | "
                  f"Net Rs{r['pnl_rs']:,.0f} | Avg Rs{r['avg_rs']:,.0f}/trade")
            print(f"       Exits: {exits}")

    # ── ORB comparison benchmark ──────────────────────────────────────────────
    print(f"\n  NOTE: ORB (PATH-A) benchmark for {instrument}:")
    if instrument == 'NIFTY':
        print(f"        22 trades | 77.3% WR | Net +Rs40,104 (backtest vX)")
    elif instrument == 'BANKNIFTY':
        print(f"        24 trades | 50.0% WR | Net +Rs18,959 (backtest vX)")


# ── Day-by-day detail for top combo ──────────────────────────────────────────
def top_combo_detail(df: pd.DataFrame, instrument: str,
                     adx_min: float, win_label: str, win_start: dtime,
                     win_end: dtime, ema_req: bool) -> None:
    """Print per-day trades for the specified parameter combo."""
    days = sorted(set(df.index.date))
    trades = []
    for d in days:
        day_df = df[df.index.date == d].copy()
        if len(day_df) < 10:
            continue
        t = simulate_day_window(day_df, instrument, d,
                                adx_min, win_start, win_end, ema_req)
        if t:
            trades.append(t)

    if not trades:
        print("  No trades for this combo.")
        return

    print(f"\n  {'Date':<12} {'DOW':>3}  {'Sig':>4}  {'EntT':>5}  "
          f"{'ADX':>5}  {'EntOpt':>6}  {'ExOpt':>6}  "
          f"{'PnL%':>6}  {'PnL Rs':>7}  Exit")
    print(f"  {'-' * 70}")
    cum = 0.0
    for t in trades:
        cum += t['pnl_rs']
        icon = '+' if t['win'] else '-'
        print(f"  {t['date']:<12} {t['dow']:>3}  {t['sig']:>4}  "
              f"{t['entry_t'][:5]:>5}  "
              f"{t['adx']:>5.1f}  {t['entry_opt']:>6.1f}  "
              f"{t['exit_opt']:>6.1f}  "
              f"{icon}{abs(t['pnl_pct']):>5.1f}%  "
              f"{t['pnl_rs']:>7,.0f}  {t['exit_reason']}")
    print(f"  {'-' * 70}")
    wins = sum(1 for t in trades if t['win'])
    print(f"  {len(trades)} trades | {wins/len(trades)*100:.1f}% WR | "
          f"Cumulative Rs{cum:,.0f}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # CLI args
    args = sys.argv[1:]
    date_from = None
    if '--from' in args:
        idx = args.index('--from')
        if idx + 1 < len(args):
            date_from = datetime.strptime(args[idx + 1], '%Y-%m-%d').date()
            args = [a for i, a in enumerate(args) if i not in (idx, idx + 1)]

    show_detail = '--detail' in args
    args = [a for a in args if not a.startswith('--')]

    instruments = args if args else list(DATA_ROOTS.keys())
    instruments = [i for i in instruments if i in DATA_ROOTS]
    if not instruments:
        print("No valid instruments.  Use: NIFTY, BANKNIFTY, or both (default).")
        return

    all_results = []
    for inst in instruments:
        print(f"\nLoading {inst} data...", end='', flush=True)
        df = load_data(inst, date_from=date_from)
        if df.empty:
            print(f" No data found.")
            continue
        days = len(set(df.index.date))
        print(f" {days} trading days loaded.")

        print(f"Running parameter grid ({len(ADX_THRESHOLDS)} ADX × "
              f"{len(TIME_WINDOWS)} windows × {len(EMA_REQUIRED)} EMA combos)...",
              end='', flush=True)
        res = run_grid(df, inst)
        all_results.extend(res)
        print(" done.")

        print_results(all_results, inst)

        # ── Detail pass for top-3 P&L combos (min 5 trades) ─────────────────
        if show_detail:
            inst_sorted = sorted(
                [r for r in res if r['instrument'] == inst and r['n'] >= 5],
                key=lambda x: -x['pnl_rs']
            )
            for r in inst_sorted[:1]:   # top-1 only (avoids wall of text)
                ema_lbl = 'EMA-required' if r['ema_gate'] else 'no EMA gate'
                print(f"\n  DETAIL — {inst} ADX>={r['adx_min']} | "
                      f"{r['window']} | {ema_lbl}")
                # Re-resolve window times
                win_start = next(s for l, s, e in TIME_WINDOWS
                                 if l == r['window'])
                win_end   = next(e for l, s, e in TIME_WINDOWS
                                 if l == r['window'])
                top_combo_detail(df, inst, r['adx_min'],
                                 r['window'], win_start, win_end, r['ema_gate'])

    # ── Cross-instrument summary ──────────────────────────────────────────────
    if len(instruments) > 1 and all_results:
        print(f"\n{'=' * 72}")
        print(f"  CROSS-INSTRUMENT SUMMARY")
        print(f"{'=' * 72}")
        # For each parameter combo, sum P&L across instruments
        combos: dict = {}
        for r in all_results:
            key = (r['adx_min'], r['window'], r['ema_gate'])
            if key not in combos:
                combos[key] = {'n': 0, 'pnl': 0.0, 'wins': 0}
            combos[key]['n']    += r['n']
            combos[key]['pnl']  += r['pnl_rs']
            combos[key]['wins'] += round(r['wr'] * r['n'] / 100)

        sorted_combos = sorted(combos.items(), key=lambda x: -x[1]['pnl'])
        print(f"  {'ADX':>4}  {'Window':<18}  {'EMA':>3}  "
              f"{'N':>5}  {'WR%':>5}  {'Net Rs':>10}")
        print(f"  {'-' * 60}")
        for (adx, win, ema), v in sorted_combos[:15]:  # top 15
            if v['n'] == 0:
                continue
            ema_tag = 'Y' if ema else 'N'
            wr = v['wins'] / v['n'] * 100 if v['n'] else 0.0
            print(f"  {adx:>4}  {win:<18}  {ema_tag:>3}  "
                  f"{v['n']:>5}  {wr:>5.1f}  {v['pnl']:>10,.0f}")

        print(f"\n  London vs Pre-London (EMA-required, full 11-14 window):")
        for adx in ADX_THRESHOLDS:
            key = (adx, 'Full 11-14', True)
            r_list = [r for r in all_results
                      if r['adx_min'] == adx and r['window'] == 'Full 11-14'
                      and r['ema_gate']]
            if not r_list:
                continue
            lo_n   = sum(r.get('london_n', 0) for r in r_list)
            pre_n  = sum(r.get('prelondon_n', 0) for r in r_list)
            lo_tr  = sum(r.get('london_n', 0) * r.get('london_wr', 0) / 100
                         for r in r_list if isinstance(r.get('london_wr'), (int, float)))
            pre_tr = sum(r.get('prelondon_n', 0) * r.get('prelondon_wr', 0) / 100
                         for r in r_list
                         if isinstance(r.get('prelondon_wr'), (int, float)))
            lo_wr  = lo_tr / lo_n * 100 if lo_n else 0
            pre_wr = pre_tr / pre_n * 100 if pre_n else 0
            print(f"    ADX>={adx}: Pre-London {pre_n} trades {pre_wr:.1f}% WR | "
                  f"London+ {lo_n} trades {lo_wr:.1f}% WR")


if __name__ == '__main__':
    main()
