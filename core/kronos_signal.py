"""
Kronos signal layer — uses the real KronosPredictor (NeoQuasar/Kronos-base +
NeoQuasar/Kronos-Tokenizer-base) to predict full OHLCV candles, then labels
patterns and returns a KronosForecast.

Falls back to EMA/ATR synthetic candles if model is unavailable.
"""
from __future__ import annotations
from dataclasses import dataclass
import sys
import numpy as np
import pandas as pd
import logging
from datetime import timedelta
from pathlib import Path

from core.candle_patterns import ForecastQuality, score_forecast

log = logging.getLogger(__name__)

# Add the quant_trading Kronos model directory to sys.path so we can import
# the real KronosPredictor rather than using HuggingFace transformers.
_KRONOS_MODEL_DIR = Path("C:/quant_trading/Kronos")
if str(_KRONOS_MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(_KRONOS_MODEL_DIR))

_predictor = None


# ── Public return type ────────────────────────────────────────────────────────

@dataclass
class KronosForecast:
    direction: str              # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float           # 0–1
    candles: pd.DataFrame       # predicted OHLCV bars
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

def _load_predictor():
    global _predictor
    if _predictor is not None:
        return _predictor

    try:
        import torch
        from model.kronos import Kronos, KronosTokenizer, KronosPredictor

        log.info("[KRONOS] Loading NeoQuasar/Kronos-base + Kronos-Tokenizer-base …")
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model     = Kronos.from_pretrained("NeoQuasar/Kronos-base")
        model.eval()

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        log.info("[KRONOS] Model loaded on %s.", device)
    except Exception as e:
        log.error("[KRONOS] Failed to load model: %s", e)
        raise

    return _predictor


# ── Main public API ───────────────────────────────────────────────────────────

