"""
orb_3v6_backtest.py — Compare 3-bar (15-min) vs 6-bar (30-min) Opening Range.

Runs the same baseline ORB simulation with two configurations:
  CONFIG_3  : OR_BARS=3, width Mon 0.25%/Tue 0.30%/Thu 0.35%, checkpoint 11:30
  CONFIG_6  : OR_BARS=6, width Mon 0.40%/Tue 0.45%/Thu 0.50%, checkpoint 12:00

Both use:
  ADX per-day minimums matching PATH_A_DAY_ADX_MIN (Mon 30/Tue 25/Wed 20/Thu 25/Fri 20)
  Gap-fade filter | VWAP filter | Thu CALL-suppressed (NIFTY/BNF)
  Stop 50% | Target 80% (calibrated Apr 2026 for monthly options)
  Black-Scholes pricing with IV 14% (NIFTY) / 17% (BNF) / 16% (SENSEX), DTE=2

Usage:
    python orb_3v6_backtest.py            # all 3 instruments
    python orb_3v6_backtest.py NIFTY      # single instrument
"""
from __future__ import annotations
import os, glob, math, sys
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import time

# ── Data ─────────────────────────────────────────────────────────────────────
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

BROKERAGE     = 40      # Rs per round-trip
OR_BUFFER     = 0.0005  # 0.05% — matches PATH_A_BUFFER
STOP_PCT      = 0.50    # PATH_A_STOP
TARGET_PCT    = 0.80    # PATH_A_TARGET
DTE           = 2
R             = 0.065
ADX_PERIOD    = 14
GAP_THRESHOLD = 0.003   # 0.30% gap threshold

ADX_BY_DAY = {'Mon': 30, 'Tue': 25, 'Wed': 20, 'Thu': 25, 'Fri': 20}
NO_CALL    = {'NIFTY': {'Thu'}, 'BANKNIFTY': {'Thu'}, 'SENSEX': set()}
SKIP_DAYS  = {'NIFTY': set(), 'BANKNIFTY': set(), 'SENSEX': {'Thu'}}

# ── Two configs to compare ───────────────────────────────────────────────────
CONFIGS = {
    '3-bar (15-min OR)': {
        'or_bars'    : 3,
        'width_max'  : {'Mon': 0.0025, 'Tue': 0.0030, 'Thu': 0.0035},
        'force_close': time(11, 30),
        'entry_start': 3,   # bar index after OR (bar 09:30)
    },
    '6-bar (30-min OR)': {
        'or_bars'    : 6,
        'width_max'  : {'Mon': 0.0040, 'Tue': 0.0045, 'Thu': 0.0050},
        'force_close': time(12, 0),
        'entry_start': 6,   # bar index after OR (bar 09:45)
    },
}

# ── Helpers ──────────────────────────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0:
        return max(float(max(S - K, 0) if call else max(K - S, 0)), 0.05)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    p  = (S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2) if call
          else K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return max(float(p), 0.05)


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
    tr  = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    df['ADX']      = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    df['DI_plus']  = pdi
    df['DI_minus'] = ndi
    df['tp']       = (hi + lo + cl) / 3
    df['VWAP']     = df.groupby('date')['tp'].transform(lambda x: x.expanding().mean())
    return df


def classify_gap(prev_close, curr_open, px_after_or):
    pct = (curr_open - prev_close) / prev_close
    if abs(pct) < GAP_THRESHOLD:
        return 'INSIDE_OPEN'
    if pct < 0:
        return 'GAP_AND_GO_DN' if px_after_or < curr_open else 'GAP_FADE_DN'
    return 'GAP_AND_GO_UP' if px_after_or > curr_open else 'GAP_FADE_UP'


def simulate_trade(day_df, direction, entry_idx, S_entry, force_close,
                   iv, lot, instrument):
    is_call = direction == 'CALL'
    stride  = STRIKES[instrument]
    K       = round(S_entry / stride) * stride
    T_entry = max((DTE - entry_idx / 75) / 252, 1 / 252 / 75)
    opt_e   = bs_price(S_entry, K, T_entry, R, iv, call=is_call)
    stop_px = opt_e * (1 - STOP_PCT)
    tgt_px  = opt_e * (1 + TARGET_PCT)

    for j in range(entry_idx + 1, len(day_df)):
        row  = day_df.iloc[j]
        t    = row['datetime'].time()
        T_j  = max((DTE - j / 75) / 252, 1 / 252 / 75)
        opt_j = bs_price(row['Close'], K, T_j, R, iv, call=is_call)
        if t >= force_close:
            return (opt_j - opt_e) * lot - BROKERAGE, 'CHECKPOINT'
        if opt_j <= stop_px:
            return (stop_px - opt_e) * lot - BROKERAGE, 'STOP'
        if opt_j >= tgt_px:
            return (tgt_px - opt_e) * lot - BROKERAGE, 'TARGET'

    last  = day_df.iloc[-1]
    T_l   = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_l = bs_price(last['Close'], K, T_l, R, iv, call=is_call)
    return (opt_l - opt_e) * lot - BROKERAGE, 'EOD'


