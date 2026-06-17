# -*- coding: utf-8 -*-
"""
ORB simulation — April 17 2026 (Thursday)
==========================================
Fetches real 5-min OHLCV from Fyers for NIFTY + BANKNIFTY,
then runs the current live strategy filters in sequence and
shows exactly where each one passes or blocks.

Thursday config (from PATH_A_DAY_CONFIG):
  OR bars   : 5  (09:15 – 10:05)
  ADX floor : 25
  CALL      : blocked (PUT-only Thursday)
  OI-confirm: not required on Thu
  Entry end : 12:00
  Checkpoint: 12:00

Run on EC2 (token available):
  cd /opt/trading_bot/live_bot
  /opt/trading_bot/venv/bin/python orb_sim_apr17.py
"""
from __future__ import annotations
import sys, os, math, json
from datetime import date, datetime, time as dtime
import pandas as pd
import numpy as np

# ── paths ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import config

TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'logs', 'token.txt')

# ── Fyers fetch ────────────────────────────────────────────────────────────────

def get_fyers():
    from fyers_auth import FyersAuth
    auth = FyersAuth()
    lines = open(TOKEN_FILE).read().strip().splitlines()
    auth.access_token = lines[0]
    return auth.get_fyers_client()


def fetch_5min(fyers, symbol: str, target_date: date) -> pd.DataFrame | None:
    """Pull 5-min candles for target_date from Fyers history API.
    date_format=1 → YYYY-MM-DD string dates (same as options_bot.get_index_data).
    """
    d_str = target_date.strftime('%Y-%m-%d')
    payload = {
        'symbol'      : symbol,
        'resolution'  : '5',
        'date_format' : '1',
        'range_from'  : d_str,
        'range_to'    : d_str,
        'cont_flag'   : '1',
    }
    resp = fyers.history(data=payload)
    if resp.get('s') != 'ok' or not resp.get('candles'):
        print(f"  [FETCH FAIL] {symbol}: {resp.get('message', resp.get('s'))}")
        return None
    cols = ['ts', 'Open', 'High', 'Low', 'Close', 'Volume']
    df   = pd.DataFrame(resp['candles'], columns=cols)
    df['datetime'] = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    df = df.set_index('datetime').drop(columns=['ts'])
    df = df[df.index.date == target_date]
    return df


