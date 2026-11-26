"""Stock book EXIT study: same entries, different exits.  NIFTY 50 names (today's constituents -> survivorship bias flatters longs and longer holds; read DIFFERENCES between variants, not levels), daily bars 2012 -> 2026.
Entry (identical for every variant, = the live signal): close > prior-20-day high AND volume >= 1.5 x prior-20-day average; one position per stock, no same-day re-entry; entry at the close.  ATR14 (simple mean of true range, includes today).
All exits are decided on closes.  Cost 0.10% round trip.  'excess' = trade return minus NIFTY's return over the same dates (strips the market drift the book collects by being long).
Variants fixed BEFORE the run (11 looks):
  BASE  stop 2 ATR (from entry), exit on close < prior-10-day low, 30 sessions                      (the live rules)
  T60 / T120      the same with a 60 / 120-session cap                (does the 30-session cap cut winners?)
  C20             channel exit on the prior-20-day low, cap 30
  C20T60          channel 20, cap 60
  NOCH            no channel exit (stop + cap 30)
  NOCHT60         no channel exit, cap 60
  S3              stop 3 ATR, channel 10, cap 30
  CH3             chandelier trail: close < highest close since entry - 3 x ATR (replaces the channel), initial stop 2 ATR, cap 60
  CH4             chandelier 4 ATR, initial stop 2 ATR, cap 120
  CH3T120         chandelier 3 ATR, cap 120
Capital-efficiency matters (3-5 slots): the headline metric is EXCESS RETURN PER POSITION-DAY (sum of excess returns / sum of sessions held), plus per-trade expectancy and the share of the total made by the top 5% of trades.
Decision rule: a variant replaces BASE only if its excess return per position-day AND its per-trade excess expectancy are higher in BOTH halves (split 2019-07-01) AND it wins in >= 10 of the 14 entry years on per-day yield.  Otherwise keep BASE."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
NIFTY = DD["idx"]["NIFTY"]["Close"]; NIFTY = NIFTY[~NIFTY.index.duplicated()]
COST = 0.001
SPLIT = pd.Timestamp("2019-07-01")
VARIANTS = {
    "BASE":    dict(stop=2.0, ch=10, cap=30),
    "T60":     dict(stop=2.0, ch=10, cap=60),
    "T120":    dict(stop=2.0, ch=10, cap=120),
    "C20":     dict(stop=2.0, ch=20, cap=30),
    "C20T60":  dict(stop=2.0, ch=20, cap=60),
    "NOCH":    dict(stop=2.0, ch=None, cap=30),
    "NOCHT60": dict(stop=2.0, ch=None, cap=60),
    "S3":      dict(stop=3.0, ch=10, cap=30),
    "CH3":     dict(stop=2.0, ch=None, cap=60, chand=3.0),
    "CH4":     dict(stop=2.0, ch=None, cap=120, chand=4.0),
    "CH3T120": dict(stop=2.0, ch=None, cap=120, chand=3.0),
}


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
    d = d[~d.index.duplicated()]
    pc = d.Close.shift(1)
    tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    out = dict(idx=d.index, c=d.Close.values, atr=tr.rolling(14).mean().values,
               hi20=d.High.rolling(20).max().shift(1).values,
               vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    for n in (10, 20): out[f"lo{n}"] = d.Low.rolling(n).min().shift(1).values
    sig = (d.Close.values > out["hi20"]) & (out["vr"] >= 1.5) & ~np.isnan(out["atr"]) & (d.Volume.values > 0)
    out["sig"] = sig
    return out


def run(A, v):
    c, atr, n = A["c"], A["atr"], len(A["c"]); out = []; pos = False; last_exit = -1
    for t in range(n):
        if pos:
            held = t - et; why = None
            hc = max(hc, c[t])
            if c[t] <= ep - v["stop"] * ea: why = "STOP"
            elif "chand" in v and c[t] < hc - v["chand"] * atr[t]: why = "CHAND"
            elif v["ch"] and c[t] < A[f"lo{v['ch']}"][t]: why = "CHAN"
            elif held >= v["cap"]: why = "TIME"
            if why or t == n - 1:
                out.append((A["idx"][et], A["idx"][t], c[t] / ep - 1 - COST, held, why or "OPEN")); pos = False; last_exit = t
            continue
        if A["sig"][t] and t != last_exit:
            pos, ep, ea, et, hc = True, c[t], atr[t], t, c[t]
    return out


def nifty_ret(a, b):
    na = NIFTY.reindex([a, b], method="ffill")
    return na.iloc[1] / na.iloc[0] - 1


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
data = {s: prep(DD["stocks"][s]) for s in names}
print(f"{len(names)} stocks, {min(A['idx'][0] for A in data.values()).date()} -> {max(A['idx'][-1] for A in data.values()).date()}")

res = {}
for k, v in VARIANTS.items():
    rows = []
    for s in names:
        rows += [(s,) + r for r in run(data[s], v)]
    T = pd.DataFrame(rows, columns=["sym", "entry", "exit", "ret", "held", "why"])
    T = T[T.why != "OPEN"].copy()
    # NIFTY over the same dates (vectorised)
    ent = NIFTY.reindex(T.entry, method="ffill").values; ext = NIFTY.reindex(T.exit, method="ffill").values
    T["exc"] = T.ret - (ext / ent - 1)
    res[k] = T

print(f"\n{'variant':<9}{'n':>6}{'win%':>6}{'avg win':>9}{'avg loss':>9}{'exp%':>8}{'excess%':>9}{'hold d':>7}{'exc bp/day':>11}{'H1 bp/d':>9}{'H2 bp/d':>9}{'H1 exc%':>8}{'H2 exc%':>8}{'top5% share':>12}{'yrs>BASE':>9}  exits")
yr_base = None
for k, T in res.items():
    perday = lambda x: x.exc.sum() / x.held.sum() * 1e4
    h1, h2 = T[T.entry < SPLIT], T[T.entry >= SPLIT]
    srt = T.ret.sort_values(ascending=False); top = srt.head(int(len(T) * 0.05)).sum() / T.ret.sum() * 100
    yr = T.groupby(T.entry.dt.year).apply(lambda x: x.exc.sum() / x.held.sum() * 1e4)
    if k == "BASE": yr_base = yr
    wins = int((yr.reindex(yr_base.index) > yr_base).sum()) if k != "BASE" else 0
    w, lo = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    ex = ", ".join(f"{a} {b}" for a, b in T.why.value_counts().items())
    print(f"{k:<9}{len(T):>6}{(T.ret>0).mean()*100:>6.0f}{w.mean()*100:>+9.2f}{lo.mean()*100:>+9.2f}{T.ret.mean()*100:>+8.2f}{T.exc.mean()*100:>+9.2f}{T.held.mean():>7.1f}{perday(T):>+11.1f}{perday(h1):>+9.1f}{perday(h2):>+9.1f}"
          f"{h1.exc.mean()*100:>+8.2f}{h2.exc.mean()*100:>+8.2f}{top:>11.0f}%{('-' if k=='BASE' else str(wins)+'/'+str(len(yr_base))):>9}  {ex}")

b = res["BASE"]
print("\nBASE by exit reason:  " + "   ".join(f"{k}: n={len(g)} mean {g.ret.mean()*100:+.2f}% excess {g.exc.mean()*100:+.2f}% hold {g.held.mean():.0f}d" for k, g in b.groupby("why")))
print("BASE by holding length:  " + "   ".join(f"{lab}: n={int(m.sum())} mean {b[m].ret.mean()*100:+.2f}%" for lab, m in (("<=5d", b.held <= 5), ("6-15d", (b.held > 5) & (b.held <= 15)), ("16-29d", (b.held > 15) & (b.held < 30)), ("hit 30d cap", b.held >= 30))))
cap = b[b.held >= 30]
print(f"\nTrades that hit the 30-session cap under BASE: {len(cap)} ({len(cap)/len(b)*100:.0f}% of trades), mean {cap.ret.mean()*100:+.2f}%; what the same entries do under T120:")
m = res["T120"].merge(cap[["sym", "entry"]], on=["sym", "entry"])
print(f"   those {len(m)} entries under T120: mean {m.ret.mean()*100:+.2f}% (hold {m.held.mean():.0f}d), exits {dict(m.why.value_counts())}")
