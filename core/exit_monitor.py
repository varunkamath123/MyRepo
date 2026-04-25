"""
Dynamic Exit Monitor — checks all exit conditions every 5 min bar.
No fixed hold duration. Exits on: SL, trail, Kronos reversal, sentiment flip, ST flip.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import logging

from config import (
    STOP_LOSS_PCT, TRAIL_ACTIVATE_PCT, TRAIL_DISTANCE_PCT,
    EXIT_ON_KRONOS_REVERSAL, EXIT_ON_SENTIMENT_FLIP, EXIT_ON_SUPERTREND_FLIP,
)

log = logging.getLogger(__name__)


@dataclass
class Position:
    instrument: str
    direction: str              # "LONG" | "SHORT"
    entry_price: float
    entry_kronos_direction: str
    entry_sentiment: float
    entry_supertrend: str       # "BULL" | "BEAR"
    lots: int
    high_water_mark: float = 0.0
    trail_active: bool = False
    trail_stop: float = 0.0
    exit_reason: Optional[str] = None


@dataclass
class BarData:
    close: float
    supertrend: str             # "BULL" | "BEAR"
    kronos_direction: str       # "LONG" | "SHORT" | "NEUTRAL"
    sentiment_score: float


def check_exit(pos: Position, bar: BarData) -> tuple[bool, str]:
    """
    Returns (should_exit, reason).
    Updates position's trailing stop in-place.
    """
    pnl_pct = _pnl_pct(pos, bar.close)

    # ── Hard stop-loss ────────────────────────────────────────────────────────
    if pnl_pct <= -STOP_LOSS_PCT:
        return True, f"STOP_LOSS ({pnl_pct:.1%})"

    # ── Trail management ──────────────────────────────────────────────────────
    if pnl_pct >= TRAIL_ACTIVATE_PCT:
        pos.trail_active = True
        if bar.close > pos.high_water_mark or pos.high_water_mark == 0.0:
            pos.high_water_mark = bar.close
            pos.trail_stop = pos.high_water_mark * (1 - TRAIL_DISTANCE_PCT) if pos.direction == "LONG" \
                else pos.high_water_mark * (1 + TRAIL_DISTANCE_PCT)

    if pos.trail_active:
        if pos.direction == "LONG" and bar.close < pos.trail_stop:
            return True, f"TRAIL_STOP (close={bar.close:.1f} < trail={pos.trail_stop:.1f})"
        if pos.direction == "SHORT" and bar.close > pos.trail_stop:
            return True, f"TRAIL_STOP (close={bar.close:.1f} > trail={pos.trail_stop:.1f})"

    # ── Kronos direction reversal ─────────────────────────────────────────────
    if EXIT_ON_KRONOS_REVERSAL:
        opposite = "SHORT" if pos.direction == "LONG" else "LONG"
        if bar.kronos_direction == opposite:
            return True, "KRONOS_REVERSAL"

    # ── Sentiment flip ────────────────────────────────────────────────────────
    if EXIT_ON_SENTIMENT_FLIP:
        if pos.direction == "LONG" and bar.sentiment_score < -0.3:
            return True, f"SENTIMENT_FLIP (score={bar.sentiment_score:.2f})"
        if pos.direction == "SHORT" and bar.sentiment_score > 0.3:
            return True, f"SENTIMENT_FLIP (score={bar.sentiment_score:.2f})"

    # ── SuperTrend flip ───────────────────────────────────────────────────────
    if EXIT_ON_SUPERTREND_FLIP:
        if pos.direction == "LONG" and bar.supertrend == "BEAR":
            return True, "SUPERTREND_FLIP_BEAR"
        if pos.direction == "SHORT" and bar.supertrend == "BULL":
            return True, "SUPERTREND_FLIP_BULL"

    return False, ""


def _pnl_pct(pos: Position, current_price: float) -> float:
    if pos.direction == "LONG":
        return (current_price - pos.entry_price) / pos.entry_price
    return (pos.entry_price - current_price) / pos.entry_price
