"""
Conviction Selector — picks the single highest-conviction trade across all instruments.
Fuses KronosForecast (candle quality) + FinGPT sentiment + MiroFish swarm score.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import logging

from core.kronos_signal import KronosForecast

log = logging.getLogger(__name__)


@dataclass
class InstrumentSignal:
    instrument: str
    direction: str              # "LONG" | "SHORT" | "NEUTRAL"
    forecast: KronosForecast    # full Kronos output (candles + quality)
    sentiment_score: float      # -1 to +1 from FinGPT
    mirofish_score: float       # 0–1 from MiroFish swarm
    adx: float
    conviction: float = 0.0     # computed composite score

    # Derived from forecast for convenience
    @property
    def kronos_confidence(self) -> float:
        return self.forecast.confidence

    @property
    def pattern_quality_score(self) -> float:
        return self.forecast.quality.score

    @property
    def risk_reward(self) -> float:
        return self.forecast.quality.risk_reward

    @property
    def price_target(self) -> float:
        return self.forecast.quality.price_target

    @property
    def stop_level(self) -> float:
        return self.forecast.quality.stop_level

    def compute_conviction(self) -> None:
        if self.direction == "NEUTRAL":
            self.conviction = 0.0
            return

        directional_sentiment = (
            self.sentiment_score if self.direction == "LONG" else -self.sentiment_score
        )

        # Weighted fusion:
        #   Kronos directional confidence  : 35%
        #   Kronos candle pattern quality  : 25%
        #   FinGPT sentiment alignment     : 25%
        #   MiroFish swarm score           : 15%
        base = (
            0.35 * self.kronos_confidence
            + 0.25 * self.pattern_quality_score
            + 0.25 * max(0.0, directional_sentiment)
            + 0.15 * self.mirofish_score
        )

        # Boost for strong R:R (>2x)
        if self.risk_reward >= 2.0:
            base = min(base * 1.15, 1.0)

        # Penalise if candle quality is CONFLICTED
        if self.forecast.quality.label == "CONFLICTED":
            base *= 0.5

        self.conviction = base


def select_best(signals: list[InstrumentSignal]) -> Optional[InstrumentSignal]:
    """
    Return the single highest-conviction signal, or None if all are below threshold.
    Logs a full breakdown of the winning signal including predicted candle patterns.
    """
    for s in signals:
        s.compute_conviction()

    actionable = [
        s for s in signals
        if s.direction != "NEUTRAL"
        and s.conviction > 0.45
        and s.forecast.quality.label != "CONFLICTED"
    ]

    if not actionable:
        log.info("[SELECTOR] No actionable signal. Scores: %s",
                 {s.instrument: f"{s.conviction:.2f}" for s in signals})
        return None

    best = max(actionable, key=lambda s: s.conviction)

    log.info(
        "[SELECTOR] ✅ Best: %s %s | conviction=%.2f | quality=%s | R:R=%.2fx",
        best.instrument, best.direction, best.conviction,
        best.forecast.quality.label, best.risk_reward,
    )
    log.info("[SELECTOR] Kronos summary:\n%s", best.forecast.summary())

    return best
