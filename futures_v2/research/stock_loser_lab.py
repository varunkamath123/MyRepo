"""What distinguishes the breakouts that fail from the ones that run?  And where do the live book's worst performers sit?
PART 1  14.9 years x NIFTY 50 names (today's constituents), the live signal (close > prior-20d high on >= 1.5x volume) and the live LONG exit (2 ATR stop / 4-ATR trail, no cap), cost 0.10%.
        Features measured AT ENTRY (fixed before the run, 8 features x 4 buckets = a descriptive scan, not a hunt: judge only effects that point the same way in BOTH halves, split 2019-07-01):
          volx (volume vs 20-day average) | extension (close above the prior 20d high, in ATR) | signal-day return | close location in the day's range | prior 20-day run-up (before today)
          | new 252-day high? | stock volatility (ATR % of price) | NIFTY's return the same day
        Outcome per bucket: n, win%, mean net return, stop-out % (hit the 2 ATR stop), mean return in each half.
        Plus the H12 cell: trades down more than 3% after 5 sessions.
PART 2  the live book's open longs with the same features at entry, sorted worst to best (positions opened today have no completed bar yet and are skipped)."""
import pickle, sys
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
NIFTY = DD["idx"]["NIFTY"]["Close"]; NIFTY = NIFTY[~NIFTY.index.duplicated()]; NRET = NIFTY.pct_change()
COST = 0.001; SPLIT = pd.Timestamp("2019-07-01")


