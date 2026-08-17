"""
Kronos Futures Bot — Daily Walk-Forward Backtest
=================================================
Fetches daily OHLCV from yfinance (default 3 years) for NIFTY, BANKNIFTY,
and SENSEX (spot index as futures proxy).

Signal logic (run at each day's close):
  - Kronos predicts next 12 daily bars (~2.5 weeks)
  - Entry: next day's open if ADX >= 25 AND confidence >= threshold
  - Exit: stop-loss / trailing stop / Kronos reversal / SuperTrend flip

Why daily candles:
  - Closer to Kronos training domain (it was trained on daily stock OHLCV)
  - 3 years of history → statistically meaningful backtest
  - Positional holds of 1–20 days; realistic for futures

Run:
    python backtest.py                         # NIFTY + BANKNIFTY + SENSEX
    python backtest.py NIFTY BANKNIFTY         # specific instruments
    python backtest.py --fallback              # force EMA signal (skip Kronos)
    python backtest.py --years 5               # extend lookback to 5 years
    python backtest.py --out my_results.csv    # custom output path

Output: per-trade log + summary metrics → stdout + CSV
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(__file__))

from config import INSTRUMENTS, MIN_ADX

# ── Global defaults ───────────────────────────────────────────────────────────
FORECAST_BARS        = 5       # daily bars ahead (1 week; enough for direction)
MIN_CONTEXT_BARS     = 60      # minimum history bars before first signal
SIGNAL_CACHE_BARS    = 3       # re-run Kronos every N bars to save CPU

EXIT_ON_KRONOS_REVERSAL  = True
EXIT_ON_SUPERTREND_FLIP  = True

# ── Per-instrument risk params ────────────────────────────────────────────────
# Tuned separately based on 3-year Kronos backtest results.
# NIFTY: avg loss was > avg win with shared 3% stop → raise conf_min to filter
#        weak entries and tighten stop slightly.
# BANKNIFTY: PF 1.97 / Sharpe 4.32 — keep working params, earlier trail activate.
# SENSEX: no backtest data yet; start conservative, same as NIFTY.
INSTRUMENT_PARAMS = {
    "NIFTY": {
        "stop_loss_pct":       0.025,   # 2.5%: tighter stop, smaller losses
        "trail_activate_pct":  0.060,   # 6%
        "trail_distance_pct":  0.025,   # 2.5%
        "kronos_conf_min":     0.55,    # higher bar: only high-conviction entries
        "kronos_rev_conf_min": 0.50,
    },
    "BANKNIFTY": {
        "stop_loss_pct":       0.030,   # 3%: BNF moves bigger, needs room
        "trail_activate_pct":  0.050,   # 5%: activate trail earlier to lock in big moves
        "trail_distance_pct":  0.025,   # 2.5%
        "kronos_conf_min":     0.45,    # keep as-is (PF 1.97 already)
        "kronos_rev_conf_min": 0.45,
    },
    "SENSEX": {
        "stop_loss_pct":       0.025,   # conservative until we have backtest
        "trail_activate_pct":  0.060,
        "trail_distance_pct":  0.025,
        "kronos_conf_min":     0.55,
        "kronos_rev_conf_min": 0.50,
    },
}
_DEFAULT_PARAMS = INSTRUMENT_PARAMS["NIFTY"]  # fallback for unknown instruments

# ── yfinance symbols ──────────────────────────────────────────────────────────
_YF_SYMBOLS = {
    "NIFTY":     "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX":    "^BSESN",
}

# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_daily(instrument: str, years: int = 3) -> pd.DataFrame:
    import yfinance as yf

    symbol = _YF_SYMBOLS[instrument]
    log.info("[DATA] Downloading %d-year daily data for %s (%s) …", years, instrument, symbol)
    raw = yf.download(symbol, period=f"{years}y", interval="1d",
                      progress=False, auto_adjust=True)

    if raw.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0].lower() for c in raw.columns]
    else:
        raw.columns = [c.lower() for c in raw.columns]

    raw = raw[["open", "high", "low", "close", "volume"]].dropna()
    raw.index = pd.to_datetime(raw.index)
    raw = raw.sort_index().reset_index()
    raw = raw.rename(columns={raw.columns[0]: "datetime"})

    log.info("[DATA] %s: %d daily bars (%.1f years)",
             instrument, len(raw),
             (raw["datetime"].iloc[-1] - raw["datetime"].iloc[0]).days / 365)
    return raw


# ── Technical indicators ──────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr      = tr.ewm(span=period, min_periods=period).mean()
    dm_plus  = (high.diff()).clip(lower=0)
    dm_minus = (-low.diff()).clip(lower=0)
    di_plus  = 100 * dm_plus.ewm(span=period, min_periods=period).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, min_periods=period).mean() / atr
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + 1e-8)
    return float(dx.ewm(span=period, min_periods=period).mean().iloc[-1])


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> str:
    hl2   = (df["high"] + df["low"]) / 2
    tr    = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr   = tr.ewm(span=period, min_periods=period).mean()
    lower = hl2 - multiplier * atr
    return "BULL" if df["close"].iloc[-1] > lower.iloc[-1] else "BEAR"


# ── Signal ────────────────────────────────────────────────────────────────────

def get_signal(ctx: pd.DataFrame, force_fallback: bool = False) -> tuple[str, float, str]:
    """
    Returns (direction, confidence, source).
    direction ∈ {"LONG", "SHORT", "NEUTRAL"}
    """
    from core.kronos_signal import forecast as _kf

    if force_fallback:
        from core import kronos_signal as _ks
        _orig = _ks._load_predictor
        _ks._load_predictor = _force_fail
        try:
            f = _kf(ctx, FORECAST_BARS)
        finally:
            _ks._load_predictor = _orig
    else:
        f = _kf(ctx, FORECAST_BARS)

    return f.direction, f.confidence, f.source


def _force_fail():
    raise RuntimeError("fallback forced")


# ── Trade record ──────────────────────────────────────────────────────────────

@dataclass
class Trade:
    instrument:  str
    direction:   str
    entry_bar:   int
    entry_date:  pd.Timestamp
    entry_price: float
    lot_size:    int
    exit_bar:    int             = 0
    exit_date:   Optional[pd.Timestamp] = None
    exit_price:  float          = 0.0
    exit_reason: str            = ""
    pnl_pts:     float          = 0.0
    pnl_inr:     float          = 0.0
    hold_days:   int            = 0


# ── Backtest engine ───────────────────────────────────────────────────────────

def backtest_instrument(
    instrument: str,
    df: pd.DataFrame,
    force_fallback: bool = False,
) -> list[Trade]:
    """
    Walk forward through daily bars.

    Signal computed on bar i close → entry at bar i+1 open.
    Exits checked each subsequent bar against close price.
    """
    lot_size = INSTRUMENTS.get(instrument, {}).get("lot_size", 1)
    if instrument not in INSTRUMENTS and instrument == "SENSEX":
        lot_size = 20
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)
    stop_loss_pct       = p["stop_loss_pct"]
    trail_activate_pct  = p["trail_activate_pct"]
    trail_distance_pct  = p["trail_distance_pct"]
    kronos_conf_min     = p["kronos_conf_min"]
    kronos_rev_conf_min = p["kronos_rev_conf_min"]

    trades: list[Trade] = []

    pos:          Optional[Trade] = None
    hwm:          float           = 0.0
    trail_active: bool            = False
    trail_stop:   float           = 0.0

    # Signal cache: avoid running Kronos inference every bar (expensive on CPU).
    # Re-evaluate every SIGNAL_CACHE_BARS bars, or immediately after a position close.
    _cached_dir:  str   = "NEUTRAL"
    _cached_conf: float = 0.0
    _cached_src:  str   = ""
    _last_signal_bar: int = -999

    log.info("[BT] %s | %d bars | lot=%d", instrument, len(df), lot_size)

    for i in range(MIN_CONTEXT_BARS, len(df) - 1):
        ctx   = df.iloc[: i + 1].copy()          # history up to and including bar i
        today = df.iloc[i]
        nxt   = df.iloc[i + 1]                    # entry / exit prices come from next bar
        nxt_open  = float(nxt["open"])
        nxt_close = float(nxt["close"])
        nxt_date  = pd.Timestamp(nxt["datetime"])

        # Refresh signal cache if stale
        need_signal = (i - _last_signal_bar) >= SIGNAL_CACHE_BARS
        if need_signal:
            try:
                _cached_dir, _cached_conf, _cached_src = get_signal(ctx, force_fallback)
                _last_signal_bar = i
            except Exception as e:
                log.debug("[BT] Signal error bar %d: %s", i, e)

        # ── Exit check (against next bar's close after entry) ─────────────────
        if pos is not None:
            price = nxt_close
            pnl_pct = ((price - pos.entry_price) / pos.entry_price
                       if pos.direction == "LONG"
                       else (pos.entry_price - price) / pos.entry_price)

            # Hard stop
            if pnl_pct <= -stop_loss_pct:
                _close(pos, i + 1, nxt_date, price, "STOP_LOSS", trades)
                pos = None; hwm = trail_stop = 0.0; trail_active = False
                _last_signal_bar = -999   # force refresh after exit
                continue

            # Trailing stop
            if pnl_pct >= trail_activate_pct:
                trail_active = True
                if price > hwm or hwm == 0.0:
                    hwm = price
                    trail_stop = (hwm * (1 - trail_distance_pct) if pos.direction == "LONG"
                                  else hwm * (1 + trail_distance_pct))

            if trail_active:
                hit = ((pos.direction == "LONG"  and price < trail_stop) or
                       (pos.direction == "SHORT" and price > trail_stop))
                if hit:
                    _close(pos, i + 1, nxt_date, price, "TRAIL_STOP", trades)
                    pos = None; hwm = trail_stop = 0.0; trail_active = False
                    _last_signal_bar = -999
                    continue

            # Signal-based exits (use cached signal)
            try:
                st = supertrend(ctx)

                if EXIT_ON_KRONOS_REVERSAL:
                    opp = "SHORT" if pos.direction == "LONG" else "LONG"
                    if _cached_dir == opp and _cached_conf >= kronos_rev_conf_min:
                        _close(pos, i + 1, nxt_date, price, "KRONOS_REV", trades)
                        pos = None; hwm = trail_stop = 0.0; trail_active = False
                        _last_signal_bar = -999
                        continue

                if EXIT_ON_SUPERTREND_FLIP:
                    if (pos.direction == "LONG"  and st == "BEAR") or \
                       (pos.direction == "SHORT" and st == "BULL"):
                        _close(pos, i + 1, nxt_date, price, "ST_FLIP", trades)
                        pos = None; hwm = trail_stop = 0.0; trail_active = False
                        _last_signal_bar = -999
                        continue
            except Exception as e:
                log.debug("[BT] Exit signal error bar %d: %s", i, e)

            continue   # still holding

        # ── Entry (signal on today's close, enter at next open) ──────────────
        try:
            adx = compute_adx(ctx)
            if adx < MIN_ADX:
                continue

            direction, confidence, source = _cached_dir, _cached_conf, _cached_src
            if confidence < kronos_conf_min or direction == "NEUTRAL":
                continue

            st = supertrend(ctx)
            if direction == "LONG"  and st != "BULL":
                continue
            if direction == "SHORT" and st != "BEAR":
                continue

            pos = Trade(
                instrument=instrument,
                direction=direction,
                entry_bar=i + 1,
                entry_date=nxt_date,
                entry_price=nxt_open,
                lot_size=lot_size,
            )
            hwm = nxt_open
            trail_active = False
            trail_stop   = 0.0
            log.info("[BT] ENTRY  %s %-5s @ %8.1f  conf=%.0f%%  src=%s  adx=%.1f",
                     instrument, direction, nxt_open, confidence * 100, source, adx)

        except Exception as e:
            log.debug("[BT] Entry error bar %d: %s", i, e)

    # Close any open position at end of data
    if pos is not None:
        last = df.iloc[-1]
        _close(pos, len(df) - 1,
               pd.Timestamp(last["datetime"]),
               float(last["close"]),
               "END_OF_DATA", trades)

    return trades


def _close(pos: Trade, bar: int, date: pd.Timestamp,
           price: float, reason: str, trades: list[Trade]):
    pos.exit_bar   = bar
    pos.exit_date  = date
    pos.exit_price = price
    pos.exit_reason = reason
    pos.hold_days  = bar - pos.entry_bar
    pts = (price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - price)
    pos.pnl_pts = pts
    pos.pnl_inr = pts * pos.lot_size
    trades.append(pos)
    log.info("[BT] EXIT   %s %s @ %.1f  %-12s  %+.0f pts / INR %+.0f  [%d days]",
             pos.instrument, pos.direction, price, reason, pts, pos.pnl_inr, pos.hold_days)


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade], instrument: str = "ALL") -> dict:
    if not trades:
        return {}

    pnl  = np.array([t.pnl_inr for t in trades])
    wins  = pnl[pnl > 0]
    losses = pnl[pnl <= 0]

    total_pnl     = float(pnl.sum())
    win_rate      = len(wins) / len(pnl)
    avg_win       = float(wins.mean())   if len(wins)   else 0.0
    avg_loss      = float(losses.mean()) if len(losses) else 0.0
    profit_factor = abs(wins.sum() / losses.sum()) if losses.sum() != 0 else float("inf")

    # Expectancy per trade
    expectancy = total_pnl / len(pnl)

    # Sharpe: per-trade series, annualised (~252 trading days)
    sharpe = (pnl.mean() / (pnl.std() + 1e-8)) * np.sqrt(252) if len(pnl) > 1 else 0.0

    # Max drawdown on cumulative INR P&L
    cum   = np.cumsum(pnl)
    peak  = np.maximum.accumulate(cum)
    dd    = cum - peak
    max_dd = float(dd.min())

    hold_days = [t.hold_days for t in trades]
    reasons   = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    # Calendar span
    first = trades[0].entry_date
    last  = trades[-1].exit_date or trades[-1].entry_date
    span_days = (last - first).days if last and first else 0

    return {
        "instrument":       instrument,
        "trades":           len(trades),
        "wins":             int(len(wins)),
        "losses":           int(len(losses)),
        "win_rate":         win_rate,
        "total_pnl_inr":    total_pnl,
        "avg_win_inr":      avg_win,
        "avg_loss_inr":     avg_loss,
        "expectancy_inr":   expectancy,
        "profit_factor":    profit_factor,
        "sharpe_ann":       sharpe,
        "max_drawdown_inr": max_dd,
        "avg_hold_days":    float(np.mean(hold_days)) if hold_days else 0.0,
        "exit_reasons":     reasons,
        "span_days":        span_days,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(trades: list[Trade], m: dict):
    sep = "=" * 70
    inst = m.get("instrument", "")
    print(f"\n{sep}")
    print(f"  BACKTEST REPORT  --  {inst}")
    print(sep)

    if not m:
        print("  No trades generated.")
        print(sep)
        return

    if trades and trades[-1].exit_date:
        print(f"  Period        : {trades[0].entry_date.date()} to {trades[-1].exit_date.date()}"
              f"  ({m['span_days']} days)")
    print(f"  Trades        : {m['trades']}  ({m['wins']}W / {m['losses']}L)")
    print(f"  Win rate      : {m['win_rate']:.1%}")
    print(f"  Total P&L     : INR {m['total_pnl_inr']:>12,.0f}")
    print(f"  Avg win       : INR {m['avg_win_inr']:>12,.0f}")
    print(f"  Avg loss      : INR {m['avg_loss_inr']:>12,.0f}")
    print(f"  Expectancy    : INR {m['expectancy_inr']:>12,.0f}  per trade")
    print(f"  Profit factor : {m['profit_factor']:.2f}")
    print(f"  Sharpe (ann)  : {m['sharpe_ann']:.2f}")
    print(f"  Max drawdown  : INR {m['max_drawdown_inr']:>12,.0f}")
    print(f"  Avg hold      : {m['avg_hold_days']:.1f} days")
    if m["exit_reasons"]:
        reasons_str = "  |  ".join(f"{k}: {v}" for k, v in sorted(m["exit_reasons"].items()))
        print(f"  Exit reasons  : {reasons_str}")
    print(sep)

    if trades:
        print(f"\n  Trade log:")
        hdr = (f"  {'#':>3}  {'Inst':<10}  {'Dir':5}  {'Entry':>10}  "
               f"{'Entry Price':>11}  {'Exit Price':>10}  {'Hold':>5}  "
               f"{'P&L pts':>8}  {'P&L INR':>10}  Reason")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for n, t in enumerate(trades, 1):
            ep = t.exit_date.date() if t.exit_date else "open"
            print(f"  {n:>3}  {t.instrument:<10}  {t.direction:5}  "
                  f"{str(t.entry_date.date()):>10}  {t.entry_price:>11.1f}  "
                  f"{t.exit_price:>10.1f}  {t.hold_days:>5}d  "
                  f"{t.pnl_pts:>+8.1f}  {t.pnl_inr:>+10,.0f}  {t.exit_reason}")


def save_csv(all_trades: list[Trade], path: str):
    if not all_trades:
        log.info("No trades to save.")
        return
    rows = [{
        "instrument":  t.instrument,
        "direction":   t.direction,
        "entry_date":  t.entry_date,
        "entry_price": t.entry_price,
        "exit_date":   t.exit_date,
        "exit_price":  t.exit_price,
        "exit_reason": t.exit_reason,
        "hold_days":   t.hold_days,
        "pnl_pts":     t.pnl_pts,
        "pnl_inr":     t.pnl_inr,
        "lot_size":    t.lot_size,
    } for t in all_trades]
    pd.DataFrame(rows).to_csv(path, index=False)
    log.info("Results saved -> %s", path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kronos Futures daily backtest")
    parser.add_argument("instruments", nargs="*",
                        default=["NIFTY", "BANKNIFTY", "SENSEX"],
                        help="Instruments (default: NIFTY BANKNIFTY SENSEX)")
    parser.add_argument("--fallback", action="store_true",
                        help="Force EMA fallback -- skip Kronos model")
    parser.add_argument("--years", type=int, default=3,
                        help="Years of history to download (default: 3)")
    parser.add_argument("--out", default="backtest_results_daily.csv",
                        help="CSV output path")
    args = parser.parse_args()

    valid = {**INSTRUMENTS, "SENSEX": {"lot_size": 20, "live": False}}
    instruments = [i.upper() for i in args.instruments if i.upper() in valid]
    if not instruments:
        log.error("No valid instruments. Choose from: %s", list(valid.keys()))
        sys.exit(1)

    all_trades: list[Trade] = []

    for inst in instruments:
        try:
            df = fetch_daily(inst, years=args.years)
        except Exception as e:
            log.error("[BT] Cannot fetch data for %s: %s", inst, e)
            continue

        trades  = backtest_instrument(inst, df, force_fallback=args.fallback)
        metrics = compute_metrics(trades, inst)
        print_report(trades, metrics)
        all_trades.extend(trades)

    if len(instruments) > 1 and all_trades:
        combined = compute_metrics(all_trades, "COMBINED")
        print_report(all_trades, combined)

    save_csv(all_trades, args.out)


if __name__ == "__main__":
    main()
