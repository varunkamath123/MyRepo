from __future__ import annotations
"""
Multi-Instrument 5-min Data Collector
Builds a local historical database from Fyers API for NIFTY, BANKNIFTY, SENSEX.

Fyers data availability for 5-min resolution:
  - Per-request limit : ~100 days
  - Total stored depth: ~400 days (varies; test with --probe)
  - Strategy          : fetch in 90-day chunks, stitch into local CSV DB

Usage
-----
  # Check how far back Fyers actually has data:
  python data_collector.py probe
  python data_collector.py probe BANKNIFTY

  # Seed up to 400 days via chunked requests (run once per instrument):
  python data_collector.py backfill 400              # NIFTY (default)
  python data_collector.py backfill BANKNIFTY 400
  python data_collector.py backfill SENSEX 400

  # Collect today's data (schedule at 15:35 IST via Task Scheduler):
  python data_collector.py                           # NIFTY
  python data_collector.py today BANKNIFTY
  python data_collector.py today SENSEX

  # Collect top-3 Nifty heavyweight stocks (for alignment filter):
  python data_collector.py stocks_backfill 400   # one-time seed
  python data_collector.py stocks_today          # daily update (run at 15:37)

  # Collect India VIX:
  python data_collector.py vix_backfill 400

  # Show local DB summary:
  python data_collector.py summary
  python data_collector.py summary BANKNIFTY

  # In your backtest / paper_bot:
  from data_collector import load_historical_data
  df = load_historical_data()                        # NIFTY (default)
  df = load_historical_data(instrument='BANKNIFTY')
"""

import os
import sys
import time
from datetime import datetime, date, timedelta

import pandas as pd
import pytz

import config
from fyers_auth import FyersAuth
from nse_holidays import is_market_open_today

IST = pytz.timezone('Asia/Kolkata')

# Legacy constant kept for backward compat (load_historical_data() with no args)
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty_5min')


# ─── Instrument-aware helpers ──────────────────────────────────────────────────

def _data_dir(instrument: str) -> str:
    return os.path.join(os.path.dirname(__file__), '..', 'data',
                        f"{instrument.lower()}_5min")


def _file_prefix(instrument: str) -> str:
    return f"{instrument.lower()}_5min"


def _index_symbol(instrument: str) -> str:
    inst = instrument.upper()
    if inst in config.INSTRUMENTS:
        return config.INSTRUMENTS[inst]['index_symbol']
    raise ValueError(f"Unknown instrument: {instrument!r}. "
                     f"Valid: {list(config.INSTRUMENTS.keys())}")


# ─── Fyers connection ─────────────────────────────────────────────────────────

def _get_fyers_client():
    token_file = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(token_file):
        print("No token found — run fyers_auth.py first.")
        return None

    with open(token_file) as f:
        lines = f.read().strip().split('\n')

    if len(lines) < 2:
        print("Token file corrupted.")
        return None

    token      = lines[0]
    token_date = datetime.fromisoformat(lines[1]).date()

    if token_date != datetime.now(IST).date():
        print("Token expired — run fyers_auth.py first.")
        return None

    auth = FyersAuth()
    auth.access_token = token
    return auth.get_fyers_client()


# ─── Fetch & save ─────────────────────────────────────────────────────────────

