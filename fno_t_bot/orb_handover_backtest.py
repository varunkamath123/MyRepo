"""
orb_handover_backtest.py — Backtest: ORB force-close at 11:30 vs handover to trailing stop

Question: For profitable ORB positions at 11:30, does holding with a trailing stop
produce better outcomes than force-closing?

Methodology:
  1. Load 5-min historical data for NF, BNF, SENSEX
  2. Replay Path A entry logic (OR breakout, ADX>=25, VWAP)
  3. For each entry:
       Strategy A: force-close at 11:30 bar regardless
       Strategy B: continue with trailing stop to 14:30 (handover)
  4. Split results into:
       - All entries
       - Profitable at 11:30 (main focus of handover question)
       - Losing at 11:30

Option P&L model:
  Option price is estimated using Black-Scholes (same as bot.py add_indicators).
  For simplicity, DAYS_TO_EXPIRY=2 and IV=15% (NIFTY), 18% (BNF/SENSEX).
  Entry premium is estimated at signal bar; subsequent prices tracked via BS.

Usage:
  python orb_handover_backtest.py                  # all 3 instruments
  python orb_handover_backtest.py NIFTY            # single instrument
  python orb_handover_backtest.py --from 2025-10-01 # date filter
"""
from __future__ import annotations
import os, sys, math, json
import pandas as pd
import numpy as np
from datetime import date, datetime, time as dtime
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────
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
LOT_SIZES    = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
BASE_IV      = {'NIFTY': 0.14, 'BANKNIFTY': 0.17, 'SENSEX': 0.16}
DAYS_TO_EXP  = 2        # 2-DTE options (same as live config)

# Path A parameters (mirrors config.py as of Apr 2026)
ORB_BARS     = 3        # 09:15, 09:20, 09:25
ORB_BUFFER   = 0.0005   # 0.05% breakout buffer
ADX_MIN      = 25       # global floor (updated from 20)
DAY_ADX_MIN  = {'Mon': 30, 'Tue': 25, 'Wed': 25, 'Thu': 25, 'Fri': 25}
NO_CALL_DAYS = {'Thu'}
OR_WIDTH_MAX = {'Mon': 0.0025, 'Tue': 0.0030, 'Thu': 0.0035}  # Wed/Fri: None

# Exit parameters
STOP         = 0.50    # 50% stop loss on option premium
TARGET       = 1.50    # 150% target
TRAIL_ACT    = 0.12    # trail activates at +12% gain
TRAIL_DIST   = 0.15    # trail distance from peak (15%)

HANDOVER_TIME = dtime(11, 30)  # old orb_bot.py force-close time
EOD_CLOSE     = dtime(14, 30)  # end of day
ORB_ENTRY_START = dtime(9, 30)
ORB_ENTRY_END   = dtime(11, 0)


# ── Black-Scholes helper ─────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def bs_call(S: float, K: float, T: float, sigma: float, r: float = 0.065) -> float:
    """Black-Scholes call price. T in years."""
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_put(S: float, K: float, T: float, sigma: float, r: float = 0.065) -> float:
    """Black-Scholes put price. T in years."""
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def est_option_px(S: float, K: float, opt_type: str, bars_elapsed: int,
                  iv: float, entry_dex: int = DAYS_TO_EXP) -> float:
    """Estimate option price: T = (entry_dex days - bars_elapsed × 5min) as fraction of year."""
    T = max((entry_dex * 390 - bars_elapsed * 5) / (252 * 390), 1e-6)
    return (bs_call(S, K, T, iv) if opt_type == 'CALL'
            else bs_put(S, K, T, iv))


