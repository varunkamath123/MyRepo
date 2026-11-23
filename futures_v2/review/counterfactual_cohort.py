"""If the stock book had been live earlier: replay the LIVE engine (stock_book.evaluate_entry / check_exit, full-day volume so no 15:25 approximation) day by day on
Upstox daily bars from START, one position per stock, uncapped signal book, marked to today's quote.  Days on or after the real start (Nov 11) double as a check:
the replay should reproduce the real entries.  Usage: python counterfactual_cohort.py [START=2026-11-02]"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import stock_book as sb

START = pd.Timestamp(sys.argv[1] if len(sys.argv) > 1 else "2026-11-02")
IST = timezone(timedelta(hours=5, minutes=30)); today = pd.Timestamp(datetime.now(IST).date())
uni = sb._load_universe(); q = sb.load_quotes([u["key"] for u in uni])
rows = []
for u in uni:
    df = sb.load_daily(u["key"], bars=150).reset_index(drop=True); df["d"] = df["datetime"].dt.normalize()
    px_now = q[u["key"]]["px"]; pos = None; exited_on = None
    days = [i for i in range(len(df)) if df.d[i] >= START]
    for i in days + [len(df)]:                              # len(df) == today (live quote)
        px = px_now if i == len(df) else float(df.close[i]); hist = df.iloc[:i]; d = today if i == len(df) else df.d[i]
        if pos is not None:
            reason = sb.check_exit(pos, hist, px)
            if reason:
                rows.append(dict(sym=u["symbol"], entry=pos.entry_date, exit=d, ret=(px / pos.entry_price - 1) - sb.COST_PCT, pnl=(px - pos.entry_price) * pos.lot - sb.COST_PCT * pos.entry_price * pos.lot,
                                 status=reason, volx=pos.volx)); pos = None; exited_on = d
            continue
        if i == len(df) or exited_on == d:
            continue
        ev = sb.evaluate_entry(hist, px, float(df.volume[i]), vol_adj=1.0, day_high=float(df.high[i]), day_low=float(df.low[i]))
        if ev["signal"]:
            pos = sb.StockPos(u["symbol"], str(d.date()), px, ev["atr"], u["lot"], False, round(ev["volx"], 3))
    if pos is not None:
        rows.append(dict(sym=u["symbol"], entry=pd.Timestamp(pos.entry_date), exit=pd.NaT, ret=(px_now / pos.entry_price - 1), pnl=(px_now - pos.entry_price) * pos.lot, status="OPEN", volx=pos.volx))
R = pd.DataFrame(rows); R["entry"] = pd.to_datetime(R["entry"])
print(f"replay of the live rules from {START.date()} to {today.date()} (signal book, uncapped, marked to today's quote; exits applied)\n")
print(f"  {'entry day':<12}{'signals':>8}{'closed':>8}{'mean ret%':>10}{'win%':>6}{'Rs per lot (sum)':>18}")
for d, g in R.groupby("entry"):
    print(f"  {d.date().isoformat():<12}{len(g):>8}{int((g.status != 'OPEN').sum()):>8}{g.ret.mean()*100:>+10.2f}{(g.ret > 0).mean()*100:>6.0f}{g.pnl.sum():>+18,.0f}")
pre, post = R[R.entry < pd.Timestamp("2026-11-11")], R[R.entry >= pd.Timestamp("2026-11-11")]
print(f"\n  before the real start (Nov 2-10): {len(pre)} positions, mean {pre.ret.mean()*100:+.2f}%, total Rs{pre.pnl.sum():+,.0f} per lot-each; {int((pre.status != 'OPEN').sum())} already closed ({', '.join(pre[pre.status != 'OPEN'].status.value_counts().index)})")
print(f"  from Nov 11 (the real book): replay {len(post)} positions, mean {post.ret.mean()*100:+.2f}%, total Rs{post.pnl.sum():+,.0f}   <-- the real book holds 14 positions")
print(f"  replay entries from Nov 11 by day: " + ", ".join(f"{d.date()}: {len(g)}" for d, g in post.groupby("entry")))
top3 = pre.sort_values("volx", ascending=False)
print(f"\n  the 3 strongest-volume signals before Nov 11 (what 3 capital slots would have held): " + ", ".join(f"{r.sym} {r.entry.date()} {r.ret*100:+.1f}%" for r in top3.head(3).itertuples()) + f" -> Rs{top3.head(3).pnl.sum():+,.0f}")
