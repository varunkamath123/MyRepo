"""Is the stock breakout edge just market beta, and does either side work in specific market regimes?
Same V1 trades as stk_triggers.py (Breakout+Vol, both sides, 0.10% cost deducted).  Two cuts:
 1 beta-hedged return: trade return minus the NIFTY move over the same holding period (long hedged with a NIFTY short,
   short hedged with a NIFTY long), less 0.03% for the hedge round trip -- the stock-selection part only
 2 regime at entry: NIFTY vs its 50-day and 200-day averages, by side."""
import os, sys
src = open("/opt/kronos_bot/research/stk_triggers.py").read().split("span_q = (cal[-1]")[0]
sys.argv = ["x", "5"]
exec(compile(src, "stk_triggers_head", "exec"))
import numpy as np
import pandas as pd

HEDGE_COST = 0.0003
nf = nifty.copy(); nf.index = pd.to_datetime(nf.index).normalize()
sma50, sma200 = nf.rolling(50).mean(), nf.rolling(200).mean()
T = run("V1 Breakout+Vol").copy()
T["entry"] = pd.to_datetime(T["entry"]).dt.normalize(); T["exit"] = pd.to_datetime(T["exit"]).dt.normalize()
px = lambda d: nf.reindex(nf.index.union([d])).ffill().loc[d]
T["nret"] = [px(x) / px(e) - 1 for e, x in zip(T["entry"], T["exit"])]
T["alpha"] = np.where(T["dir"] == 1, T["ret"] - T["nret"], T["ret"] + T["nret"]) - HEDGE_COST
T["above50"] = [px(e) > sma50.reindex(nf.index.union([e])).ffill().loc[e] for e in T["entry"]]
T["above200"] = [px(e) > sma200.reindex(nf.index.union([e])).ffill().loc[e] for e in T["entry"]]


def wk_t(x, col):
    r = x[col].values; n = len(r); mu = r.mean()
    if n < 5: return float("nan")
    wk = x["entry"].dt.isocalendar(); key = (wk["year"].astype(str) + "-" + wk["week"].astype(str)).values
    g = pd.Series(r - mu).groupby(key).sum(); return mu / (np.sqrt((g ** 2).sum()) / n)


def row(label, x):
    if len(x) == 0:
        print(f"  {label:<34}{0:>5}"); return
    print(f"  {label:<34}{len(x):>5}{(x.ret>0).mean()*100:>6.0f}%{x.ret.mean()*100:>+9.2f}{x.alpha.mean()*100:>+10.2f}{wk_t(x,'alpha'):>+8.2f}{x.nret.mean()*100:>+9.2f}")


print(f"\nNIFTY 50 stocks, V1 breakout + volume, {len(T)} trades; NIFTY moved {T[T.dir==1].nret.mean()*100:+.2f}% on average over the long trades' holding periods")
print(f"  {'':<34}{'n':>5}{'win%':>7}{'net%':>9}{'hedged%':>10}{'t(wk)':>8}{'NIFTY%':>9}")
print("[1] beta-hedged (net minus NIFTY move over the same holding period, minus 0.03% hedge cost)")
row("LONG  (hedged with NIFTY short)", T[T.dir == 1])
row("SHORT (hedged with NIFTY long)", T[T.dir == -1])
print("[2] by market regime at entry")
for lab, m in (("NIFTY above 200-DMA", T.above200), ("NIFTY below 200-DMA", ~T.above200), ("NIFTY above 50-DMA", T.above50), ("NIFTY below 50-DMA", ~T.above50)):
    row(f"LONG  | {lab}", T[(T.dir == 1) & m])
for lab, m in (("NIFTY above 200-DMA", T.above200), ("NIFTY below 200-DMA", ~T.above200), ("NIFTY above 50-DMA", T.above50), ("NIFTY below 50-DMA", ~T.above50)):
    row(f"SHORT | {lab}", T[(T.dir == -1) & m])
print("[3] longs by year, hedged vs unhedged")
L = T[T.dir == 1]
for y, g in L.groupby(L["entry"].dt.year):
    print(f"  {y}: n={len(g):>3}  net {g.ret.mean()*100:+.2f}%  hedged {g.alpha.mean()*100:+.2f}%  NIFTY over holds {g.nret.mean()*100:+.2f}%")
S_ = T[T.dir == -1]
print("    shorts:")
for y, g in S_.groupby(S_["entry"].dt.year):
    print(f"  {y}: n={len(g):>3}  net {g.ret.mean()*100:+.2f}%  hedged {g.alpha.mean()*100:+.2f}%  NIFTY over holds {g.nret.mean()*100:+.2f}%")
print(f"\nNIFTY 50-DMA state over the sample: above {np.mean(nf.loc[cal[WARM]:] > sma50.loc[cal[WARM]:])*100:.0f}% of days; 200-DMA: above {np.mean(nf.loc[cal[WARM]:] > sma200.loc[cal[WARM]:])*100:.0f}% of days")
