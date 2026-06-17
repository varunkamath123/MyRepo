"""
early_session_backtest.py
=========================
Backtest comparing three early-session entry approaches:

  PREDICTIVE : Enter at 9:30 when gap + DI dominance + ADX slope score >= 5
  ORB        : Wait for OR breakout (current Path A behaviour)
  SKIP       : Score < 2, no early trade

Simplified predictive score (IV skew / MaxPain excluded — not in historical CSVs):
  Gap AND_GO aligns with direction    -> +2
  DI dominant spread >= 20 pts       -> +2   (10-19 -> +1)
  ADX >= 20 AND rising bar-over-bar  -> +1
  9:30 price already beyond OR       -> +2
  MAX = 7

Modes:
  Score >= 5  -> PREDICTIVE  (enter at 9:30 close)
  Score 2-4   -> ORB         (wait for OR level break)
  Score < 2   -> SKIP

Also runs a BASELINE simulation of pure ORB (current Path A, no score gate)
so we can see the delta clearly.

Option pricing: Black-Scholes, DTE=2
Stops/targets : PREDICTIVE stop=40% target=150% | ORB stop=50% target=150%
Force-close   : 11:30
"""

from __future__ import annotations
import os, glob, math, json
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import time, date as date_t

# ── Config ───────────────────────────────────────────────────────────────────
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
STRIKES   = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}

BROKERAGE     = 40      # Rs per round-trip trade
PRED_STOP     = 0.40
PRED_TARGET   = 1.50
ORB_STOP      = 0.50
ORB_TARGET    = 1.50
FORCE_CLOSE   = time(11, 30)
OR_BARS       = 3       # 9:15, 9:20, 9:25 — matches PATH_A_ORB_BARS=3 in config
OR_BUFFER     = 0.0005  # matches PATH_A_BUFFER in config
DTE           = 2
R             = 0.065
ADX_PERIOD    = 14

PRED_THRESHOLD = 5
ORB_THRESHOLD  = 2

# FIX 1: Gap threshold 0.3% matches live bot (was 0.2% — caused over-classification)
GAP_THRESHOLD  = 0.003  # 0.30% — matches live config (gap_pct > 0.003 / < -0.003)

# FIX 2: Per-day ADX thresholds — matches PATH_A_DAY_ADX_MIN in config
#   (was hardcoded 20 for all days — too permissive Mon/Tue/Thu)
ORB_ADX_BY_DAY = {'Mon': 30, 'Tue': 25, 'Wed': 20, 'Thu': 25, 'Fri': 20}

# FIX 3: Per-day OR width gate — matches PATH_A_OR_WIDTH_MAX in config
#   (was missing entirely — included chaotic wide-OR days the live bot skips)
OR_WIDTH_MAX   = {'Mon': 0.0025, 'Tue': 0.0030, 'Thu': 0.0035}
# Wed/Fri have no width gate (None = no limit)

# Day-of-week rules (mirrors live config)
NO_CALL_DAYS  = {'NIFTY': {'Thu'}, 'BANKNIFTY': {'Thu'}, 'SENSEX': set()}
SKIP_DAYS     = {'NIFTY': set(), 'BANKNIFTY': set(), 'SENSEX': {'Thu'}}


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if call else max(K - S, 0)
        return max(float(intrinsic), 0.05)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return max(float(price), 0.05)


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