# ── Indicator helpers ────────────────────────────────────────────────────────
def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Compute ADX, DI+, DI- in-place."""
    hi, lo, cl = df['High'], df['Low'], df['Close']
    tr   = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    dmp  = (hi - hi.shift()).clip(lower=0)
    dmm  = (lo.shift() - lo).clip(lower=0)
    dmp[dmp <= dmm] = 0; dmm[dmm <= dmp] = 0   # type: ignore
    dip  = 100 * dmp.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr
    dim  = 100 * dmm.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr
    dx   = 100 * (dip - dim).abs() / (dip + dim).clip(lower=1e-9)
    adx  = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    df['ADX'] = adx; df['DI_plus'] = dip; df['DI_minus'] = dim
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Compute intraday VWAP (resets each day)."""
    df['_date'] = df.index.date
    df['_tp']   = (df['High'] + df['Low'] + df['Close']) / 3
    df['_tpv']  = df['_tp'] * df['Volume'].clip(lower=1)
    df['VWAP']  = (df.groupby('_date')['_tpv'].cumsum()
                   / df.groupby('_date')['Volume'].transform('cumsum').clip(lower=1))
    df.drop(columns=['_date', '_tp', '_tpv'], inplace=True)
    return df


# ── Data loading ─────────────────────────────────────────────────────────────
def load_data(instrument: str, date_from: Optional[date] = None,
              date_to: Optional[date] = None) -> pd.DataFrame:
    root   = DATA_ROOTS[instrument]
    prefix = FILE_PREFIXES[instrument]
    files  = sorted(f for f in os.listdir(root) if f.startswith(prefix) and f.endswith('.csv'))
    frames = []
    for fn in files:
        date_str = fn.replace(prefix, '').replace('.csv', '')   # YYYYMMDD
        try:
            d = datetime.strptime(date_str, '%Y%m%d').date()
        except ValueError:
            continue
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        df = pd.read_csv(os.path.join(root, fn))
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # Timestamp column is 'ts', parse with timezone then convert to naive IST
    df['Datetime'] = pd.to_datetime(df['ts'], utc=False).dt.tz_localize(None)
    df = df.drop(columns=['ts']).sort_values('Datetime').set_index('Datetime')
    if 'Volume' not in df.columns:
        df['Volume'] = 1_000_000
    add_adx(df)
    add_vwap(df)
    return df


