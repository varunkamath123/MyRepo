# -*- coding: utf-8 -*-
"""
early_backtest.py — Backtest for the ORB (Opening Range Breakout) early session strategy.

Strategy mirrors early_bot.py exactly:
  - Opening Range : first 3 × 5-min bars (09:15–09:25)
  - Entry window  : 09:30–10:55 (force-close at 10:55)
  - Signal        : close > OR_high*(1+buf) → CALL  |  close < OR_low*(1-buf) → PUT
  - Confirm       : ADX ≥ EARLY_SESSION_ADX_MIN  +  VWAP alignment
  - Sizing        : 1 lot always (max 1 trade / day / instrument)
  - Stop          : 50%  |  Target : 150%  |  Trail : from 80%, distance 20%
  - Pricing       : Black-Scholes (same model as paper_bot)

Usage:
  python early_backtest.py [NIFTY | BANKNIFTY | SENSEX | ALL]
  python early_backtest.py             # defaults to ALL
  python early_backtest.py blind       # in-sample / out-of-sample split (NIFTY only)
"""

import io
import os
import sys
from datetime import datetime, timedelta, date

# Force UTF-8 output on Windows (handles ₹ symbol)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
from scipy.stats import norm
from ta.trend import ADXIndicator

sys.path.insert(0, os.path.dirname(__file__))
import config

IST            = pytz.timezone('Asia/Kolkata')
BARS_PER_DAY   = 75                 # 9:15–15:25, 5-min bars

# ─── Black-Scholes ────────────────────────────────────────────────────────────

def bs_price(opt_type: str, S: float, K: float,
             T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0) if opt_type == 'CALL' else max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == 'CALL':
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


# ─── Transaction costs ────────────────────────────────────────────────────────