# ── Indicators ────────────────────────────────────────────────────────────────
def add_adx(df: pd.DataFrame) -> pd.DataFrame:
    hi, lo, cl = df['High'], df['Low'], df['Close']
    pdm = hi.diff().clip(lower=0)
    ndm = (-lo.diff()).clip(lower=0)
    pdm = pdm.where(pdm > ndm, 0.0)
    ndm = ndm.where(ndm > pdm, 0.0)
    tr  = pd.concat([hi - lo,
                     (hi - cl.shift()).abs(),
                     (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    df['ADX']      = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    df['DI_plus']  = pdi
    df['DI_minus'] = ndi
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['tp']   = (df['High'] + df['Low'] + df['Close']) / 3
    df['VWAP'] = df.groupby('date')['tp'].transform(
        lambda x: x.expanding().mean()
    )
    return df


# ── Gap classification ────────────────────────────────────────────────────────
def classify_gap(prev_close: float, curr_open: float,
                 px_930: float) -> str:
    # FIX 1: 0.3% threshold matches live bot (was 0.2%)
    pct = (curr_open - prev_close) / prev_close
    if abs(pct) < GAP_THRESHOLD:
        return 'INSIDE_OPEN'
    if pct < 0:
        return 'GAP_AND_GO_DN' if px_930 < curr_open else 'GAP_FADE_DN'
    return 'GAP_AND_GO_UP' if px_930 > curr_open else 'GAP_FADE_UP'


# ── Predictive score ──────────────────────────────────────────────────────────
def score_at_930(gap_type: str, dip: float, dim: float,
                 adx_930: float, adx_925: float,
                 px_930: float, or_high: float, or_low: float,
                 direction: str) -> int:
    s = 0
    # 1. Gap aligns (2 pts)
    if direction == 'CALL' and gap_type == 'GAP_AND_GO_UP':
        s += 2
    elif direction == 'PUT' and gap_type == 'GAP_AND_GO_DN':
        s += 2

    # 2. DI dominance (2 pts)
    spread = (dip - dim) if direction == 'CALL' else (dim - dip)
    if spread >= 20:
        s += 2
    elif spread >= 10:
        s += 1

    # 3. ADX >= 20 and rising (1 pt)
    if adx_930 >= 20 and adx_930 > adx_925:
        s += 1

    # 4. Price already beyond OR boundary (2 pts)
    if direction == 'CALL' and px_930 > or_high * (1 + OR_BUFFER):
        s += 2
    elif direction == 'PUT' and px_930 < or_low * (1 - OR_BUFFER):
        s += 2

    return s


# ── Trade simulation ──────────────────────────────────────────────────────────
def simulate_trade(day_df: pd.DataFrame, direction: str,
                   entry_idx: int, S_entry: float,
                   stop_pct: float, target_pct: float,
                   iv: float, lot: int, instrument: str) -> tuple:
    is_call = direction == 'CALL'
    stride  = STRIKES[instrument]
    K = round(S_entry / stride) * stride

    # Rough T at entry: DTE minus fraction of day elapsed
    entry_bar_in_day = entry_idx  # 0-based within day_df
    T_entry = max((DTE - entry_bar_in_day / 75) / 252, 1 / 252 / 75)

    opt_entry = bs_price(S_entry, K, T_entry, R, iv, call=is_call)
    stop_px   = opt_entry * (1 - stop_pct)
    tgt_px    = opt_entry * (1 + target_pct)

    for j in range(entry_idx + 1, len(day_df)):
        row = day_df.iloc[j]
        t   = row['datetime'].time()

        bars_from_entry = j - entry_idx
        T_j = max((DTE - (entry_bar_in_day + bars_from_entry) / 75) / 252,
                  1 / 252 / 75)
        opt_j = bs_price(row['Close'], K, T_j, R, iv, call=is_call)

        if t >= FORCE_CLOSE:
            pnl = (opt_j - opt_entry) * lot - BROKERAGE
            return pnl, 'FORCE_CLOSE'

        if opt_j <= stop_px:
            pnl = (stop_px - opt_entry) * lot - BROKERAGE
            return pnl, 'STOP'

        if opt_j >= tgt_px:
            pnl = (tgt_px - opt_entry) * lot - BROKERAGE
            return pnl, 'TARGET'

    # Ran out of bars without force-close (short day / data gap)
    last = day_df.iloc[-1]
    T_last = max((DTE - len(day_df) / 75) / 252, 1 / 252 / 75)
    opt_last = bs_price(last['Close'], K, T_last, R, iv, call=is_call)
    pnl = (opt_last - opt_entry) * lot - BROKERAGE
    return pnl, 'EOD'


# ── Single-day simulation ─────────────────────────────────────────────────────
def simulate_day(day_df: pd.DataFrame, prev_close: float,
                 instrument: str) -> dict | None:
    if len(day_df) < 5:
        return None

    lot = LOT_SIZES[instrument]
    iv  = IVS[instrument]
    dow = day_df.iloc[0]['datetime'].strftime('%a')

    if dow in SKIP_DAYS[instrument]:
        return None

    # OR from bars 0-2 (9:15, 9:20, 9:25)
    or_hi = day_df.iloc[:OR_BARS]['High'].max()
    or_lo = day_df.iloc[:OR_BARS]['Low'].min()

    # FIX 2: OR width gate — matches PATH_A_OR_WIDTH_MAX per-day limits
    or_width = (or_hi - or_lo) / or_lo
    width_max = OR_WIDTH_MAX.get(dow)   # None = no limit (Wed, Fri)
    if width_max is not None and or_width > width_max:
        return None   # live bot would block ORB entirely this day

    # 9:25 and 9:30 bars
    b925 = day_df.iloc[2]
    b930 = day_df.iloc[3]

    px_930   = b930['Close']
    adx_930  = b930['ADX']
    adx_925  = b925['ADX']
    dip_930  = b930['DI_plus']
    dim_930  = b930['DI_minus']

    curr_open = day_df.iloc[0]['Open']
    gap_type  = classify_gap(prev_close, curr_open, px_930)

    # Score direction: DI at 9:30 determines which side we prefer for PREDICTIVE
    pref_dir = 'CALL' if dip_930 > dim_930 else 'PUT'
    if dow in NO_CALL_DAYS[instrument] and pref_dir == 'CALL':
        pref_dir = 'PUT'

    sc = score_at_930(gap_type, dip_930, dim_930, adx_930, adx_925,
                      px_930, or_hi, or_lo, pref_dir)

    rec = {'date': day_df.iloc[0]['date'], 'instrument': instrument,
           'dow': dow, 'gap_type': gap_type, 'direction': pref_dir,
           'score': sc, 'adx_930': round(adx_930, 1),
           'di_spread': round(abs(dip_930 - dim_930), 1)}

    # FIX 3: Per-day ADX threshold (was hardcoded 20)
    adx_floor = ORB_ADX_BY_DAY.get(dow, 20)

    # ── PREDICTIVE mode ──────────────────────────────────────────────────────
    if sc >= PRED_THRESHOLD:
        pnl, why = simulate_trade(day_df, pref_dir, 3, px_930,
                                   PRED_STOP, PRED_TARGET, iv, lot, instrument)
        rec.update(mode='PREDICTIVE', pnl=round(pnl), win=pnl > 0, exit=why)
        return rec

    # ── ORB mode ─────────────────────────────────────────────────────────────
    # FIX 4: Take EITHER side that breaks first (matches live bot).
    #   Direction is NOT pre-locked to DI at 9:30. The live bot fires on
    #   whichever of OR_high or OR_low is breached first, with gap-fade filter.
    if sc >= ORB_THRESHOLD:
        for i in range(3, len(day_df)):
            row = day_df.iloc[i]
            if row['datetime'].time() >= FORCE_CLOSE:
                break
            adx_i  = row['ADX']
            if adx_i < adx_floor:          # FIX 3: per-day floor
                continue
            px_i   = row['Close']
            vwap_i = row['VWAP']
            # Gap-fade filter: live bot suppresses CALL on GAP_FADE_UP days, PUT on GAP_FADE_DN
            call_ok = gap_type != 'GAP_FADE_UP' and dow not in NO_CALL_DAYS[instrument]
            put_ok  = gap_type != 'GAP_FADE_DN'
            if call_ok and px_i > or_hi * (1 + OR_BUFFER) and px_i > vwap_i:
                pnl, why = simulate_trade(day_df, 'CALL', i, px_i,
                                           ORB_STOP, ORB_TARGET, iv, lot, instrument)
                rec.update(mode='ORB', pnl=round(pnl), win=pnl > 0, exit=why,
                           direction='CALL')
                return rec
            if put_ok and px_i < or_lo * (1 - OR_BUFFER) and px_i < vwap_i:
                pnl, why = simulate_trade(day_df, 'PUT', i, px_i,
                                           ORB_STOP, ORB_TARGET, iv, lot, instrument)
                rec.update(mode='ORB', pnl=round(pnl), win=pnl > 0, exit=why,
                           direction='PUT')
                return rec
        rec.update(mode='ORB_MISS', pnl=0, win=None, exit='NO_BREAK')
        return rec

    # ── SKIP ─────────────────────────────────────────────────────────────────
    rec.update(mode='SKIP', pnl=0, win=None, exit='SKIP')
    return rec


# ── Baseline: pure ORB (current Path A, no score gate) ───────────────────────
def simulate_day_baseline(day_df: pd.DataFrame, prev_close: float,
                           instrument: str) -> dict | None:
    """Current Path A: always wait for OR breakout, no score."""
    if len(day_df) < 5:
        return None

    lot = LOT_SIZES[instrument]
    iv  = IVS[instrument]
    dow = day_df.iloc[0]['datetime'].strftime('%a')

    if dow in SKIP_DAYS[instrument]:
        return None

    or_hi = day_df.iloc[:OR_BARS]['High'].max()
    or_lo = day_df.iloc[:OR_BARS]['Low'].min()

    # Apply same OR width gate as simulate_day
    or_width  = (or_hi - or_lo) / or_lo
    width_max = OR_WIDTH_MAX.get(dow)
    if width_max is not None and or_width > width_max:
        return None   # live bot skips wide-OR days

    b930      = day_df.iloc[3]
    curr_open = day_df.iloc[0]['Open']
    px_930    = b930['Close']
    gap_type  = classify_gap(prev_close, curr_open, px_930)
    adx_floor = ORB_ADX_BY_DAY.get(dow, 20)   # per-day ADX threshold

    for i in range(3, len(day_df)):
        row = day_df.iloc[i]
        if row['datetime'].time() >= FORCE_CLOSE:
            break
        if row['ADX'] < adx_floor:   # per-day floor (was hardcoded 20)
            continue
        px_i   = row['Close']
        vwap_i = row['VWAP']
        # Either-side breakout with gap-fade filter (matches live bot)
        call_ok = gap_type != 'GAP_FADE_UP' and dow not in NO_CALL_DAYS[instrument]
        put_ok  = gap_type != 'GAP_FADE_DN'
        if call_ok and px_i > or_hi * (1 + OR_BUFFER) and px_i > vwap_i:
            pnl, why = simulate_trade(day_df, 'CALL', i, px_i,
                                       ORB_STOP, ORB_TARGET, iv, lot, instrument)
            return {'mode': 'BASELINE_ORB', 'pnl': round(pnl), 'win': pnl > 0,
                    'exit': why, 'date': day_df.iloc[0]['date'], 'direction': 'CALL'}
        if put_ok and px_i < or_lo * (1 - OR_BUFFER) and px_i < vwap_i:
            pnl, why = simulate_trade(day_df, 'PUT', i, px_i,
                                       ORB_STOP, ORB_TARGET, iv, lot, instrument)
            return {'mode': 'BASELINE_ORB', 'pnl': round(pnl), 'win': pnl > 0,
                    'exit': why, 'date': day_df.iloc[0]['date'], 'direction': 'PUT'}

    return {'mode': 'BASELINE_ORB_MISS', 'pnl': 0, 'win': None,
            'exit': 'NO_BREAK', 'date': day_df.iloc[0]['date']}


# ── Reporting ─────────────────────────────────────────────────────────────────
def stats_block(label: str, rows: pd.DataFrame) -> None:
    trades = rows[rows['win'].notna()]
    if len(trades) == 0:
        print(f'  {label}: 0 trades')
        return
    wins   = trades[trades['win'] == True]
    losses = trades[trades['win'] == False]
    wr  = len(wins) / len(trades) * 100
    net = trades['pnl'].sum()
    aw  = wins['pnl'].mean() if len(wins) else 0
    al  = losses['pnl'].mean() if len(losses) else 0
    exits = trades['exit'].value_counts().to_dict() if 'exit' in trades else {}
    print(f'  {label}')
    print(f'    Trades: {len(trades):3d}  |  WR: {wr:5.1f}%  |  '
          f'Net: Rs{net:+,.0f}  |  Avg W: Rs{aw:+,.0f}  |  Avg L: Rs{al:+,.0f}')
    if exits:
        print(f'    Exits : {exits}')


# ── Main ──────────────────────────────────────────────────────────────────────
def analyse(instrument: str) -> dict:
    print(f'\n{"="*60}')
    print(f' {instrument}')
    print(f'{"="*60}')

    df = load_data(instrument)
    if df is None:
        print('  No data found.')
        return {}

    df = add_adx(df)
    df = add_vwap(df)

    dates = sorted(df['date'].unique())
    print(f'  Days in dataset: {len(dates)}')

    results, baseline = [], []
    prev_close = None

    for d in dates:
        day_df = df[df['date'] == d].copy().reset_index(drop=True)
        if prev_close is None:
            prev_close = day_df.iloc[-1]['Close']
            continue

        r = simulate_day(day_df, prev_close, instrument)
        if r:
            results.append(r)

        b = simulate_day_baseline(day_df, prev_close, instrument)
        if b:
            baseline.append(b)

        prev_close = day_df.iloc[-1]['Close']

    if not results:
        print('  No results.')
        return {}

    rdf = pd.DataFrame(results)
    bdf = pd.DataFrame(baseline)

    # ── Score distribution ─────────────────────────────────────────────────
    print(f'\n  Score distribution at 9:30:')
    sc_counts = rdf['score'].value_counts().sort_index()
    for sc, cnt in sc_counts.items():
        mode = 'PREDICTIVE' if sc >= PRED_THRESHOLD else ('ORB' if sc >= ORB_THRESHOLD else 'SKIP')
        bar  = '#' * cnt
        print(f'    Score {sc}: {cnt:3d} days  [{mode}]  {bar}')

    pred_days = len(rdf[rdf['mode'] == 'PREDICTIVE'])
    orb_days  = len(rdf[rdf['mode'] == 'ORB'])
    skip_days = len(rdf[rdf['mode'].isin(['SKIP', 'ORB_MISS'])])
    print(f'\n  Mode split: PREDICTIVE={pred_days}  ORB={orb_days}  '
          f'SKIP/MISS={skip_days}')

    # ── Results by mode ────────────────────────────────────────────────────
    print()
    stats_block('PREDICTIVE entries (score>=5, enter 9:30)',
                rdf[rdf['mode'] == 'PREDICTIVE'])
    stats_block('ORB entries        (score 2-4, wait OR break)',
                rdf[rdf['mode'] == 'ORB'])

    # Combined (PREDICTIVE + ORB)
    combined = rdf[rdf['mode'].isin(['PREDICTIVE', 'ORB'])]
    print()
    stats_block('COMBINED  (unified early session)',    combined)
    stats_block('BASELINE  (current Path A, no score)', bdf[bdf['win'].notna()])

    # ── Gap-type breakdown for PREDICTIVE ──────────────────────────────────
    pred_trades = rdf[rdf['mode'] == 'PREDICTIVE']
    if len(pred_trades) > 0:
        print(f'\n  PREDICTIVE breakdown by gap type:')
        for gt, grp in pred_trades.groupby('gap_type'):
            trades_g = grp[grp['win'].notna()]
            if len(trades_g) > 0:
                wr = trades_g['win'].mean() * 100
                net = trades_g['pnl'].sum()
                print(f'    {gt:20s}: {len(trades_g):2d} trades  WR {wr:.0f}%  Rs{net:+,.0f}')

    summary = {
        'instrument': instrument,
        'pred_trades': int(len(pred_trades[pred_trades['win'].notna()])),
        'pred_wr': float(pred_trades[pred_trades['win'].notna()]['win'].mean() * 100) if len(pred_trades[pred_trades['win'].notna()]) else 0,
        'pred_net': int(pred_trades['pnl'].sum()),
        'orb_trades': int(len(rdf[rdf['mode'] == 'ORB'][rdf['mode'] == 'ORB']['win'].notna() if False else rdf[(rdf['mode'] == 'ORB') & rdf['win'].notna()])),
        'orb_wr': float(rdf[(rdf['mode'] == 'ORB') & rdf['win'].notna()]['win'].mean() * 100) if len(rdf[(rdf['mode'] == 'ORB') & rdf['win'].notna()]) else 0,
        'orb_net': int(rdf[rdf['mode'] == 'ORB']['pnl'].sum()),
        'combined_net': int(combined['pnl'].sum()),
        'baseline_net': int(bdf[bdf['win'].notna()]['pnl'].sum()),
    }
    return summary


if __name__ == '__main__':
    all_summaries = {}
    for inst in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
        s = analyse(inst)
        if s:
            all_summaries[inst] = s

    # ── Combined summary across all instruments ────────────────────────────
    if all_summaries:
        print(f'\n{"="*60}')
        print(' COMBINED ACROSS ALL INSTRUMENTS')
        print(f'{"="*60}')
        total_pred_net    = sum(v['pred_net']     for v in all_summaries.values())
        total_orb_net     = sum(v['orb_net']      for v in all_summaries.values())
        total_combined    = sum(v['combined_net']  for v in all_summaries.values())
        total_baseline    = sum(v['baseline_net']  for v in all_summaries.values())
        print(f'  PREDICTIVE entries net   : Rs{total_pred_net:+,.0f}')
        print(f'  ORB entries net          : Rs{total_orb_net:+,.0f}')
        print(f'  COMBINED (new approach)  : Rs{total_combined:+,.0f}')
        print(f'  BASELINE (current Path A): Rs{total_baseline:+,.0f}')
        delta = total_combined - total_baseline
        print(f'  Delta vs baseline        : Rs{delta:+,.0f}')

        # Save results
        out_path = os.path.join(os.path.dirname(__file__), 'early_session_results.json')
        with open(out_path, 'w') as f:
            json.dump(all_summaries, f, indent=2)
        print(f'\n  Results saved -> {out_path}')
