"""Stress test of V1 Breakout+Vol (60 F&O stocks, price-only).  Same mechanics as stk_triggers.py.
 1 by-year   2 concentration (top trades / stocks)   3 long-only vs long-only shifted null
 4 neighbouring parameters (channel 20/55 x volume 1.0/1.5/2.0) -- sensitivity, NOT a search for a winner"""
import json, os, sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
prices, lots, nifty = pickle.load(open(RES / "stk_prices.pkl", "rb"))
COST, TOPN, WARM = 0.0010, 60, 200
turn = {s: float((d["Close"] * d["Volume"]).tail(250).median()) for s, d in prices.items()}
names = json.load(open(os.environ["STK_UNIV"])) if os.environ.get("STK_UNIV") else sorted(turn, key=turn.get, reverse=True)[:TOPN]


def prep(N, volm, exitN):
    S = {}
    for s in names:
        d = prices[s]; c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        hiN, loN = h.shift(1).rolling(N).max(), l.shift(1).rolling(N).min()
        hiX, loX = h.shift(1).rolling(exitN).max(), l.shift(1).rolling(exitN).min()
        volx = v / v.shift(1).rolling(20).mean()
        ok = volx >= volm
        sig = np.where((c > hiN) & ok, 1, np.where((c < loN) & ok, -1, 0))
        sig = np.where(np.arange(len(c)) >= WARM, sig, 0)
        S[s] = dict(dates=list(d.index), sig=sig, c=c.tolist(), atr=atr.tolist(), hiX=hiX.tolist(), loX=loX.tolist())
    return S


def simulate(A, sig, maxhold=30):
    c, atr, n = A["c"], A["atr"], len(A["c"]); pos, out = 0, []
    for t in range(WARM, n):
        if pos:
            ex = None
            if pos * (c[t] - ep) <= -2 * ea: ex = "STOP"
            elif (pos == 1 and c[t] < A["loX"][t]) or (pos == -1 and c[t] > A["hiX"][t]): ex = "CHANNEL"
            elif t - et >= maxhold: ex = "TIME"
            if ex or t == n - 1:
                out.append((A["dates"][et], A["dates"][t], pos, pos * (c[t] - ep) / ep - COST, ex or "OPEN")); pos = 0
            continue
        if sig[t]:
            pos, ep, ea, et = int(sig[t]), c[t], atr[t], t
    return out


def run(S, side=0, rng=None):
    rows = []
    for s in names:
        sg = S[s]["sig"].copy()
        if side: sg = np.where(sg == side, sg, 0)
        if rng is not None:
            seg = np.roll(sg[WARM:], int(rng.integers(10, len(sg) - WARM - 10))); sg = np.concatenate([sg[:WARM], seg])
        rows += [(s,) + r for r in simulate(S[s], sg.tolist())]
    return pd.DataFrame(rows, columns=["stock", "entry", "exit", "dir", "ret", "reason"])


def wk_t(T):
    r = T["ret"].values; n = len(r); mu = r.mean()
    wk = T["entry"].dt.isocalendar(); key = (wk["year"].astype(str) + "-" + wk["week"].astype(str)).values
    g = pd.Series(r - mu).groupby(key).sum(); return mu / (np.sqrt((g ** 2).sum()) / n)


S = prep(20, 1.5, 10)
T = run(S)
print(f"V1 baseline: {len(T)} trades, net {T.ret.mean()*100:+.2f}%/trade, median trade {T.ret.median()*100:+.2f}%, win {(T.ret>0).mean()*100:.0f}%")
print("\n[1] by entry year (net %/trade, n)")
T["yr"] = T["entry"].dt.year
for y, g in T.groupby("yr"):
    L, Sh = g[g.dir == 1], g[g.dir == -1]
    print(f"  {y}: n={len(g):>4}  all {g.ret.mean()*100:+.2f}  long {L.ret.mean()*100:+.2f} (n={len(L)})  short {Sh.ret.mean()*100:+.2f} (n={len(Sh)})")
print("\n[2] concentration")
srt = T.ret.sort_values(ascending=False)
tot = T.ret.sum()
print(f"  top 10 trades = {srt.head(10).sum()/tot*100:.0f}% of total return; top 5% of trades ({int(len(T)*.05)}) = {srt.head(int(len(T)*.05)).sum()/tot*100:.0f}%")
print(f"  mean excluding the top 5% of trades: {srt.iloc[int(len(T)*.05):].mean()*100:+.2f}%/trade")
bs = T.groupby("stock").ret.agg(["mean", "count"])
print(f"  stocks with positive mean: {(bs['mean']>0).sum()}/{len(bs)};  best 5: " + ", ".join(f"{i} {r['mean']*100:+.1f}%" for i, r in bs.sort_values('mean', ascending=False).head(5).iterrows()))
print("\n[3] long-only vs its own shifted null (100 draws)")
rng = np.random.default_rng(9)
Tl = run(S, side=1); mu = Tl.ret.mean() * 100
nl = np.array([run(S, 1, rng).ret.mean() * 100 for _ in range(100)])
print(f"  long-only: n={len(Tl)} net {mu:+.2f}%/trade, week-clustered t {wk_t(Tl):+.2f} | shifted-null median {np.median(nl):+.2f}%, 95th pct {np.percentile(nl,95):+.2f}% | p={float((nl>=mu).mean()):.3f}")
print(f"  excess over null median: {mu-np.median(nl):+.2f}%/trade")
Ts = run(S, side=-1)
print(f"  short-only: n={len(Ts)} net {Ts.ret.mean()*100:+.2f}%/trade")
print("\n[4] neighbouring parameters (net %/trade, n, week-clustered t, shift-null p with 50 draws)")
print(f"  {'channel/exit':<14}{'vol>=':>6}{'n':>6}{'net%':>8}{'t(wk)':>7}{'null med%':>10}{'p':>7}")
for N, X in ((20, 10), (55, 20)):
    for vm in (1.0, 1.5, 2.0):
        Sg = prep(N, vm, X); Tg = run(Sg); rg = np.random.default_rng(3)
        ng = np.array([run(Sg, 0, rg).ret.mean() * 100 for _ in range(50)])
        m = Tg.ret.mean() * 100
        print(f"  {N}/{X:<11}{vm:>6.1f}{len(Tg):>6}{m:>+8.2f}{wk_t(Tg):>+7.2f}{np.median(ng):>+10.2f}{float((ng>=m).mean()):>7.3f}", flush=True)