def _fetch_range(fyers, from_date: date, to_date: date,
                 instrument: str = 'NIFTY') -> pd.DataFrame | None:
    """
    Fetch all 5-min candles for a date range (up to 90 days per call).
    Returns a DataFrame indexed by IST timestamp, or None on failure.
    """
    resp = fyers.history({
        "symbol"     : _index_symbol(instrument),
        "resolution" : "5",
        "date_format": "1",
        "range_from" : from_date.strftime('%Y-%m-%d'),
        "range_to"   : to_date.strftime('%Y-%m-%d'),
        "cont_flag"  : "1",
    })

    if resp.get('s') != 'ok':
        print(f"  x API error ({from_date}->{to_date}): {resp.get('message', resp)}")
        return None

    candles = resp.get('candles', [])
    if not candles:
        return None

    df = pd.DataFrame(candles, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert(IST)
    df.set_index('ts', inplace=True)
    df = df.between_time('09:15', '15:30')
    return df if len(df) > 0 else None


def _fetch_day(fyers, target: date,
               instrument: str = 'NIFTY') -> pd.DataFrame | None:
    """Fetch 5-min candles for a single trading day."""
    df = _fetch_range(fyers, target - timedelta(days=1), target, instrument)
    if df is None:
        return None
    day_data = df[df.index.date == target]
    return day_data if len(day_data) > 0 else None


def _save_day(df: pd.DataFrame, target: date,
              instrument: str = 'NIFTY') -> str:
    d = _data_dir(instrument)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{_file_prefix(instrument)}_{target.strftime('%Y%m%d')}.csv")
    df.to_csv(path)
    return path


def _file_for(d: date, instrument: str = 'NIFTY') -> str:
    return os.path.join(_data_dir(instrument),
                        f"{_file_prefix(instrument)}_{d.strftime('%Y%m%d')}.csv")


# ─── Public API ───────────────────────────────────────────────────────────────

def collect_today(instrument: str = 'NIFTY') -> None:
    """
    Fetch and save today's 5-min data for the given instrument.
    Schedule this to run at 15:35 IST via Windows Task Scheduler.
    """
    now   = datetime.now(IST)
    today = now.date()

    print(f"\n{instrument} 5-min Data Collector -- {today}")
    print("=" * 50)

    if not is_market_open_today(today):
        print("Market closed today. Nothing to collect.")
        return

    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        print(f"Market not closed yet ({now.strftime('%H:%M')} IST). Run after 15:30.")
        return

    if os.path.exists(_file_for(today, instrument)):
        print(f"Already collected for {today}.")
        return

    fyers = _get_fyers_client()
    if not fyers:
        return

    print(f"Fetching {today}...")
    df = _fetch_day(fyers, today, instrument)

    if df is not None:
        path = _save_day(df, today, instrument)
        print(f"  ok {len(df)} candles saved -> {path}")
    else:
        print("  x No data saved.")


CHUNK_DAYS = 90   # stay safely under the 100-day per-request limit


def collect_historical(days_back: int = 400,
                       instrument: str = 'NIFTY') -> None:
    """
    Backfill up to `days_back` calendar days of 5-min data via chunked requests.
    Fyers stores ~400 days of 5-min data; each API call covers up to 90 days.

      python data_collector.py backfill 400              # NIFTY
      python data_collector.py backfill BANKNIFTY 400
      python data_collector.py backfill SENSEX 400
    """
    fyers = _get_fyers_client()
    if not fyers:
        return

    today    = datetime.now(IST).date()
    fetched  = 0
    skipped  = 0
    failed   = 0

    # Build 90-day chunks from today backwards
    chunks = []
    chunk_end = today
    while (today - chunk_end).days < days_back:
        chunk_start = max(chunk_end - timedelta(days=CHUNK_DAYS - 1),
                          today - timedelta(days=days_back))
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - timedelta(days=1)

    print(f"\nFnO_T_Bot -- {instrument} Chunked Historical Backfill")
    print(f"Target : {days_back} days back ({today - timedelta(days=days_back)} -> {today})")
    print(f"Chunks : {len(chunks)} x {CHUNK_DAYS}-day requests")
    print("=" * 55)

    for chunk_start, chunk_end in chunks:
        # Check which days in this chunk we already have
        missing_days = [
            chunk_start + timedelta(days=i)
            for i in range((chunk_end - chunk_start).days + 1)
            if is_market_open_today(chunk_start + timedelta(days=i))
            and not os.path.exists(_file_for(chunk_start + timedelta(days=i), instrument))
        ]

        if not missing_days:
            already = sum(
                1 for i in range((chunk_end - chunk_start).days + 1)
                if is_market_open_today(chunk_start + timedelta(days=i))
            )
            print(f"  {chunk_start} -> {chunk_end} | ok already complete ({already} days)")
            skipped += already
            continue

        print(f"  {chunk_start} -> {chunk_end} | fetching ({len(missing_days)} days needed)...",
              end=' ', flush=True)

        df = _fetch_range(fyers, chunk_start, chunk_end, instrument)

        if df is None or len(df) == 0:
            print(f"x no data returned (Fyers limit likely reached)")
            failed += len(missing_days)
            print("  Stopping: data not available this far back.")
            break

        # Split the chunk response into per-day files
        days_saved = 0
        for d in missing_days:
            day_df = df[df.index.date == d]
            if len(day_df) > 0:
                _save_day(day_df, d, instrument)
                days_saved += 1
                fetched += 1
            else:
                failed += 1

        print(f"ok {days_saved}/{len(missing_days)} days saved "
              f"({len(df)} total candles in chunk)")
        time.sleep(0.5)   # rate limit courtesy

    print(f"\nDone -- saved: {fetched} days | skipped: {skipped} | failed: {failed}")


def probe_data_depth(instrument: str = 'NIFTY') -> None:
    """
    Probe how far back Fyers actually stores 5-min data for the given instrument.

      python data_collector.py probe
      python data_collector.py probe BANKNIFTY
    """
    fyers = _get_fyers_client()
    if not fyers:
        return

    today     = datetime.now(IST).date()
    max_probe = 800   # probe up to ~2 years back

    print(f"\nFnO_T_Bot -- Probing Fyers {instrument} 5-min data depth...")
    print("=" * 50)

    chunk_end   = today
    last_ok     = today
    total_days  = 0

    while (today - chunk_end).days < max_probe:
        chunk_start = chunk_end - timedelta(days=CHUNK_DAYS - 1)
        df = _fetch_range(fyers, chunk_start, chunk_end, instrument)

        if df is not None and len(df) > 0:
            last_ok = chunk_start
            candles = len(df)
            print(f"  ok {chunk_start} -> {chunk_end}: {candles:,} candles available")
            total_days += CHUNK_DAYS
        else:
            print(f"  x {chunk_start} -> {chunk_end}: no data (limit reached)")
            break

        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(0.5)

    print(f"\nResult: Fyers has {instrument} 5-min data from ~{last_ok} onwards")
    print(f"        (~{(today - last_ok).days} calendar days)")
    print(f"\nRun:  python data_collector.py backfill {instrument} {(today - last_ok).days}")


def load_historical_data(
    start_date: date | None = None,
    end_date:   date | None = None,
    instrument: str = 'NIFTY',
) -> pd.DataFrame:
    """
    Load all saved 5-min CSVs into a single sorted DataFrame.

    Use this in your backtest instead of yfinance for real historical data:

        from data_collector import load_historical_data
        df = load_historical_data()                        # NIFTY (default)
        df = load_historical_data(instrument='BANKNIFTY')

    Falls back gracefully if no local data exists.
    """
    d = _data_dir(instrument)
    prefix = _file_prefix(instrument)

    if not os.path.exists(d):
        return pd.DataFrame()

    files = sorted(f for f in os.listdir(d) if f.endswith('.csv'))
    if not files:
        return pd.DataFrame()

    dfs = []
    for fname in files:
        try:
            file_date = datetime.strptime(fname, f'{prefix}_%Y%m%d.csv').date()
        except ValueError:
            continue

        if start_date and file_date < start_date:
            continue
        if end_date and file_date > end_date:
            continue

        try:
            tmp = pd.read_csv(
                os.path.join(d, fname),
                index_col=0,
                parse_dates=True,
            )
            dfs.append(tmp)
        except Exception as e:
            print(f"Warning: could not load {fname}: {e}")

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs).sort_index()
    combined = combined[~combined.index.duplicated(keep='first')]
    print(f"Loaded {instrument}: {len(combined)} candles across {len(dfs)} trading days "
          f"({dfs[0].index[0].date()} -> {dfs[-1].index[-1].date()})")
    return combined