# ── Per-day simulation ────────────────────────────────────────────────────────
def simulate_day(day_df, prev_close, instrument, cfg):
    if len(day_df) < cfg['or_bars'] + 2:
        return None

    lot = LOT_SIZES[instrument]
    iv  = IVS[instrument]
    dow = day_df.iloc[0]['datetime'].strftime('%a')

    if dow in SKIP_DAYS[instrument]:
        return None

    n = cfg['or_bars']
    or_hi = day_df.iloc[:n]['High'].max()
    or_lo = day_df.iloc[:n]['Low'].min()
    or_mid = (or_hi + or_lo) / 2
    or_width = (or_hi - or_lo) / or_mid

    width_max = cfg['width_max'].get(dow)   # None = no limit
    if width_max is not None and or_width > width_max:
        return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
                'dow': dow, 'result': 'WIDTH_BLOCKED', 'pnl': 0, 'win': None,
                'or_width_pct': round(or_width * 100, 3), 'direction': '-'}

    # Reference bar right after OR is established
    ref_bar   = day_df.iloc[n]
    curr_open = day_df.iloc[0]['Open']
    gap_type  = classify_gap(prev_close, curr_open, ref_bar['Close'])
    adx_floor = ADX_BY_DAY.get(dow, 20)
    start_idx = cfg['entry_start']   # bar index to start scanning
    force_t   = cfg['force_close']

    for i in range(start_idx, len(day_df)):
        row = day_df.iloc[i]
        if row['datetime'].time() >= force_t:
            break
        if row['ADX'] < adx_floor:
            continue
        px_i   = row['Close']
        vwap_i = row['VWAP']
        call_ok = gap_type != 'GAP_FADE_UP' and dow not in NO_CALL[instrument]
        put_ok  = gap_type != 'GAP_FADE_DN'
        if call_ok and px_i > or_hi * (1 + OR_BUFFER) and px_i > vwap_i:
            pnl, why = simulate_trade(day_df, 'CALL', i, px_i, force_t,
                                      iv, lot, instrument)
            return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
                    'dow': dow, 'result': why, 'pnl': round(pnl), 'win': pnl > 0,
                    'or_width_pct': round(or_width * 100, 3), 'direction': 'CALL',
                    'gap_type': gap_type, 'adx': round(row['ADX'], 1)}
        if put_ok and px_i < or_lo * (1 - OR_BUFFER) and px_i < vwap_i:
            pnl, why = simulate_trade(day_df, 'PUT', i, px_i, force_t,
                                      iv, lot, instrument)
            return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
                    'dow': dow, 'result': why, 'pnl': round(pnl), 'win': pnl > 0,
                    'or_width_pct': round(or_width * 100, 3), 'direction': 'PUT',
                    'gap_type': gap_type, 'adx': round(row['ADX'], 1)}

    return {'date': day_df.iloc[0]['date'], 'instrument': instrument,
            'dow': dow, 'result': 'NO_BREAK', 'pnl': 0, 'win': None,
            'or_width_pct': round(or_width * 100, 3), 'direction': '-'}


# ── Run one instrument for one config ────────────────────────────────────────
def run_instrument(instrument, df, cfg_name, cfg):
    dates = sorted(df['date'].unique())
    results = []
    for i, d in enumerate(dates):
        day_df = df[df['date'] == d].reset_index(drop=True)
        prev_close = df[df['date'] == dates[i-1]]['Close'].iloc[-1] if i > 0 else day_df.iloc[0]['Open']
        r = simulate_day(day_df, prev_close, instrument, cfg)
        if r:
            results.append(r)
    return results


