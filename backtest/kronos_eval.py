"""
Kronos directional accuracy evaluation on historical NSE 5-min data.
Usage: python backtest/kronos_eval.py --instrument NIFTY --bars 12
Goal: validate Kronos-mini directional accuracy before live use.
If directional accuracy > 58% on trending days → earns a role as entry filter.
"""
from __future__ import annotations
import argparse
import logging
import pandas as pd
import numpy as np
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def load_data(instrument: str, data_dir: str = "data") -> pd.DataFrame:
    path = Path(data_dir) / f"{instrument.lower()}_5min.csv"
    if not path.exists():
        raise FileNotFoundError(f"No data at {path}. Run data collection first.")
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    log.info("Loaded %d bars for %s", len(df), instrument)
    return df


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
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
    return dx.ewm(span=period).mean()


def run_eval(instrument: str, forecast_bars: int, lookback: int, adx_filter: float):
    df = load_data(instrument)
    df["adx"] = compute_adx(df)

    try:
        from core.kronos_signal import forecast_direction
        use_kronos = True
        log.info("Kronos model available — running real inference.")
    except Exception:
        use_kronos = False
        log.warning("Kronos unavailable — using EMA fallback for evaluation baseline.")

    results = []
    min_lookback = max(lookback, 50)

    for i in range(min_lookback, len(df) - forecast_bars):
        window = df.iloc[i - lookback: i]
        future = df.iloc[i: i + forecast_bars]
        adx_val = df["adx"].iloc[i]

        if adx_filter > 0 and adx_val < adx_filter:
            continue  # only evaluate on trending days

        if use_kronos:
            direction, confidence = forecast_direction(window, forecast_bars)
        else:
            # EMA fallback baseline
            ema9 = window["close"].ewm(span=9).mean().iloc[-1]
            ema21 = window["close"].ewm(span=21).mean().iloc[-1]
            direction = "LONG" if ema9 > ema21 else "SHORT"
            confidence = 0.55

        actual_move = future["close"].iloc[-1] - df["close"].iloc[i]
        actual_direction = "LONG" if actual_move > 0 else "SHORT"
        correct = (direction == actual_direction)

        results.append({
            "datetime": df["datetime"].iloc[i],
            "adx": adx_val,
            "predicted": direction,
            "actual": actual_direction,
            "correct": correct,
            "confidence": confidence,
            "actual_move_pct": actual_move / df["close"].iloc[i] * 100,
        })

    if not results:
        log.warning("No results — check data or ADX filter threshold.")
        return

    rdf = pd.DataFrame(results)
    overall_acc = rdf["correct"].mean()
    high_conf = rdf[rdf["confidence"] >= 0.65]
    high_conf_acc = high_conf["correct"].mean() if len(high_conf) else float("nan")

    print(f"\n{'='*60}")
    print(f"Kronos Evaluation: {instrument} | forecast={forecast_bars} bars | ADX≥{adx_filter}")
    print(f"{'='*60}")
    print(f"Total windows evaluated : {len(rdf)}")
    print(f"Overall directional acc : {overall_acc:.1%}")
    print(f"High-conf (≥0.65) count : {len(high_conf)}")
    print(f"High-conf accuracy      : {high_conf_acc:.1%}")
    print(f"Mean ADX in sample      : {rdf['adx'].mean():.1f}")
    print(f"\nVerdict: {'✅ PASS — earns role as entry filter' if overall_acc >= 0.58 else '❌ FAIL — below 58% threshold'}")

    out_path = f"logs/kronos_eval_{instrument.lower()}.csv"
    rdf.to_csv(out_path, index=False)
    log.info("Full results saved to %s", out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default="NIFTY")
    parser.add_argument("--bars", type=int, default=12, help="Forecast bars ahead")
    parser.add_argument("--lookback", type=int, default=256, help="Input bars to Kronos")
    parser.add_argument("--adx-filter", type=float, default=25.0, help="Only eval on ADX≥N days")
    args = parser.parse_args()
    run_eval(args.instrument.upper(), args.bars, args.lookback, args.adx_filter)
