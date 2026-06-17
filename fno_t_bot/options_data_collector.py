# -*- coding: utf-8 -*-
"""
NIFTY Options Data Collector
=============================
Fetches real 5-min OHLCV data for NIFTY options contracts from Fyers API
and saves them as CSVs in data/nifty_options/.

Why this matters
----------------
The backtest currently uses Black-Scholes to estimate option prices.
BS gets several things wrong:
  - Ignores IV skew (puts have ~5-15% higher IV than calls on NIFTY)
  - Ignores IV crush (IV drops after events, hurting long options)
  - Ignores liquidity gaps and wide spreads in deep OTM options
  - Misestimates gamma for very near-expiry contracts

Real options OHLCV data lets us:
  - Price entries/exits accurately
  - Compute actual MFE/MAE (using High/Low in each bar)
  - Measure true theta decay rate
  - Calibrate and quantify BS pricing error

Collection strategy
-------------------
Two modes:

  targeted   (default/recommended)
    - Loads backtest_trades.csv to find the exact (date, strike, expiry, type)
      for each trade the strategy generated
    - Fetches only those specific contracts + ATM±1 strike neighbors
    - ~100-300 API calls for a full 13-month backtest
    - Requires: backtest_trades.csv (run bot.py first)

  full
    - For every trading day in the NIFTY spot CSV database,
      computes the ATM strike during the entry window (11:00-14:45)
    - Fetches all unique (expiry, strike, CE/PE) combinations
    - More comprehensive but more API calls (~500-1000)
    - Good for building a broader options price database

Fyers data depth for options: ~90-100 days of 5-min history.
Run this script whenever you have a fresh token (daily after market).

Usage
-----
  python fyers_auth.py                          # get fresh token first
  python options_data_collector.py targeted     # fetch only traded contracts
  python options_data_collector.py full         # fetch all ATM contracts
  python options_data_collector.py summary      # show what's been collected

Symbol format: NSE:NIFTY{DDMMMYY}{STRIKE}{CE/PE}
  e.g.         NSE:NIFTY27FEB2625000CE
"""

import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
import pytz

import config
from fyers_auth import FyersAuth
from nse_holidays import is_market_open_today

# ─── Constants ────────────────────────────────────────────────────────────────

IST          = pytz.timezone('Asia/Kolkata')
SPOT_DIR     = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty_5min')
OPTIONS_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty_options')
TRADES_CSV   = os.path.join(os.path.dirname(__file__), '..', 'backtest_trades.csv')

STRIKE_GAP   = 50       # NIFTY strike gap
MIN_DTE      = 2        # minimum days-to-expiry (matches config)
API_SLEEP    = 0.35     # seconds between API calls (Fyers rate limit ~3/sec)


# ─── Fyers Client ─────────────────────────────────────────────────────────────

def _get_fyers():
    token_file = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(token_file):
        print("No token found. Run: python fyers_auth.py")
        return None
    with open(token_file) as f:
        lines = f.read().strip().split('\n')
    if len(lines) < 2:
        print("Token file corrupted.")
        return None
    token      = lines[0]
    token_date = datetime.fromisoformat(lines[1]).date()
    if token_date != datetime.now(IST).date():
        print("Token expired. Run: python fyers_auth.py")
        return None
    auth = FyersAuth()
    auth.access_token = token
    return auth.get_fyers_client()


# ─── Symbol Helpers ───────────────────────────────────────────────────────────

def expiry_for_date(trade_date: date) -> date:
    """
    Find the NIFTY weekly expiry (Thursday) with >= MIN_DTE days remaining
    from trade_date — mirrors the live bot's get_next_expiry() logic.
    """
    check = trade_date
    for _ in range(14):
        if check.weekday() == 3:          # Thursday
            if (check - trade_date).days >= MIN_DTE:
                return check
        check += timedelta(days=1)
    raise RuntimeError(f"Cannot find expiry for {trade_date}")


def atm_strike(spot: float) -> int:
    return int(round(spot / STRIKE_GAP) * STRIKE_GAP)


def fyers_symbol(expiry: date, strike: int, opt_type: str) -> str:
    """Build Fyers options symbol string. e.g. NSE:NIFTY27FEB2625000CE"""
    exp_str  = expiry.strftime('%d%b%y').upper()
    suffix   = 'CE' if opt_type == 'CALL' else 'PE'
    return f"NSE:NIFTY{exp_str}{strike}{suffix}"


