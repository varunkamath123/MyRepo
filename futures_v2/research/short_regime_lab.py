"""Shorting in a DOWNTURN: does a market-regime gate rescue the short side?  (H27 showed shorts lose over the whole sample; its regime split used one definition.)  14.9 years, daily, 2012-2026.
Four downturn definitions on NIFTY (state at the signal close; fixed BEFORE the run):
  D1  NIFTY < its 200-day average          D2  NIFTY < its 50-day average
  D3  NIFTY < 50-DMA and 50-DMA < 200-DMA (a confirmed downtrend)          D4  NIFTY >= 10% below its trailing-250-day high
PART A  STOCK shorts (the live short-shadow signal: close < prior-20d low on >= 1.5x volume; NIFTY 50 names, today's constituents -> survivorship runs AGAINST shorts), taken ONLY when the regime holds at entry.
        Exits: S-BASE (stop +2 ATR, close > prior-10d high, 30 sessions) and S-TR4 (stop +2 ATR, close > lowest close + 4 ATR).  Cost 0.10%; hedged = short + NIFTY move - 0.03%.  8 looks.
        Reported raw and hedged, with the 2020 crash (entries 15 Feb - 30 Jun 2020) removed, and as episodes (regime spells with gaps <= 20 sessions merged): how many episodes with >= 5 trades were positive.
PART B  INDEX shorts (NIFTY and BANKNIFTY futures, no survivorship issue): short at the close while the regime holds, flat otherwise, daily compounding, 0.047% / 0.054% round trip per entry, 8 looks.
        Reported with the long-in-the-same-regime alternative, with 2020 removed, and by calendar year.
Decision rule (stricter because the looks are many): a regime short earns a SHADOW ledger only if RAW expectancy > 0 AND hedged > 0 with t > 2.7, still positive with 2020 removed, and positive in >= 2/3 of episodes with >= 5 trades (Part A);
for Part B annualised return > 0 with 2020 removed AND in >= 2/3 of years in the regime.  Otherwise the short side stays a shadow ledger only."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IDX = {u: DD["idx"][u]["Close"][~DD["idx"][u]["Close"].index.duplicated()] for u in ("NIFTY", "BANKNIFTY")}
NIFTY = IDX["NIFTY"]
COST, HEDGE_COST = 0.001, 0.0003
IDX_COST = {"NIFTY": 0.000472, "BANKNIFTY": 0.000541}
CRASH = (pd.Timestamp("2020-02-15"), pd.Timestamp("2020-06-30"))


def regimes(c):
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean(); dd = c / c.rolling(250).max() - 1
    return pd.DataFrame({"D1": c < s200, "D2": c < s50, "D3": (c < s50) & (s50 < s200), "D4": dd <= -0.10}).fillna(False)


REG = regimes(NIFTY)


def episodes(flag, gap=20):
    """episode id per date: contiguous True spells, gaps <= `gap` sessions merged; NaN outside."""
    ids = pd.Series(np.nan, index=flag.index); eid, last_true, inside = 0, None, False
    pos = {d: i for i, d in enumerate(flag.index)}
    for d, v in flag.items():
        i = pos[d]
        if v:
            if last_true is None or i - last_true > gap + 1: eid += 1
            last_true = i; ids[d] = eid
    return ids


EP = {k: episodes(REG[k]) for k in REG}


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(); d = d[~d.index.duplicated()]
    pc = d.Close.shift(1); tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    A = dict(idx=d.index, c=d.Close.values, atr=tr.rolling(14).mean().values, lo20=d.Low.rolling(20).min().shift(1).values, hi10=d.High.rolling(10).max().shift(1).values,
             vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    A["sig"] = (d.Close.values < A["lo20"]) & (A["vr"] >= 1.5) & ~np.isnan(A["atr"]) & (d.Volume.values > 0)
    return A


def run(A, kind, k=4.0):
    c, atr, n = A["c"], A["atr"], len(A["c"]); out = []; pos = False; last = -1
    for t in range(n):
        if pos:
            held = t - et; lc = min(lc, c[t]); why = None
            if c[t] >= ep + 2.0 * ea: why = "STOP"
            elif kind == "base":
                if c[t] > A["hi10"][t]: why = "CHAN"
                elif held >= 30: why = "TIME"
            elif c[t] > lc + k * atr[t]: why = "TRAIL"
            if why or t == n - 1:
                out.append((A["idx"][et], A["idx"][t], -(c[t] / ep - 1) - COST, held, why or "OPEN")); pos = False; last = t
            continue
        if A["sig"][t] and t != last: pos, ep, ea, et, lc = True, c[t], atr[t], t, c[t]
    return out


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
data = {s: prep(DD["stocks"][s]) for s in names}
ALL = {}
for kind, label in (("base", "S-BASE"), ("trail", "S-TR4")):
    rows = []
    for s in names: rows += [(s,) + r for r in run(data[s], kind)]
    T = pd.DataFrame(rows, columns=["sym", "entry", "exit", "ret", "held", "why"]); T = T[T.why != "OPEN"].copy()
    ent = NIFTY.reindex(T.entry, method="ffill").values; ext = NIFTY.reindex(T.exit, method="ffill").values
    T["hedged"] = T.ret + (ext / ent - 1) - HEDGE_COST
    for k in REG: T[k] = REG[k].reindex(T.entry, method="ffill").values.astype(bool); T["ep_" + k] = EP[k].reindex(T.entry, method="ffill").values
    ALL[label] = T

print("PART A  stock shorts (breakdown on >= 1.5x volume), taken only in the regime at entry")
print(f"  {'exit':<7}{'regime':<7}{'n':>5}{'win%':>5}{'raw%':>7}{'hdg%':>7}{'t raw':>6}{'t hdg':>6} | {'ex-2020 n':>9}{'raw%':>7}{'hdg%':>7} | episodes>=5 trades: positive{'':<2}{'verdict'}")
for label, T in ALL.items():
    base_all = T
    print(f"  {label:<7}{'ALL':<7}{len(T):>5}{(T.ret>0).mean()*100:>5.0f}{T.ret.mean()*100:>+7.2f}{T.hedged.mean()*100:>+7.2f}")
    for k in REG:
        X = T[T[k]]
        if len(X) < 30:
            print(f"  {label:<7}{k:<7}{len(X):>5}  too few"); continue
        Y = X[~((X.entry >= CRASH[0]) & (X.entry <= CRASH[1]))]
        tr_ = X.ret.mean() / (X.ret.std() / np.sqrt(len(X))); th = X.hedged.mean() / (X.hedged.std() / np.sqrt(len(X)))
        e = X.groupby("ep_" + k).agg(n=("ret", "size"), m=("ret", "mean")); e5 = e[e.n >= 5]
        pos_e = int((e5.m > 0).sum())
        ok = X.ret.mean() > 0 and X.hedged.mean() > 0 and tr_ > 2.7 and th > 2.7 and len(Y) > 0 and Y.ret.mean() > 0 and len(e5) > 0 and pos_e / len(e5) >= 2 / 3
        print(f"  {label:<7}{k:<7}{len(X):>5}{(X.ret>0).mean()*100:>5.0f}{X.ret.mean()*100:>+7.2f}{X.hedged.mean()*100:>+7.2f}{tr_:>+6.1f}{th:>+6.1f} | {len(Y):>9}{Y.ret.mean()*100:>+7.2f}{Y.hedged.mean()*100:>+7.2f} | {pos_e}/{len(e5)} episodes{'':<9}{'PASS' if ok else 'fail'}")
print("\n  episodes of D1 (NIFTY < 200-DMA, gaps <= 20 sessions merged), S-BASE shorts: start .. end, NIFTY move over the spell, trades, mean raw / hedged %")
T = ALL["S-BASE"]; X = T[T["D1"]]
for eid, g in X.groupby("ep_D1"):
    span = EP["D1"][EP["D1"] == eid].index; nm = NIFTY.reindex([span[0], span[-1]]).values
    if len(g) >= 3: print(f"    {span[0].date()} .. {span[-1].date()}  NIFTY {nm[1]/nm[0]-1:+6.1%}  n={len(g):>3}  raw {g.ret.mean()*100:+6.2f}%  hedged {g.hedged.mean()*100:+6.2f}%")

print("\nPART B  index shorts (short while the regime holds, flat otherwise; costs per flip), annualised % of notional")
print(f"  {'index':<10}{'regime':<7}{'in regime%':>11}{'flips/yr':>9}{'SHORT ann%':>11}{'Sharpe':>7}{'maxDD%':>8}{'ex-2020 ann%':>13}{'pos yrs':>9} | {'LONG same days ann%':>20}  {'verdict'}")
for u, c in IDX.items():
    r = c.pct_change().fillna(0); yrs = (c.index[-1] - c.index[0]).days / 365.25
    for k in REG:
        f = REG[k].reindex(c.index).fillna(False).values; hold = np.r_[False, f[:-1]]           # decided at the close, earns the NEXT day
        sr = np.where(hold, -r.values, 0.0); flips = np.r_[0, (np.diff(f.astype(int)) != 0)]
        sr = sr - np.r_[0, (np.diff(hold.astype(int)) != 0)[:] * (IDX_COST[u] / 2)]
        s = pd.Series(sr, index=c.index); eq = (1 + s).cumprod(); ann = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
        sh = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else np.nan; mdd = ((eq / eq.cummax()) - 1).min() * 100
        mask = ~((c.index >= CRASH[0]) & (c.index <= CRASH[1])); s2 = s[mask]; yrs2 = mask.sum() / 252
        ann2 = ((1 + s2).prod() ** (1 / yrs2) - 1) * 100
        yy = s.groupby(s.index.year).apply(lambda x: (1 + x).prod() - 1); iny = pd.Series(hold, index=c.index).groupby(c.index.year).mean()
        yy = yy[iny > 0.05]; posy = int((yy > 0).sum())
        lg = pd.Series(np.where(hold, r.values, 0.0), index=c.index); la = ((1 + lg).prod() ** (1 / yrs) - 1) * 100
        ok = ann2 > 0 and len(yy) > 0 and posy / len(yy) >= 2 / 3
        print(f"  {u:<10}{k:<7}{hold.mean()*100:>11.0f}{flips.sum()/yrs:>9.1f}{ann:>+11.1f}{sh:>7.2f}{mdd:>8.0f}{ann2:>+13.1f}{posy:>5}/{len(yy):<3} | {la:>+20.1f}  {'PASS' if ok else 'fail'}")
print("\n  by calendar year, NIFTY SHORT overlay return % (regime D2: below 50-DMA) and % of days in regime:")
c = IDX["NIFTY"]; r = c.pct_change().fillna(0); f = REG["D2"].reindex(c.index).fillna(False).values; hold = np.r_[False, f[:-1]]
s = pd.Series(np.where(hold, -r.values, 0.0), index=c.index); yy = s.groupby(s.index.year).apply(lambda x: (1 + x).prod() - 1) * 100; iny = pd.Series(hold, index=c.index).groupby(c.index.year).mean() * 100
bh = r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1) * 100
print("    " + "  ".join(f"{y}: {yy[y]:+.0f} ({iny[y]:.0f}%, mkt {bh[y]:+.0f})" for y in yy.index))
