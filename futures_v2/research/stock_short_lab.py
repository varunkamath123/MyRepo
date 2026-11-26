"""Can the stock book short?  The earlier short tests (H10, H17) used the OLD exits.  The long side changed completely once the exit became a trailing stop (H24), so re-test the SHORT side with the mirrored trail.
Same data and conventions as stock_exit_lab2.py: NIFTY 50 names (TODAY's constituents -> survivorship bias runs AGAINST shorts, because stocks that crashed out of the index are missing), daily bars 2012-2026, cost 0.10% round trip, one position per stock,
no same-day re-entry, exits on closes.  SHORT entry = the live mirror: close < prior-20-day low AND volume >= 1.5x the prior-20-day average.  'hedged' = the short's return plus NIFTY's return over the holding period minus 0.03%
(the live definition: a long NIFTY hedge against the stock short, i.e. the stock-specific part of the trade).
Variants fixed BEFORE the run (6 looks):
  S-BASE      the live short-shadow rules: stop = entry + 2 ATR, exit on close > prior-10-day high, 30 sessions
  S-TR4       2 ATR initial stop, exit on close > lowest close since entry + 4 x ATR14, no cap (mirror of the long trail)
  S-TR3       the same with 3 ATR
  each also with a TREND-ALIGNED filter: only shorts of stocks already below their own 200-day average at entry (3 more rows = the 'T' rows) -> 6 variants in total
Reported for each: n, win%, avg win / loss, expectancy, hedged expectancy, hold, hedged bp per position-day, both halves (split 2019-07-01), entry years with positive hedged expectancy, the t-stat of the hedged mean (trades overlap across stocks, so it is generous).
Regime split (descriptive, not a look): trades opened with NIFTY below / above its own 200-day average.
Decision rule: shorts get a capital ledger only if a variant has hedged expectancy > 0 with t > 2.6 (6 looks), positive in BOTH halves and in >= 10 of 15 entry years.  Survivorship bias is against shorts, so a fail is 'no evidence', a pass would be a strong result."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
NIFTY = DD["idx"]["NIFTY"]["Close"]; NIFTY = NIFTY[~NIFTY.index.duplicated()]
NSMA = NIFTY.rolling(200).mean().shift(1)
COST, HEDGE_COST = 0.001, 0.0003
SPLIT = pd.Timestamp("2019-07-01")
VARIANTS = {"S-BASE": dict(kind="base"), "S-TR4": dict(kind="trail", k=4.0), "S-TR3": dict(kind="trail", k=3.0)}


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(); d = d[~d.index.duplicated()]
    pc = d.Close.shift(1); tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    A = dict(idx=d.index, c=d.Close.values, atr=tr.rolling(14).mean().values, lo20=d.Low.rolling(20).min().shift(1).values, hi10=d.High.rolling(10).max().shift(1).values,
             sma200=d.Close.rolling(200).mean().values, vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    A["sig"] = (d.Close.values < A["lo20"]) & (A["vr"] >= 1.5) & ~np.isnan(A["atr"]) & (d.Volume.values > 0)
    return A


def run(A, v):
    c, atr, n = A["c"], A["atr"], len(A["c"]); out = []; pos = False; last = -1
    for t in range(n):
        if pos:
            held = t - et; lc = min(lc, c[t]); why = None
            if c[t] >= ep + 2.0 * ea: why = "STOP"
            elif v["kind"] == "base":
                if c[t] > A["hi10"][t]: why = "CHAN"
                elif held >= 30: why = "TIME"
            else:
                if c[t] > lc + v["k"] * atr[t]: why = "TRAIL"
                elif held >= 250: why = "TIME"
            if why or t == n - 1:
                out.append((A["idx"][et], A["idx"][t], -(c[t] / ep - 1) - COST, held, why or "OPEN", bool(c[et] < A["sma200"][et]) if not np.isnan(A["sma200"][et]) else False)); pos = False; last = t
            continue
        if A["sig"][t] and t != last:
            pos, ep, ea, et, lc = True, c[t], atr[t], t, c[t]
    return out


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
data = {s: prep(DD["stocks"][s]) for s in names}
print(f"{len(names)} stocks, shorts: breakdown (close < prior-20d low) on >= 1.5x volume\n")
res = {}
for k, v in VARIANTS.items():
    rows = []
    for s in names: rows += [(s,) + r for r in run(data[s], v)]
    T = pd.DataFrame(rows, columns=["sym", "entry", "exit", "ret", "held", "why", "below200"]); T = T[T.why != "OPEN"].copy()
    ent = NIFTY.reindex(T.entry, method="ffill").values; ext = NIFTY.reindex(T.exit, method="ffill").values
    T["nret"] = ext / ent - 1; T["hedged"] = T.ret + T.nret - HEDGE_COST
    nsm = NSMA.reindex(T.entry, method="ffill").values; T["nifty_below200"] = ent < nsm
    res[k] = T; res[k + " T"] = T[T.below200].copy()

print(f"{'variant':<10}{'n':>6}{'win%':>5}{'avg win':>8}{'avg loss':>9}{'exp%':>7}{'hedged%':>8}{'hold':>6}{'hdg bp/d':>9}{'H1 hdg%':>8}{'H2 hdg%':>8}{'pos yrs':>8}{'t(hdg)':>7}  {'verdict':<8} exits")
for k, T in res.items():
    if len(T) < 20:
        print(f"{k:<10}{len(T):>6}  too few"); continue
    h1, h2 = T[T.entry < SPLIT], T[T.entry >= SPLIT]; w, lo = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    yrs = T.groupby(T.entry.dt.year).hedged.mean(); pos_y = int((yrs > 0).sum())
    t = T.hedged.mean() / (T.hedged.std() / np.sqrt(len(T))); perday = T.hedged.sum() / T.held.sum() * 1e4
    ok = T.hedged.mean() > 0 and t > 2.6 and h1.hedged.mean() > 0 and h2.hedged.mean() > 0 and pos_y >= 10
    ex = ", ".join(f"{a} {b}" for a, b in T.why.value_counts().items())
    print(f"{k:<10}{len(T):>6}{(T.ret>0).mean()*100:>5.0f}{w.mean()*100:>+8.2f}{lo.mean()*100:>+9.2f}{T.ret.mean()*100:>+7.2f}{T.hedged.mean()*100:>+8.2f}{T.held.mean():>6.0f}{perday:>+9.1f}{h1.hedged.mean()*100:>+8.2f}{h2.hedged.mean()*100:>+8.2f}{pos_y:>5}/{len(yrs):<2}{t:>+7.1f}  {'PASS' if ok else 'fail':<8} {ex}")

print("\nRegime split (trades opened with NIFTY below / above its 200-day average), hedged expectancy % and unhedged expectancy %:")
for k in VARIANTS:
    T = res[k]
    a, b = T[T.nifty_below200], T[~T.nifty_below200]
    print(f"  {k:<8} NIFTY below 200-DMA: n={len(a):>4} hedged {a.hedged.mean()*100:+.2f}% raw {a.ret.mean()*100:+.2f}% win {(a.ret>0).mean()*100:.0f}%   |   above: n={len(b):>4} hedged {b.hedged.mean()*100:+.2f}% raw {b.ret.mean()*100:+.2f}% win {(b.ret>0).mean()*100:.0f}%")
print("\nBy entry year, S-TR4 hedged expectancy % (n):  " + "  ".join(f"{y}:{g.hedged.mean()*100:+.1f}({len(g)})" for y, g in res["S-TR4"].groupby(res["S-TR4"].entry.dt.year)))
