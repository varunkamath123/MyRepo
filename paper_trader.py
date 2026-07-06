"""
Kronos Futures — Daily Paper Trader
====================================
Runs once per day (after market close, 15:35 IST) via cron.

Workflow:
  1. Fetch latest daily OHLCV from yfinance
  2. Compute Kronos signal for each active instrument
  3. If not in a position and signal is valid → queue entry at tomorrow's open
  4. If in a position → check stop/trail/Kronos-reversal/SuperTrend exit
  5. At 09:20 IST the next day, "fill" queued entries at today's open price
  6. Log all actions to paper_trades.jsonl and print daily summary

Capital: one lot per instrument, max one open position at a time across all instruments.

Run manually:
    python paper_trader.py                  # signal check (normal daily run)
    python paper_trader.py --fill-open      # fill pending entries at today's open
    python paper_trader.py --status         # print current positions + P&L
    python paper_trader.py --reset          # wipe all state (fresh start)
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
class PendingEntry:
    instrument:  str
    direction:   str
    confidence:  float
    signal_date: str            # date signal was generated
    lot_size:    int

@dataclass
class PaperState:
    capital:       float
    positions:     dict[str, Position]    # instrument -> Position
    pending:       dict[str, PendingEntry]  # instrument -> PendingEntry
    realised_pnl:  float
    trade_count:   int


def _load_state() -> PaperState:
    if Path(STATE_FILE).exists():
        raw = json.loads(Path(STATE_FILE).read_text())
        positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        pending   = {k: PendingEntry(**v) for k, v in raw.get("pending", {}).items()}
        return PaperState(
            capital=raw["capital"],
            positions=positions,
            pending=pending,
            realised_pnl=raw.get("realised_pnl", 0.0),
            trade_count=raw.get("trade_count", 0),
        )
    return PaperState(
        capital=TOTAL_CAPITAL,
        positions={},
        pending={},
        realised_pnl=0.0,
        trade_count=0,
    )


def _save_state(state: PaperState):
    raw = {
        "capital":      state.capital,
        "realised_pnl": state.realised_pnl,
        "trade_count":  state.trade_count,
        "positions":    {k: asdict(v) for k, v in state.positions.items()},
        "pending":      {k: asdict(v) for k, v in state.pending.items()},
    }
    Path(STATE_FILE).write_text(json.dumps(raw, indent=2))


def _log_trade(record: dict):
    with open(TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Signal evaluation ─────────────────────────────────────────────────────────

def evaluate_signal(instrument: str, df: pd.DataFrame) -> tuple[str, float, float]:
    """Returns (direction, confidence, last_close)."""
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)
    ctx = df.copy()
    last_close = float(ctx["close"].iloc[-1])

    adx = compute_adx(ctx)
    if adx < MIN_ADX:
        log.info("[%s] ADX %.1f < %d — no signal", instrument, adx, MIN_ADX)
        return "NEUTRAL", 0.0, last_close

    direction, confidence, source = get_signal(ctx, force_fallback=False)
    log.info("[%s] Kronos: %s conf=%.0f%% src=%s adx=%.1f",
             instrument, direction, confidence * 100, source, adx)

    if confidence < p["kronos_conf_min"] or direction == "NEUTRAL":
        return "NEUTRAL", 0.0, last_close

    st = supertrend(ctx)
    if direction == "LONG" and st != "BULL":
        log.info("[%s] SuperTrend is BEAR — skipping LONG entry", instrument)
        return "NEUTRAL", 0.0, last_close
    if direction == "SHORT" and st != "BULL":
        pass  # SHORT + BEAR ST is fine

    return direction, confidence, last_close


# ── Exit check ────────────────────────────────────────────────────────────────

def check_exit(instrument: str, pos: Position, df: pd.DataFrame) -> Optional[str]:
    """Return exit reason string if position should be closed, else None."""
    p = INSTRUMENT_PARAMS.get(instrument, _DEFAULT_PARAMS)
    last_close = float(df["close"].iloc[-1])

    pnl_pct = ((last_close - pos.entry_price) / pos.entry_price
               if pos.direction == "LONG"
               else (pos.entry_price - last_close) / pos.entry_price)

    # Hard stop
    if pnl_pct <= -p["stop_loss_pct"]:
        return "STOP_LOSS"

    # Trail
    if pnl_pct >= p["trail_activate_pct"]:
        pos.trail_active = True
        if pos.direction == "LONG" and last_close > pos.hwm:
            pos.hwm = last_close
            pos.trail_stop = pos.hwm * (1 - p["trail_distance_pct"])
        elif pos.direction == "SHORT" and last_close < pos.hwm:
            pos.hwm = last_close
            pos.trail_stop = pos.hwm * (1 + p["trail_distance_pct"])

    if pos.trail_active:
        if pos.direction == "LONG" and last_close < pos.trail_stop:
            return "TRAIL_STOP"
        if pos.direction == "SHORT" and last_close > pos.trail_stop:
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

def cmd_signal_check(instruments: list[str]):
    """Run at 15:35 IST: generate signals for tomorrow's potential entries."""
    state = _load_state()
    today = date.today().isoformat()

    for inst in instruments:
        log.info("=== %s ===", inst)
        df = fetch_daily(inst, years=1)   # 1 year is enough for signal context

        # ── Exit check for open positions ──────────────────────────────────
        if inst in state.positions:
            pos = state.positions[inst]
            reason = check_exit(inst, pos, df)
            if reason:
                last_close = float(df["close"].iloc[-1])
                pts = ((last_close - pos.entry_price) if pos.direction == "LONG"
                       else (pos.entry_price - last_close))
                pnl = pts * pos.lot_size
                state.realised_pnl += pnl
                state.trade_count  += 1
                log.info("[%s] EXIT %s @ %.1f  reason=%s  PnL INR %+.0f",
                         inst, pos.direction, last_close, reason, pnl)
                _log_trade({
                    "event":       "EXIT",
                    "instrument":  inst,
                    "direction":   pos.direction,
                    "entry_date":  pos.entry_date,
                    "entry_price": pos.entry_price,
                    "exit_date":   today,
                    "exit_price":  last_close,
                    "exit_reason": reason,
                    "pnl_pts":     pts,
                    "pnl_inr":     pnl,
                    "lot_size":    pos.lot_size,
                })
                del state.positions[inst]
            else:
                last_close = float(df["close"].iloc[-1])
                pts = ((last_close - pos.entry_price) if pos.direction == "LONG"
                       else (pos.entry_price - last_close))
                log.info("[%s] HOLD %s  entry=%.1f  close=%.1f  unrealised INR %+.0f",
                         inst, pos.direction, pos.entry_price, last_close, pts * pos.lot_size)
            continue   # already in a position — don't generate new entry signal

        # ── Entry signal check ─────────────────────────────────────────────
        # Only enter if no position and capital is available
        if len(state.positions) >= 1:
            log.info("[%s] Another position is open — skipping entry check", inst)
            continue

        direction, confidence, last_close = evaluate_signal(inst, df)
        if direction != "NEUTRAL":
            lot = LOT_SIZES.get(inst, 1)
            state.pending[inst] = PendingEntry(
                instrument=inst,
                direction=direction,
                confidence=confidence,
                signal_date=today,
                lot_size=lot,
            )
            log.info("[%s] SIGNAL %s conf=%.0f%%  -> pending entry at tomorrow's open",
                     inst, direction, confidence * 100)
        else:
            if inst in state.pending:
                del state.pending[inst]
                log.info("[%s] Previous pending signal cleared (no signal today)", inst)

    _save_state(state)
    print_status(state)


