"""Does Kronos separate better from worse Breakout+Vol (V1) entries?  Uses the cached reads on V1 signal days.
Take every V1 entry from the price-only simulation, tag it with the Kronos read the live bot would have seen
(complete bars through the signal day's previous bar), and compare outcomes by agreement.  Permutation test: shuffle the
agree/other labels over the same entries.  First-order test (does not re-simulate the position slots a filter would free up).
Kronos EXITS are not tested here (they need a read on every held day)."""
import json, os, pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
prices, lots, nifty = pickle.load(open(RES / "stk_prices.pkl", "rb"))
reads = json.load(open(RES / "stk_kronos_reads.json"))
COST, TOPN, WARM = 0.0010, 60, 200
turn = {s: float((d["Close"] * d["Volume"]).tail(250).median()) for s, d in prices.items()}
names = json.load(open(os.environ["STK_UNIV"])) if os.environ.get("STK_UNIV") else sorted(turn, key=turn.get, reverse=True)[:TOPN]


def simulate(s):
    d = prices[s]; c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().tolist()
    hi20, lo20 = h.shift(1).rolling(20).max(), l.shift(1).rolling(20).min()
    hi10, lo10 = h.shift(1).rolling(10).max().tolist(), l.shift(1).rolling(10).min().tolist()
    volx = v / v.shift(1).rolling(20).mean()
    fire_l, fire_s = ((c > hi20) & (volx >= 1.5)).tolist(), ((c < lo20) & (volx >= 1.5)).tolist()
    cl, dates, n = c.tolist(), list(d.index), len(c)
    pos, out = 0, []
    for t in range(WARM, n):
        if pos:
            ex = None
            if pos * (cl[t] - ep) <= -2 * ea: ex = "STOP"
            elif (pos == 1 and cl[t] < lo10[t]) or (pos == -1 and cl[t] > hi10[t]): ex = "CHANNEL"
            elif t - et >= 30: ex = "TIME"
            if ex or t == n - 1:
                out.append(dict(stock=s, entry=dates[et], exit=dates[t], dir=pos, ret=pos * (cl[t] - ep) / ep - COST,
                                key=f"{s}|{dates[et-1]}")); pos = 0
            continue
        if fire_l[t] or fire_s[t]:
            pos = 1 if fire_l[t] else -1; ep, ea, et = cl[t], atr[t], t
    return out


T = pd.DataFrame([r for s in names for r in simulate(s)])
T = T[T.key.isin(reads)].copy()
rd = T.key.map(reads)
T["rdir"] = rd.map(lambda x: x[0]); T["rconf"] = rd.map(lambda x: x[1])
want = T.dir.map({1: "LONG", -1: "SHORT"})
T["agree"] = T.rdir == want
T["oppose"] = T.rdir == T.dir.map({1: "SHORT", -1: "LONG"})
T["neutral"] = T.rdir == "NEUTRAL"
print(f"V1 entries with a cached Kronos read: {len(T)}")
print(f"Kronos read mix at entries: LONG {np.mean(T.rdir=='LONG')*100:.0f}% | SHORT {np.mean(T.rdir=='SHORT')*100:.0f}% | NEUTRAL {np.mean(T.rdir=='NEUTRAL')*100:.0f}%;"
      f" conf>=0.35 in {np.mean(T.rconf>=.35)*100:.0f}%, >=0.55 in {np.mean(T.rconf>=.55)*100:.0f}%\n")


def wk_t(X):
    r = X["ret"].values; n = len(r); mu = r.mean()
    if n < 5: return float("nan")
    wk = X["entry"].dt.isocalendar(); key = (wk["year"].astype(str) + "-" + wk["week"].astype(str)).values
    g = pd.Series(r - mu).groupby(key).sum(); return mu / (np.sqrt((g ** 2).sum()) / n)


rng = np.random.default_rng(1)
for label, X in (("ALL V1 entries", T), ("LONG entries only", T[T.dir == 1])):
    print(f"=== {label} (n={len(X)}; net %/trade) ===")
    print(f"  {'group':<22}{'n':>6}{'mean%':>8}{'win%':>6}{'t(wk)':>7}{'vs rest%':>10}{'perm p':>8}")
    groups = [("all (no filter)", np.ones(len(X), bool)),
              ("Kronos agrees (any)", X.agree.values),
              ("agrees, conf>=0.35", (X.agree & (X.rconf >= .35)).values),
              ("agrees, conf>=0.55", (X.agree & (X.rconf >= .55)).values),
              ("NEUTRAL", X.neutral.values),
              ("opposes", X.oppose.values)]
    for name, m in groups:
        g, rest = X[m], X[~m]
        if len(g) < 5:
            print(f"  {name:<22}{len(g):>6}"); continue
        diff = g.ret.mean() - rest.ret.mean() if len(rest) else float("nan")
        p = float("nan")
        if name != "all (no filter)" and len(rest) >= 5:
            r = X.ret.values; cnt = 0
            for _ in range(5000):
                idx = rng.permutation(len(r))[:len(g)]
                cnt += (r[idx].mean() - np.delete(r, idx).mean()) >= diff
            p = cnt / 5000
        print(f"  {name:<22}{len(g):>6}{g.ret.mean()*100:>+8.2f}{(g.ret>0).mean()*100:>5.0f}%{wk_t(g):>+7.2f}{diff*100:>+10.2f}{p:>8.3f}")
    print()