# ── Summary stats ─────────────────────────────────────────────────────────────
def stats(results):
    trades = [r for r in results if r.get('win') is not None and r['pnl'] != 0]
    if not trades:
        return {'n': 0, 'wr': 0, 'pnl': 0, 'best': 0, 'worst': 0,
                'avg': 0, 'blocked': 0, 'no_break': 0}
    pnls = [t['pnl'] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    blocked   = sum(1 for r in results if r.get('result') == 'WIDTH_BLOCKED')
    no_break  = sum(1 for r in results if r.get('result') == 'NO_BREAK')
    return {
        'n'       : len(trades),
        'wr'      : wins / len(trades) * 100,
        'pnl'     : sum(pnls),
        'best'    : max(pnls),
        'worst'   : min(pnls),
        'avg'     : sum(pnls) / len(trades),
        'blocked' : blocked,
        'no_break': no_break,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    target_instruments = sys.argv[1:] if len(sys.argv) > 1 else list(DATA_ROOTS.keys())
    target_instruments = [i for i in target_instruments if i in DATA_ROOTS]
    if not target_instruments:
        print("Usage: python orb_3v6_backtest.py [NIFTY] [BANKNIFTY] [SENSEX]")
        return

    # Load & prep data once per instrument
    dfs = {}
    for inst in target_instruments:
        print(f"Loading {inst}...", end=' ', flush=True)
        df = load_data(inst)
        if df is None:
            print("NO DATA — skipped")
            continue
        df = add_indicators(df)
        dfs[inst] = df
        print(f"{len(df['date'].unique())} days")

    print()

    # Results per config per instrument
    all_results: dict[str, dict[str, list]] = {cfg: {} for cfg in CONFIGS}

    for cfg_name, cfg in CONFIGS.items():
        print(f"Simulating: {cfg_name} ...")
        for inst, df in dfs.items():
            all_results[cfg_name][inst] = run_instrument(inst, df, cfg_name, cfg)

    # ── Print comparison ─────────────────────────────────────────────────────
    sep = '─' * 80
    print()
    print('=' * 80)
    print('  ORB Backtest: 3-bar (15-min) vs 6-bar (30-min) Opening Range')
    print('=' * 80)

    combined = {cfg: [] for cfg in CONFIGS}

    for inst in target_instruments:
        if inst not in dfs:
            continue
        print()
        print(f'  ── {inst} ─────────────────────────────────────────────────')
        print(f'  {"Config":<26} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
              f'{"Avg/trade":>10} {"Best":>9} {"Worst":>9} {"Blocked":>8} {"NoBreak":>8}')
        print(f'  {sep}')
        for cfg_name in CONFIGS:
            res = all_results[cfg_name].get(inst, [])
            s   = stats(res)
            combined[cfg_name].extend([r for r in res if r.get('win') is not None and r['pnl'] != 0])
            print(f'  {cfg_name:<26} {s["n"]:>7} {s["wr"]:>5.1f}% '
                  f'₹{s["pnl"]:>10,.0f} ₹{s["avg"]:>9,.0f} '
                  f'₹{s["best"]:>8,.0f} ₹{s["worst"]:>8,.0f} '
                  f'{s["blocked"]:>8} {s["no_break"]:>8}')

    # ── Combined totals ───────────────────────────────────────────────────────
    if len(target_instruments) > 1:
        print()
        print(f'  ── COMBINED (all instruments) ──────────────────────────────')
        print(f'  {"Config":<26} {"Trades":>7} {"WR%":>6} {"Net P&L":>11} '
              f'{"Avg/trade":>10} {"Best":>9} {"Worst":>9}')
        print(f'  {sep}')
        for cfg_name in CONFIGS:
            trades = combined[cfg_name]
            if not trades:
                continue
            pnls = [t['pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            n    = len(pnls)
            print(f'  {cfg_name:<26} {n:>7} {wins/n*100:>5.1f}% '
                  f'₹{sum(pnls):>10,.0f} ₹{sum(pnls)/n:>9,.0f} '
                  f'₹{max(pnls):>8,.0f} ₹{min(pnls):>8,.0f}')

    # ── Breakdown by DOW ──────────────────────────────────────────────────────
    print()
    print('  ── DOW breakdown (combined, all instruments) ───────────────')
    print(f'  {"DOW":<6} {"Config":<26} {"Trades":>7} {"WR%":>6} {"Net P&L":>11}')
    print(f'  {sep}')
    for dow in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
        for cfg_name in CONFIGS:
            trades = [t for t in combined[cfg_name] if t.get('dow') == dow]
            if not trades:
                print(f'  {dow:<6} {cfg_name:<26} {"–":>7}')
                continue
            pnls = [t['pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            print(f'  {dow:<6} {cfg_name:<26} {len(trades):>7} '
                  f'{wins/len(trades)*100:>5.1f}% ₹{sum(pnls):>10,.0f}')
        print()

    print()


if __name__ == '__main__':
    main()