def cmd_fill_open(instruments: list[str]):
    """Run at 09:20 IST: fill pending entries at today's open price."""
    state = _load_state()
    today = date.today().isoformat()

    for inst in instruments:
        if inst not in state.pending:
            continue

        pending = state.pending[inst]

        # Don't fill if signal is stale (more than 1 day old)
        signal_dt = date.fromisoformat(pending.signal_date)
        if (date.today() - signal_dt).days > 1:
            log.info("[%s] Pending signal is stale (%s) — skipping fill", inst, pending.signal_date)
            del state.pending[inst]
            continue

        # Use today's open from yfinance (or fallback to yesterday's close)
        df = fetch_daily(inst, years=1)
        today_open = float(df["open"].iloc[-1])

        log.info("[%s] FILL %s @ %.1f (today's open)", inst, pending.direction, today_open)
        state.positions[inst] = Position(
            instrument=inst,
            direction=pending.direction,
            entry_date=today,
            entry_price=today_open,
            lot_size=pending.lot_size,
            hwm=today_open,
            trail_active=False,
            trail_stop=0.0,
        )
        _log_trade({
            "event":       "ENTRY",
            "instrument":  inst,
            "direction":   pending.direction,
            "entry_date":  today,
            "entry_price": today_open,
            "confidence":  pending.confidence,
            "lot_size":    pending.lot_size,
        })
        del state.pending[inst]

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

    if state.pending:
        print("  PENDING ENTRIES (fill at tomorrow's open):")
        for inst, pnd in state.pending.items():
            print(f"    {inst:<10} {pnd.direction}  conf={pnd.confidence:.0%}  "
                  f"signal={pnd.signal_date}")
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
    parser = argparse.ArgumentParser(description="Kronos paper trader")
    parser.add_argument("--fill-open",  action="store_true",
                        help="Fill pending entries at today's open (run at 09:20 IST)")
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
    elif args.fill_open:
        cmd_fill_open(instruments)
    else:
        cmd_signal_check(instruments)


if __name__ == "__main__":
    main()
