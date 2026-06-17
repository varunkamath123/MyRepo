# -*- coding: utf-8 -*-
"""
pullback_backtest.py — ORB Pullback / Retest Entry Analysis
============================================================
Tests whether waiting for a retest of the OR boundary after the initial
breakout gives a better entry price and improved P&L vs entering immediately.

Three strategies compared on every ORB signal:
  IMMEDIATE   — enter at breakout bar close (current live behaviour)
  PB_STRICT   — wait up to WAIT_BARS for price to retest OR boundary
                 (within RETEST_PCT of OR level). Enter at retest bar.
                 If no retest within WAIT_BARS → SKIP the trade.
  PB_FALLBACK — same wait; if no retest within WAIT_BARS → enter at
                 breakout close anyway (same as IMMEDIATE for those trades).

Sweeps:
  RETEST_PCT  : 0.1%, 0.2%, 0.3%  (how close to OR boundary counts as retest)
  WAIT_BARS   : 2, 3, 4 bars       (10, 15, 20 min)

Usage:
    python pullback_backtest.py              # all instruments
    python pullback_backtest.py NIFTY        # single instrument
    python pullback_backtest.py NIFTY 0.002 3  # custom retest_pct, wait_bars
"""
from __future__ import annotations

import glob
import io
import math
import os
import sys
from datetime import time as dtime
from itertools import product

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── CLI ───────────────────────────────────────────────────────────────────────
_args       = [a for a in sys.argv[1:] if not a.startswith('--')]
_inst_arg   = _args[0] if _args and _args[0] in ('NIFTY', 'BANKNIFTY', 'SENSEX') else None
_rp_arg     = float(_args[1]) if len(_args) > 1 else None
_wb_arg     = int(_args[2])   if len(_args) > 2 else None
SWEEP_MODE  = (_rp_arg is None)   # full sweep when no custom params given

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
INSTRUMENTS = ['NIFTY', 'BANKNIFTY', 'SENSEX'] if _inst_arg is None else [_inst_arg]

# ── Trade parameters (matching live config May 2026) ─────────────────────────
BROKERAGE     = 40
OR_BUFFER     = 0.0005          # 0.05% breakout buffer
DTE           = 2
R             = 0.065
ADX_PERIOD    = 14
GAP_THRESHOLD = 0.003

# Per-instrument PATH-A exit params (deployed May 2026)
STOP_PCT_BY_INST   = {'NIFTY': 0.50, 'BANKNIFTY': 0.45, 'SENSEX': 0.50}
TARGET_PCT_BY_INST = {'NIFTY': 0.80, 'BANKNIFTY': 0.90, 'SENSEX': 0.80}
TRAIL_ACT_BY_INST  = {'NIFTY': 0.12, 'BANKNIFTY': 0.10, 'SENSEX': 0.15}
TRAIL_DIST_BY_INST = {'NIFTY': 0.10, 'BANKNIFTY': 0.08, 'SENSEX': 0.12}

# ── Session / OR config (live PATH_A_DAY_CONFIG) ──────────────────────────────
OR_BARS_BY_DOW  = {'Mon': 4, 'Tue': 5, 'Wed': 3, 'Thu': 5, 'Fri': 5}
ENTRY_END_BY_DOW = {'Mon': dtime(12, 0), 'Tue': dtime(12, 0),
                    'Wed': dtime(10, 55), 'Thu': dtime(12, 0), 'Fri': dtime(12, 0)}
CHECKPOINT_T     = dtime(12, 0)
EOD_CLOSE_T      = dtime(14, 30)
ADX_FLOOR_BY_DOW = {'Mon': 30, 'Tue': 28, 'Wed': 25, 'Thu': 25, 'Fri': 20}
WIDTH_MAX_BY_DOW = {'Mon': 0.0025, 'Tue': 0.0030, 'Wed': 0.0025,
                    'Thu': 0.0035, 'Fri': None}
NO_CALL_DAYS    = {'Thu'}
SKIP_DAYS_BY_INST = {
    'NIFTY'    : set(),
    'BANKNIFTY': set(),
    'SENSEX'   : {'Thu'},
}

# ── Sweep params ──────────────────────────────────────────────────────────────
RETEST_PCTS = [_rp_arg] if _rp_arg else [0.001, 0.002, 0.003]
WAIT_BARS_L = [_wb_arg] if _wb_arg else [2, 3, 4]


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0:
        return max(float(S - K if call else K - S), 0.05)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    p  = (S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2) if call
          else K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return max(float(p), 0.05)


# ── Data loading ──────────────────────────────────────────────────────────────
def load_data(instrument):
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


