"""
Conviction Selector — picks the single highest-conviction trade across all instruments.
Fuses Kronos price forecast + FinGPT sentiment + MiroFish swarm score.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import logging

log = logging.getLogger(__name__)


@dataclass
class InstrumentSignal:
    instrument: str
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    kronos_confidence: float    # 0–1, directional confidence from Kronos
    sentiment_score: float      # -1 to +1 from FinGPT
    mirofish_score: float       # 0–1 from MiroFish swarm
    adx: float
    conviction: float = 0.0     # computed composite score

    def compute_conviction(self) -> None:
        if self.direction == "NEUTRAL":
            self.conviction = 0.0
            return
        # weighted fusion: Kronos 50%, sentiment 30%, mirofish 20%
        directional_sentiment = self.sentiment_score if self.direction == "LONG" else -self.sentiment_score
        self.conviction = (
            0.50 * self.kronos_confidence
            + 0.30 * max(0.0, directional_sentiment)
            + 0.20 * self.mirofish_score
        )


def select_best(signals: list[InstrumentSignal]) -> Optional[InstrumentSignal]:
    """Return the single highest-conviction signal, or None if all are NEUTRAL."""
    for s in signals:
        s.compute_conviction()

    actionable = [s for s in signals if s.direction != "NEUTRAL" and s.conviction > 0.45]
    if not actionable:
        log.info("[SELECTOR] No actionable signal across instruments.")
        return None

    best = max(actionable, key=lambda s: s.conviction)
    log.info(
        "[SELECTOR] Best: %s %s | conviction=%.2f (kronos=%.2f sent=%.2f miro=%.2f adx=%.1f)",
        best.instrument, best.direction, best.conviction,
        best.kronos_confidence, best.sentiment_score, best.mirofish_score, best.adx,
    )
    return best
