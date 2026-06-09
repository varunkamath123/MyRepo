"""
Candlestick pattern detector — works on both historical and Kronos-predicted candles.
Labels each candle individually, then scores multi-bar sequences.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np


# ── Single-bar pattern labels ──────────────────────────────────────────────────
BULLISH  = {"MARUBOZU_BULL", "HAMMER", "BULLISH_ENGULFING", "BULLISH_HARAMI",
            "PIERCING_LINE", "MORNING_STAR", "DRAGONFLY_DOJI"}
BEARISH  = {"MARUBOZU_BEAR", "SHOOTING_STAR", "BEARISH_ENGULFING", "BEARISH_HARAMI",
            "DARK_CLOUD", "EVENING_STAR", "GRAVESTONE_DOJI"}
NEUTRAL  = {"DOJI", "SPINNING_TOP", "LONG_LEGGED_DOJI"}
MOMENTUM = {"MARUBOZU_BULL", "MARUBOZU_BEAR"}


@dataclass
class CandleLabel:
    index: int
    pattern: str
    bias: str               # "BULLISH" | "BEARISH" | "NEUTRAL"
    strength: float         # 0–1: how clean/textbook the pattern is
    description: str


@dataclass
class ForecastQuality:
    label: str              # "STRONG" | "MODERATE" | "WEAK" | "CONFLICTED"
    score: float            # 0–1
    dominant_bias: str      # "BULLISH" | "BEARISH" | "NEUTRAL"
    patterns: list[CandleLabel]
    sequence_notes: list[str]  # narrative observations about the sequence
    price_target: float     # projected close at final bar
    stop_level: float       # projected worst-case wick in forecast window
    risk_reward: float      # abs(target - entry) / abs(stop - entry)


def label_candle(i: int, candles: pd.DataFrame) -> CandleLabel:
    """Label a single candle at index i. Needs i>=1 for 2-bar patterns."""
    o, h, l, c = candles["open"].iloc[i], candles["high"].iloc[i], \
                 candles["low"].iloc[i], candles["close"].iloc[i]

    body   = abs(c - o)
    rng    = h - l + 1e-8
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_pct  = body / rng
    bull      = c >= o

    # ── Doji family ───────────────────────────────────────────────────────────
    if body_pct < 0.08:
        if lower_wick > 2 * upper_wick and lower_wick > 0.6 * rng:
            return CandleLabel(i, "DRAGONFLY_DOJI", "BULLISH", 0.75,
                               "Doji with long lower wick — bullish reversal signal")
        if upper_wick > 2 * lower_wick and upper_wick > 0.6 * rng:
            return CandleLabel(i, "GRAVESTONE_DOJI", "BEARISH", 0.75,
                               "Doji with long upper wick — bearish reversal signal")
        if upper_wick > 0.3 * rng and lower_wick > 0.3 * rng:
            return CandleLabel(i, "LONG_LEGGED_DOJI", "NEUTRAL", 0.6,
                               "Long-legged doji — high indecision, breakout pending")
        return CandleLabel(i, "DOJI", "NEUTRAL", 0.5,
                           "Doji — equilibrium between buyers and sellers")

    # ── Spinning top ──────────────────────────────────────────────────────────
    if body_pct < 0.25 and upper_wick > 0.25 * rng and lower_wick > 0.25 * rng:
        return CandleLabel(i, "SPINNING_TOP", "NEUTRAL", 0.5,
                           "Spinning top — indecision, trend may pause")

    # ── Marubozu ─────────────────────────────────────────────────────────────
    if body_pct > 0.85:
        if bull:
            return CandleLabel(i, "MARUBOZU_BULL", "BULLISH", 0.9,
                               "Bullish marubozu — strong conviction, no wick rejection")
        return CandleLabel(i, "MARUBOZU_BEAR", "BEARISH", 0.9,
                           "Bearish marubozu — strong conviction sellers, no wick rejection")

    # ── Hammer / Shooting Star ────────────────────────────────────────────────
    if lower_wick > 2 * body and upper_wick < 0.3 * body and body_pct > 0.08:
        strength = min(lower_wick / rng * 1.5, 1.0)
        return CandleLabel(i, "HAMMER", "BULLISH", strength,
                           "Hammer — buyers absorbed selling, reversal likely")

    if upper_wick > 2 * body and lower_wick < 0.3 * body and body_pct > 0.08:
        strength = min(upper_wick / rng * 1.5, 1.0)
        return CandleLabel(i, "SHOOTING_STAR", "BEARISH", strength,
                           "Shooting star — sellers rejected rally, reversal likely")

    # ── 2-bar patterns (need prior candle) ────────────────────────────────────
    if i >= 1:
        po, ph, pl, pc = candles["open"].iloc[i-1], candles["high"].iloc[i-1], \
                         candles["low"].iloc[i-1],  candles["close"].iloc[i-1]
        prior_bull = pc >= po
        prior_body = abs(pc - po)

        # Engulfing
        if bull and not prior_bull and c > po and o < pc and body > prior_body * 1.1:
            return CandleLabel(i, "BULLISH_ENGULFING", "BULLISH", 0.85,
                               "Bullish engulfing — current bar swallows prior bearish bar")
        if not bull and prior_bull and c < po and o > pc and body > prior_body * 1.1:
            return CandleLabel(i, "BEARISH_ENGULFING", "BEARISH", 0.85,
                               "Bearish engulfing — current bar swallows prior bullish bar")

        # Harami
        if bull and not prior_bull and o > pc and c < po:
            return CandleLabel(i, "BULLISH_HARAMI", "BULLISH", 0.65,
                               "Bullish harami — small bar inside prior bearish bar")
        if not bull and prior_bull and o < pc and c > po:
            return CandleLabel(i, "BEARISH_HARAMI", "BEARISH", 0.65,
                               "Bearish harami — small bar inside prior bullish bar")

    # ── General bullish / bearish body ────────────────────────────────────────
    if bull:
        strength = min(body_pct * 1.2, 0.75)
        return CandleLabel(i, "BULLISH_BODY", "BULLISH", strength,
                           f"Bullish bar — body {body_pct:.0%} of range")
    strength = min(body_pct * 1.2, 0.75)
    return CandleLabel(i, "BEARISH_BODY", "BEARISH", strength,
                       f"Bearish bar — body {body_pct:.0%} of range")


def _three_bar_patterns(labels: list[CandleLabel], candles: pd.DataFrame) -> list[str]:
    """Detect morning/evening star and momentum sequences across the forecast."""
    notes = []
    n = len(labels)

    for i in range(2, n):
        a, b, c = labels[i-2], labels[i-1], labels[i]
        # Morning star
        if a.bias == "BEARISH" and b.bias == "NEUTRAL" and c.bias == "BULLISH":
            notes.append(f"[BAR {i}] Morning star sequence → bullish reversal building")
        # Evening star
        if a.bias == "BULLISH" and b.bias == "NEUTRAL" and c.bias == "BEARISH":
            notes.append(f"[BAR {i}] Evening star sequence → bearish reversal building")

    # Momentum run: 3+ consecutive same-bias candles
    bullish_run = sum(1 for lb in labels if lb.bias == "BULLISH")
    bearish_run = sum(1 for lb in labels if lb.bias == "BEARISH")
    if bullish_run >= int(n * 0.65):
        notes.append(f"Dominant bullish momentum — {bullish_run}/{n} predicted bars bullish")
    if bearish_run >= int(n * 0.65):
        notes.append(f"Dominant bearish momentum — {bearish_run}/{n} predicted bars bearish")

    # Expanding range (acceleration)
    ranges = (candles["high"] - candles["low"]).values
    if len(ranges) >= 4:
        first_half = ranges[:len(ranges)//2].mean()
        second_half = ranges[len(ranges)//2:].mean()
        if second_half > first_half * 1.25:
            notes.append("Range expanding mid-forecast — momentum accelerating")
        elif second_half < first_half * 0.75:
            notes.append("Range contracting mid-forecast — momentum fading, watch for reversal")

    return notes


def score_forecast(
    candles: pd.DataFrame,
    direction: str,
    entry_price: float,
) -> ForecastQuality:
    """
    Label all predicted candles and produce an overall quality score.

    Args:
        candles: DataFrame [open, high, low, close, volume] — predicted bars
        direction: intended trade direction "LONG" | "SHORT"
        entry_price: current price at time of entry
    """
    labels = [label_candle(i, candles) for i in range(len(candles))]
    notes  = _three_bar_patterns(labels, candles)

    bullish_score = sum(lb.strength for lb in labels if lb.bias == "BULLISH")
    bearish_score = sum(lb.strength for lb in labels if lb.bias == "BEARISH")
    total = bullish_score + bearish_score + 1e-8

    dominant_bias = "BULLISH" if bullish_score > bearish_score else \
                    "BEARISH" if bearish_score > bullish_score else "NEUTRAL"

    # Alignment: does forecast bias match intended direction?
    aligned = (direction == "LONG" and dominant_bias == "BULLISH") or \
              (direction == "SHORT" and dominant_bias == "BEARISH")

    raw_score = (bullish_score if direction == "LONG" else bearish_score) / total
    score = raw_score if aligned else raw_score * 0.4   # penalise misalignment

    # Quality label
    if score >= 0.70:
        label = "STRONG"
    elif score >= 0.50:
        label = "MODERATE"
    elif score >= 0.30:
        label = "WEAK"
    else:
        label = "CONFLICTED"

    # Price target: forecast close at last bar
    price_target = float(candles["close"].iloc[-1])

    # Stop level: worst-case wick across forecast
    if direction == "LONG":
        stop_level = float(candles["low"].min())
    else:
        stop_level = float(candles["high"].max())

    # Risk/reward
    target_dist = abs(price_target - entry_price)
    stop_dist   = abs(stop_level - entry_price) + 1e-8
    risk_reward = target_dist / stop_dist

    if not notes:
        notes.append(f"{dominant_bias.title()} bias across {len(labels)} predicted bars")

    return ForecastQuality(
        label=label,
        score=score,
        dominant_bias=dominant_bias,
        patterns=labels,
        sequence_notes=notes,
        price_target=price_target,
        stop_level=stop_level,
        risk_reward=risk_reward,
    )
