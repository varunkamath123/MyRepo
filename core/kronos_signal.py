"""
Kronos signal layer — loads Kronos-mini from HuggingFace, predicts full OHLCV candles,
labels patterns, and returns a rich KronosForecast object.

Runs on CPU (Kronos-mini = 4.1M params, ~1–2s per inference on t3.small).
Falls back to synthetic candles via EMA/ATR if model unavailable.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

from core.candle_patterns import ForecastQuality, score_forecast

log = logging.getLogger(__name__)

_model     = None
_tokenizer = None


# ── Public return type ────────────────────────────────────────────────────────

@dataclass
class KronosForecast:
    direction: str              # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float           # 0–1
    candles: pd.DataFrame       # predicted OHLCV bars (index = bar offset from now)
    quality: ForecastQuality    # pattern labels + score + notes
    source: str                 # "kronos" | "ema_fallback"

    def summary(self) -> str:
        q = self.quality
        lines = [
            f"Direction : {self.direction} (confidence={self.confidence:.0%})",
            f"Quality   : {q.label} (score={q.score:.0%}, bias={q.dominant_bias})",
            f"Target    : {q.price_target:.2f} | Stop: {q.stop_level:.2f} | R:R={q.risk_reward:.2f}x",
            "Sequence  :",
        ]
        for note in q.sequence_notes:
            lines.append(f"  • {note}")
        lines.append("Patterns  :")
        for lb in q.patterns:
            lines.append(f"  Bar {lb.index+1:2d}: [{lb.bias:8s}] {lb.pattern:22s} — {lb.description}")
        return "\n".join(lines)


# ── Model loading ─────────────────────────────────────────────────────────────

def _load_model() -> None:
    global _model, _tokenizer
    if _model is not None:
        return
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch
        model_id = "NeoQuasar/Kronos-mini"
        log.info("[KRONOS] Loading %s ...", model_id)
        _tokenizer = AutoTokenizer.from_pretrained(model_id)
        _model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float32)
        _model.eval()
        log.info("[KRONOS] Model loaded.")
    except Exception as e:
        log.error("[KRONOS] Failed to load model: %s", e)
        raise


# ── Main public API ───────────────────────────────────────────────────────────

def forecast(ohlcv: pd.DataFrame, bars: int = 12) -> KronosForecast:
    """
    Given a DataFrame with columns [open, high, low, close, volume],
    returns a KronosForecast with predicted candles, pattern labels, and quality score.

    Args:
        ohlcv   : historical 5-min OHLCV bars (recommend 256+ bars)
        bars    : number of future bars to predict

    Returns:
        KronosForecast — see .summary() for a human-readable breakdown
    """
    entry_price = float(ohlcv["close"].iloc[-1])

    try:
        _load_model()
        predicted = _kronos_predict_candles(ohlcv, bars)
        source = "kronos"
        log.info("[KRONOS] Predicted %d candles via model.", bars)
    except Exception as e:
        log.warning("[KRONOS] Model unavailable (%s) — using EMA/ATR fallback.", e)
        predicted = _synthetic_candles(ohlcv, bars)
        source = "ema_fallback"

    # Direction from predicted close trajectory
    last_close    = entry_price
    forecast_close = float(predicted["close"].iloc[-1])
    mid_close      = float(predicted["close"].iloc[bars // 2])
    move_pct       = (forecast_close - last_close) / last_close

    if move_pct > 0.003:
        direction = "LONG"
        # confidence scales with magnitude of predicted move (capped at 1.0)
        confidence = min(abs(move_pct) * 60, 1.0)
    elif move_pct < -0.003:
        direction = "SHORT"
        confidence = min(abs(move_pct) * 60, 1.0)
    else:
        direction = "NEUTRAL"
        confidence = 0.0

    # Penalise confidence if mid-point contradicts final direction (U-shape / whipsaw)
    if direction == "LONG" and mid_close < last_close:
        confidence *= 0.7
        log.debug("[KRONOS] Mid-forecast dip detected — confidence penalised.")
    elif direction == "SHORT" and mid_close > last_close:
        confidence *= 0.7

    quality = score_forecast(predicted, direction, entry_price)

    return KronosForecast(
        direction=direction,
        confidence=confidence,
        candles=predicted,
        quality=quality,
        source=source,
    )


# Backwards-compatible alias used by existing code
def forecast_direction(ohlcv: pd.DataFrame, forecast_bars: int = 12) -> tuple[str, float]:
    f = forecast(ohlcv, forecast_bars)
    return f.direction, f.confidence


# ── Kronos model inference ────────────────────────────────────────────────────

def _kronos_predict_candles(ohlcv: pd.DataFrame, bars: int) -> pd.DataFrame:
    """
    Run Kronos model to predict full OHLCV candles.

    Kronos tokenizes each OHLCV bar into discrete tokens via its proprietary
    tokenizer, then autoregressively generates future tokens.
    We decode the token sequence back to price space using the input stats.
    """
    import torch

    # Use last 256 bars; normalise per-column
    window = ohlcv[["open", "high", "low", "close", "volume"]].tail(256).copy()
    stats  = {col: (window[col].mean(), window[col].std() + 1e-8) for col in window.columns}

    norm = pd.DataFrame({
        col: (window[col] - stats[col][0]) / stats[col][1]
        for col in window.columns
    })

    # Flatten to sequence: [o1,h1,l1,c1,v1, o2,h2,...] — 5 tokens per bar
    seq = norm.values.flatten().astype(np.float32)
    input_tensor = torch.tensor(seq, dtype=torch.float32).unsqueeze(0)

    with torch.no_grad():
        outputs = _model.generate(
            input_tensor,
            max_new_tokens=bars * 5,     # 5 tokens per predicted bar
            do_sample=False,
        )

    # Extract predicted tokens and reshape to (bars, 5)
    new_tokens = outputs[0, -(bars * 5):].numpy().reshape(bars, 5)

    # Denormalise back to price space
    predicted_rows = []
    for row in new_tokens:
        denorm = {
            col: float(row[j] * stats[col][1] + stats[col][0])
            for j, col in enumerate(["open", "high", "low", "close", "volume"])
        }
        # Enforce OHLC consistency: high >= max(o,c), low <= min(o,c)
        denorm["high"] = max(denorm["high"], denorm["open"], denorm["close"])
        denorm["low"]  = min(denorm["low"],  denorm["open"], denorm["close"])
        denorm["volume"] = max(denorm["volume"], 0)
        predicted_rows.append(denorm)

    return pd.DataFrame(predicted_rows)


# ── EMA/ATR fallback — realistic synthetic candles ────────────────────────────

def _synthetic_candles(ohlcv: pd.DataFrame, bars: int) -> pd.DataFrame:
    """
    When Kronos is unavailable, generate plausible candles using:
    - EMA 9/21 for direction
    - ATR(14) for volatility sizing
    - Slight random perturbation for realism
    """
    closes = ohlcv["close"]
    ema9   = closes.ewm(span=9).mean().iloc[-1]
    ema21  = closes.ewm(span=21).mean().iloc[-1]
    trend  = 1 if ema9 > ema21 else -1

    tr = pd.concat([
        ohlcv["high"] - ohlcv["low"],
        (ohlcv["high"] - ohlcv["close"].shift()).abs(),
        (ohlcv["low"]  - ohlcv["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=14).mean().iloc[-1]

    avg_vol = float(ohlcv["volume"].tail(20).mean())
    rng     = np.random.default_rng(seed=42)
    rows    = []
    prev_close = float(closes.iloc[-1])

    for _ in range(bars):
        # Bias move in trend direction with noise
        body_size = atr * rng.uniform(0.3, 0.8)
        direction = trend if rng.random() > 0.3 else -trend
        o = prev_close
        c = o + direction * body_size
        wick_factor = rng.uniform(0.1, 0.4)
        h = max(o, c) + atr * wick_factor
        l = min(o, c) - atr * wick_factor
        v = avg_vol * rng.uniform(0.7, 1.3)
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        prev_close = c

    return pd.DataFrame(rows)
