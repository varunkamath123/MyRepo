"""What happens in the last half hour?  Since 2026-08-03 the index feed is flat from 15:15 to ~15:28.  Is that the index only, or do stocks
(and therefore the stock book's 15:25 price) and futures freeze too?  Uses 1-minute candles."""
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

today = C.now_ist().date()
frm = today - timedelta(days=24)


def by_minute(d, label):
    d = d[d.ts.dt.dayofweek < 5].copy(); d["t"] = d.ts.dt.strftime("%H:%M"); d["day"] = d.ts.dt.date
    d["flat"] = (d.h == d.l)
    tot_v = d.groupby("day")["v"].transform("sum")
    d["vshare"] = d["v"] / tot_v * 100
    w = d[(d.t >= "15:08") & (d.t <= "15:40")]
    g = w.groupby("t").agg(flat=("flat", "mean"), vol_share=("vshare", "mean"), days=("day", "nunique"))
    print(f"\n--- {label}: {d.day.nunique()} sessions; per-minute share of bars with no price movement, and mean share of the day's volume ---")
    print("   " + "  ".join(f"{t}: flat {r.flat:.2f} vol {r.vol_share:.2f}%" for t, r in g.iloc[::2].iterrows()))
    return d


for sym, key in (("HDFCBANK", "NSE_EQ|INE040A01034"), ("RELIANCE", "NSE_EQ|INE002A01018"), ("SBIN", "NSE_EQ|INE062A01020")):
    try:
        d = C.candles_chunked(key, "minutes", 1, frm, today - timedelta(days=1))
        d = by_minute(d, f"{sym} equity")
        last = d[(d.t == "15:29")]
        dly = C.candles_range(key, "days", 1, frm, today - timedelta(days=1)); dly["day"] = dly.ts.dt.date
        j = last.merge(dly[["day", "c"]], on="day", suffixes=("", "_daily"))
        err = ((j["c"] / j["c_daily"]) - 1).abs() * 100
        print(f"   {sym}: close of the 15:29 bar vs official daily close: median |diff| {err.median():.3f}%, 90th pct {err.quantile(.9):.3f}%, max {err.max():.3f}%")
        d15 = d[d.t == "15:15"].set_index("day")["c"]; d29 = d[d.t == "15:29"].set_index("day")["c"]
        print(f"   {sym}: move from the 15:15 price to the 15:29 price: median |{((d29/d15-1).abs()*100).median():.3f}|%, max {((d29/d15-1).abs()*100).max():.2f}%")
    except Exception as e:
        print(f"{sym}: failed {str(e)[:120]}")

for label in ("NIFTY FUT 23 NOV 26",):
    p = C.candle_path(label, 1)
    d = pd.read_csv(p, parse_dates=["ts"]); d = d[d.ts.dt.date >= frm]
    by_minute(d, f"{label} (future)")