def csv_filename(expiry: date, strike: int, opt_type: str) -> str:
    """Local filename for a specific options contract."""
    exp_str = expiry.strftime('%Y%m%d')
    suffix  = 'CE' if opt_type == 'CALL' else 'PE'
    return f"NIFTY_{exp_str}_{strike}_{suffix}.csv"


def csv_path(expiry: date, strike: int, opt_type: str) -> str:
    return os.path.join(OPTIONS_DIR, csv_filename(expiry, strike, opt_type))


# ─── Fetch One Options Contract ───────────────────────────────────────────────

def fetch_contract(fyers, symbol: str, from_date: date, to_date: date) -> pd.DataFrame | None:
    """
    Fetch 5-min OHLCV for one options contract from Fyers.
    Returns a DataFrame or None if no data.
    """
    resp = fyers.history({
        "symbol"     : symbol,
        "resolution" : "5",
        "date_format": "1",
        "range_from" : from_date.strftime('%Y-%m-%d'),
        "range_to"   : to_date.strftime('%Y-%m-%d'),
        "cont_flag"  : "1",
    })

    if resp.get('s') != 'ok':
        msg = resp.get('message', str(resp))
        # "no data" is expected for expired contracts before data depth
        if 'no data' in msg.lower() or 'data not found' in msg.lower():
            return None
        print(f"    API error for {symbol}: {msg}")
        return None

    candles = resp.get('candles', [])
    if not candles:
        return None

    df = pd.DataFrame(candles, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert(IST)
    df.set_index('ts', inplace=True)
    df = df.between_time('09:15', '15:30')
    return df if len(df) > 0 else None


def save_contract(df: pd.DataFrame, expiry: date, strike: int, opt_type: str) -> str:
    os.makedirs(OPTIONS_DIR, exist_ok=True)
    path = csv_path(expiry, strike, opt_type)
    df.to_csv(path)
    return path


# ─── Mode 1: Targeted Collection ─────────────────────────────────────────────

def collect_targeted(neighbors: int = 1) -> None:
    """
    Fetch options data only for contracts the strategy actually traded,
    plus ATM±neighbors strikes for robustness.

    Requires: backtest_trades.csv (run: python bot.py)

    Parameters
    ----------
    neighbors : int
        Number of adjacent strikes to also fetch (1 = ATM±50 = 3 strikes total)
    """
    if not os.path.exists(TRADES_CSV):
        print(f"No trades CSV found at {TRADES_CSV}.")
        print("Run: python bot.py   (generates backtest_trades.csv)")
        return

    trades = pd.read_csv(TRADES_CSV, parse_dates=['Entry Date', 'Exit Date'])
    if len(trades) == 0:
        print("backtest_trades.csv is empty — no trades to process.")
        return

    # Filter to NIFTY only and full exits (skip partial exit rows if present)
    if 'Instrument' in trades.columns:
        trades = trades[trades['Instrument'] == 'NIFTY']
    trades = trades[trades.get('Exit Reason', pd.Series([''] * len(trades))) != 'Partial 80%'] \
        if 'Exit Reason' in trades.columns else trades

    print(f"\nNIFTY Options Data Collector — TARGETED mode")
    print(f"  Trades loaded    : {len(trades)}")
    print(f"  Strike neighbors : ±{neighbors} (={2*neighbors+1} strikes per trade)")
    print("=" * 60)

    fyers = _get_fyers()
    if not fyers:
        return

    # Build the set of (expiry, strike, opt_type) to fetch
    to_fetch = set()
    for _, row in trades.iterrows():
        entry_date = row['Entry Date'].date() if hasattr(row['Entry Date'], 'date') \
            else pd.Timestamp(row['Entry Date']).date()
        strike_atm = int(row['Strike']) if 'Strike' in row and not pd.isna(row['Strike']) \
            else atm_strike(row['Entry Price'] if 'Entry Price' in row else 0)
        opt_type   = row['Type']   # 'CALL' or 'PUT'
        expiry     = expiry_for_date(entry_date)

        # Add ATM and neighbors
        for offset in range(-neighbors, neighbors + 1):
            s = strike_atm + offset * STRIKE_GAP
            to_fetch.add((expiry, s, opt_type))
            # Also fetch the opposite type for analysis (understand the other side)
            other = 'PUT' if opt_type == 'CALL' else 'CALL'
            to_fetch.add((expiry, s, other))

    print(f"  Unique contracts : {len(to_fetch)}")

    fetched  = 0
    skipped  = 0
    failed   = 0

    for i, (expiry, strike, opt_type) in enumerate(sorted(to_fetch), 1):
        dest = csv_path(expiry, strike, opt_type)
        if os.path.exists(dest):
            skipped += 1
            continue

        sym = fyers_symbol(expiry, strike, opt_type)
        # Fetch from 7 days before expiry to expiry day (captures the full life of 2-day contracts)
        from_d = expiry - timedelta(days=7)
        to_d   = expiry

        print(f"  [{i}/{len(to_fetch)}] {sym} ({from_d} to {expiry})...", end=' ', flush=True)
        df = fetch_contract(fyers, sym, from_d, to_d)

        if df is not None and len(df) > 0:
            save_contract(df, expiry, strike, opt_type)
            print(f"✓ {len(df)} bars saved")
            fetched += 1
        else:
            print("✗ no data (contract may be outside Fyers depth)")
            failed += 1

        time.sleep(API_SLEEP)

    print(f"\nDone — fetched: {fetched} | skipped: {skipped} | failed: {failed}")
    print(f"Options data saved to: {os.path.abspath(OPTIONS_DIR)}")


# ─── Mode 2: Full ATM Collection ─────────────────────────────────────────────

def collect_full(days_back: int = 100) -> None:
    """
    For every trading day in the NIFTY spot CSV database (up to days_back),
    fetch all unique ATM strikes encountered during the entry window.

    This builds a broader options database beyond just traded contracts.

    Parameters
    ----------
    days_back : int
        How far back to go. Fyers options depth is ~90-100 days.
    """
    if not os.path.exists(SPOT_DIR):
        print(f"No NIFTY spot data found at {SPOT_DIR}.")
        print("Run: python data_collector.py backfill 400")
        return

    print(f"\nNIFTY Options Data Collector — FULL mode ({days_back} days back)")
    print("=" * 60)

    # Load spot data
    cutoff    = date.today() - timedelta(days=days_back)
    spot_files = sorted(f for f in os.listdir(SPOT_DIR) if f.endswith('.csv'))

    all_contracts = set()   # (expiry, strike, opt_type)

    print("  Scanning spot data for ATM strikes in entry window...")
    entry_start_h, entry_start_m = 11, 0
    entry_end_h,   entry_end_m   = 14, 45

    for fname in spot_files:
        try:
            d = datetime.strptime(fname, 'nifty_5min_%Y%m%d.csv').date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        if not is_market_open_today(d):
            continue

        try:
            df = pd.read_csv(os.path.join(SPOT_DIR, fname), index_col=0, parse_dates=True)
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize(IST)
            # Filter to entry window
            window = df.between_time(
                f'{entry_start_h:02d}:{entry_start_m:02d}',
                f'{entry_end_h:02d}:{entry_end_m:02d}'
            )
            if len(window) == 0:
                continue

            expiry = expiry_for_date(d)
            for close in window['Close']:
                s = atm_strike(close)
                all_contracts.add((expiry, s, 'CALL'))
                all_contracts.add((expiry, s, 'PUT'))
        except Exception as e:
            print(f"  Warning: {fname}: {e}")
            continue

    print(f"  Unique contracts identified: {len(all_contracts)}")

    fyers = _get_fyers()
    if not fyers:
        return

    fetched = skipped = failed = 0
    sorted_contracts = sorted(all_contracts)

    for i, (expiry, strike, opt_type) in enumerate(sorted_contracts, 1):
        dest = csv_path(expiry, strike, opt_type)
        if os.path.exists(dest):
            skipped += 1
            continue

        sym    = fyers_symbol(expiry, strike, opt_type)
        from_d = expiry - timedelta(days=7)
        to_d   = expiry

        print(f"  [{i}/{len(sorted_contracts)}] {sym}...", end=' ', flush=True)
        df = fetch_contract(fyers, sym, from_d, to_d)

        if df is not None and len(df) > 0:
            save_contract(df, expiry, strike, opt_type)
            print(f"✓ {len(df)} bars")
            fetched += 1
        else:
            print("✗ no data")
            failed += 1

        time.sleep(API_SLEEP)

    print(f"\nDone — fetched: {fetched} | skipped: {skipped} | failed: {failed}")
    print(f"Options data: {os.path.abspath(OPTIONS_DIR)}")


# ─── Load Options Price at Timestamp ─────────────────────────────────────────

def load_options_cache() -> dict:
    """
    Load all saved options CSVs into a dict keyed by (expiry, strike, opt_type).
    Returns: { (expiry_date, strike_int, 'CALL'/'PUT'): DataFrame }

    Used by bot.py to look up real options prices by timestamp.
    """
    cache = {}
    if not os.path.exists(OPTIONS_DIR):
        return cache

    for fname in os.listdir(OPTIONS_DIR):
        if not fname.endswith('.csv'):
            continue
        # NIFTY_20250227_25000_CE.csv
        parts = fname.replace('.csv', '').split('_')
        if len(parts) != 4 or parts[0] != 'NIFTY':
            continue
        try:
            exp    = datetime.strptime(parts[1], '%Y%m%d').date()
            strike = int(parts[2])
            otype  = 'CALL' if parts[3] == 'CE' else 'PUT'
            df     = pd.read_csv(
                os.path.join(OPTIONS_DIR, fname),
                index_col=0, parse_dates=True
            )
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize(IST)
            cache[(exp, strike, otype)] = df
        except Exception:
            continue

    return cache


def get_option_price(cache: dict, expiry: date, strike: int,
                     opt_type: str, timestamp) -> float | None:
    """
    Look up the real options Close price for a given timestamp.
    Falls back to the nearest available timestamp within 10 minutes.
    Returns None if not in cache.
    """
    key = (expiry, strike, opt_type)
    if key not in cache:
        return None

    df = cache[key]
    if timestamp in df.index:
        return float(df.loc[timestamp, 'Close'])

    # Nearest-timestamp fallback (±10 min)
    try:
        ts_idx  = pd.Timestamp(timestamp)
        nearest = df.index[abs(df.index - ts_idx).argmin()]
        if abs((nearest - ts_idx).total_seconds()) <= 600:
            return float(df.loc[nearest, 'Close'])
    except Exception:
        pass

    return None


def get_option_ohlc(cache: dict, expiry: date, strike: int,
                    opt_type: str, timestamp) -> dict | None:
    """
    Return full OHLC for a bar (useful for accurate MFE/MAE tracking).
    """
    key = (expiry, strike, opt_type)
    if key not in cache:
        return None

    df = cache[key]
    ts_idx = pd.Timestamp(timestamp)

    # Exact match first
    if ts_idx in df.index:
        row = df.loc[ts_idx]
        return {'O': row['Open'], 'H': row['High'], 'L': row['Low'], 'C': row['Close']}

    # Nearest within 10 min
    try:
        nearest = df.index[abs(df.index - ts_idx).argmin()]
        if abs((nearest - ts_idx).total_seconds()) <= 600:
            row = df.loc[nearest]
            return {'O': row['Open'], 'H': row['High'], 'L': row['Low'], 'C': row['Close']}
    except Exception:
        pass

    return None


# ─── Summary ─────────────────────────────────────────────────────────────────

def print_summary() -> None:
    """Print a summary of all options data collected so far."""
    if not os.path.exists(OPTIONS_DIR):
        print("No options data collected yet.")
        print("Run: python options_data_collector.py targeted")
        return

    files = [f for f in os.listdir(OPTIONS_DIR) if f.endswith('.csv')]
    if not files:
        print("Options directory exists but no CSV files found.")
        return

    expiries = set()
    strikes  = set()
    calls    = puts = total_bars = 0

    for fname in files:
        parts = fname.replace('.csv', '').split('_')
        if len(parts) != 4:
            continue
        try:
            expiries.add(datetime.strptime(parts[1], '%Y%m%d').date())
            strikes.add(int(parts[2]))
            if parts[3] == 'CE':
                calls += 1
            else:
                puts += 1
            df = pd.read_csv(os.path.join(OPTIONS_DIR, fname))
            total_bars += len(df)
        except Exception:
            continue

    print(f"\nNIFTY Options Database Summary")
    print(f"  Contracts  : {len(files)} ({calls} CE + {puts} PE)")
    print(f"  Expiries   : {len(expiries)} "
          f"({min(expiries)} to {max(expiries)})" if expiries else "")
    print(f"  Strikes    : {len(strikes)} "
          f"({min(strikes)} to {max(strikes)})" if strikes else "")
    print(f"  Total bars : {total_bars:,}  (5-min OHLCV)")
    print(f"  Directory  : {os.path.abspath(OPTIONS_DIR)}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'targeted'

    if cmd == 'targeted':
        neighbors = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        collect_targeted(neighbors)

    elif cmd == 'full':
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        collect_full(days)

    elif cmd == 'summary':
        print_summary()

    else:
        print(__doc__)