def add_indicators(df):
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
    df['tp']   = (hi + lo + cl) / 3
    df['VWAP'] = df.groupby('date')['tp'].transform(lambda x: x.expanding().mean())
    return df


def classify_gap(prev_close, curr_open, ref_close):
    pct = (curr_open - prev_close) / prev_close
    if abs(pct) < GAP_THRESHOLD:
        return 'INSIDE_OPEN'
    if pct < 0:
        return 'GAP_AND_GO_DN' if ref_close < curr_open else 'GAP_FADE_DN'
    return 'GAP_AND_GO_UP' if ref_close > curr_open else 'GAP_FADE_UP'


# ── Trade simulation ──────────────────────────────────────────────────────────
def simulate_trade(day_df, direction, entry_idx, S_entry, instrument):
    """Simulate a trade from entry_idx. Returns (net_pnl, exit_reason)."""
    is_call   = (direction == 'CALL')
    lot       = LOT_SIZES[instrument]
    iv        = IVS[instrument]
    stride    = STRIKES[instrument]
    stop_pct  = STOP_PCT_BY_INST[instrument]
    target_pct= TARGET_PCT_BY_INST[instrument]
    trail_act = TRAIL_ACT_BY_INST[instrument]
    trail_dist= TRAIL_DIST_BY_INST[instrument]

    K       = round(S_entry / stride) * stride
    T_entry = max((DTE - entry_idx / 75) / 252, 1 / 252 / 75)
    opt_e   = bs_price(S_entry, K, T_entry, R, iv, is_call)
    stop_px = opt_e * (1 - stop_pct)
    tgt_px  = opt_e * (1 + target_pct)
    peak_px = opt_e
    trail_px = None

    post_checkpoint = False

    for j in range(entry_idx + 1, len(day_df)):
        row   = day_df.iloc[j]
        t     = row['datetime'].time()
        T_j   = max((DTE - j / 75) / 252, 1 / 252 / 75)
        opt_j = bs_price(row['Close'], K, T_j, R, iv, is_call)

        # 12:00 conditional checkpoint
        if not post_checkpoint and t >= CHECKPOINT_T:
            post_checkpoint = True
            pnl_pct = (opt_j - opt_e) / opt_e
            if pnl_pct < 0:
                return (opt_j - opt_e) * lot - BROKERAGE, 'CKPT_LOSS'

        if t >= EOD_CLOSE_T:
            return (opt_j - opt_e) * lot - BROKERAGE, 'EOD'

        # Stop / target / trail
        if opt_j <= stop_px:
            return (stop_px - opt_e) * lot - BROKERAGE, 'STOP'
        if opt_j >= tgt_px:
            return (tgt_px - opt_e) * lot - BROKERAGE, 'TARGET'

        # Trailing stop
        if opt_j > peak_px:
            peak_px = opt_j
        gain_pct = (peak_px - opt_e) / opt_e
        if gain_pct >= trail_act:
            trail_px = peak_px * (1 - trail_dist)
            if opt_j <= trail_px:
                return (trail_px - opt_e) * lot - BROKERAGE, 'TRAIL'

    last  = day_df.iloc[-1]
    T_l   = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_l = bs_price(last['Close'], K, T_l, R, iv, is_call)
    return (opt_l - opt_e) * lot - BROKERAGE, 'EOD'


# ── Pullback detection ────────────────────────────────────────────────────────
def find_pullback(day_df, breakout_idx, direction, or_hi, or_lo, retest_pct, wait_bars):
    """
    Scan bars after the breakout for a pullback toward the OR boundary.

    Returns (retest_idx, True) if a retest bar is found within wait_bars.
    Returns (breakout_idx + wait_bars, False) if no retest found (fallback idx).
    """
    is_put = (direction == 'PUT')
    end_idx = min(breakout_idx + wait_bars + 1, len(day_df))

    for k in range(breakout_idx + 1, end_idx):
        row = day_df.iloc[k]
        if is_put:
            # PUT: looking for price to bounce back UP toward OR Low
            # Retest = High came within retest_pct above OR Low
            retest_level = or_lo * (1 + retest_pct)
            if row['High'] >= retest_level:
                return k, True
        else:
            # CALL: looking for price to dip back DOWN toward OR High
            # Retest = Low came within retest_pct below OR High
            retest_level = or_hi * (1 - retest_pct)
            if row['Low'] <= retest_level:
                return k, True

    # No retest found within wait window
    fallback_idx = min(breakout_idx + wait_bars, len(day_df) - 2)
    return fallback_idx, False