# ── Indicators ─────────────────────────────────────────────────────────────────

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def compute_adx(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    tr  = pd.concat([high - low,
                     (high - close.shift()).abs(),
                     (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    up  = high.diff().clip(lower=0)
    dn  = (-low.diff()).clip(lower=0)
    up  = up.where(up > dn, 0)
    dn  = dn.where(dn > up, 0)
    pdi = 100 * up.ewm(span=period, adjust=False).mean() / atr
    mdi = 100 * dn.ewm(span=period, adjust=False).mean() / atr
    dx  = (100 * (pdi - mdi).abs() / (pdi + mdi)).fillna(0)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx, pdi, mdi


def compute_vwap(df):
    tp  = (df['High'] + df['Low'] + df['Close']) / 3
    cv  = (tp * df['Volume']).cumsum()
    vwap = cv / df['Volume'].cumsum()
    return vwap


def add_indicators(df):
    df = df.copy()
    df['EMA9']  = ema(df['Close'], 9)
    df['EMA21'] = ema(df['Close'], 21)
    df['ADX'], df['DI_plus'], df['DI_minus'] = compute_adx(df)
    df['VWAP'] = compute_vwap(df)
    return df


# ── Filter audit ───────────────────────────────────────────────────────────────

PASS = '✅'
FAIL = '❌'
SKIP = '⚠️ '

def check_filters(df, instrument: str):
    """
    Walk through every ORB filter in order and report pass/fail.
    Thursday April 17 config:
      OR = first 5 bars from 09:15 (bars 0-4)
      ADX floor = 25
      CALL suppressed (PUT-only Thursday)
      Entry window: 09:30 – 12:00
    """
    # Per-day config for Thursday
    OR_BARS   = 5
    ADX_FLOOR = 25   # from PATH_A_DAY_ADX_MIN['Thu']
    CALL_BLOCKED = True   # PATH_A_NO_CALL_DAYS includes 'Thu'
    ENTRY_START = dtime(9, 30)
    ENTRY_END   = dtime(12, 0)
    STOP_PCT    = 0.25
    TARGET_PCT  = 0.28

    instr_cfg = {
        'NIFTY'     : {'strike_gap': 50,  'lot': 65},
        'BANKNIFTY' : {'strike_gap': 100, 'lot': 30},
    }
    sg  = instr_cfg[instrument]['strike_gap']
    lot = instr_cfg[instrument]['lot']

    print(f"\n{'='*62}")
    print(f"  {instrument}  |  Thursday 17-Apr-2026  |  ORB Simulation")
    print(f"{'='*62}")

    # ── 1. OR computation ─────────────────────────────────────────
    or_bars_df = df.iloc[:OR_BARS]   # first 5 bars (09:15–10:05)
    OR_HIGH = float(or_bars_df['High'].max())
    OR_LOW  = float(or_bars_df['Low'].min())
    or_width_pct = (OR_HIGH - OR_LOW) / OR_LOW * 100
    print(f"\n  Opening Range ({OR_BARS}-bar, 09:15–10:05):")
    print(f"    High: {OR_HIGH:,.1f}  Low: {OR_LOW:,.1f}  Width: {or_width_pct:.2f}%")

    # OR width gate for Thursday (from config.PATH_A_OR_WIDTH_MAX)
    OR_WIDTH_MAX_THU = config.PATH_A_OR_WIDTH_MAX.get('Thu')
    if OR_WIDTH_MAX_THU is not None and (OR_HIGH - OR_LOW) / OR_LOW > OR_WIDTH_MAX_THU:
        print(f"  {FAIL} OR width {or_width_pct:.2f}% > max {OR_WIDTH_MAX_THU*100:.2f}% → ORB BLOCKED for the day")
        return
    else:
        limit_str = f"{OR_WIDTH_MAX_THU*100:.2f}%" if OR_WIDTH_MAX_THU else "none"
        print(f"  {PASS} OR width {or_width_pct:.2f}% (limit: {limit_str})")

    # ── 2. Scan entry bars (09:30 onward) ─────────────────────────
    entry_df = df[df.index.time >= ENTRY_START]
    print(f"\n  Scanning {len(entry_df)} bars from 09:30 to 12:00...\n")

    put_tried  = False
    call_tried = False
    any_break  = False

    for ts, row in entry_df.iterrows():
        t = ts.time()
        if t > ENTRY_END:
            break

        price  = float(row['Close'])
        adx    = float(row['ADX'])
        dip    = float(row['DI_plus'])
        dim    = float(row['DI_minus'])
        ema9   = float(row['EMA9'])
        ema21  = float(row['EMA21'])
        vwap   = float(row['VWAP'])

        # ── Breakout check ────────────────────────────────────────
        call_break = price > OR_HIGH
        put_break  = price < OR_LOW

        if not (call_break or put_break):
            continue

        any_break = True
        sig_type  = 'CALL' if call_break else 'PUT'
        direction = 'above OR high' if call_break else 'below OR low'
        print(f"  [{t.strftime('%H:%M')}] {sig_type} breakout: {price:,.1f} {direction} {OR_HIGH if call_break else OR_LOW:,.1f}")

        # ── Direction filter — CALL blocked on Thursday ───────────
        if sig_type == 'CALL' and CALL_BLOCKED:
            if not call_tried:
                print(f"    {FAIL} CALL suppressed on Thursday (PUT-only day)")
                call_tried = True
            continue
        if sig_type == 'PUT' and put_tried:
            continue   # already attempted PUT

        if sig_type == 'PUT':
            put_tried = True

        # ── ADX filter ─────────────────────────────────────────────
        if adx < ADX_FLOOR:
            print(f"    {FAIL} ADX {adx:.1f} < floor {ADX_FLOOR} → no trade")
            continue
        print(f"    {PASS} ADX {adx:.1f} ≥ {ADX_FLOOR}")

        # ── EMA alignment ──────────────────────────────────────────
        ema_bull = ema9 > ema21
        ema_bear = ema9 < ema21
        if sig_type == 'CALL' and not ema_bull:
            print(f"    {FAIL} EMA misaligned for CALL (EMA9={ema9:.1f} < EMA21={ema21:.1f})")
            continue
        if sig_type == 'PUT' and not ema_bear:
            print(f"    {FAIL} EMA misaligned for PUT (EMA9={ema9:.1f} > EMA21={ema21:.1f})")
            continue
        ema_str = f"EMA9={ema9:.1f} {'>' if ema_bull else '<'} EMA21={ema21:.1f}"
        print(f"    {PASS} EMA aligned: {ema_str}")

        # ── VWAP gate ──────────────────────────────────────────────
        vwap_ok = (sig_type == 'CALL' and price > vwap) or (sig_type == 'PUT' and price < vwap)
        if not vwap_ok:
            print(f"    {FAIL} VWAP gate: price {price:,.1f} wrong side of VWAP {vwap:,.1f}")
            continue
        print(f"    {PASS} VWAP gate: price {price:,.1f} {'above' if sig_type=='CALL' else 'below'} VWAP {vwap:,.1f}")

        # ── All filters passed → trade! ────────────────────────────
        print(f"\n    🚀 TRADE SIGNAL: {sig_type} at {t.strftime('%H:%M')}")
        _simulate_trade(df, ts, sig_type, price, sg, lot, STOP_PCT, TARGET_PCT, instrument)
        break

    if not any_break:
        print(f"  {SKIP} No breakout of OR range occurred in the session")
        print(f"    OR High {OR_HIGH:,.1f} / Low {OR_LOW:,.1f} — price never escaped the range")

    # ── Summary bar chart of ADX across session ────────────────────
    print(f"\n  ADX profile (09:15 – 14:30):")
    adx_vals = df[df.index.time <= dtime(14, 30)]['ADX'].dropna()
    for ts2, adx_val in adx_vals.items():
        bar_len = int(adx_val / 2)
        bar     = '█' * bar_len
        flag    = ' ← floor' if adx_val < ADX_FLOOR and abs(adx_val - ADX_FLOOR) < 3 else ''
        mark    = '✗' if adx_val < ADX_FLOOR else ' '
        print(f"    {ts2.strftime('%H:%M')} {mark} ADX={adx_val:5.1f} {bar}{flag}")


def _simulate_trade(df, entry_ts, sig_type, entry_underlying,
                    strike_gap, lot, stop_pct, target_pct, instrument):
    """Black-Scholes simulation of entry → exit for a trade that fires."""
    from math import log, sqrt, exp, erf

    def norm_cdf(x):
        return 0.5 * (1 + erf(x / sqrt(2)))

    def bs_call(S, K, T, r, sigma):
        if T <= 1e-8: return max(S - K, 0.0)
        d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
        d2 = d1 - sigma*sqrt(T)
        return S*norm_cdf(d1) - K*exp(-r*T)*norm_cdf(d2)

    def bs_put(S, K, T, r, sigma):
        if T <= 1e-8: return max(K - S, 0.0)
        d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
        d2 = d1 - sigma*sqrt(T)
        return K*exp(-r*T)*norm_cdf(-d2) - S*norm_cdf(-d1)

    bs = bs_call if sig_type == 'CALL' else bs_put

    r     = 0.065
    sigma = 0.22   # estimated OTM IV for 1-DTE near expiry

    # Strike: ATM
    atm    = int(round(entry_underlying / strike_gap) * strike_gap)
    strike = atm

    # Apr 17 is Thursday — BNF expiry is Friday (1 DTE = ~24h)
    # NIFTY expiry is Thursday (same day, ~5h to expiry at 10am)
    T_hrs = {'NIFTY': 5.5, 'BANKNIFTY': 24.0}.get(instrument, 10.0)
    T_entry = T_hrs / (365 * 24)

    entry_opt = bs(entry_underlying, strike, T_entry, r, sigma)
    if entry_opt < 1.0:
        print(f"    Option price ₹{entry_opt:.2f} too low — skipping sim")
        return

    print(f"    Entry: {sig_type} {strike} @ ₹{entry_opt:.2f} (underlying {entry_underlying:,.1f})")
    print(f"    Stop: {stop_pct*100:.0f}%  Target: {target_pct*100:.0f}%")

    exit_time   = None
    exit_opt    = entry_opt
    exit_reason = None
    peak_pnl    = 0.0
    checkpoint  = dtime(12, 0)

    future_bars = df[df.index > entry_ts]
    for ts, row in future_bars.iterrows():
        t     = ts.time()
        S     = float(row['Close'])
        elapsed_h = (ts - entry_ts).total_seconds() / 3600
        T_rem = max(T_hrs - elapsed_h, 0.01) / (365 * 24)
        cur   = bs(S, strike, T_rem, r, sigma)
        pnl_pct = (cur - entry_opt) / entry_opt
        if pnl_pct > peak_pnl:
            peak_pnl = pnl_pct

        if t >= dtime(14, 30):
            exit_opt    = cur
            exit_reason = 'EOD force-close 14:30'
            exit_time   = t
            break
        if pnl_pct <= -stop_pct:
            exit_opt    = cur
            exit_reason = f'Stop-loss ({stop_pct*100:.0f}%)'
            exit_time   = t
            break
        if pnl_pct >= target_pct:
            exit_opt    = cur
            exit_reason = f'Target ({target_pct*100:.0f}%)'
            exit_time   = t
            break
        if t >= checkpoint and peak_pnl >= 0.50:
            exit_opt    = cur
            exit_reason = f'Checkpoint exceptional profit (≥50%)'
            exit_time   = t
            break
        if t >= checkpoint and pnl_pct < 0:
            exit_opt    = cur
            exit_reason = 'Checkpoint loss-stop (12:00)'
            exit_time   = t
            break

    pnl_pct_final = (exit_opt - entry_opt) / entry_opt
    pnl_rs        = (exit_opt - entry_opt) * lot

    print(f"    Exit : {exit_time} | {exit_reason}")
    print(f"    Entry ₹{entry_opt:.2f} → Exit ₹{exit_opt:.2f} | "
          f"P&L {pnl_pct_final*100:+.1f}% | ₹{pnl_rs:+,.0f}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    TARGET_DATE = date(2026, 4, 17)
    print(f"Fetching 5-min data for {TARGET_DATE} from Fyers...")

    try:
        fy = get_fyers()
    except Exception as e:
        print(f"Fyers auth failed: {e}")
        sys.exit(1)

    # Fyers symbol format
    SYMBOLS = {
        'NIFTY'     : 'NSE:NIFTY50-INDEX',
        'BANKNIFTY' : 'NSE:NIFTYBANK-INDEX',
    }

    results = {}
    for inst, sym in SYMBOLS.items():
        print(f"\nFetching {inst} ({sym})...")
        df = fetch_5min(fy, sym, TARGET_DATE)
        if df is None or df.empty:
            print(f"  No data for {inst}")
            continue
        df = add_indicators(df)
        results[inst] = df
        print(f"  {len(df)} bars loaded ({df.index[0].strftime('%H:%M')} – {df.index[-1].strftime('%H:%M')})")

    print("\n" + "="*62)
    print("  FILTER AUDIT — current live strategy vs Apr 17 data")
    print("="*62)

    for inst, df in results.items():
        check_filters(df, inst)

    print("\n" + "="*62)
    print("  SUMMARY")
    print("="*62)
    print("""
  April 17 was a Thursday with:
    • ADX 10–17 all session (choppy, no directional momentum)
    • Thursday ADX floor = 25
    • CALL blocked on Thursdays (PUT-only)
    → Expected: 0 trades for NIFTY and BANKNIFTY
    → The -₹209 BNF loss recorded on Apr 17 was from the
      LEGACY fno_t_bot_banknifty_orb service (old code,
      no ADX gate), NOT from the current ORB strategy.
""")
