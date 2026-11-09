"""Kronos reads on the Breakout+Vol (V1) signal days only, for the 60-stock universe from stk_triggers.py.
Live-matching timing: the read at signal day t uses COMPLETE bars through t-1 (what the 14:55 run sees); entry would be at close t.
Resumable cache: /opt/kronos_bot/research/stk_kronos_reads.json  key "SYM|<datetime of last context bar>" -> [dir, conf, src, rr, tgt]."""
import os, sys, json, pickle, logging, time
from pathlib import Path
sys.path.insert(0, "/opt/kronos_bot/repo")
import numpy as np
import pandas as pd
import backtest as bt

logging.disable(logging.CRITICAL)
RES = Path("/opt/kronos_bot/research"); OUT = RES / "stk_kronos_reads.json"
prices, lots, nifty = pickle.load(open(RES / "stk_prices.pkl", "rb"))
TOPN, WARM = 60, 200
turn = {s: float((d["Close"] * d["Volume"]).tail(250).median()) for s, d in prices.items()}
names = json.load(open(os.environ["STK_UNIV"])) if os.environ.get("STK_UNIV") else sorted(turn, key=turn.get, reverse=True)[:TOPN]

jobs = []
for s in names:
    d = prices[s]; c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    hi20, lo20 = h.shift(1).rolling(20).max(), l.shift(1).rolling(20).min()
    volx = v / v.shift(1).rolling(20).mean()
    fire = ((c > hi20) | (c < lo20)) & (volx >= 1.5)
    for t in range(WARM, len(d)):
        if bool(fire.iloc[t]):
            jobs.append((d.index[t], s, t))
jobs.sort()
cache = json.load(open(OUT)) if OUT.exists() else {}
todo = [(dt, s, t) for dt, s, t in jobs if f"{s}|{prices[s].index[t-1]}" not in cache]
print(f"V1 signal days: {len(jobs)} | already cached {len(jobs)-len(todo)} | to compute {len(todo)} (~{len(todo)*1.7/60:.0f} min)", flush=True)
t0 = time.time()
for i, (dt, s, t) in enumerate(todo, 1):
    d = prices[s].iloc[max(0, t - 200):t]                       # bars through t-1
    ctx = pd.DataFrame({"datetime": d.index, "open": d["Open"].values, "high": d["High"].values, "low": d["Low"].values,
                        "close": d["Close"].values, "volume": d["Volume"].values}).reset_index(drop=True)
    dr, cf, src, rr, tg = bt.get_signal(ctx, False)
    cache[f"{s}|{prices[s].index[t-1]}"] = [dr, float(cf), src, float(rr), float(tg)]
    if i % 50 == 0:
        json.dump(cache, open(OUT, "w"))
        print(f"  {i}/{len(todo)}  {(time.time()-t0)/60:.1f} min elapsed", flush=True)
json.dump(cache, open(OUT, "w"))
print("KRONOS_READS_COMPLETE", flush=True)