# ── Day simulation ────────────────────────────────────────────────────────────
def simulate_day(day_df, prev_close, instrument, retest_pct, wait_bars):
    """
    Run one trading day. Returns a list of trade records with results for
    IMMEDIATE, PB_STRICT, and PB_FALLBACK strategies.
    """
    if len(day_df) < 6:
        return []

    lot  = LOT_SIZES[instrument]
    dow  = day_df.iloc[0]['datetime'].strftime('%a')

    if dow in SKIP_DAYS_BY_INST[instrument]:
        return []

    or_bars   = OR_BARS_BY_DOW[dow]
    entry_end = ENTRY_END_BY_DOW[dow]
    adx_floor = ADX_FLOOR_BY_DOW[dow]
    width_max = WIDTH_MAX_BY_DOW[dow]

    if len(day_df) < or_bars + 2:
        return []

    or_hi  = day_df.iloc[:or_bars]['High'].max()
    or_lo  = day_df.iloc[:or_bars]['Low'].min()
    or_mid = (or_hi + or_lo) / 2
    or_w   = (or_hi - or_lo) / or_mid

    if width_max is not None and or_w > width_max:
        return []

    curr_open = day_df.iloc[0]['Open']
    ref_bar   = day_df.iloc[or_bars]
    gap_type  = classify_gap(prev_close, curr_open, ref_bar['Close'])

    results = []
    fired   = False

    for i in range(or_bars, len(day_df)):
        if fired:
            break
        row = day_df.iloc[i]
        t   = row['datetime'].time()
        if t >= entry_end:
            break
        if row['ADX'] < adx_floor:
            continue

        px   = row['Close']
        vwap = row['VWAP']
        no_c = dow in NO_CALL_DAYS

        call_ok = (not no_c and gap_type != 'GAP_FADE_UP' and
                   px > or_hi * (1 + OR_BUFFER) and px > vwap)
        put_ok  = (gap_type != 'GAP_FADE_DN' and
                   px < or_lo * (1 - OR_BUFFER) and px < vwap)

        if not call_ok and not put_ok:
            continue

        direction = 'CALL' if call_ok else 'PUT'
        fired     = True

        # ── IMMEDIATE entry (baseline) ─────────────────────────────────────
        imm_pnl, imm_why = simulate_trade(day_df, direction, i, px, instrument)
        imm_opt = bs_price(px,
                           round(px / STRIKES[instrument]) * STRIKES[instrument],
                           max((DTE - i / 75) / 252, 1/252/75),
                           R, IVS[instrument], direction == 'CALL')

        # ── Find pullback ──────────────────────────────────────────────────
        pb_idx, had_retest = find_pullback(
            day_df, i, direction, or_hi, or_lo, retest_pct, wait_bars
        )

        pb_row    = day_df.iloc[pb_idx]
        pb_price  = pb_row['Close']
        pb_time   = pb_row['datetime'].time()
        pb_opt    = bs_price(pb_price,
                             round(pb_price / STRIKES[instrument]) * STRIKES[instrument],
                             max((DTE - pb_idx / 75) / 252, 1/252/75),
                             R, IVS[instrument], direction == 'CALL')
        entry_delta_pct = (pb_opt - imm_opt) / imm_opt  # negative = cheaper

        # ── PB_FALLBACK: always enter (at retest if found, else at +wait_bars) ─
        fb_pnl, fb_why = simulate_trade(day_df, direction, pb_idx, pb_price, instrument)

        results.append({
            'date'       : day_df.iloc[0]['date'],
            'dow'        : dow,
            'instrument' : instrument,
            'direction'  : direction,
            'breakout_t' : row['datetime'].time(),
            'retest'     : had_retest,
            'pb_bar_t'   : pb_time,
            'or_width'   : round(or_w * 100, 3),
            # IMMEDIATE
            'imm_pnl'    : round(imm_pnl, 2),
            'imm_win'    : imm_pnl > 0,
            'imm_why'    : imm_why,
            'imm_opt'    : round(imm_opt, 2),
            # PB (strict: None if no retest)
            'pb_pnl'     : round(fb_pnl, 2) if had_retest else None,
            'pb_win'     : (fb_pnl > 0) if had_retest else None,
            'pb_why'     : fb_why if had_retest else 'SKIPPED',
            # FALLBACK
            'fb_pnl'     : round(fb_pnl, 2),
            'fb_win'     : fb_pnl > 0,
            'fb_why'     : fb_why,
            'pb_opt'     : round(pb_opt, 2),
            'entry_delta_pct': round(entry_delta_pct * 100, 2),  # % cheaper/pricier
        })

    return results