def database_summary(instrument: str = 'NIFTY') -> None:
    """Print a summary of how much local data you have for an instrument."""
    d = _data_dir(instrument)
    prefix = _file_prefix(instrument)

    if not os.path.exists(d):
        print(f"No local data yet for {instrument}. "
              f"Run: python data_collector.py backfill {instrument}")
        return

    files = sorted(f for f in os.listdir(d) if f.endswith('.csv'))
    if not files:
        print("No CSV files found.")
        return

    dates = []
    total_candles = 0
    for fname in files:
        try:
            day  = datetime.strptime(fname, f'{prefix}_%Y%m%d.csv').date()
            tmp  = pd.read_csv(os.path.join(d, fname))
            dates.append(day)
            total_candles += len(tmp)
        except Exception:
            pass

    print(f"\nLocal {instrument} 5-min Database")
    print(f"  Trading days : {len(dates)}")
    print(f"  Date range   : {dates[0]} -> {dates[-1]}")
    print(f"  Total candles: {total_candles:,}")
    print(f"  Directory    : {os.path.abspath(d)}")


# ─── India VIX Data Collection ───────────────────────────────────────────────
# VIX captures geopolitical/macro fear — used as a sentiment filter by the bot.

VIX_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'vix_5min')
VIX_SYMBOL   = "NSE:INDIAVIX-INDEX"


