"""
Kronos Futures Bot — main live loop.
Run: python futures_bot.py NIFTY BANKNIFTY SENSEX
"""
from __future__ import annotations
import sys
import time
import logging
import pandas as pd
from datetime import datetime, time as dtime

from config import (
    INSTRUMENTS, ORB_WINDOW_START, ORB_WINDOW_END,
    MAIN_SESSION_END, MIN_ADX, KRONOS_FORECAST_BARS,
    KRONOS_CONFIDENCE_MIN,
)
from core.kronos_signal import forecast_direction
from core.sentiment_signal import get_sentiment
from core.conviction_selector import InstrumentSignal, select_best
from core.exit_monitor import Position, BarData, check_exit
from brokers.upstox_orders import place_order, get_ltp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("logs/futures_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

active_position: Position | None = None


def now_ist() -> datetime:
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def market_open() -> bool:
    t = now_ist().time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def in_entry_window() -> bool:
    t = now_ist().time()
    start = dtime(*map(int, ORB_WINDOW_START.split(":")))
    end = dtime(*map(int, MAIN_SESSION_END.split(":")))
    return start <= t <= end


def load_ohlcv(instrument: str) -> pd.DataFrame:
    """Load recent 5-min OHLCV from data directory."""
    import os
    path = f"data/{instrument.lower()}_5min.csv"
    if not os.path.exists(path):
        raise FileNotFoundError(f"No data file: {path}")
    df = pd.read_csv(path, parse_dates=["datetime"])
    return df.tail(300).reset_index(drop=True)


def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Simple ADX calculation."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period).mean()
    dm_plus = (high.diff()).clip(lower=0)
    dm_minus = (-low.diff()).clip(lower=0)
    di_plus = 100 * dm_plus.ewm(span=period).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period).mean() / atr
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus)
    return dx.ewm(span=period).mean().iloc[-1]


def supertrend(df: pd.DataFrame, period: int = 7, multiplier: float = 2.5) -> str:
    """Returns 'BULL' or 'BEAR' for last bar."""
    hl2 = (df["high"] + df["low"]) / 2
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period).mean()
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    close = df["close"]
    return "BULL" if close.iloc[-1] > lower.iloc[-1] else "BEAR"


def run_loop(instruments: list[str], headlines: list[str], paper: bool = False):
    global active_position

    sentiment = get_sentiment(headlines)
    log.info("[BOT] Sentiment score=%.2f | instruments=%s | paper=%s", sentiment, instruments, paper)

    while True:
        if not market_open():
            log.info("[BOT] Market closed. Sleeping 60s.")
            time.sleep(60)
            continue

        # ── Exit check for active position ────────────────────────────────────
        if active_position:
            try:
                df = load_ohlcv(active_position.instrument)
                ltp = get_ltp(INSTRUMENTS[active_position.instrument]["symbol"])
                k_dir, _ = forecast_direction(df, KRONOS_FORECAST_BARS)
                st = supertrend(df)
                bar = BarData(close=ltp, supertrend=st, kronos_direction=k_dir, sentiment_score=sentiment)
                should_exit, reason = check_exit(active_position, bar)
                if should_exit:
                    cfg = INSTRUMENTS[active_position.instrument]
                    side = "SELL" if active_position.direction == "LONG" else "BUY"
                    place_order(cfg["symbol"], side, cfg["lot_size"] * active_position.lots,
                                paper=paper or not cfg["live"])
                    log.info("[EXIT] %s %s → reason: %s", active_position.instrument,
                             active_position.direction, reason)
                    active_position = None
            except Exception as e:
                log.error("[EXIT_CHECK] Error: %s", e)

        # ── Entry scan (only if no position) ─────────────────────────────────
        if not active_position and in_entry_window():
            signals = []
            for inst in instruments:
                try:
                    df = load_ohlcv(inst)
                    adx = compute_adx(df)
                    if adx < MIN_ADX:
                        log.info("[SCAN] %s ADX=%.1f < %d, skip", inst, adx, MIN_ADX)
                        continue
                    k_dir, k_conf = forecast_direction(df, KRONOS_FORECAST_BARS)
                    if k_conf < KRONOS_CONFIDENCE_MIN or k_dir == "NEUTRAL":
                        continue
                    st = supertrend(df)
                    mirofish_score = 0.5   # placeholder until MiroFish pre-market run integrated
                    sig = InstrumentSignal(
                        instrument=inst, direction=k_dir,
                        kronos_confidence=k_conf, sentiment_score=sentiment,
                        mirofish_score=mirofish_score, adx=adx,
                    )
                    signals.append(sig)
                except Exception as e:
                    log.error("[SCAN] %s error: %s", inst, e)

            best = select_best(signals)
            if best:
                cfg = INSTRUMENTS[best.instrument]
                ltp = get_ltp(cfg["symbol"])
                side = "BUY" if best.direction == "LONG" else "SELL"
                place_order(cfg["symbol"], side, cfg["lot_size"],
                            paper=paper or not cfg["live"])
                active_position = Position(
                    instrument=best.instrument,
                    direction=best.direction,
                    entry_price=ltp,
                    entry_kronos_direction=best.direction,
                    entry_sentiment=sentiment,
                    entry_supertrend=supertrend(load_ohlcv(best.instrument)),
                    lots=1,
                )
                log.info("[ENTRY] %s %s @ %.2f | conviction=%.2f",
                         best.instrument, best.direction, ltp, best.conviction)

        time.sleep(300)   # 5-min bar cadence


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:] if a.upper() in INSTRUMENTS]
    if not args:
        args = list(INSTRUMENTS.keys())
    # Fetch headlines from a news source before market opens — placeholder
    sample_headlines = [
        "RBI holds rates steady amid global uncertainty",
        "FII inflows strong for third consecutive session",
        "Nifty eyes 25,000 as global cues remain positive",
    ]
    run_loop(args, sample_headlines, paper="--paper" in sys.argv)