# ── Run one instrument ────────────────────────────────────────────────────────
def run_instrument(instrument, retest_pct, wait_bars):
    df = load_data(instrument)
    if df is None or df.empty:
        print(f"  {instrument}: no data found")
        return []

    df = add_indicators(df)
    all_results = []
    prev_close  = None

    for date, day_df in df.groupby('date'):
        day_df = day_df.reset_index(drop=True)
        if prev_close is None:
            prev_close = day_df.iloc[-1]['Close']
            continue
        recs = simulate_day(day_df, prev_close, instrument, retest_pct, wait_bars)
        all_results.extend(recs)
        prev_close = day_df.iloc[-1]['Close']

    return all_results


# ── Summary stats ─────────────────────────────────────────────────────────────
def summarize(records, label):
    if not records:
        return {'label': label, 'n': 0, 'wr': 0, 'pnl': 0, 'avg_delta': 0}
    n        = len(records)
    wins     = sum(1 for r in records if r)
    pnl      = sum(r[0] for r in records)
    return {'label': label, 'n': n, 'wr': wins / n * 100, 'pnl': pnl}


# ── Print comparison table ────────────────────────────────────────────────────
def print_results(all_records, retest_pct, wait_bars, instrument_label):
    print(f"\n{'='*72}")
    print(f"  PULLBACK BACKTEST — {instrument_label}")
    print(f"  Retest zone: {retest_pct*100:.1f}% of OR boundary | "
          f"Wait: {wait_bars} bars ({wait_bars*5} min)")
    print(f"{'='*72}")

    total = len(all_records)
    if total == 0:
        print("  No signals found.")
        return

    retest_count = sum(1 for r in all_records if r['retest'])
    retest_rate  = retest_count / total * 100

    # IMMEDIATE
    imm_wins = sum(1 for r in all_records if r['imm_win'])
    imm_pnl  = sum(r['imm_pnl'] for r in all_records)
    imm_wr   = imm_wins / total * 100

    # PB_STRICT (only trades with retest)
    pb_recs  = [r for r in all_records if r['retest']]
    imm_pb_pnl = sum(r['imm_pnl'] for r in pb_recs)  # immediate P&L on same subset
    pb_wins  = sum(1 for r in pb_recs if r['pb_win'])
    pb_pnl   = sum(r['pb_pnl'] for r in pb_recs)
    pb_wr    = pb_wins / len(pb_recs) * 100 if pb_recs else 0

    # PB_FALLBACK (all trades)
    fb_wins  = sum(1 for r in all_records if r['fb_win'])
    fb_pnl   = sum(r['fb_pnl'] for r in all_records)
    fb_wr    = fb_wins / total * 100

    # Avg entry delta (for trades that had retest)
    avg_delta = (sum(r['entry_delta_pct'] for r in pb_recs) / len(pb_recs)
                 if pb_recs else 0)

    W = 18
    print(f"  {'':20} {'IMMEDIATE':>12}  {'PB-STRICT':>12}  {'PB-FALLBACK':>12}")
    print(f"  {'-'*60}")
    print(f"  {'Signals':20} {total:>12}  "
          f"{retest_count:>11}✓  {total:>12}")
    print(f"  {'Retest rate':20} {'—':>12}  "
          f"{retest_rate:>10.1f}%  {'(fallback rest)':>12}")
    print(f"  {'Win rate':20} {imm_wr:>11.1f}%  {pb_wr:>11.1f}%  {fb_wr:>11.1f}%")
    print(f"  {'Net P&L':20} ₹{imm_pnl:>10,.0f}  ₹{pb_pnl:>10,.0f}  ₹{fb_pnl:>10,.0f}")
    print(f"  {'Matched subset P&L':20} {'(all)':>12}  ₹{imm_pb_pnl:>10,.0f}  {'(all)':>12}")
    print(f"  {'Avg entry Δ (%)':20} {'0%':>12}  {avg_delta:>+11.2f}%  {avg_delta:>+11.2f}%")
    print(f"  {'  (- = cheaper opt)':20}")

    # Per-DOW breakdown
    print(f"\n  Per-DOW (IMMEDIATE vs PB-STRICT on retest trades):")
    print(f"  {'DOW':5} {'Signals':>7} {'Retest%':>8} {'Imm WR':>7} "
          f"{'Imm P&L':>10} {'PB WR':>7} {'PB P&L':>10} {'EntryΔ':>8}")
    for dow in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
        dr = [r for r in all_records if r['dow'] == dow]
        if not dr:
            continue
        dr_pb = [r for r in dr if r['retest']]
        d_imm_wr  = sum(1 for r in dr if r['imm_win']) / len(dr) * 100
        d_imm_pnl = sum(r['imm_pnl'] for r in dr)
        d_pb_wr   = sum(1 for r in dr_pb if r['pb_win']) / len(dr_pb) * 100 if dr_pb else 0
        d_pb_pnl  = sum(r['pb_pnl'] for r in dr_pb) if dr_pb else 0
        d_delta   = (sum(r['entry_delta_pct'] for r in dr_pb) / len(dr_pb)
                     if dr_pb else 0)
        d_rr      = len(dr_pb) / len(dr) * 100
        print(f"  {dow:5} {len(dr):>7} {d_rr:>7.0f}%  {d_imm_wr:>6.0f}%  "
              f"₹{d_imm_pnl:>8,.0f}  {d_pb_wr:>6.0f}%  ₹{d_pb_pnl:>8,.0f}  "
              f"{d_delta:>+7.1f}%")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if SWEEP_MODE:
        # Full sweep: print best combo per instrument
        print("\nPULLBACK SWEEP — all retest_pct × wait_bars combinations")
        print("="*72)

        for inst in INSTRUMENTS:
            print(f"\n{inst} — loading data...")
            df = load_data(inst)
            if df is None:
                continue
            df = add_indicators(df)

            best = None
            best_key = None
            all_combos = []

            for rp, wb in product(RETEST_PCTS, WAIT_BARS_L):
                records = []
                prev_close = None
                for date, day_df in df.groupby('date'):
                    day_df = day_df.reset_index(drop=True)
                    if prev_close is None:
                        prev_close = day_df.iloc[-1]['Close']
                        continue
                    recs = simulate_day(day_df, prev_close, inst, rp, wb)
                    records.extend(recs)
                    prev_close = day_df.iloc[-1]['Close']

                total = len(records)
                if total == 0:
                    continue
                retest_r  = [r for r in records if r['retest']]
                imm_pnl   = sum(r['imm_pnl'] for r in records)
                pb_pnl    = sum(r['pb_pnl'] for r in retest_r)
                fb_pnl    = sum(r['fb_pnl'] for r in records)
                imm_wr    = sum(1 for r in records if r['imm_win']) / total * 100
                pb_wr     = (sum(1 for r in retest_r if r['pb_win']) / len(retest_r) * 100
                             if retest_r else 0)
                fb_wr     = sum(1 for r in records if r['fb_win']) / total * 100
                rr        = len(retest_r) / total * 100
                avg_delta = (sum(r['entry_delta_pct'] for r in retest_r) / len(retest_r)
                             if retest_r else 0)

                all_combos.append({
                    'rp': rp, 'wb': wb, 'total': total, 'rr': rr,
                    'imm_pnl': imm_pnl, 'imm_wr': imm_wr,
                    'pb_pnl': pb_pnl, 'pb_wr': pb_wr,
                    'fb_pnl': fb_pnl, 'fb_wr': fb_wr,
                    'avg_delta': avg_delta,
                })

            if not all_combos:
                continue

            # Header
            print(f"\n{inst}")
            print(f"  {'RP%':>5} {'WB':>3} {'Total':>6} {'Retest%':>8} "
                  f"{'ImmWR':>7} {'ImmPnL':>10} "
                  f"{'PB-WR':>7} {'PB-PnL':>10} "
                  f"{'FB-WR':>7} {'FB-PnL':>10} {'EntΔ%':>7}")
            print(f"  {'-'*85}")
            for c in all_combos:
                marker = ' ←best-PB' if c['pb_pnl'] == max(x['pb_pnl'] for x in all_combos) else ''
                print(f"  {c['rp']*100:>4.1f}% {c['wb']:>3}  {c['total']:>5}  "
                      f"{c['rr']:>7.1f}%  {c['imm_wr']:>6.1f}%  ₹{c['imm_pnl']:>8,.0f}  "
                      f"{c['pb_wr']:>6.1f}%  ₹{c['pb_pnl']:>8,.0f}  "
                      f"{c['fb_wr']:>6.1f}%  ₹{c['fb_pnl']:>8,.0f}  "
                      f"{c['avg_delta']:>+6.1f}%{marker}")

    else:
        # Detailed mode: specific retest_pct and wait_bars, full breakdown
        rp = RETEST_PCTS[0]
        wb = WAIT_BARS_L[0]
        for inst in INSTRUMENTS:
            records = run_instrument(inst, rp, wb)
            print_results(records, rp, wb, inst)

    print()
