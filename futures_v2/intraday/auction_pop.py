"""Closing-auction timing for the stock breakout rule.  Since 2026-08-03 cash trading is continuous until 15:15; the official close is a single auction print ~15:28.
The live stock book decides at 15:25 on the LAST CONTINUOUS PRICE (15:14 bar close) and books the fill there.  Question: for stocks flagged as breakouts at 15:14
(price above the prior-20d high, volume through 15:14 x 1.03 >= 1.5x the 20-day average), how far does the official close sit from that price?
If the auction systematically pushes them up, (a) the booked paper entries flatter the research convention and (b) buying in the continuous session before 15:15 beats the close.
All 50 NIFTY 50 names, every session from 2026-08-03, 1-minute candles."""
import json, sys
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import common as C

uni = json.load(open(Path(__file__).resolve().parents[2] / "stock_universe.json"))
today = C.now_ist().date(); frm = date(2026, 6, 1); AUC = pd.Timestamp("2026-08-03")
rows = []
for u in uni:
    try:
        m = C.candles_chunked(u["key"], "minutes", 1, frm, today - timedelta(days=1))
        dly = C.candles_chunked(u["key"], "days", 1, date(2026, 4, 1), today - timedelta(days=1), chunk_days=200)
    except Exception as e:
        print(u["symbol"], "failed", str(e)[:80]); continue
    m = m[m.ts.dt.dayofweek < 5].copy(); m["day"] = m.ts.dt.normalize(); m["hm"] = m.ts.dt.strftime("%H:%M")
    dly = dly[dly.ts.dt.dayofweek < 5].copy(); dly["day"] = dly.ts.dt.normalize(); dly = dly.set_index("day")
    hi20 = dly["h"].shift(1).rolling(20).max(); av20 = dly["v"].shift(1).rolling(20).mean()
    for day, g in m.groupby("day"):
        if day < AUC or day not in dly.index or pd.isna(hi20.get(day)):
            continue
        pre = g[g.hm <= "15:14"]; last = g[g.hm == "15:29"]
        if pre.empty or last.empty or g.hm.min() != "09:15":
            continue
        px, close = float(pre.c.iloc[-1]), float(last.c.iloc[-1]); cum = float(pre.v.sum())
        rows.append(dict(sym=u["symbol"], day=day, px=px, close=close, jump=(close / px - 1) * 100, flagged=bool(px > hi20[day] and cum * 1.03 / av20[day] >= 1.5),
                         sig_close=bool(close > hi20[day] and float(dly.loc[day, "v"]) / av20[day] >= 1.5), above=bool(px > hi20[day]), volpct_after=float(g[g.hm >= "15:15"].v.sum() / g.v.sum() * 100)))
R = pd.DataFrame(rows)
print(f"{R.day.nunique()} sessions x {R.sym.nunique()} stocks = {len(R)} stock-days since {AUC.date()}")
print(f"\n  {'group':<58}{'n':>6}{'mean pop%':>10}{'median%':>9}{'% up':>6}{'sd%':>7}")
for lab, m in (("ALL stock-days (baseline: 15:14 price -> official close)", R.jump.notna()), ("price above the prior-20d high at 15:14 (any volume)", R.above),
               ("FLAGGED at 15:14 (what the live book sees)", R.flagged), ("official-close breakout signals (the research definition, hindsight)", R.sig_close),
               ("flagged at 15:14 AND still a signal at the close", R.flagged & R.sig_close), ("flagged at 15:14 but NOT a signal at the close", R.flagged & ~R.sig_close)):
    g = R[m]
    if len(g) >= 3:
        print(f"  {lab:<58}{len(g):>6}{g.jump.mean():>+10.3f}{g.jump.median():>+9.3f}{(g.jump > 0).mean()*100:>6.0f}{g.jump.std():>7.2f}")
print(f"\n  share of the day's volume that prints after 15:15 (the auction): mean {R.volpct_after.mean():.1f}%")
F = R[R.flagged]
if len(F):
    se = F.jump.std() / np.sqrt(len(F)); print(f"  flagged: mean pop {F.jump.mean():+.3f}% +/- {1.96*se:.3f} (95% interval)")
    print("  by month: " + "  ".join(f"{k}: n={len(g)} {g.jump.mean():+.2f}%" for k, g in F.groupby(F.day.dt.to_period('M'))))
