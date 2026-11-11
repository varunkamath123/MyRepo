"""What did Kronos actually forecast before the rally?  Same inputs as the live bot (Upstox zero-volume daily bars, complete bars only),
forecast of the next 5 daily candles, the half-ATR direction rule and the 0.45 confidence bar -- plus what the stock-breakout rule saw."""
import sys, json, logging, pickle
sys.path.insert(0, "/opt/kronos_bot/repo")
import numpy as np
import pandas as pd
import backtest as bt
from brokers.upstox_data import load_daily_ohlcv
from core.kronos_signal import forecast, _recent_atr_pct

logging.disable(logging.CRITICAL)
for inst in ("BANKNIFTY", "NIFTY"):
    d = load_daily_ohlcv(inst, bars=215)
    d = d[d["datetime"].dt.dayofweek < 5].reset_index(drop=True)
    conf_min = bt.INSTRUMENT_PARAMS[inst]["kronos_conf_min"]
    print(f"\n=== {inst}: Kronos 5-bar forecast at each close (rule: |move| > 0.5 ATR for a direction; confidence = 0.35 x move/ATR, must be >= {conf_min}) ===")
    print(f"  {'bar':<11}{'close':>9}{'fcst 5d%':>9}{'ATR%':>6}{'move/ATR':>9}{'read':>9}{'conf':>6}{'needs 5d%':>10}{'actual next 5d%':>16}")
    n = len(d)
    for k in range(n - 11, n):
        ctx = d.iloc[: k + 1].copy()
        f = forecast(ctx, 5)
        last = float(ctx["close"].iloc[-1]); fc = float(f.candles["close"].iloc[-1])
        move = (fc / last - 1) * 100
        atr = _recent_atr_pct(ctx) * 100
        need = conf_min / 0.35 * atr          # move% needed to clear the confidence bar (ignoring the U-shape haircut)
        fwd = (float(d["close"].iloc[min(k + 5, n - 1)]) / last - 1) * 100 if k + 1 < n else float("nan")
        fwd_note = f"{fwd:+.1f}" + ("" if k + 5 < n else "*")
        print(f"  {str(d['datetime'].iloc[k].date()):<11}{last:>9,.0f}{move:>+9.2f}{atr:>6.2f}{move/atr:>+9.2f}{f.direction:>9}{f.confidence:>6.2f}{need:>10.2f}{fwd_note:>16}")
print("  (* = fewer than 5 bars after the date; measured to the latest close)")

print("\n=== what the stock breakout rule (NIFTY 50, long) saw before the rally: count of names breaking their 20-day high on >=1.5x volume ===")
prices, lots, nifty = pickle.load(open("/opt/kronos_bot/research/stk_prices.pkl", "rb"))
names = json.load(open("/opt/kronos_bot/research/nifty50_syms.json"))
nf = nifty.copy(); nf.index = pd.to_datetime(nf.index).normalize()
sma200 = nf.rolling(200).mean()
cal = [c for c in sorted(set().union(*[set(pd.to_datetime(prices[s].index).normalize()) for s in names])) if c >= pd.Timestamp("2026-10-23")]
for D in cal:
    hits = []
    for s in names:
        p = prices[s].copy(); p.index = pd.to_datetime(p.index).normalize()
        if D not in p.index:
            continue
        i = p.index.get_loc(D)
        if i < 40:
            continue
        hi20 = p["High"].iloc[i - 20:i].max(); vx = p["Volume"].iloc[i] / p["Volume"].iloc[i - 20:i].mean()
        if p["Close"].iloc[i] > hi20 and vx >= 1.5:
            hits.append(s)
    lvl = float(nf.loc[D]) if D in nf.index else float("nan")
    reg = "above" if lvl > float(sma200.loc[D]) else "BELOW"
    print(f"  {D.date()}  NIFTY {lvl:>8,.0f} ({reg} 200-DMA {float(sma200.loc[D]):,.0f})  breakouts: {len(hits):>2}  {', '.join(hits[:8])}{' ...' if len(hits) > 8 else ''}")