def round_trip_costs(entry: float, exit_p: float, lot_size: int) -> float:
    buy_val  = entry   * lot_size
    sell_val = exit_p  * lot_size
    brokerage = config.BROKERAGE_PER_ORDER * 2
    exchange  = (buy_val + sell_val) * config.NSE_EXCHANGE_CHARGE_RATE
    sebi      = (buy_val + sell_val) * config.SEBI_CHARGES_RATE
    gst       = (brokerage + exchange + sebi) * config.GST_RATE
    stt       = sell_val * config.STT_RATE
    stamp     = buy_val  * config.STAMP_DUTY_RATE
    return brokerage + exchange + sebi + gst + stt + stamp


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_instrument_data(instrument: str) -> pd.DataFrame:
    """Load all 5-min CSV files for an instrument into one DataFrame."""
    folder_map = {
        'NIFTY'    : 'nifty_5min',
        'BANKNIFTY': 'banknifty_5min',
        'SENSEX'   : 'sensex_5min',
    }
    folder = folder_map.get(instrument.upper())
    if folder is None:
        raise ValueError(f'Unknown instrument: {instrument}')

    data_dir = Path(__file__).parent.parent / 'data' / folder
    if not data_dir.exists():
        raise FileNotFoundError(f'Data directory not found: {data_dir}')

    frames = []
    for csv_file in sorted(data_dir.glob('*.csv')):
        df = pd.read_csv(csv_file)
        # normalise timestamp column name (some files use 'Datetime')
        if 'ts' not in df.columns and 'Datetime' in df.columns:
            df = df.rename(columns={'Datetime': 'ts'})
        frames.append(df)

    if not frames:
        raise FileNotFoundError(f'No CSV files found in {data_dir}')

    all_df = pd.concat(frames, ignore_index=True)
    all_df['ts'] = pd.to_datetime(all_df['ts'], utc=True).dt.tz_convert(IST)
    all_df = all_df.sort_values('ts').reset_index(drop=True)

    # Rename for consistency
    all_df = all_df.rename(columns={'ts': 'Date'})
    all_df = all_df.set_index('Date')
    all_df = all_df[~all_df.index.duplicated(keep='last')]
    return all_df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ADX14 and per-day VWAP + HV to the full dataset."""
    df = df.copy()

    # ADX 14
    adx_ind  = ADXIndicator(df['High'], df['Low'], df['Close'], window=14)
    df['ADX'] = adx_ind.adx()

    # VWAP — reset each calendar day
    df['_date'] = df.index.date
    df['_tp']   = (df['High'] + df['Low'] + df['Close']) / 3
    df['_cum_tpv'] = df.groupby('_date').apply(
        lambda g: (g['_tp'] * g['Volume']).cumsum()
    ).reset_index(level=0, drop=True)
    df['_cum_vol'] = df.groupby('_date')['Volume'].cumsum()
    mask         = df['_cum_vol'] > 0
    df['VWAP']   = np.where(mask, df['_cum_tpv'] / df['_cum_vol'], df['_tp'])
    df           = df.drop(columns=['_date', '_tp', '_cum_tpv', '_cum_vol'])

    # HV — 30-bar rolling std annualised (same formula as bot.py)
    df['Returns'] = df['Close'].pct_change()
    df['HV']      = df['Returns'].rolling(30).std() * np.sqrt(252 * BARS_PER_DAY)
    df['HV']      = df['HV'].bfill().fillna(0.18)

    return df


# ─── Single-day simulation ─────────────────────────────────────────────────────

def simulate_day(day_df: pd.DataFrame, inst_cfg: dict,
                 adx_min: float) -> dict | None:
    """
    Simulate one trading day.
    Returns a trade dict (with exit_reason, pnl_net, etc.) or None if no trade.
    """
    lot_size   = inst_cfg['lot_size']
    strike_gap = inst_cfg['strike_gap']

    # Bar indices (09:15 = bar 0, …, each bar is 5 min)
    # Opening Range: bars 0, 1, 2  → times 09:15, 09:20, 09:25
    # First entry bar: index 3 → 09:30
    # Force close bar: index 20 → 09:15 + 20*5=100 min = 10:55

    OR_BARS       = config.EARLY_SESSION_ORB_BARS   # 3
    ENTRY_START_I = OR_BARS                           # bar 3 → 09:30
    FORCE_CLOSE_I = 20                                # bar 20 → 10:55
    ENTRY_END_I   = 21                                # bar 21 → 11:00 (exclusive)

    STOP   = config.EARLY_SESSION_STOP        # 0.50
    TARGET = config.EARLY_SESSION_TARGET      # 1.50
    T_ACT  = config.EARLY_SESSION_TRAIL_ACT   # 0.80
    T_DIST = config.EARLY_SESSION_TRAIL_DIST  # 0.20
    BUF    = config.EARLY_SESSION_ORB_BUFFER  # 0.0005
    DTE    = config.EARLY_SESSION_DAYS_TO_EXP # 2

    if len(day_df) < OR_BARS + 1:
        return None

    # Opening Range
    or_bars  = day_df.iloc[:OR_BARS]
    or_high  = float(or_bars['High'].max())
    or_low   = float(or_bars['Low'].min())

    call_trigger = or_high * (1 + BUF)
    put_trigger  = or_low  * (1 - BUF)

    # Scan bars from ENTRY_START_I to ENTRY_END_I
    signal      = None
    entry_bar_i = None

    for i in range(ENTRY_START_I, min(FORCE_CLOSE_I + 1, len(day_df))):
        row   = day_df.iloc[i]
        close = float(row['Close'])
        adx   = float(row.get('ADX', 0))
        vwap  = float(row.get('VWAP', float('nan')))

        if pd.isna(adx) or adx < adx_min:
            continue

        if close > call_trigger:
            if pd.isna(vwap) or close > vwap:
                signal = {'type': 'CALL', 'price': close,
                          'adx': adx, 'vwap': vwap, 'bar_i': i}
                entry_bar_i = i
                break
        elif close < put_trigger:
            if pd.isna(vwap) or close < vwap:
                signal = {'type': 'PUT', 'price': close,
                          'adx': adx, 'vwap': vwap, 'bar_i': i}
                entry_bar_i = i
                break

    if signal is None:
        return None

    # Entry option price (Black-Scholes)
    hv_entry   = float(day_df.iloc[entry_bar_i].get('HV', 0.18))
    underlying = signal['price']
    strike     = int(round(underlying / strike_gap) * strike_gap)
    T_entry    = DTE / 365
    entry_px   = bs_price(signal['type'], underlying, strike,
                          T_entry, config.RISK_FREE_RATE, hv_entry)

    if entry_px < config.MIN_OPTION_PRICE:
        return None   # option too cheap — same gate as early_bot

    # Track exit bar by bar
    highest_pnl_pct = 0.0
    exit_reason     = None
    exit_px         = entry_px
    exit_bar_i      = entry_bar_i

    MINS_PER_BAR    = 5
    MINS_PER_YEAR   = 365 * 24 * 60

    for j in range(entry_bar_i + 1, len(day_df)):
        row     = day_df.iloc[j]
        spot    = float(row['Close'])
        hv_now  = float(row.get('HV', hv_entry))

        elapsed_mins = (j - entry_bar_i) * MINS_PER_BAR
        T_rem = max(DTE - elapsed_mins / (24 * 60), 0.01) / 365

        cur_px  = bs_price(signal['type'], spot, strike, T_rem,
                           config.RISK_FREE_RATE, hv_now)
        pnl_pct = (cur_px - entry_px) / entry_px

        if pnl_pct > highest_pnl_pct:
            highest_pnl_pct = pnl_pct

        force = (j >= FORCE_CLOSE_I)

        if force:
            exit_reason = f'Force-Close (10:55)'
        elif pnl_pct <= -STOP:
            exit_reason = f'Stop-Loss ({STOP*100:.0f}%)'
        elif pnl_pct >= TARGET:
            exit_reason = f'Target ({TARGET*100:.0f}%)'
        elif (highest_pnl_pct >= T_ACT
              and pnl_pct < highest_pnl_pct - T_DIST):
            exit_reason = 'Trailing Stop'

        if exit_reason:
            exit_px    = cur_px
            exit_bar_i = j
            break

    # If we never hit an exit condition, exit at last bar (end of day)
    if exit_reason is None:
        j       = len(day_df) - 1
        row     = day_df.iloc[j]
        spot    = float(row['Close'])
        hv_now  = float(row.get('HV', hv_entry))
        elapsed_mins = (j - entry_bar_i) * MINS_PER_BAR
        T_rem = max(DTE - elapsed_mins / (24 * 60), 0.01) / 365
        exit_px     = bs_price(signal['type'], spot, strike, T_rem,
                               config.RISK_FREE_RATE, hv_now)
        exit_bar_i  = j
        exit_reason = 'EOD'

    # P&L
    costs   = round_trip_costs(entry_px, exit_px, lot_size)
    pnl_net = (exit_px - entry_px) * lot_size - costs
    pnl_pct_final = (exit_px - entry_px) / entry_px

    entry_time = day_df.index[entry_bar_i]
    exit_time  = day_df.index[exit_bar_i]

    return {
        'date'        : day_df.index[0].date(),
        'weekday'     : day_df.index[0].day_name()[:3],
        'entry_time'  : entry_time.strftime('%H:%M'),
        'exit_time'   : exit_time.strftime('%H:%M'),
        'type'        : signal['type'],
        'entry_bar'   : entry_bar_i,
        'or_high'     : round(or_high, 2),
        'or_low'      : round(or_low, 2),
        'or_width'    : round(or_high - or_low, 2),
        'or_width_pct': round((or_high - or_low) / or_low * 100, 3),
        'underlying'  : round(underlying, 2),
        'strike'      : strike,
        'adx'         : round(signal['adx'], 1),
        'vwap'        : round(signal['vwap'], 2) if not pd.isna(signal['vwap']) else None,
        'entry_price' : round(entry_px, 2),
        'exit_price'  : round(exit_px, 2),
        'pnl_pct'     : round(pnl_pct_final * 100, 2),
        'costs'       : round(costs, 2),
        'pnl_net'     : round(pnl_net, 2),
        'exit_reason' : exit_reason,
        'lot_size'    : lot_size,
    }


# ─── Instrument backtest ───────────────────────────────────────────────────────

def backtest_instrument(instrument: str, df_full: pd.DataFrame,
                        adx_min: float | None = None,
                        date_from: date | None = None,
                        date_to:   date | None = None) -> list[dict]:
    """Run the ORB backtest for one instrument across all days in df_full."""
    inst_cfg = config.INSTRUMENTS[instrument]
    adx_min  = adx_min or config.EARLY_SESSION_ADX_MIN

    # Filter date range
    if date_from:
        df_full = df_full[df_full.index.date >= date_from]
    if date_to:
        df_full = df_full[df_full.index.date <= date_to]

    trades = []
    for day, day_df in df_full.groupby(df_full.index.date):
        day_df = day_df.sort_index()
        # Full-day only (must have at least 21 bars = up to 10:55)
        if len(day_df) < 21:
            continue
        result = simulate_day(day_df, inst_cfg, adx_min)
        if result:
            result['instrument'] = instrument
            trades.append(result)

    return trades


# ─── Stats printer ────────────────────────────────────────────────────────────

def print_stats(trades: list[dict], label: str, capital: float = 50_000) -> None:
    if not trades:
        print(f'\n{label}: No trades.')
        return

    df   = pd.DataFrame(trades)
    n    = len(df)
    wins = (df['pnl_net'] > 0).sum()
    wr   = wins / n * 100
    net  = df['pnl_net'].sum()
    avg_w = df.loc[df['pnl_net'] > 0, 'pnl_net'].mean() if wins else 0
    avg_l = df.loc[df['pnl_net'] <= 0, 'pnl_net'].mean() if (n - wins) else 0
    pf    = (abs(avg_w * wins) / abs(avg_l * (n - wins))
             if (n - wins) > 0 and avg_l != 0 else float('inf'))

    # Max drawdown (sequential)
    cum = df['pnl_net'].cumsum()
    peak = cum.cummax()
    dd_pct = ((cum - peak) / capital * 100).min()

    # Sharpe (daily P&L over days traded)
    pnl_series = df.set_index('date')['pnl_net']
    sharpe = (pnl_series.mean() / pnl_series.std() * np.sqrt(252)
              if pnl_series.std() > 0 else 0)

    print(f'\n{"="*65}')
    print(f'  {label}')
    print(f'{"="*65}')
    print(f'  Trades    : {n}  |  Win Rate  : {wr:.1f}%  |  P/F: {pf:.2f}')
    print(f'  Net P&L   : ₹{net:>+10,.0f}  |  Sharpe: {sharpe:.2f}  |  Max DD: {dd_pct:.2f}%')
    print(f'  Avg Win   : ₹{avg_w:>+8,.0f}  |  Avg Loss: ₹{avg_l:>+8,.0f}  '
          f'|  W/L ratio: {abs(avg_w/avg_l):.2f}x' if avg_l else '')
    print()

    # By signal type
    for sig_type in ['CALL', 'PUT']:
        sub = df[df['type'] == sig_type]
        if sub.empty:
            continue
        sub_wins = (sub['pnl_net'] > 0).sum()
        print(f'  {sig_type:4s}: {len(sub):3d} trades | '
              f'WR {sub_wins/len(sub)*100:.1f}% | '
              f'Net ₹{sub["pnl_net"].sum():>+8,.0f}')

    # By exit reason
    print()
    for reason, grp in df.groupby('exit_reason'):
        g_wins = (grp['pnl_net'] > 0).sum()
        print(f'  Exit [{reason:30s}]: {len(grp):3d} trades | '
              f'WR {g_wins/len(grp)*100:.1f}% | '
              f'Net ₹{grp["pnl_net"].sum():>+8,.0f}')

    # By weekday
    print()
    for wd in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
        sub = df[df['weekday'] == wd]
        if sub.empty:
            continue
        sub_wins = (sub['pnl_net'] > 0).sum()
        print(f'  {wd}: {len(sub):3d} trades | '
              f'WR {sub_wins/len(sub)*100:.1f}% | '
              f'Net ₹{sub["pnl_net"].sum():>+8,.0f}')

    # By entry bar (hour)
    print()
    print('  Entry bar distribution:')
    df['entry_hour'] = df['entry_time'].str[:5]
    for t, grp in df.groupby('entry_hour'):
        g_wins = (grp['pnl_net'] > 0).sum()
        print(f'    {t}: {len(grp):3d} | WR {g_wins/len(grp)*100:.1f}% | '
              f'Net ₹{grp["pnl_net"].sum():>+8,.0f}')


def print_combined_stats(all_trades: list[dict]) -> None:
    """Print summary across all instruments."""
    if not all_trades:
        return
    df  = pd.DataFrame(all_trades)
    n   = len(df)
    wins = (df['pnl_net'] > 0).sum()
    net  = df['pnl_net'].sum()
    wr   = wins / n * 100
    capital = config.EARLY_SESSION_CAPITAL * len(df['instrument'].unique())

    print(f'\n{"="*65}')
    print(f'  COMBINED — All Instruments')
    print(f'{"="*65}')
    print(f'  Trades: {n}  |  Win Rate: {wr:.1f}%  |  Net P&L: ₹{net:>+,.0f}')
    print(f'  Capital: ₹{capital:,.0f}  |  Return: {net/capital*100:+.2f}%')
    print()
    for inst, grp in df.groupby('instrument'):
        g_wins = (grp['pnl_net'] > 0).sum()
        print(f'  {inst:10s}: {len(grp):3d} trades | '
              f'WR {g_wins/len(grp)*100:.1f}% | '
              f'Net ₹{grp["pnl_net"].sum():>+9,.0f}')


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    arg = sys.argv[1].upper() if len(sys.argv) > 1 else 'ALL'

    instruments = (
        ['NIFTY', 'BANKNIFTY', 'SENSEX'] if arg in ('ALL', '')
        else ['NIFTY'] if arg == 'BLIND'
        else [arg] if arg in config.INSTRUMENTS
        else None
    )
    if instruments is None:
        print(f'Usage: python early_backtest.py [NIFTY | BANKNIFTY | SENSEX | ALL | blind]')
        sys.exit(1)

    all_trades = []

    for instrument in instruments:
        print(f'\nLoading {instrument} data…', end=' ', flush=True)
        df_full = load_instrument_data(instrument)
        df_full = add_indicators(df_full)
        days = len(set(df_full.index.date))
        print(f'{days} days')
        print(f'  {days} trading days | {df_full.index[0].date()} to {df_full.index[-1].date()}')

        if arg == 'BLIND':
            # Blind / OOS split — same split date as bot.py
            split = date(2025, 10, 1)
            is_trades  = backtest_instrument(instrument, df_full, date_to=split - timedelta(days=1))
            oos_trades = backtest_instrument(instrument, df_full, date_from=split)
            for t in is_trades:  t['split'] = 'IS'
            for t in oos_trades: t['split'] = 'OOS'
            print_stats(is_trades,  f'BLIND — {instrument} IN-SAMPLE  (Jan 2025–Sep 2025)')
            print_stats(oos_trades, f'BLIND — {instrument} OUT-OF-SAMPLE (Oct 2025–Mar 2026)')
            all_trades.extend(is_trades + oos_trades)
        else:
            trades = backtest_instrument(instrument, df_full)
            all_trades.extend(trades)
            print_stats(trades, f'ORB Backtest — {instrument}')

    if arg != 'BLIND' and len(instruments) > 1:
        print_combined_stats(all_trades)

    # Save trades to CSV
    if all_trades:
        out_path = Path(__file__).parent.parent / 'early_backtest_trades.csv'
        pd.DataFrame(all_trades).to_csv(out_path, index=False)
        print(f'\nTrades saved → {out_path}')


if __name__ == '__main__':
    main()