# ── Core simulation ──────────────────────────────────────────────────────────
def simulate_day(day_df: pd.DataFrame, instrument: str, day_date: date) -> Optional[dict]:
    """
    Simulate one trading day for Path A (ORB).
    Returns a result dict or None if no entry.
    """
    iv      = BASE_IV[instrument]
    lot     = LOT_SIZES[instrument]
    day_str = day_date.strftime('%a')

    bars = list(day_df.itertuples())
    if len(bars) < ORB_BARS + 2:
        return None

    # ── Build opening range ──────────────────────────────────────────────────
    or_bars = [b for b in bars if b.Index.time() < dtime(9, 45)][:ORB_BARS]
    if len(or_bars) < ORB_BARS:
        return None
    or_high = max(b.High for b in or_bars)
    or_low  = min(b.Low  for b in or_bars)
    or_width = (or_high - or_low) / or_low

    width_max = OR_WIDTH_MAX.get(day_str)
    if width_max is not None and or_width > width_max:
        return None  # width gate

    adx_min = DAY_ADX_MIN.get(day_str, ADX_MIN)

    # ── Scan for entry ───────────────────────────────────────────────────────
    entry = None
    entry_bar_idx = None
    for i, bar in enumerate(bars):
        t = bar.Index.time()
        if not (ORB_ENTRY_START <= t < ORB_ENTRY_END):
            continue
        adx = getattr(bar, 'ADX', float('nan'))
        if pd.isna(adx) or adx < adx_min:
            continue
        px = bar.Close

        call_break = px > or_high * (1 + ORB_BUFFER)
        put_break  = px < or_low  * (1 - ORB_BUFFER)
        if not call_break and not put_break:
            continue
        sig = 'CALL' if call_break else 'PUT'

        if sig == 'CALL' and day_str in NO_CALL_DAYS:
            continue
        vwap = getattr(bar, 'VWAP', float('nan'))
        if not pd.isna(vwap) and vwap > 0:
            if sig == 'CALL' and px < vwap:
                continue
            if sig == 'PUT' and px > vwap:
                continue

        # Entry found
        K = round(px / 100) * 100  # ATM strike
        entry_px = est_option_px(px, K, sig, 0, iv)
        if entry_px < 1:
            entry_px = max(abs(px - K) + 10, 20)  # floor for deep ITM/OTM edge cases
        entry = {
            'bar_idx': i,
            'time'   : t,
            'sig'    : sig,
            'px_idx' : px,
            'strike' : K,
            'entry_opt': entry_px,
            'adx'    : adx,
            'or_width': or_width,
        }
        entry_bar_idx = i
        break

    if entry is None:
        return None

    # ── Simulate BOTH strategies from entry ──────────────────────────────────
    def run_strategy(use_handover: bool) -> dict:
        """
        use_handover=False -> force-close at 11:30 (old ORB bot)
        use_handover=True  -> trail to 14:30 (unified paper_bot)
        """
        pos = {
            'trailing': False,
            'trail_high': entry['entry_opt'],
            'peak_opt'  : entry['entry_opt'],
        }
        stop_px  = entry['entry_opt'] * (1 - STOP)
        tgt_px   = entry['entry_opt'] * (1 + TARGET)
        trail_fl = None  # trailing stop floor
        close_time = HANDOVER_TIME if not use_handover else EOD_CLOSE
        exit_reason = None
        exit_opt    = None
        exit_t      = None
        at_handover_opt = None  # option price AT 11:30

        bars_elapsed = 0
        for i in range(entry_bar_idx + 1, len(bars)):
            bar = bars[i]
            t   = bar.Index.time()
            bars_elapsed += 1

            # Estimate current option price
            S   = bar.Close
            K   = entry['strike']
            sig = entry['sig']
            opt = est_option_px(S, K, sig, bars_elapsed, iv)

            # Record value at handover time
            if t >= HANDOVER_TIME and at_handover_opt is None:
                at_handover_opt = opt

            # Force-close at strategy boundary
            if not use_handover and t >= HANDOVER_TIME:
                exit_opt = opt; exit_t = t; exit_reason = 'Handover(11:30)'
                break

            if t >= close_time:
                exit_opt = opt; exit_t = t; exit_reason = 'EOD'
                break

            # Update trailing
            if opt > pos['peak_opt']:
                pos['peak_opt'] = opt
            gain_pct = (opt - entry['entry_opt']) / entry['entry_opt']
            if not pos['trailing'] and gain_pct >= TRAIL_ACT:
                pos['trailing'] = True
            if pos['trailing']:
                trail_fl = pos['peak_opt'] * (1 - TRAIL_DIST)
                if opt <= trail_fl:
                    exit_opt = opt; exit_t = t; exit_reason = 'Trail'
                    break

            # Stop / target
            if opt <= stop_px:
                exit_opt = opt; exit_t = t; exit_reason = 'Stop'
                break
            if opt >= tgt_px:
                exit_opt = opt; exit_t = t; exit_reason = 'Target'
                break

        if exit_opt is None:
            exit_opt = est_option_px(bars[-1].Close, entry['strike'],
                                     entry['sig'], len(bars)-entry_bar_idx-1, iv)
            exit_t = bars[-1].Index.time()
            exit_reason = 'EOD'

        pnl_pct = (exit_opt - entry['entry_opt']) / entry['entry_opt']
        pnl_rs  = pnl_pct * entry['entry_opt'] * lot
        return {
            'exit_reason': exit_reason,
            'exit_t'     : exit_t,
            'exit_opt'   : round(exit_opt, 2),
            'pnl_pct'    : round(pnl_pct * 100, 1),
            'pnl_rs'     : round(pnl_rs, 0),
            'at_handover': round(at_handover_opt, 2) if at_handover_opt else None,
        }

    strat_a = run_strategy(use_handover=False)   # force-close 11:30
    strat_b = run_strategy(use_handover=True)    # trail to 14:30

    # Status at 11:30
    at_ho = strat_a.get('at_handover') or strat_a['exit_opt']
    profitable_at_ho = at_ho > entry['entry_opt']

    return {
        'date'        : str(day_date),
        'instrument'  : instrument,
        'sig'         : entry['sig'],
        'entry_t'     : str(entry['time']),
        'entry_opt'   : round(entry['entry_opt'], 2),
        'adx'         : round(entry['adx'], 1),
        'or_width_pct': round(entry['or_width'] * 100, 3),
        'at_handover' : round(at_ho, 2),
        'profit_at_ho': profitable_at_ho,
        'ho_gain_pct' : round((at_ho - entry['entry_opt']) / entry['entry_opt'] * 100, 1),
        'strategy_A'  : strat_a,  # force-close 11:30
        'strategy_B'  : strat_b,  # trail to 14:30
        'edge_B_over_A': round(strat_b['pnl_rs'] - strat_a['pnl_rs'], 0),  # positive = B better
    }