def _vix_file(d: date) -> str:
    return os.path.join(VIX_DATA_DIR, f"vix_5min_{d.strftime('%Y%m%d')}.csv")


def _fetch_vix_range(fyers, from_date: date, to_date: date) -> pd.DataFrame | None:
    """Fetch 5-min India VIX candles for a date range."""
    resp = fyers.history({
        "symbol"     : VIX_SYMBOL,
        "resolution" : "5",
        "date_format": "1",
        "range_from" : from_date.strftime('%Y-%m-%d'),
        "range_to"   : to_date.strftime('%Y-%m-%d'),
        "cont_flag"  : "1",
    })
    if resp.get('s') != 'ok':
        print(f"  VIX API error ({from_date}->{to_date}): {resp.get('message', resp)}")
        return None
    candles = resp.get('candles', [])
    if not candles:
        return None
    vdf = pd.DataFrame(candles, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
    vdf['ts'] = pd.to_datetime(vdf['ts'], unit='s', utc=True).dt.tz_convert(IST)
    vdf.set_index('ts', inplace=True)
    vdf = vdf.between_time('09:15', '15:30')
    return vdf if len(vdf) > 0 else None


def collect_vix_historical(days_back: int = 400) -> None:
    """Backfill India VIX 5-min data (same chunked approach as index data)."""
    fyers = _get_fyers_client()
    if not fyers:
        return

    today   = datetime.now(IST).date()
    fetched = skipped = failed = 0

    chunks = []
    chunk_end = today
    while (today - chunk_end).days < days_back:
        chunk_start = max(chunk_end - timedelta(days=CHUNK_DAYS - 1),
                          today - timedelta(days=days_back))
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - timedelta(days=1)

    print(f"\nFnO_T_Bot -- India VIX Chunked Backfill")
    print(f"Target : {days_back} days back | Chunks : {len(chunks)}")
    print("=" * 55)

    for chunk_start, chunk_end in chunks:
        missing = [
            chunk_start + timedelta(days=i)
            for i in range((chunk_end - chunk_start).days + 1)
            if is_market_open_today(chunk_start + timedelta(days=i))
            and not os.path.exists(_vix_file(chunk_start + timedelta(days=i)))
        ]
        if not missing:
            skipped += 1
            continue

        print(f"  {chunk_start} -> {chunk_end} | {len(missing)} days...", end=' ', flush=True)
        vdf = _fetch_vix_range(fyers, chunk_start, chunk_end)

        if vdf is None or len(vdf) == 0:
            print("x no data")
            failed += len(missing)
            print("  Stopping -- VIX data not available this far back.")
            break

        os.makedirs(VIX_DATA_DIR, exist_ok=True)
        days_saved = 0
        for d in missing:
            day_df = vdf[vdf.index.date == d]
            if len(day_df) > 0:
                day_df.to_csv(_vix_file(d))
                days_saved += 1
                fetched += 1
            else:
                failed += 1
        print(f"ok {days_saved}/{len(missing)} days saved")
        time.sleep(0.5)

    print(f"\nDone -- VIX saved: {fetched} | skipped: {skipped} | failed: {failed}")


# ─── Top-3 Nifty Heavyweight Stock Data Collection ───────────────────────────
#
# These three stocks together account for ~21.75% of Nifty 50's weighting
# (as of Feb 2026: Reliance 9.33%, HDFC Bank 6.75%, Bharti Airtel 5.67%).
# Their alignment with the EMA crossover signal direction is used as a
# conviction filter in bot.py variant v10.
#
# Fyers symbols:
#   NSE:RELIANCE-EQ    -- Reliance Industries Ltd
#   NSE:HDFCBANK-EQ    -- HDFC Bank Ltd
#   NSE:BHARTIARTL-EQ  -- Bharti Airtel Ltd (replaced ICICI at #3 in 2025)

STOCKS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'stocks_5min')

# Map: short name -> Fyers symbol
HEAVYWEIGHT_STOCKS = {
    'RELIANCE'  : 'NSE:RELIANCE-EQ',
    'HDFCBANK'  : 'NSE:HDFCBANK-EQ',
    'BHARTIARTL': 'NSE:BHARTIARTL-EQ',
}