def forecast(ohlcv: pd.DataFrame, bars: int = 12) -> KronosForecast:
    """
    Predict `bars` future 5-min candles from `ohlcv` (needs datetime column or index).

    Returns KronosForecast with direction, confidence, predicted candles, quality score.
    """
    entry_price = float(ohlcv["close"].iloc[-1])

    try:
        predicted, source = _kronos_predict(ohlcv, bars)
    except Exception as e:
        log.warning("[KRONOS] Model unavailable (%s) — using EMA/ATR fallback.", e)
        predicted = _synthetic_candles(ohlcv, bars)
        source = "ema_fallback"

    last_close     = entry_price
    forecast_close = float(predicted["close"].iloc[-1])
    mid_close      = float(predicted["close"].iloc[bars // 2])
    move_pct       = (forecast_close - last_close) / last_close

    # ATR-normalised confidence: compare predicted move against recent volatility.
    # Raw `move_pct * 60` calibrated for individual stocks (big moves); for NSE
    # futures indices a 0.3% move in 1 hour is already a strong directional bar.
    atr_pct = _recent_atr_pct(ohlcv)          # ATR as fraction of close
    # Confidence = how many ATRs the predicted move covers (capped at 1.0)
    atrs_covered = abs(move_pct) / max(atr_pct, 1e-6)

    if move_pct > atr_pct * 0.5:              # must move at least half an ATR
        direction  = "LONG"
        confidence = min(atrs_covered * 0.35, 1.0)
    elif move_pct < -atr_pct * 0.5:
        direction  = "SHORT"
        confidence = min(atrs_covered * 0.35, 1.0)
    else:
        direction  = "NEUTRAL"
        confidence = 0.0

    # Penalise if mid-point contradicts final direction (U-shape / whipsaw)
    if direction == "LONG" and mid_close < last_close:
        confidence *= 0.7
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


def forecast_direction(ohlcv: pd.DataFrame, forecast_bars: int = 12) -> tuple[str, float]:
    f = forecast(ohlcv, forecast_bars)
    return f.direction, f.confidence


# ── Real Kronos inference ─────────────────────────────────────────────────────

def _kronos_predict(ohlcv: pd.DataFrame, bars: int) -> tuple[pd.DataFrame, str]:
    """
    Run the real KronosPredictor.  ohlcv must have columns:
      open, high, low, close, volume  (amount is optional — filled from volume × price)
    and either a 'datetime' column or a DatetimeIndex.
    """
    predictor = _load_predictor()

    df = ohlcv.copy().tail(512)   # use at most 512 bars as context

    # Resolve timestamp column
    if "datetime" in df.columns:
        x_ts = pd.to_datetime(df["datetime"])
        df = df.drop(columns=["datetime"])
    elif isinstance(df.index, pd.DatetimeIndex):
        x_ts = df.index.to_series().reset_index(drop=True)
        df = df.reset_index(drop=True)
    else:
        raise ValueError("[KRONOS] ohlcv needs a 'datetime' column or DatetimeIndex")

    df = df[["open", "high", "low", "close", "volume"]].reset_index(drop=True)

    # Generate future timestamps spaced 5 min apart
    last_ts  = pd.Timestamp(x_ts.iloc[-1])
    y_ts     = pd.Series([last_ts + timedelta(minutes=5 * (i + 1)) for i in range(bars)])

    pred_df = predictor.predict(
        df=df,
        x_timestamp=x_ts,
        y_timestamp=y_ts,
        pred_len=bars,
        T=1.0,
        top_k=0,
        top_p=0.9,
        sample_count=1,
        verbose=False,
    )

    # Normalise column names to lowercase open/high/low/close/volume
    pred_df = pred_df.rename(columns=str.lower).reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in pred_df.columns:
            pred_df[col] = float("nan")

    # Enforce OHLC consistency
    pred_df["high"] = pred_df[["high", "open", "close"]].max(axis=1)
    pred_df["low"]  = pred_df[["low",  "open", "close"]].min(axis=1)
    pred_df["volume"] = pred_df["volume"].clip(lower=0)

    return pred_df[["open", "high", "low", "close", "volume"]], "kronos"


# ── EMA/ATR fallback ──────────────────────────────────────────────────────────

def _recent_atr_pct(ohlcv: pd.DataFrame, period: int = 14) -> float:
    """Return ATR as a fraction of the last close price."""
    tr = pd.concat([
        ohlcv["high"] - ohlcv["low"],
        (ohlcv["high"] - ohlcv["close"].shift()).abs(),
        (ohlcv["low"]  - ohlcv["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.ewm(span=period).mean().iloc[-1])
    last_close = float(ohlcv["close"].iloc[-1])
    return atr / last_close if last_close > 0 else 1e-4


def _synthetic_candles(ohlcv: pd.DataFrame, bars: int) -> pd.DataFrame:
    closes = ohlcv["close"]
    ema9   = closes.ewm(span=9).mean().iloc[-1]
    ema21  = closes.ewm(span=21).mean().iloc[-1]
    trend  = 1 if ema9 > ema21 else -1

    tr = pd.concat([
        ohlcv["high"] - ohlcv["low"],
        (ohlcv["high"] - ohlcv["close"].shift()).abs(),
        (ohlcv["low"]  - ohlcv["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr     = tr.ewm(span=14).mean().iloc[-1]
    avg_vol = float(ohlcv["volume"].tail(20).mean())
    # Use the last close value as seed so different bars give different predictions
    seed    = int(abs(closes.iloc[-1]) * 100) % (2 ** 31)
    rng     = np.random.default_rng(seed=seed)
    rows    = []
    prev    = float(closes.iloc[-1])

    for _ in range(bars):
        body_size = atr * rng.uniform(0.3, 0.8)
        d = trend if rng.random() > 0.3 else -trend
        o = prev
        c = o + d * body_size
        wick = atr * rng.uniform(0.1, 0.4)
        h = max(o, c) + wick
        l = min(o, c) - wick
        v = avg_vol * rng.uniform(0.7, 1.3)
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        prev = c

    return pd.DataFrame(rows)