# ── Analysis ─────────────────────────────────────────────────────────────────
def analyse(results: list[dict], instrument: str) -> None:
    if not results:
        print(f"\n{instrument}: No entries found.")
        return

    total = len(results)
    wins_a = sum(1 for r in results if r['strategy_A']['pnl_rs'] > 0)
    wins_b = sum(1 for r in results if r['strategy_B']['pnl_rs'] > 0)
    pnl_a  = sum(r['strategy_A']['pnl_rs'] for r in results)
    pnl_b  = sum(r['strategy_B']['pnl_rs'] for r in results)

    profitable = [r for r in results if r['profit_at_ho']]
    losing     = [r for r in results if not r['profit_at_ho']]

    def pnl_comparison(subset: list[dict], label: str) -> None:
        if not subset:
            print(f"    {label}: 0 trades")
            return
        n = len(subset)
        pA = sum(r['strategy_A']['pnl_rs'] for r in subset)
        pB = sum(r['strategy_B']['pnl_rs'] for r in subset)
        wA = sum(1 for r in subset if r['strategy_A']['pnl_rs'] > 0)
        wB = sum(1 for r in subset if r['strategy_B']['pnl_rs'] > 0)
        avg_edge = sum(r['edge_B_over_A'] for r in subset) / n
        b_better = sum(1 for r in subset if r['edge_B_over_A'] > 0)
        reasons_b = {}
        for r in subset:
            rr = r['strategy_B']['exit_reason']
            reasons_b[rr] = reasons_b.get(rr, 0) + 1
        print(f"    {label} ({n} trades):")
        print(f"      Force-close 11:30: {wA}/{n} wins ({wA/n*100:.0f}%) | Net Rs{pA:,.0f}")
        print(f"      Handover trail:    {wB}/{n} wins ({wB/n*100:.0f}%) | Net Rs{pB:,.0f}")
        print(f"      Edge B over A: avg Rs{avg_edge:,.0f}/trade | B better {b_better}/{n} days ({b_better/n*100:.0f}%)")
        print(f"      Handover exits: {dict(sorted(reasons_b.items(), key=lambda x: -x[1]))}")

    print(f"\n{'='*64}")
    print(f"  {instrument}  ORB Handover Backtest  ({total} entries)")
    print(f"{'='*64}")
    print(f"  ALL ENTRIES:")
    print(f"    Force-close 11:30: {wins_a}/{total} wins ({wins_a/total*100:.0f}%) | Net Rs{pnl_a:,.0f}")
    print(f"    Handover trail:    {wins_b}/{total} wins ({wins_b/total*100:.0f}%) | Net Rs{pnl_b:,.0f}")
    print(f"    Net edge of handover: Rs{pnl_b - pnl_a:,.0f}")
    print()
    print(f"  SPLIT BY STATUS AT 11:30:")
    pnl_comparison(profitable, f"Profitable at 11:30 ({len(profitable)} trades)")
    pnl_comparison(losing,     f"Losing at 11:30 ({len(losing)} trades)")

    print()
    print("  RECOMMENDATION:")
    if pnl_b > pnl_a and len(profitable) > 0:
        ho_pnl_gain = sum(r['edge_B_over_A'] for r in profitable)
        if ho_pnl_gain > 0:
            print(f"  -> HANDOVER profitable positions: adds Rs{ho_pnl_gain:,.0f} ({len(profitable)} trades)")
            pct_b_better_in_profitable = sum(1 for r in profitable if r['edge_B_over_A'] > 0) / len(profitable)
            if pct_b_better_in_profitable >= 0.55:
                print(f"    Handover beats force-close in {pct_b_better_in_profitable*100:.0f}% of profitable days -> ACTIVATE")
            else:
                print(f"    Handover beats force-close in only {pct_b_better_in_profitable*100:.0f}% of profitable days -> MONITOR")
        else:
            print(f"  -> Force-close beats handover even for profitable positions")
    else:
        print(f"  -> Force-close 11:30 is better overall")


