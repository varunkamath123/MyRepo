"""
Sentiment signal layer — FinGPT v3 via Claude Haiku API (cost-optimized).
Falls back to keyword scoring if API unavailable.
"""
from __future__ import annotations
import os
import logging
import json
from datetime import date

log = logging.getLogger(__name__)

CACHE: dict[str, float] = {}   # date → score, avoid repeat API calls same day


def get_sentiment(headlines: list[str]) -> float:
    """
    Returns sentiment score: -1.0 (very bearish) to +1.0 (very bullish).
    Uses Claude Haiku as a cost-efficient FinGPT-style sentiment extractor.
    """
    today = str(date.today())
    if today in CACHE:
        return CACHE[today]

    try:
        score = _claude_sentiment(headlines)
    except Exception as e:
        log.warning("[SENTIMENT] Claude API failed (%s), using keyword fallback.", e)
        score = _keyword_fallback(headlines)

    CACHE[today] = score
    log.info("[SENTIMENT] score=%.2f from %d headlines", score, len(headlines))
    return score


def _claude_sentiment(headlines: list[str]) -> float:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    text = "\n".join(f"- {h}" for h in headlines[:20])
    prompt = (
        "You are a financial sentiment analyst. Rate the overall market sentiment "
        "for Indian index futures (NIFTY/BANKNIFTY/SENSEX) based on these headlines.\n\n"
        f"{text}\n\n"
        "Reply with ONLY a JSON object: {\"score\": <float from -1.0 to 1.0>, \"reason\": \"<10 words>\"}. "
        "-1.0 = strongly bearish, 0 = neutral, 1.0 = strongly bullish."
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    result = json.loads(raw.strip())
    return float(result["score"])


def _keyword_fallback(headlines: list[str]) -> float:
    bullish = ["rally", "surge", "gains", "bull", "positive", "record", "high", "up"]
    bearish = ["crash", "fall", "drop", "bear", "negative", "low", "down", "sell-off", "tariff", "war"]
    text = " ".join(headlines).lower()
    score = sum(1 for w in bullish if w in text) - sum(1 for w in bearish if w in text)
    return max(-1.0, min(1.0, score * 0.1))
