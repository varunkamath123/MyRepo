"""Gap-fade lab: buy the gap-down open / sell short the gap-up open, exit at the same day's close (buy low, sell high within the day).
NIFTY 50 stocks 2012-2026, daily data.  The first study's 'hedged' column for these rows was WRONG (index close-to-close vs a stock open-to-close); here the hedge is the
NIFTY open->close move on the same day (a short NIFTY future for a long, a long for a short), plus 0.03%.
Pre-registered variants (7 looks):
  G1 raw gap >= g, g = 0.5% / 1.0% / 1.5% / 2.0%  (both sides)
  G2 IDIOSYNCRATIC gap >= 1.0%: (stock open / prior close - 1) - (NIFTY open / prior close - 1), i.e. fade only the part of the gap that is not the market's
  G3 as G2 but only the 3 largest idiosyncratic gaps of each day (what a small account could actually hold)
Costs per round trip 0.10% (stress 0.15% / 0.20%: the open auction fill is the worst-liquidity moment).  Halves: before / from 2020-01-01."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
SPLIT, HEDGE = pd.Timestamp("2020-01-01"), 0.0003
ix = D["idx"]["NIFTY"]; ix = ix[ix.index < TODAY]

rows = []
for s in D["n50"]:
    d = D["stocks"][s]; d = d[d.index < TODAY].join(ix[["Open", "Close"]].rename(columns={"Open": "NO", "Close": "NC"}), how="inner")
    o, c = d["Open"].values, d["Close"].values; no, nc = d["NO"].values, d["NC"].values
    pc, npc = np.r_[np.nan, c[:-1]], np.r_[np.nan, nc[:-1]]
    gap, ngap = o / pc - 1, no / npc - 1
    rows.append(pd.DataFrame(dict(date=d.index, sym=s, gap=gap, idio=gap - ngap, intra=c / o - 1, mkt=nc / no - 1)))
A = pd.concat(rows).dropna()
A = A[A.date >= A.date.min() + pd.Timedelta(days=60)]
yrs = (A.date.max() - A.date.min()).days / 365.25
print(f"{len(A):,} stock-days, {A.date.min().date()} -> {A.date.max().date()} ({yrs:.1f} years)\n")


def evaluate(T, label, cost=0.0010):
    T = T.copy(); T["side"] = np.where(T.signal > 0, 1, -1)
    T["ret"] = T.side * T.intra - cost; T["hedged"] = T.ret - T.side * T.mkt - HEDGE
    if len(T) < 30:
        print(f"  {label:<44}{len(T):>6}"); return
    w, l = T.ret[T.ret > 0], T.ret[T.ret <= 0]; pf = w.sum() / abs(l.sum())
    h = lambda x, col: f"{x[col].mean()*100:+.2f}" if len(x) else " n/a"
    Lg, Sh = T[T.side == 1], T[T.side == -1]
    print(f"  {label:<44}{len(T):>6}{len(T)/yrs:>6.0f}{(T.ret>0).mean()*100:>5.0f}{T.ret.mean()*100:>+8.2f}{pf:>6.2f}{h(T[T.date<SPLIT],'ret'):>7}{h(T[T.date>=SPLIT],'ret'):>7}"
          f"{T.hedged.mean()*100:>+9.2f}{h(T[T.date<SPLIT],'hedged'):>7}{h(T[T.date>=SPLIT],'hedged'):>7} | long {h(Lg,'hedged'):>6} (n={len(Lg)}) short {h(Sh,'hedged'):>6} (n={len(Sh)})"
          f" | net exp at cost 0.15%: {(T.ret.mean()-0.0005)*100:+.2f}  0.20%: {(T.ret.mean()-0.0010)*100:+.2f}")


print(f"  {'variant':<44}{'n':>6}{'/yr':>6}{'win%':>5}{'exp%':>8}{'PF':>6}{'H1':>7}{'H2':>7}{'hedged%':>9}{'H1':>7}{'H2':>7}")
for g in (0.005, 0.01, 0.015, 0.02):
    T = A[(A.gap <= -g) | (A.gap >= g)].assign(signal=lambda x: np.where(x.gap <= -g, 1, -1))
    evaluate(T, f"G1 raw gap >= {g*100:.1f}% (buy gap-down / short gap-up)")
T2 = A[A.idio.abs() >= 0.01].assign(signal=lambda x: np.where(x.idio <= -0.01, 1, -1))
evaluate(T2, "G2 idiosyncratic gap >= 1.0%")
T3 = A[A.idio.abs() >= 0.01].assign(score=lambda x: x.idio.abs()).sort_values("score", ascending=False).groupby("date").head(3).assign(signal=lambda x: np.where(x.idio <= -0.01, 1, -1))
evaluate(T3, "G3 idiosyncratic, top 3 per day")
print("\n  G2 by year (hedged exp%, n): " + "  ".join(f"{y}: {g.hedged.mean()*100:+.2f} ({len(g)})" for y, g in T2.assign(side=np.where(T2.signal > 0, 1, -1), ret=lambda x: np.where(x.signal > 0, 1, -1) * x.intra - 0.001).assign(hedged=lambda x: x.ret - x.side * x.mkt - HEDGE).groupby(T2.date.dt.year)))