# ── Late ORB simulation (11:00–12:00, tighter gates) ─────────────────────────
LATE_ORB_START = dtime(11, 0)
LATE_ORB_END   = dtime(12, 0)
LATE_ADX_MIN   = 35      # matches config.PATH_A_LATE_ADX_MIN
LATE_STR_MIN   = 2       # matches config.PATH_A_LATE_MIN_STRENGTH


def simulate_late_entry(day_df: pd.DataFrame, instrument: str,
                        day_date: date) -> Optional[dict]:
    """
    Simulate late ORB entry (11:00–12:00).
    Only fires when:
      1. OR survived intact through 9:30–11:00 (no early breakout that day)
      2. ADX ≥ 35 AND strength ≥ 2 at entry bar
    Exit: stop/target/trail to 14:30 (full runway, no 11:30 handover question).
    """
    iv      = BASE_IV[instrument]
    lot     = LOT_SIZES[instrument]
    day_str = day_date.strftime('%a')

    bars = list(day_df.itertuples())
    if len(bars) < ORB_BARS + 2:
        return None

    # ── Build opening range (same logic as early ORB) ───────────────────────
    or_bars = [b for b in bars if b.Index.time() < dtime(9, 45)][:ORB_BARS]
    if len(or_bars) < ORB_BARS:
        return None
    or_high  = max(b.High for b in or_bars)
    or_low   = min(b.Low  for b in or_bars)
    or_width = (or_high - or_low) / or_low

    width_max = OR_WIDTH_MAX.get(day_str)
    if width_max is not None and or_width > width_max:
        return None  # same width gate as early window

    # ── Gate: OR must have survived intact through 9:30–11:00 ───────────────
    # If early ORB fired, we already captured the move — skip for late sim.
    for bar in bars:
        t = bar.Index.time()
        if not (ORB_ENTRY_START <= t < LATE_ORB_START):
            continue
        px = bar.Close
        if px > or_high * (1 + ORB_BUFFER) or px < or_low * (1 - ORB_BUFFER):
            return None  # OR broken early; late entry not applicable

    # ── Scan 11:00–12:00 for late breakout ──────────────────────────────────
    entry         = None
    entry_bar_idx = None
    for i, bar in enumerate(bars):
        t = bar.Index.time()
        if not (LATE_ORB_START <= t < LATE_ORB_END):
            continue

        adx  = getattr(bar, 'ADX',  float('nan'))
        vwap = getattr(bar, 'VWAP', float('nan'))
        px   = bar.Close

        if pd.isna(adx) or adx < LATE_ADX_MIN:
            continue

        call_break = px > or_high * (1 + ORB_BUFFER)
        put_break  = px < or_low  * (1 - ORB_BUFFER)
        if not call_break and not put_break:
            continue
        sig = 'CALL' if call_break else 'PUT'

        if sig == 'CALL' and day_str in NO_CALL_DAYS:
            continue
        if not pd.isna(vwap) and vwap > 0:
            if sig == 'CALL' and px < vwap:
                continue
            if sig == 'PUT' and px > vwap:
                continue

        # Strength scoring — mirrors options_bot.py get_path_a_signal()
        strength = 0
        if adx >= 35:
            strength += 1
        break_pct = ((px - or_high) / or_high if sig == 'CALL'
                     else (or_low - px) / or_low)
        if break_pct > 0.001:
            strength += 1
        if not pd.isna(vwap) and vwap > 0 and abs(px - vwap) / vwap >= 0.001:
            strength += 1

        if strength < LATE_STR_MIN:
            continue  # late window requires ≥2 strength

        K        = round(px / 100) * 100
        entry_px = est_option_px(px, K, sig, 0, iv)
        if entry_px < 1:
            entry_px = max(abs(px - K) + 10, 20)

        entry = {
            'bar_idx'  : i,
            'time'     : t,
            'sig'      : sig,
            'px_idx'   : px,
            'strike'   : K,
            'entry_opt': entry_px,
            'adx'      : adx,
            'strength' : strength,
            'or_width' : or_width,
        }
        entry_bar_idx = i
        break

    if entry is None:
        return None

    # ── Simulate exit from late entry → 14:30 ───────────────────────────────
    stop_px     = entry['entry_opt'] * (1 - STOP)
    tgt_px      = entry['entry_opt'] * (1 + TARGET)
    pos         = {'trailing': False, 'peak_opt': entry['entry_opt']}
    exit_reason = None
    exit_opt    = None
    exit_t      = None

    bars_elapsed = 0
    for i in range(entry_bar_idx + 1, len(bars)):
        bar = bars[i]
        t   = bar.Index.time()
        bars_elapsed += 1

        S   = bar.Close
        K   = entry['strike']
        opt = est_option_px(S, K, entry['sig'], bars_elapsed, iv)

        if t >= EOD_CLOSE:
            exit_opt = opt; exit_t = t; exit_reason = 'EOD'
            break

        if opt > pos['peak_opt']:
            pos['peak_opt'] = opt
        gain_pct = (opt - entry['entry_opt']) / entry['entry_opt']
        if not pos['trailing'] and gain_pct >= TRAIL_ACT:
            pos['trailing'] = True
        if pos['trailing']:
            trail_fl = pos['peak_opt'] * (1 - TRAIL_DIST)
            if opt <= trail_fl:
                exit_opt = opt; exit_t = t; exit_reason = 'Trail'
                break

        if opt <= stop_px:
            exit_opt = opt; exit_t = t; exit_reason = 'Stop'
            break
        if opt >= tgt_px:
            exit_opt = opt; exit_t = t; exit_reason = 'Target'
            break

    if exit_opt is None:
        exit_opt = est_option_px(bars[-1].Close, entry['strike'],
                                 entry['sig'], len(bars)-entry_bar_idx-1, iv)
        exit_t      = bars[-1].Index.time()
        exit_reason = 'EOD'

    pnl_pct = (exit_opt - entry['entry_opt']) / entry['entry_opt']
    pnl_rs  = pnl_pct * entry['entry_opt'] * lot

    return {
        'date'        : str(day_date),
        'instrument'  : instrument,
        'sig'         : entry['sig'],
        'entry_t'     : str(entry['time']),
        'entry_opt'   : round(entry['entry_opt'], 2),
        'adx'         : round(entry['adx'], 1),
        'strength'    : entry['strength'],
        'or_width_pct': round(entry['or_width'] * 100, 3),
        'exit_reason' : exit_reason,
        'exit_t'      : str(exit_t),
        'pnl_pct'     : round(pnl_pct * 100, 1),
        'pnl_rs'      : round(pnl_rs, 0),
    }


