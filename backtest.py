"""
Kronos Futures Bot — Walk-Forward Backtest
==========================================
Fetches 5-min OHLCV from yfinance (last 60 days) for NIFTY and BANKNIFTY,
then replays the same entry/exit logic used in futures_bot.py bar-by-bar.

Run:
    python backtest.py                      # both instruments
    python backtest.py NIFTY                # single instrument
    python backtest.py --fallback           # force EMA fallback (skip Kronos model)

Output: per-trade log + summary metrics to stdout and backtest_results.csv
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Make sure project imports resolve ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from config import (
    INSTRUMENTS, MIN_ADX, ORB_WINDOW_START, MAIN_SESSION_END,
    KRONOS_FORECAST_BARS, KRONOS_CONFIDENCE_MIN,
    STOP_LOSS_PCT, TRAIL_ACTIVATE_PCT, TRAIL_DISTANCE_PCT,
    EXIT_ON_KRONOS_REVERSAL, EXIT_ON_SUPERTREND_FLIP,
)

# ── yfinance symbols for NSE indices (spot — futures price closely tracks) ───
_YF_SYMBOLS = {
    "NIFTY":     "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}

IST = timezone(timedelta(hours=5, minutes=30))


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_5min(instrument: str) -> pd.DataFrame:
    """Download last 60 days of 5-min bars from yfinance."""
    import yfinance as yf

    symbol = _YF_SYMBOLS[instrument]
    log.info("[DATA] Downloading 5-min data for %s (%s) …", instrument, symbol)
    raw = yf.download(symbol, period="60d", interval="5m", progress=False, auto_adjust=True)

    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")

    # Flatten multi-level columns if present
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0].lower() for c in raw.columns]
    else:
        raw.columns = [c.lower() for c in raw.columns]

    raw = raw.rename(columns={"open": "open", "high": "high", "low": "low",
                               "close": "close", "volume": "volume"})
    raw = raw[["open", "high", "low", "close", "volume"]].dropna()
    raw.index = pd.to_datetime(raw.index)
    if raw.index.tzinfo is None:
        raw.index = raw.index.tz_localize("Asia/Kolkata")
    else:
        raw.index = raw.index.tz_convert("Asia/Kolkata")

    # Keep only market hours 09:15 – 15:30
    raw = raw.between_time("09:15", "15:30")
    raw = raw.sort_index().reset_index()
    raw = raw.rename(columns={"index": "datetime", "Datetime": "datetime"})
    if "datetime" not in raw.columns:
        raw = raw.rename(columns={raw.columns[0]: "datetime"})

    log.info("[DATA] %s: %d bars (%.1f days)", instrument, len(raw),
             (raw["datetime"].iloc[-1] - raw["datetime"].iloc[0]).days)
    return raw


# ── Technical indicators ──────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr      = tr.ewm(span=period).mean()
    dm_plus  = (high.diff()).clip(lower=0)
    dm_minus = (-low.diff()).clip(lower=0)
    di_plus  = 100 * dm_plus.ewm(span=period).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period).mean() / atr
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus)
    return float(dx.ewm(span=period).mean().iloc[-1])


def supertrend(df: pd.DataFrame, period: int = 7, multiplier: float = 2.5) -> str:
    hl2 = (df["high"] + df["low"]) / 2
    tr  = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr   = tr.ewm(span=period).mean()
    lower = hl2 - multiplier * atr
    return "BULL" if df["close"].iloc[-1] > lower.iloc[-1] else "BEAR"


# ── Entry window check ────────────────────────────────────────────────────────

def _in_entry_window(ts: pd.Timestamp) -> bool:
    t = ts.time()
    start = dtime(*map(int, ORB_WINDOW_START.split(":")))
    end   = dtime(*map(int, MAIN_SESSION_END.split(":")))
    return start <= t <= end


# ── Trade record ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    instrument: str
    direction: str
    entry_bar: int
    entry_time: pd.Timestamp
    entry_price: float
    exit_bar: int = 0
    exit_time: pd.Timestamp = None
    exit_price: float = 0.0
    exit_reason: str = ""
    lot_size: int = 1
    pnl_pts: float = 0.0
    pnl_inr: float = 0.0


# ── Backtest engine ───────────────────────────────────────────────────────────

def backtest_instrument(
    instrument: str,
    df: pd.DataFrame,
    force_fallback: bool = False,
) -> list[Trade]:
    """
    Walk forward through df bar-by-bar, applying the bot's entry/exit logic.
    Returns list of completed Trade objects.
    """
    from core.kronos_signal import forecast as kronos_forecast

    lot_size = INSTRUMENTS[instrument]["lot_size"]
    min_context = 256   # bars needed before first signal

    trades: list[Trade] = []
    position: Trade | None = None
    hwm: float = 0.0
    trail_active: bool = False
    trail_stop: float = 0.0

    log.info("[BT] Starting backtest: %s | %d bars | lot=%d", instrument, len(df), lot_size)

    for i in range(min_context, len(df)):
        ctx  = df.iloc[: i + 1].copy()     # bars up to and including bar i
        bar  = df.iloc[i]
        ts   = pd.Timestamp(bar["datetime"])
        close = float(bar["close"])

        # ── Exit logic ────────────────────────────────────────────────────────
        if position is not None:
            pnl_pct = ((close - position.entry_price) / position.entry_price
                       if position.direction == "LONG"
                       else (position.entry_price - close) / position.entry_price)

            # Hard stop
            if pnl_pct <= -STOP_LOSS_PCT:
                _close_trade(position, i, ts, close, "STOP_LOSS", trades)
                position = None
                hwm = trail_stop = 0.0
                trail_active = False
                continue

            # Trail
            if pnl_pct >= TRAIL_ACTIVATE_PCT:
                trail_active = True
                if close > hwm or hwm == 0.0:
                    hwm = close
                    trail_stop = (hwm * (1 - TRAIL_DISTANCE_PCT) if position.direction == "LONG"
                                  else hwm * (1 + TRAIL_DISTANCE_PCT))

            if trail_active:
                hit = (position.direction == "LONG" and close < trail_stop) or \
                      (position.direction == "SHORT" and close > trail_stop)
                if hit:
                    _close_trade(position, i, ts, close, "TRAIL_STOP", trades)
                    position = None
                    hwm = trail_stop = 0.0
                    trail_active = False
                    continue

            # Kronos reversal / SuperTrend flip
            if EXIT_ON_KRONOS_REVERSAL or EXIT_ON_SUPERTREND_FLIP:
                try:
                    kf = kronos_forecast(ctx, KRONOS_FORECAST_BARS)
                    st = supertrend(ctx)

                    if EXIT_ON_KRONOS_REVERSAL:
                        opp = "SHORT" if position.direction == "LONG" else "LONG"
                        if kf.direction == opp:
                            _close_trade(position, i, ts, close, "KRONOS_REVERSAL", trades)
                            position = None
                            hwm = trail_stop = 0.0
                            trail_active = False
                            continue

                    if EXIT_ON_SUPERTREND_FLIP:
                        if (position.direction == "LONG" and st == "BEAR") or \
                           (position.direction == "SHORT" and st == "BULL"):
                            _close_trade(position, i, ts, close, "ST_FLIP", trades)
                            position = None
                            hwm = trail_stop = 0.0
                            trail_active = False
                            continue
                except Exception as e:
                    log.debug("[BT] Exit signal error at bar %d: %s", i, e)

            continue   # still holding, nothing else to do this bar

        # ── Entry logic (no position) ─────────────────────────────────────────
        if not _in_entry_window(ts):
            continue

        try:
            adx = compute_adx(ctx)
            if adx < MIN_ADX:
                continue

            if force_fallback:
                # Monkey-patch to use EMA fallback
                from core import kronos_signal as _ks
                orig = _ks._load_predictor
                _ks._load_predictor = lambda: (_ for _ in ()).throw(RuntimeError("forced fallback"))
                try:
                    kf = kronos_forecast(ctx, KRONOS_FORECAST_BARS)
                finally:
                    _ks._load_predictor = orig
            else:
                kf = kronos_forecast(ctx, KRONOS_FORECAST_BARS)

            if kf.confidence < KRONOS_CONFIDENCE_MIN or kf.direction == "NEUTRAL":
                continue

            st = supertrend(ctx)
            # Require SuperTrend aligned with Kronos direction
            if kf.direction == "LONG" and st != "BULL":
                continue
            if kf.direction == "SHORT" and st != "BEAR":
                continue

            position = Trade(
                instrument=instrument,
                direction=kf.direction,
                entry_bar=i,
                entry_time=ts,
                entry_price=close,
                lot_size=lot_size,
            )
            hwm = close
            trail_active = False
            trail_stop = 0.0
            log.info("[BT] ENTRY  %s %s @ %.2f  bar=%d  conf=%.0f%%  src=%s",
                     instrument, kf.direction, close, i, kf.confidence * 100, kf.source)

        except Exception as e:
            log.debug("[BT] Entry error at bar %d: %s", i, e)

    # Close any open position at end of data
    if position is not None:
        last_bar  = df.iloc[-1]
        _close_trade(position, len(df) - 1,
                     pd.Timestamp(last_bar["datetime"]),
                     float(last_bar["close"]),
                     "EOD_CLOSE", trades)

    return trades


def _close_trade(pos: Trade, bar: int, ts: pd.Timestamp,
                 price: float, reason: str, trades: list[Trade]):
    pos.exit_bar    = bar
    pos.exit_time   = ts
    pos.exit_price  = price
    pos.exit_reason = reason
    pts = (price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - price)
    pos.pnl_pts = pts
    pos.pnl_inr = pts * pos.lot_size
    trades.append(pos)
    log.info("[BT] EXIT   %s %s @ %.2f  reason=%-16s  P&L=%.0f pts / ₹%.0f",
             pos.instrument, pos.direction, price, reason, pts, pos.pnl_inr)


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {}

    pnl = [t.pnl_inr for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]

    total_pnl    = sum(pnl)
    win_rate     = len(wins) / len(pnl) if pnl else 0
    avg_win      = np.mean(wins) if wins else 0
    avg_loss     = np.mean(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Sharpe (per-trade, annualised approx)
    pnl_arr = np.array(pnl)
    sharpe  = (np.mean(pnl_arr) / (np.std(pnl_arr) + 1e-8)) * np.sqrt(252) if len(pnl) > 1 else 0

    # Max drawdown on cumulative P&L
    cum = np.cumsum(pnl_arr)
    peak = np.maximum.accumulate(cum)
    dd   = cum - peak
    max_dd = float(dd.min())

    durations = [(t.exit_bar - t.entry_bar) * 5 for t in trades]  # minutes

    return {
        "trades":         len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       win_rate,
        "total_pnl_inr":  total_pnl,
        "avg_win_inr":    avg_win,
        "avg_loss_inr":   avg_loss,
        "profit_factor":  profit_factor,
        "sharpe":         sharpe,
        "max_drawdown_inr": max_dd,
        "avg_duration_min": np.mean(durations) if durations else 0,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(instrument: str, trades: list[Trade], metrics: dict):
    sep = "=" * 68
    print(f"\n{sep}")
    print(f"  BACKTEST REPORT — {instrument}")
    print(sep)
    if not metrics:
        print("  No trades generated.")
        return

    print(f"  Trades        : {metrics['trades']}  ({metrics['wins']}W / {metrics['losses']}L)")
    print(f"  Win rate      : {metrics['win_rate']:.1%}")
    print(f"  Total P&L     : ₹{metrics['total_pnl_inr']:,.0f}")
    print(f"  Avg win       : ₹{metrics['avg_win_inr']:,.0f}")
    print(f"  Avg loss      : ₹{metrics['avg_loss_inr']:,.0f}")
    print(f"  Profit factor : {metrics['profit_factor']:.2f}")
    print(f"  Sharpe (ann)  : {metrics['sharpe']:.2f}")
    print(f"  Max drawdown  : ₹{metrics['max_drawdown_inr']:,.0f}")
    print(f"  Avg hold time : {metrics['avg_duration_min']:.0f} min")
    print(sep)

    if trades:
        print(f"\n  Trade log ({len(trades)} trades):")
        hdr = f"  {'#':>3}  {'Dir':6}  {'Entry':>10}  {'Exit':>10}  {'P&L pts':>8}  {'P&L ₹':>10}  Reason"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for n, t in enumerate(trades, 1):
            print(f"  {n:>3}  {t.direction:6}  {t.entry_price:>10.1f}  {t.exit_price:>10.1f}"
                  f"  {t.pnl_pts:>8.1f}  {t.pnl_inr:>10,.0f}  {t.exit_reason}")


def save_csv(all_trades: list[Trade], path: str = "backtest_results.csv"):
    if not all_trades:
        return
    rows = [{
        "instrument":    t.instrument,
        "direction":     t.direction,
        "entry_time":    t.entry_time,
        "entry_price":   t.entry_price,
        "exit_time":     t.exit_time,
        "exit_price":    t.exit_price,
        "exit_reason":   t.exit_reason,
        "pnl_pts":       t.pnl_pts,
        "pnl_inr":       t.pnl_inr,
        "lot_size":      t.lot_size,
    } for t in all_trades]
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\n  Results saved → {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kronos Futures Bot backtest")
    parser.add_argument("instruments", nargs="*", default=["NIFTY", "BANKNIFTY"],
                        help="Instruments to backtest (default: NIFTY BANKNIFTY)")
    parser.add_argument("--fallback", action="store_true",
                        help="Force EMA fallback — skip loading Kronos model")
    parser.add_argument("--out", default="backtest_results.csv",
                        help="CSV output path")
    args = parser.parse_args()

    instruments = [i.upper() for i in args.instruments if i.upper() in INSTRUMENTS]
    if not instruments:
        log.error("No valid instruments. Choose from: %s", list(INSTRUMENTS.keys()))
        sys.exit(1)

    all_trades: list[Trade] = []

    for inst in instruments:
        try:
            df = fetch_5min(inst)
        except Exception as e:
            log.error("[BT] Cannot fetch data for %s: %s", inst, e)
            continue

        trades = backtest_instrument(inst, df, force_fallback=args.fallback)
        metrics = compute_metrics(trades)
        print_report(inst, trades, metrics)
        all_trades.extend(trades)

    if len(instruments) > 1 and all_trades:
        print("\n" + "=" * 68)
        print("  COMBINED SUMMARY")
        combined = compute_metrics(all_trades)
        print(f"  Total trades  : {combined['trades']}")
        print(f"  Win rate      : {combined['win_rate']:.1%}")
        print(f"  Total P&L     : ₹{combined['total_pnl_inr']:,.0f}")
        print(f"  Profit factor : {combined['profit_factor']:.2f}")
        print(f"  Sharpe (ann)  : {combined['sharpe']:.2f}")
        print(f"  Max drawdown  : ₹{combined['max_drawdown_inr']:,.0f}")
        print("=" * 68)

    save_csv(all_trades, args.out)


if __name__ == "__main__":
    main()
