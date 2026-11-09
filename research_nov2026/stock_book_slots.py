"""Does a 3-slot capital book keep the signal book's expectancy?  Slot membership does not change a trade's path, so
replay the live engine once (uncapped signal book), then assign slots offline under different priority rules.
Rules: volume-ranked (what stock_book.py does), first-listed (alphabetical), and 300 random priorities."""
import sys, json, pickle, logging
sys.path.insert(0, "/opt/kronos_bot/research/stage_stock")
import numpy as np
import pandas as pd
import stock_book as sb

logging.disable(logging.CRITICAL)
prices, lots, nifty = pickle.load(open("/opt/kronos_bot/research/stk_prices.pkl", "rb"))
names = json.load(open("/opt/kronos_bot/research/nifty50_syms.json"))
uni = [{"symbol": s, "key": s, "lot": lots[s]} for s in names]
full, idx = {}, {}
for s in names:
    d = prices[s].copy(); d.index.name = "datetime"; d = d.reset_index(); d.columns = [c.lower() for c in d.columns]
    full[s] = d; idx[s] = {pd.Timestamp(t): i for i, t in enumerate(d["datetime"])}
cal = sorted(set().union(*[set(idx[s]) for s in names]))
st = {"positions": {}, "stats": {"signal": sb._empty_stats(), "capital": sb._empty_stats()}}
ents, exits = {}, []
for D in cal:
    data = {}
    for s in names:
        j = idx[s].get(D)
        if j is None or j < 200:
            continue
        data[s] = {"df": full[s].iloc[:j], "px": float(full[s]["close"].iloc[j]), "vol": float(full[s]["volume"].iloc[j]),
                   "high": float(full[s]["high"].iloc[j]), "low": float(full[s]["low"].iloc[j])}
    for e in sb.run_day(st, uni, data, str(D.date()), vol_adj=1.0, write_logs=False):
        if e["event"] == "ENTRY":
            ents[(e["symbol"], e["entry_date"])] = e
        else:
            exits.append(e)
T = pd.DataFrame([{"symbol": e["symbol"], "entry": pd.Timestamp(e["entry_date"]), "exit": pd.Timestamp(e["exit_date"]),
                   "ret": e["ret_pct"], "volx": e["volx"], "reason": e["exit_reason"]} for e in exits])
print(f"signal-book trades (closed): {len(T)}, mean {T.ret.mean():+.2f}%/trade, win {(T.ret>0).mean()*100:.0f}%")


def assign(T, prio):
    """prio: array aligned with T; higher = served first among same-day entries. Returns boolean slot mask."""
    order = T.sort_values("entry").index
    open_exit = []                      # exit dates of slot positions
    got = pd.Series(False, index=T.index)
    for d, g in T.loc[order].groupby("entry", sort=True):
        open_exit = [x for x in open_exit if x > d]
        free = sb.MAX_SLOTS - len(open_exit)
        if free <= 0:
            continue
        for i in g.assign(p=prio[g.index]).sort_values("p", ascending=False).index[:free]:
            got[i] = True; open_exit.append(T.loc[i, "exit"])
    return got


def report(label, mask):
    c = T[mask]
    return f"  {label:<34} n={len(c):>4} ({len(c)/len(T)*100:>3.0f}% of signals)  win {(c.ret>0).mean()*100:>3.0f}%  {c.ret.mean():>+6.2f}%/trade  sum {c.ret.sum():>+7.1f}%"


print(report("volume-ranked (current code)", assign(T, T.volx.values.astype(float))))
print(report("alphabetical priority", assign(T, -pd.Series(T.symbol.rank(method="first").values, index=T.index).values)))
rng = np.random.default_rng(4)
means, sums = [], []
for _ in range(300):
    m = assign(T, rng.random(len(T)))
    means.append(T[m].ret.mean()); sums.append(T[m].ret.sum())
means, sums = np.array(means), np.array(sums)
print(f"  random priority x300: mean {means.mean():+.2f}%/trade (5th-95th pct {np.percentile(means,5):+.2f} .. {np.percentile(means,95):+.2f}); "
      f"sum of returns median {np.median(sums):+.1f}% (5th-95th {np.percentile(sums,5):+.1f} .. {np.percentile(sums,95):+.1f})")
print(f"  signal book for reference: {T.ret.mean():+.2f}%/trade, sum {T.ret.sum():+.1f}%")
v = T.volx.astype(float)
for lo, hi in ((1.5, 2.0), (2.0, 3.0), (3.0, 99)):
    g = T[(v >= lo) & (v < hi)]
    print(f"  signal book, volx {lo}-{hi if hi < 99 else 'inf'}: n={len(g):>3}  {g.ret.mean():>+6.2f}%/trade  win {(g.ret>0).mean()*100:.0f}%")