def analyse_late(results: list[dict], instrument: str) -> None:
    if not results:
        print(f"\n  {instrument} Late ORB: No entries — OR broke early every day")
        return
    total = len(results)
    wins  = sum(1 for r in results if r['pnl_rs'] > 0)
    pnl   = sum(r['pnl_rs'] for r in results)
    exits: dict = {}
    for r in results:
        exits[r['exit_reason']] = exits.get(r['exit_reason'], 0) + 1

    calls = [r for r in results if r['sig'] == 'CALL']
    puts  = [r for r in results if r['sig'] == 'PUT']
    cw    = sum(1 for r in calls if r['pnl_rs'] > 0)
    pw    = sum(1 for r in puts  if r['pnl_rs'] > 0)

    sep = '-' * 56
    print(f"\n  {sep}")
    print(f"  {instrument} LATE ORB (11:00-12:00, ADX>={LATE_ADX_MIN}, str>={LATE_STR_MIN})"
          f"  --  {total} entries")
    print(f"    Win-rate  : {wins}/{total} ({wins/total*100:.0f}%)")
    print(f"    Net P&L   : Rs{pnl:,.0f}  |  avg Rs{pnl/total:,.0f}/trade")
    if calls:
        print(f"    CALL      : {cw}/{len(calls)} ({cw/len(calls)*100:.0f}%)")
    if puts:
        print(f"    PUT       : {pw}/{len(puts)} ({pw/len(puts)*100:.0f}%)")
    print(f"    Exits     : {dict(sorted(exits.items(), key=lambda x: -x[1]))}")
    print(f"  {sep}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    instruments = [a for a in args if a in DATA_ROOTS]
    if not instruments:
        instruments = list(DATA_ROOTS.keys())

    date_from = None
    for a in args:
        if a.startswith('--from'):
            try:
                date_from = datetime.strptime(a.split('=')[1], '%Y-%m-%d').date()
            except Exception:
                pass

    if date_from is None:
        # Default: last 200 trading days
        date_from = date(2025, 7, 1)

    print(f"ORB Handover Backtest  |  from {date_from}  |  instruments: {instruments}")
    print(f"Path A config: ADX>={ADX_MIN}(global) | Stop {STOP*100:.0f}% | Target {TARGET*100:.0f}% "
          f"| Trail from +{TRAIL_ACT*100:.0f}% dist {TRAIL_DIST*100:.0f}%")
    print(f"Strategy A = force-close at 11:30  |  Strategy B = trail to 14:30 (handover)")

    all_results = {}
    for inst in instruments:
        print(f"\nLoading {inst}...", end=' ', flush=True)
        df = load_data(inst, date_from=date_from)
        if df.empty:
            print("no data"); continue
        print(f"{len(df)} bars loaded")

        results = []
        for day_date, day_df in df.groupby(df.index.date):
            r = simulate_day(day_df, inst, day_date)
            if r:
                results.append(r)

        all_results[inst] = results
        analyse(results, inst)

        # ── Late ORB extension analysis ──────────────────────────────────────
        late_results = []
        for day_date, day_df in df.groupby(df.index.date):
            r = simulate_late_entry(day_df, inst, day_date)
            if r:
                late_results.append(r)
        analyse_late(late_results, inst)

    # Combined summary
    all_flat = [r for rlist in all_results.values() for r in rlist]
    if len(instruments) > 1 and all_flat:
        pnl_a_total = sum(r['strategy_A']['pnl_rs'] for r in all_flat)
        pnl_b_total = sum(r['strategy_B']['pnl_rs'] for r in all_flat)
        profitable_all = [r for r in all_flat if r['profit_at_ho']]
        ho_gain = sum(r['edge_B_over_A'] for r in profitable_all) if profitable_all else 0
        print(f"\n{'='*64}")
        print(f"  COMBINED ({len(all_flat)} entries across {len(instruments)} instruments)")
        print(f"  Force-close 11:30 total: Rs{pnl_a_total:,.0f}")
        print(f"  Handover trail total:    Rs{pnl_b_total:,.0f}")
        print(f"  Net edge of handover (all): Rs{pnl_b_total - pnl_a_total:,.0f}")
        print(f"  Net edge on profitable-at-11:30 only: Rs{ho_gain:,.0f} ({len(profitable_all)} trades)")
        print(f"{'='*64}")

    # Save raw results
    out_path = os.path.join(os.path.dirname(__file__), 'orb_handover_results.json')
    with open(out_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nRaw results saved -> {out_path}")


if __name__ == '__main__':
    main()