def feats(c, h, l, atr, hi20, hi252, vr, t, idx_t):
    return dict(volx=vr[t], ext=(c[t] - hi20[t]) / atr[t], day=c[t] / c[t - 1] - 1, loc=((c[t] - l[t]) / (h[t] - l[t])) if h[t] > l[t] else np.nan,
                run20=(c[t - 1] / c[t - 21] - 1) if t >= 21 else np.nan, newhi=bool(c[t] >= hi252[t]) if not np.isnan(hi252[t]) else False, atrpct=atr[t] / c[t],
                nday=NRET.get(idx_t, np.nan))


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(); d = d[~d.index.duplicated()]
    pc = d.Close.shift(1); tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    A = dict(idx=d.index, c=d.Close.values, h=d.High.values, l=d.Low.values, atr=tr.rolling(14).mean().values, hi20=d.High.rolling(20).max().shift(1).values,
             hi252=d.High.rolling(252).max().shift(1).values, vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    A["sig"] = (d.Close.values > A["hi20"]) & (A["vr"] >= 1.5) & ~np.isnan(A["atr"]) & (d.Volume.values > 0)
    return A


def run(A):
    c, atr, n = A["c"], A["atr"], len(A["c"]); out = []; pos = False; last = -1
    for t in range(n):
        if pos:
            held = t - et; hc = max(hc, c[t])
            if held == 5: r5 = c[t] / ep - 1
            why = "STOP" if c[t] <= ep - 2 * ea else ("TRAIL" if c[t] < hc - 4 * atr[t] else None)
            if why or t == n - 1:
                f = dict(F); f.update(entry=A["idx"][et], ret=c[t] / ep - 1 - COST, held=held, why=why or "OPEN", r5=r5, alive5=held > 5)
                out.append(f); pos = False; last = t
            continue
        if A["sig"][t] and t != last and t >= 21:
            pos, ep, ea, et, hc, r5 = True, c[t], atr[t], t, c[t], np.nan
            F = feats(A["c"], A["h"], A["l"], atr, A["hi20"], A["hi252"], A["vr"], t, A["idx"][t])
    return out


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
rows = []
for s in names:
    A = prep(DD["stocks"][s]); rows += [dict(sym=s, **r) for r in run(A)]
T = pd.DataFrame(rows); T = T[T.why != "OPEN"].copy(); T["stop"] = T.why == "STOP"
print(f"PART 1  {len(T)} trades, mean {T.ret.mean()*100:+.2f}%, win {(T.ret>0).mean()*100:.0f}%, stopped out {T.stop.mean()*100:.0f}% (the stop is 2 ATR; the trail ends the rest)\n")
SPEC = [("volx", "volume vs 20d avg (x)", [1.5, 2, 3, 5, 1e9], ["1.5-2", "2-3", "3-5", "5+"]),
        ("ext", "extension above 20d high (ATR)", [-1e9, 0.5, 1, 2, 1e9], ["<0.5", "0.5-1", "1-2", "2+"]),
        ("day", "signal-day return", [-1e9, 0.01, 0.025, 0.05, 1e9], ["<1%", "1-2.5%", "2.5-5%", "5%+"]),
        ("loc", "close location in day range", [-1e9, 0.6, 0.85, 0.97, 1e9], ["<0.6", "0.6-0.85", "0.85-0.97", "0.97+"]),
        ("run20", "prior 20-day run-up", [-1e9, 0.0, 0.05, 0.10, 1e9], ["<0%", "0-5%", "5-10%", "10%+"]),
        ("atrpct", "stock volatility (ATR % price)", [0, 0.015, 0.025, 0.04, 1e9], ["<1.5%", "1.5-2.5%", "2.5-4%", "4%+"]),
        ("nday", "NIFTY same-day return", [-1e9, -0.005, 0.005, 1e9], ["down >0.5%", "flat", "up >0.5%"])]
for col, title, bins, labs in SPEC:
    T["b"] = pd.cut(T[col], bins=bins, labels=labs, right=False)
    print(f"  {title:<34}{'n':>6}{'win%':>6}{'mean%':>8}{'stop%':>7}{'H1 %':>7}{'H2 %':>7}")
    for b in labs:
        g = T[T.b == b]
        if len(g) < 20: continue
        h1, h2 = g[g.entry < SPLIT], g[g.entry >= SPLIT]
        print(f"    {b:<32}{len(g):>6}{(g.ret>0).mean()*100:>6.0f}{g.ret.mean()*100:>+8.2f}{g.stop.mean()*100:>7.0f}{h1.ret.mean()*100:>+7.2f}{h2.ret.mean()*100:>+7.2f}")
    print()
print(f"  {'new 252-day high at entry':<34}{'n':>6}{'win%':>6}{'mean%':>8}{'stop%':>7}{'H1 %':>7}{'H2 %':>7}")
for lab, g in (("yes", T[T.newhi]), ("no", T[~T.newhi])):
    h1, h2 = g[g.entry < SPLIT], g[g.entry >= SPLIT]
    print(f"    {lab:<32}{len(g):>6}{(g.ret>0).mean()*100:>6.0f}{g.ret.mean()*100:>+8.2f}{g.stop.mean()*100:>7.0f}{h1.ret.mean()*100:>+7.2f}{h2.ret.mean()*100:>+7.2f}")
a5 = T[T.alive5]
print("\n  H12 cell (trades still open after 5 sessions): by the return after 5 sessions")
for lab, m in (("down > 3%", a5.r5 < -0.03), ("-3% .. 0%", (a5.r5 >= -0.03) & (a5.r5 < 0)), ("0% .. +3%", (a5.r5 >= 0) & (a5.r5 < 0.03)), ("up > 3%", a5.r5 >= 0.03)):
    g = a5[m]; print(f"    {lab:<32}{len(g):>6}{(g.ret>0).mean()*100:>6.0f}{g.ret.mean()*100:>+8.2f}{g.stop.mean()*100:>7.0f}   day-5 level {g.r5.mean()*100:+.2f}% -> final {g.ret.mean()*100:+.2f}%  (what happened AFTER day 5: {(g.ret - g.r5).mean()*100:+.2f}%)")

# ---------------- PART 2: live book
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import stock_book as sb
try:                                                              # NIFTY returns for the live entry days (the 14-year file ends Nov 11)
    from brokers.upstox_data import load_daily_ohlcv
    nd = load_daily_ohlcv("NIFTY", bars=60); nd = nd[nd["datetime"].dt.dayofweek < 5]
    ls = pd.Series(nd["close"].values, index=nd["datetime"].dt.normalize()).pct_change()
    NRET = pd.concat([NRET, ls[~ls.index.isin(NRET.index)]])
except Exception as e:
    print("  (live NIFTY returns unavailable:", e, ")")
st = sb._load_state(); uni = {u["symbol"]: u for u in sb._load_universe()}
q = sb.load_quotes([uni[s]["key"] for s in st["positions"]])
L = []
for sym, p in st["positions"].items():
    px = q[uni[sym]["key"]]["px"]; df = sb.load_daily(uni[sym]["key"], bars=330)
    m = df["datetime"].dt.normalize() == pd.Timestamp(p["entry_date"])
    if not m.any(): continue
    i = int(np.where(m.values)[0][0]); c, h, l = df["close"].values, df["high"].values, df["low"].values
    pc = np.r_[np.nan, c[:-1]]; tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc))); atr = pd.Series(tr).rolling(14).mean().values
    hi20 = pd.Series(h).rolling(20).max().shift(1).values; hi252 = pd.Series(h).rolling(252).max().shift(1).values
    vr = (pd.Series(df["volume"].values.astype(float)) / pd.Series(df["volume"].values.astype(float)).rolling(20).mean().shift(1)).values
    f = feats(c, h, l, atr, hi20, hi252, vr, i, df["datetime"].iloc[i].normalize())
    L.append(dict(sym=sym, entry=p["entry_date"], ret=(px / p["entry_price"] - 1) * 100, **f))
L = pd.DataFrame(L).sort_values("ret")
print(f"\nPART 2  the live book's longs, worst first, with the entry features (compare with the Part 1 tables):")
print(f"  {'symbol':<11}{'entered':<11}{'ret now%':>9}{'volx':>6}{'ext ATR':>8}{'day ret%':>9}{'loc':>6}{'run20%':>8}{'new 252d hi':>12}{'ATR%':>6}{'NIFTY day%':>11}")
for _, r in L.iterrows():
    print(f"  {r['sym']:<11}{r['entry']:<11}{r['ret']:>+9.2f}{r['volx']:>6.2f}{r['ext']:>8.2f}{r['day']*100:>+9.2f}{r['loc']:>6.2f}{r['run20']*100:>+8.1f}{('yes' if r['newhi'] else 'no'):>12}{r['atrpct']*100:>6.1f}{r['nday']*100:>+11.2f}")