def _stock_file(ticker: str, d: date) -> str:
    return os.path.join(STOCKS_DIR, f"{ticker}_5min_{d.strftime('%Y%m%d')}.csv")


def _fetch_stock_range(fyers, symbol: str, from_date: date,
                       to_date: date) -> pd.DataFrame | None:
    """Fetch 5-min candles for a single stock over a date range."""
    resp = fyers.history({
        "symbol"     : symbol,
        "resolution" : "5",
        "date_format": "1",
        "range_from" : from_date.strftime('%Y-%m-%d'),
        "range_to"   : to_date.strftime('%Y-%m-%d'),
        "cont_flag"  : "1",
    })
    if resp.get('s') != 'ok':
        print(f"    Stock API error {symbol} ({from_date}->{to_date}): "
              f"{resp.get('message', resp)}")
        return None
    candles = resp.get('candles', [])
    if not candles:
        return None
    df = pd.DataFrame(candles, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert(IST)
    df.set_index('ts', inplace=True)
    df = df.between_time('09:15', '15:30')
    return df if len(df) > 0 else None


def collect_stocks_historical(days_back: int = 400) -> None:
    """
    Backfill 5-min OHLCV data for all HEAVYWEIGHT_STOCKS.

    Saves to data/stocks_5min/TICKER_5min_YYYYMMDD.csv

    Run once:
      python data_collector.py stocks_backfill 400
    """
    fyers = _get_fyers_client()
    if not fyers:
        return

    os.makedirs(STOCKS_DIR, exist_ok=True)
    today = datetime.now(IST).date()

    for ticker, symbol in HEAVYWEIGHT_STOCKS.items():
        print(f"\n{'=' * 55}")
        print(f"  Backfilling {ticker} ({symbol})")
        print(f"{'=' * 55}")

        fetched = skipped = failed = 0
        chunks  = []
        chunk_end = today
        while (today - chunk_end).days < days_back:
            chunk_start = max(chunk_end - timedelta(days=CHUNK_DAYS - 1),
                              today - timedelta(days=days_back))
            chunks.append((chunk_start, chunk_end))
            chunk_end = chunk_start - timedelta(days=1)

        for chunk_start, chunk_end in chunks:
            missing = [
                chunk_start + timedelta(days=i)
                for i in range((chunk_end - chunk_start).days + 1)
                if is_market_open_today(chunk_start + timedelta(days=i))
                and not os.path.exists(_stock_file(ticker, chunk_start + timedelta(days=i)))
            ]

            if not missing:
                skipped += sum(
                    1 for i in range((chunk_end - chunk_start).days + 1)
                    if is_market_open_today(chunk_start + timedelta(days=i))
                )
                continue

            print(f"  {chunk_start} -> {chunk_end} | {len(missing)} days...",
                  end=' ', flush=True)
            df = _fetch_stock_range(fyers, symbol, chunk_start, chunk_end)

            if df is None or len(df) == 0:
                print("x no data (Fyers limit reached?)")
                failed += len(missing)
                print("  Stopping -- no older data available.")
                break

            days_saved = 0
            for d in missing:
                day_df = df[df.index.date == d]
                if len(day_df) > 0:
                    day_df.to_csv(_stock_file(ticker, d))
                    days_saved += 1
                    fetched += 1
                else:
                    failed += 1

            print(f"ok {days_saved}/{len(missing)} days saved")
            time.sleep(0.5)   # rate limit

        print(f"  {ticker} done -- saved: {fetched} | skipped: {skipped} | failed: {failed}")


def collect_stocks_today() -> None:
    """
    Fetch and save today's 5-min stock data for all heavyweight stocks.
    Schedule at 15:37 IST (2 minutes after NIFTY collect_today).
    """
    now   = datetime.now(IST)
    today = now.date()

    print(f"\nHeavyweight Stocks 5-min Collector -- {today}")
    print("=" * 50)

    if not is_market_open_today(today):
        print("Market closed today. Nothing to collect.")
        return

    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        print(f"Market not closed yet ({now.strftime('%H:%M')} IST). Run after 15:30.")
        return

    fyers = _get_fyers_client()
    if not fyers:
        return

    os.makedirs(STOCKS_DIR, exist_ok=True)

    for ticker, symbol in HEAVYWEIGHT_STOCKS.items():
        out = _stock_file(ticker, today)
        if os.path.exists(out):
            print(f"  {ticker}: already collected.")
            continue
        df = _fetch_stock_range(fyers, symbol, today - timedelta(days=1), today)
        if df is not None:
            day_df = df[df.index.date == today]
            if len(day_df) > 0:
                day_df.to_csv(out)
                print(f"  {ticker}: ok {len(day_df)} candles saved -> {out}")
                continue
        print(f"  {ticker}: x no data returned")


def load_stocks_data() -> dict:
    """
    Load all saved stock 5-min CSVs into a dict of DataFrames.
    Returns {ticker: DataFrame} with IST-timezone index.
    Called at bot.py startup when heavyweight alignment filter is active.

    Returns empty dict if no stock data has been collected yet.
    """
    import pytz as _pytz
    _IST = _pytz.timezone('Asia/Kolkata')

    result = {}
    if not os.path.exists(STOCKS_DIR):
        return result

    for ticker in HEAVYWEIGHT_STOCKS:
        files = sorted(
            f for f in os.listdir(STOCKS_DIR)
            if f.startswith(f"{ticker}_5min_") and f.endswith('.csv')
        )
        if not files:
            continue
        dfs = []
        for fname in files:
            try:
                tmp = pd.read_csv(os.path.join(STOCKS_DIR, fname),
                                  index_col=0, parse_dates=True)
                if len(tmp) > 0:
                    dfs.append(tmp)
            except Exception:
                continue
        if not dfs:
            continue
        df = pd.concat(dfs).sort_index()
        df = df[~df.index.duplicated(keep='first')]
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize(_IST)
        else:
            df.index = df.index.tz_convert(_IST)
        result[ticker] = df

    return result


def stocks_summary() -> None:
    """Print a summary of collected heavyweight stock data."""
    if not os.path.exists(STOCKS_DIR):
        print("No stock data yet. Run: python data_collector.py stocks_backfill 400")
        return

    for ticker in HEAVYWEIGHT_STOCKS:
        files = sorted(
            f for f in os.listdir(STOCKS_DIR)
            if f.startswith(f"{ticker}_5min_") and f.endswith('.csv')
        )
        if not files:
            print(f"  {ticker}: no data")
            continue
        total = sum(
            len(pd.read_csv(os.path.join(STOCKS_DIR, f)))
            for f in files
        )
        first_d = files[0].replace(f"{ticker}_5min_", '').replace('.csv', '')
        last_d  = files[-1].replace(f"{ticker}_5min_", '').replace('.csv', '')
        print(f"  {ticker}: {len(files)} days | {total:,} candles | "
              f"{first_d} -> {last_d}")


# ─── CLI entry point ──────────────────────────────────────────────────────────

def _parse_instrument(args: list[str], default: str = 'NIFTY') -> tuple[str, list[str]]:
    """
    Extract optional instrument name from args list.
    Returns (instrument, remaining_args).
    Instrument must be a key in config.INSTRUMENTS (case-insensitive).
    """
    valid = {k.upper() for k in config.INSTRUMENTS}
    if args and args[0].upper() in valid:
        return args[0].upper(), args[1:]
    return default, args


if __name__ == "__main__":
    cmd  = sys.argv[1] if len(sys.argv) > 1 else 'today'
    rest = sys.argv[2:]

    if cmd == 'backfill':
        # Supports: backfill [INSTRUMENT] [DAYS]
        # e.g.  backfill 400  OR  backfill BANKNIFTY 400  OR  backfill BANKNIFTY
        inst, tail = _parse_instrument(rest)
        days = int(tail[0]) if tail else 400
        collect_historical(days, instrument=inst)

    elif cmd == 'today':
        inst, _ = _parse_instrument(rest)
        collect_today(instrument=inst)

    elif cmd == 'probe':
        inst, _ = _parse_instrument(rest)
        probe_data_depth(instrument=inst)

    elif cmd == 'summary':
        inst, _ = _parse_instrument(rest)
        database_summary(instrument=inst)

    elif cmd == 'vix_backfill':
        days = int(rest[0]) if rest else 400
        collect_vix_historical(days)

    elif cmd == 'stocks_backfill':
        days = int(rest[0]) if rest else 400
        collect_stocks_historical(days)

    elif cmd == 'stocks_today':
        collect_stocks_today()

    elif cmd == 'stocks_summary':
        stocks_summary()

    else:
        # Default: collect today's NIFTY data
        collect_today()
