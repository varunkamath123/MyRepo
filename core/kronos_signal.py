"""
Kronos signal layer — loads Kronos-mini from HuggingFace, generates directional forecast.
Runs on CPU (Kronos-mini = 4.1M params, fast enough for pre-bar inference).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import logging
from typing import Optional

log = logging.getLogger(__name__)

_model = None
_tokenizer = None


def _load_model():
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


def forecast_direction(ohlcv: pd.DataFrame, forecast_bars: int = 12) -> tuple[str, float]:
    """
    Given a DataFrame with columns [open, high, low, close, volume],
    returns (direction, confidence) where direction is "LONG" | "SHORT" | "NEUTRAL".

    Falls back to simple EMA trend if Kronos unavailable.
    """
    try:
        _load_model()
        direction, confidence = _kronos_inference(ohlcv, forecast_bars)
        log.info("[KRONOS] direction=%s confidence=%.2f", direction, confidence)
        return direction, confidence
    except Exception as e:
        log.warning("[KRONOS] Inference failed (%s), falling back to EMA trend.", e)
        return _ema_fallback(ohlcv)


def _kronos_inference(ohlcv: pd.DataFrame, forecast_bars: int) -> tuple[str, float]:
    """Runs actual Kronos model inference."""
    import torch
    # Kronos expects normalized OHLCV sequences — see models/kronos/README for tokenizer API
    # This is a placeholder that mirrors the Kronos inference pipeline structure
    closes = ohlcv["close"].values[-256:].astype(np.float32)
    mean, std = closes.mean(), closes.std() + 1e-8
    normalized = (closes - mean) / std

    input_tensor = torch.tensor(normalized, dtype=torch.float32).unsqueeze(0)
    with torch.no_grad():
        # Kronos tokenizer converts OHLCV to discrete tokens
        # outputs logits over next-bar distribution
        outputs = _model.generate(
            input_tensor,
            max_new_tokens=forecast_bars,
            do_sample=False,
        )
    forecast = outputs[0, -forecast_bars:].numpy() * std + mean

    last_close = closes[-1]
    forecast_end = forecast[-1]
    move_pct = (forecast_end - last_close) / last_close

    if move_pct > 0.003:
        return "LONG", min(abs(move_pct) * 50, 1.0)
    elif move_pct < -0.003:
        return "SHORT", min(abs(move_pct) * 50, 1.0)
    return "NEUTRAL", 0.0


def _ema_fallback(ohlcv: pd.DataFrame) -> tuple[str, float]:
    """Simple EMA 9/21 directional fallback when Kronos is unavailable."""
    closes = ohlcv["close"]
    ema9 = closes.ewm(span=9).mean().iloc[-1]
    ema21 = closes.ewm(span=21).mean().iloc[-1]
    if ema9 > ema21:
        return "LONG", 0.55
    elif ema9 < ema21:
        return "SHORT", 0.55
    return "NEUTRAL", 0.0
