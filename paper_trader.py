"""
Kronos Futures — Daily Paper Trader (same-day close entry)
==========================================================
Runs once per day near market close (~15:20 IST) via cron.

Why near-close, same-day entry?
  The old flow generated a signal at 15:35 and only entered at the NEXT day's
  open — burning the overnight gap plus the first day of the forecast window.
  This version signals on the last 128 COMPLETE daily candles (through the last
  closed session) and enters immediately at the live price (~today's close),
  so the full multi-day forecast window is captured.

Workflow (single daily run):
  1. Fetch complete daily OHLCV (Upstox, fallback yfinance) — context bars
  2. Fetch live price (Upstox LTP) — the same-day entry/exit price
  3. PASS 1 — exits: for each open position, check stop/trail/Kronos-rev/ST-flip
     against the live price; close same-day if any fires
  4. PASS 2 — entries: if flat and no other position open, evaluate the Kronos
     signal; if all gates pass (incl. MiroFish news veto), ENTER at live price
  5. Log all actions to paper_trades.jsonl and print the daily summary

MiroFish news gate:
  Kronos reads price shape only — no news, macro, or flow awareness. Run
  mirofish_swarm.py before this script (see cron) to write mirofish_scores.json;
  a strongly opposing news lean vetoes an otherwise-qualified Kronos entry.
  Missing/stale MiroFish data does not block entries (gate is skipped, logged).

Capital: one lot per instrument, max one open position across all instruments.

Run manually:
    python paper_trader.py                  # daily run (signal + same-day entry/exit)
    python paper_trader.py --status         # print current positions + P&L
    python paper_trader.py --reset          # wipe all state (fresh start)
    python paper_trader.py --sensex         # include SENSEX in active instruments
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from backtest import (
    INSTRUMENT_PARAMS, _DEFAULT_PARAMS, EXIT_ON_KRONOS_REVERSAL,
    EXIT_ON_SUPERTREND_FLIP, MIN_ADX, FORECAST_BARS,
    compute_adx, supertrend, fetch_daily, get_signal,
)
from config import INSTRUMENTS

# Live Upstox data (complete daily bars + LTP). Falls back to yfinance if unavailable.
try:
    from brokers.upstox_data import load_daily_ohlcv as _upstox_daily, get_ltp as _upstox_ltp
    _UPSTOX_OK = True
except Exception:
    _UPSTOX_OK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TOTAL_CAPITAL     = 350_000          # INR — ₹3.5L paper capital
STATE_FILE        = "paper_state.json"
TRADES_LOG        = "paper_trades.jsonl"

# Instruments active for paper trading (SENSEX added if --sensex flag used)
ACTIVE_INSTRUMENTS = ["NIFTY", "BANKNIFTY"]

LOT_SIZES = {
    "NIFTY":     65,
    "BANKNIFTY": 30,
    "SENSEX":    20,
}

# MiroFish news veto: only block on a fairly strong OPPOSING lean, not a mild
# one — this is a veto for outright contradiction, not a confirmation filter.
MIROFISH_FILE            = Path(__file__).parent / "mirofish_scores.json"
MIROFISH_MAX_AGE_HOURS   = 6      # ignore stale data (e.g. yesterday's run)
MIROFISH_BEARISH_VETO    = 0.35   # score below this vetoes a LONG entry
MIROFISH_BULLISH_VETO    = 0.65   # score above this vetoes a SHORT entry

# ── State schema ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    instrument:    str
    direction:     str
    entry_date:    str          # ISO date string
    entry_price:   float
    lot_size:      int
    hwm:           float        # high-water mark for trail
    trail_active:  bool
    trail_stop:    float

@dataclass
class PaperState:
    capital:       float
    positions:     dict[str, Position]    # instrument -> Position
    realised_pnl:  float
    trade_count:   int


def _load_state() -> PaperState:
    if Path(STATE_FILE).exists():
        raw = json.loads(Path(STATE_FILE).read_text())
        positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return PaperState(
            capital=raw["capital"],
            positions=positions,
            realised_pnl=raw.get("realised_pnl", 0.0),
            trade_count=raw.get("trade_count", 0),
        )
    return PaperState(
        capital=TOTAL_CAPITAL,
        positions={},
        realised_pnl=0.0,
        trade_count=0,
    )


def _save_state(state: PaperState):
    raw = {
        "capital":      state.capital,
        "realised_pnl": state.realised_pnl,
        "trade_count":  state.trade_count,
        "positions":    {k: asdict(v) for k, v in state.positions.items()},
    }
    Path(STATE_FILE).write_text(json.dumps(raw, indent=2))


# ── Data access: complete daily context + live entry price ────────────────────

def get_context(instrument: str, bars: int = 150) -> pd.DataFrame:
    """
    Return complete daily OHLCV bars (through the last closed session) as context.
    Prefers Upstox (reliable, no lag); falls back to yfinance.
    """
    if _UPSTOX_OK:
        try:
            df = _upstox_daily(instrument, bars=bars)
            if len(df) >= 60:
                return df
            log.warning("[%s] Upstox returned only %d daily bars — falling back to yfinance",
                        instrument, len(df))
        except Exception as e:
            log.warning("[%s] Upstox daily fetch failed (%s) — falling back to yfinance",
                        instrument, e)
    df = fetch_daily(instrument, years=1)
    # Drop a partial current-day bar if yfinance included one
    today = pd.Timestamp(date.today())
    df = df[pd.to_datetime(df["datetime"]).dt.normalize() < today].reset_index(drop=True)
    return df


def get_entry_price(instrument: str, context: pd.DataFrame) -> tuple[float, str]:
    """
    Return (price, source) for the same-day entry/exit fill.
    Prefers live Upstox LTP (~today's close during market hours);
    falls back to the last complete daily close.
    """
    if _UPSTOX_OK:
        try:
            return float(_upstox_ltp(instrument)), "upstox_ltp"
        except Exception as e:
            log.warning("[%s] LTP fetch failed (%s) — using last daily close", instrument, e)
    return float(context["close"].iloc[-1]), "last_close"


def get_mirofish(instrument: str) -> Optional[dict]:
    """
    Return {"lean": ..., "score": ..., "reasons": [...]} for `instrument` if
    mirofish_scores.json exists and is fresh, else None (gate is skipped).
    """
    if not MIROFISH_FILE.exists():
        return None
    try:
        raw = json.loads(MIROFISH_FILE.read_text())
        generated_at = datetime.fromisoformat(raw["generated_at"])
        age_hours = (datetime.now(generated_at.tzinfo) - generated_at).total_seconds() / 3600
        if age_hours > MIROFISH_MAX_AGE_HOURS:
            log.info("[%s] MiroFish data is %.1fh old (max %dh) — skipping news gate",
                     instrument, age_hours, MIROFISH_MAX_AGE_HOURS)
            return None
        return raw.get(instrument)
    except Exception as e:
        log.warning("[%s] Failed to read MiroFish data (%s) — skipping news gate", instrument, e)
        return None


def _log_trade(record: dict):
    with open(TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Signal evaluation ─────────────────────────────────────────────────────────

def evaluate_signal(instrument: str, df: pd.DataFrame) -> tuple[str, float]:
    """
    Evaluate the full entry gate chain on complete daily context `df`.
    Returns (direction, confidence); direction is NEUTRAL if any gate fails.
    """
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)
    ctx = df.copy()

    adx = compute_adx(ctx)
    if adx < MIN_ADX:
        log.info("[%s] Gate FAIL: ADX %.1f < %d (ranging market)", instrument, adx, MIN_ADX)
        return "NEUTRAL", 0.0

    direction, confidence, source = get_signal(ctx, force_fallback=False)
    log.info("[%s] Kronos: %s conf=%.0f%% src=%s adx=%.1f",
             instrument, direction, confidence * 100, source, adx)

    if direction == "NEUTRAL":
        return "NEUTRAL", 0.0
    if confidence < p["kronos_conf_min"]:
        log.info("[%s] Gate FAIL: confidence %.0f%% < %.0f%%",
                 instrument, confidence * 100, p["kronos_conf_min"] * 100)
        return "NEUTRAL", 0.0

    st = supertrend(ctx)
    if direction == "LONG" and st != "BULL":
        log.info("[%s] Gate FAIL: SuperTrend %s not aligned with LONG", instrument, st)
        return "NEUTRAL", 0.0
    if direction == "SHORT" and st != "BEAR":
        log.info("[%s] Gate FAIL: SuperTrend %s not aligned with SHORT", instrument, st)
        return "NEUTRAL", 0.0

    mf = get_mirofish(instrument)
    if mf is not None:
        mf_score = float(mf["score"])
        if direction == "LONG" and mf_score < MIROFISH_BEARISH_VETO:
            log.info("[%s] Gate FAIL: MiroFish bearish (score=%.2f) vetoes LONG — %s",
                     instrument, mf_score, "; ".join(mf.get("reasons", [])[:2]))
            return "NEUTRAL", 0.0
        if direction == "SHORT" and mf_score > MIROFISH_BULLISH_VETO:
            log.info("[%s] Gate FAIL: MiroFish bullish (score=%.2f) vetoes SHORT — %s",
                     instrument, mf_score, "; ".join(mf.get("reasons", [])[:2]))
            return "NEUTRAL", 0.0
        log.info("[%s] MiroFish %s (score=%.2f) does not contradict %s",
                 instrument, mf.get("lean"), mf_score, direction)

    log.info("[%s] Gate PASS: %s conf=%.0f%% adx=%.1f st=%s — entry qualified",
             instrument, direction, confidence * 100, adx, st)
    return direction, confidence


# ── Exit check ────────────────────────────────────────────────────────────────

def check_exit(instrument: str, pos: Position, df: pd.DataFrame,
               price: float) -> Optional[str]:
    """
    Return exit reason if the position should close at `price` (live), else None.
    Indicators/signal are computed on the complete daily context `df`; the P&L
    and stop/trail checks use the live `price`.
    """
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)

    pnl_pct = ((price - pos.entry_price) / pos.entry_price
               if pos.direction == "LONG"
               else (pos.entry_price - price) / pos.entry_price)

    # Hard stop
    if pnl_pct <= -p["stop_loss_pct"]:
        return "STOP_LOSS"

    # Trail
    if pnl_pct >= p["trail_activate_pct"]:
        pos.trail_active = True
        if pos.direction == "LONG" and price > pos.hwm:
            pos.hwm = price
            pos.trail_stop = pos.hwm * (1 - p["trail_distance_pct"])
        elif pos.direction == "SHORT" and price < pos.hwm:
            pos.hwm = price
            pos.trail_stop = pos.hwm * (1 + p["trail_distance_pct"])

    if pos.trail_active:
        if pos.direction == "LONG" and price < pos.trail_stop:
            return "TRAIL_STOP"
        if pos.direction == "SHORT" and price > pos.trail_stop:
            return "TRAIL_STOP"

    # Signal exits
    try:
        direction, confidence, _ = get_signal(df, force_fallback=False)
        st = supertrend(df)

        if EXIT_ON_KRONOS_REVERSAL:
            opp = "SHORT" if pos.direction == "LONG" else "LONG"
            if direction == opp and confidence >= p["kronos_rev_conf_min"]:
                return "KRONOS_REV"

        if EXIT_ON_SUPERTREND_FLIP:
            if pos.direction == "LONG" and st == "BEAR":
                return "ST_FLIP"
            if pos.direction == "SHORT" and st == "BULL":
                return "ST_FLIP"
    except Exception as e:
        log.warning("[%s] Signal error during exit check: %s", instrument, e)

    return None


# ── Main commands ─────────────────────────────────────────────────────────────

def cmd_daily_run(instruments: list[str]):
    """
    Single daily run near market close (~15:20 IST).
    PASS 1 closes any exits at the live price; PASS 2 enters new signals
    at the live price the same day.
    """
    state = _load_state()
    today = date.today().isoformat()

    # Fetch context + live price once per instrument
    ctx: dict[str, pd.DataFrame] = {}
    px:  dict[str, float]        = {}
    for inst in instruments:
        try:
            df = get_context(inst, bars=150)
            price, src = get_entry_price(inst, df)
            ctx[inst] = df
            px[inst]  = price
            log.info("[%s] context=%d bars  last_close=%.1f  live=%.1f (%s)",
                     inst, len(df), float(df["close"].iloc[-1]), price, src)
        except Exception as e:
            log.error("[%s] Data fetch failed: %s — skipping", inst, e)

    # ── PASS 1: exits ──────────────────────────────────────────────────────
    for inst in list(state.positions.keys()):
        if inst not in ctx:
            continue
        pos    = state.positions[inst]
        price  = px[inst]
        reason = check_exit(inst, pos, ctx[inst], price)
        pts = ((price - pos.entry_price) if pos.direction == "LONG"
               else (pos.entry_price - price))
        if reason:
            pnl = pts * pos.lot_size
            state.realised_pnl += pnl
            state.trade_count  += 1
            log.info("[%s] EXIT %s @ %.1f  reason=%s  PnL INR %+.0f  [held from %s]",
                     inst, pos.direction, price, reason, pnl, pos.entry_date)
            _log_trade({
                "event":       "EXIT",
                "instrument":  inst,
                "direction":   pos.direction,
                "entry_date":  pos.entry_date,
                "entry_price": pos.entry_price,
                "exit_date":   today,
                "exit_price":  price,
                "exit_reason": reason,
                "pnl_pts":     pts,
                "pnl_inr":     pnl,
                "lot_size":    pos.lot_size,
            })
            del state.positions[inst]
        else:
            log.info("[%s] HOLD %s  entry=%.1f  live=%.1f  unrealised INR %+.0f",
                     inst, pos.direction, pos.entry_price, price, pts * pos.lot_size)

    # ── PASS 2: entries (max one open position across all instruments) ─────
    for inst in instruments:
        if inst not in ctx:
            continue
        if inst in state.positions:
            continue
        if len(state.positions) >= 1:
            log.info("[%s] A position is already open — one at a time, skipping entry", inst)
            continue

        direction, confidence = evaluate_signal(inst, ctx[inst])
        if direction == "NEUTRAL":
            continue

        price = px[inst]
        lot   = LOT_SIZES.get(inst, 1)
        state.positions[inst] = Position(
            instrument=inst,
            direction=direction,
            entry_date=today,
            entry_price=price,
            lot_size=lot,
            hwm=price,
            trail_active=False,
            trail_stop=0.0,
        )
        log.info("[%s] ENTRY %s @ %.1f  conf=%.0f%%  lot=%d  (same-day close)",
                 inst, direction, price, confidence * 100, lot)
        _log_trade({
            "event":       "ENTRY",
            "instrument":  inst,
            "direction":   direction,
            "entry_date":  today,
            "entry_price": price,
            "confidence":  confidence,
            "lot_size":    lot,
        })

    _save_state(state)
    print_status(state)


def print_status(state: PaperState):
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  PAPER TRADER STATUS  |  Capital: INR {state.capital:,.0f}")
    print(f"  Realised P&L : INR {state.realised_pnl:+,.0f}  ({state.trade_count} trades closed)")
    print(sep)

    if state.positions:
        print("  OPEN POSITIONS:")
        for inst, pos in state.positions.items():
            print(f"    {inst:<10} {pos.direction}  entry={pos.entry_price:.1f}  "
                  f"date={pos.entry_date}  lot={pos.lot_size}")
    else:
        print("  No open positions.")
    print(sep)


def cmd_status():
    print_status(_load_state())


def cmd_reset():
    for f in [STATE_FILE]:
        if Path(f).exists():
            Path(f).unlink()
    log.info("State reset. Trades log preserved at %s.", TRADES_LOG)
    _load_state()   # creates fresh state
    _save_state(_load_state())


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kronos paper trader (same-day close entry)")
    parser.add_argument("--status",     action="store_true",
                        help="Print current positions and P&L")
    parser.add_argument("--reset",      action="store_true",
                        help="Wipe all state and start fresh")
    parser.add_argument("--sensex",     action="store_true",
                        help="Include SENSEX in active instruments")
    args = parser.parse_args()

    instruments = list(ACTIVE_INSTRUMENTS)
    if args.sensex:
        instruments.append("SENSEX")

    if args.reset:
        cmd_reset()
    elif args.status:
        cmd_status()
    else:
        cmd_daily_run(instruments)


if __name__ == "__main__":
    main()
