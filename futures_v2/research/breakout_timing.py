"""Breakout TIMING lab.  NIFTY 50 stocks, 2013-2026 (idx_data_long.pkl), signals = the live ones: close above the prior-20d high (long) / below the prior-20d low (breakdown)
on >= 1.5x the prior-20d volume.  Every signal is simulated on its own (overlaps allowed) so variants compare cleanly; 0.10% round-trip cost; stop 2 ATR, channel exit
(10-day low / high), 30 sessions.  Halves: signals before 2020-01-01 vs from it -- an effect must hold in BOTH.  t-stats are clustered by ISO week of the signal.

PART 1  event-time profile: mean / median return by day after the signal (raw and net of NIFTY), overnight vs intraday split of day 1, and what happens AFTER a breakout fails.
PART 2  long entry timing (pre-registered, 4 variants + baseline):
        T0 close of the signal day (live)            T1 next day's open
        T2 close of day 1, only if it is above the signal close (confirmation)
        T3 retest: first close within days 1-5 inside [L, L + 0.5 ATR] (L = breakout level), else no trade
        T4 skip stretched breakouts: take the signal only if close - L <= 1.5 ATR
PART 3  short timing for breakdowns (3 variants + baseline):
        S0 close of the signal day                   S1 close of day 1, only if it is below the signal close (continuation)
        S2 signal day, only if NIFTY is below its own 50-day average (the market is weak too)"""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
COST, HEDGE, SPLIT = 0.0010, 0.0003, pd.Timestamp("2020-01-01")
nf = D["idx"]["NIFTY"]["Close"]; nf = nf[nf.index < TODAY]
nf50 = nf.rolling(50).mean()

ST = {}
for s in D["n50"]:
    d = D["stocks"][s]; d = d[d.index < TODAY]
    o, h, l, c, v = (d[k].values.astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    cs, hs, ls, vs = pd.Series(c), pd.Series(h), pd.Series(l), pd.Series(v)
    tr = pd.concat([hs - ls, (hs - cs.shift()).abs(), (ls - cs.shift()).abs()], axis=1).max(axis=1)
    ST[s] = dict(dates=d.index, o=o, h=h, l=l, c=c, atr=tr.ewm(alpha=1 / 14, adjust=False).mean().values,
                 hi20=hs.shift(1).rolling(20).max().values, lo20=ls.shift(1).rolling(20).min().values,
                 hi10=hs.shift(1).rolling(10).max().values, lo10=ls.shift(1).rolling(10).min().values,
                 volx=(vs / vs.shift(1).rolling(20).mean()).values,
                 n=nf.reindex(d.index, method="ffill").values, n50=nf50.reindex(d.index, method="ffill").values)


def signals(side):
    out = []
    for s, A in ST.items():
        for t in range(60, len(A["c"]) - 40):
            if A["volx"][t] >= 1.5 and ((side == 1 and A["c"][t] > A["hi20"][t]) or (side == -1 and A["c"][t] < A["lo20"][t])):
                out.append((s, t))
    return out


def trade(s, e, p, side, at_open, ea):
    A = ST[s]; c, n = A["c"], len(A["c"])
    for t in range(e if at_open else e + 1, n):
        ex = None
        if side * (c[t] - p) <= -2 * ea: ex = "STOP"
        elif side == 1 and c[t] < A["lo10"][t]: ex = "CHANNEL"
        elif side == -1 and c[t] > A["hi10"][t]: ex = "CHANNEL"
        elif t - e >= 30: ex = "TIME"
        if ex or t == n - 1:
            nret = A["n"][t] / A["n"][e] - 1
            r = side * (c[t] - p) / p - COST
            return dict(date=A["dates"][e], ret=r, hedged=r - side * nret - HEDGE, held=t - e, reason=ex or "OPEN")


def wk_t(T, col="ret"):
    r = T[col].values; mu = r.mean(); wk = pd.to_datetime(T["date"]).dt.isocalendar()
    key = (wk["year"].astype(str) + "-" + wk["week"].astype(str)).values
    g = pd.Series(r - mu).groupby(key).sum(); return mu / (np.sqrt((g ** 2).sum()) / len(r))


def table(title, rows, nsig):
    print(f"\n  {title}")
    print(f"  {'variant':<52}{'n':>6}{'fill%':>6}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'exp%':>8}{'PF':>6}{'H1':>7}{'H2':>7}{'hedged':>8}{'t(wk)':>7}{'per signal%':>12}")
    for lab, T in rows:
        T = pd.DataFrame(T)
        if len(T) < 20:
            print(f"  {lab:<52}{len(T):>6}"); continue
        w, l = T.ret[T.ret > 0], T.ret[T.ret <= 0]; pf = w.sum() / abs(l.sum())
        h = lambda x: f"{x.ret.mean()*100:+.2f}" if len(x) else " n/a"
        print(f"  {lab:<52}{len(T):>6}{len(T)/nsig*100:>6.0f}{(T.ret>0).mean()*100:>6.0f}{w.mean()*100:>+9.2f}{l.mean()*100:>+10.2f}{T.ret.mean()*100:>+8.2f}{pf:>6.2f}"
              f"{h(T[T.date < SPLIT]):>7}{h(T[T.date >= SPLIT]):>7}{T.hedged.mean()*100:>+8.2f}{wk_t(T):>+7.2f}{T.ret.sum()/nsig*100:>+12.3f}")


LONG, SHORT = signals(1), signals(-1)
print(f"signals 2013-2026: {len(LONG)} breakouts, {len(SHORT)} breakdowns in {len(ST)} stocks; halves split {SPLIT.date()}")

# ---------------- PART 1: event-time profile ----------------
print("\nPART 1: return by day after the signal (close of day 0 = signal day), mean / median / % positive, raw and net of NIFTY; t clustered by week")


def profile(sigs, side, lab):
    rows = []
    for s, t in sigs:
        A = ST[s]; c = A["c"]
        r = {k: c[t + k] / c[t] - 1 for k in (1, 2, 3, 5, 10, 20)}
        a = {k: r[k] - (A["n"][t + k] / A["n"][t] - 1) for k in r}
        rows.append(dict(date=A["dates"][t], on=A["o"][t + 1] / c[t] - 1, intra=c[t + 1] / A["o"][t + 1] - 1, **{f"r{k}": r[k] for k in r}, **{f"a{k}": a[k] for k in a}))
    P = pd.DataFrame(rows)
    print(f"\n  {lab} (n={len(P)}; sign shown is the STOCK's move, so a short profits when it is negative)")
    print(f"  {'day':<8}" + "".join(f"{k:>10}" for k in ("1", "2", "3", "5", "10", "20")))
    for nm, pre in (("mean%", "r"), ("median%", "r"), ("net NIFTY", "a")):
        vals = []
        for k in (1, 2, 3, 5, 10, 20):
            x = P[f"{pre}{k}"]; vals.append(x.median() if nm == "median%" else x.mean())
        print(f"  {nm:<8}" + "".join(f"{v*100:>+10.2f}" for v in vals))
    print(f"  {'% up':<8}" + "".join(f"{(P[f'r{k}']>0).mean()*100:>10.0f}" for k in (1, 2, 3, 5, 10, 20)))
    print(f"  {'H1 mean':<8}" + "".join(f"{P[P.date<SPLIT][f'r{k}'].mean()*100:>+10.2f}" for k in (1, 2, 3, 5, 10, 20)) + "   (2013-2019)")
    print(f"  {'H2 mean':<8}" + "".join(f"{P[P.date>=SPLIT][f'r{k}'].mean()*100:>+10.2f}" for k in (1, 2, 3, 5, 10, 20)) + "   (2020-2026)")
    print(f"  day-1 split: overnight (close -> next open) {P.on.mean()*100:+.3f}%, intraday (next open -> close) {P.intra.mean()*100:+.3f}%")


profile(LONG, 1, "BREAKOUTS (long signals)"); profile(SHORT, -1, "BREAKDOWNS (short signals)")
print("\n  after a long breakout FAILS (day-3 close back below the breakout level L): move from the day-3 close")
rows = []
for s, t in LONG:
    A = ST[s]; c = A["c"]
    if c[t + 3] < A["hi20"][t]:
        rows.append(dict(date=A["dates"][t], **{f"f{k}": c[t + 3 + k] / c[t + 3] - 1 for k in (1, 2, 5, 10)}, **{f"fa{k}": c[t + 3 + k] / c[t + 3] - 1 - (A["n"][t + 3 + k] / A["n"][t + 3] - 1) for k in (1, 2, 5, 10)}))
F = pd.DataFrame(rows)
print(f"  failed by day 3: n={len(F)} of {len(LONG)} ({len(F)/len(LONG)*100:.0f}%)")
print("  day after failure:   " + "".join(f"{k:>9}" for k in (1, 2, 5, 10)))
print("  mean stock move %:   " + "".join(f"{F[f'f{k}'].mean()*100:>+9.2f}" for k in (1, 2, 5, 10)) + "   (negative = continued down)")
print("  net of NIFTY %:      " + "".join(f"{F[f'fa{k}'].mean()*100:>+9.2f}" for k in (1, 2, 5, 10)))
print("  H1 / H2 at day 5:    " + f"{F[F.date<SPLIT].f5.mean()*100:+.2f} / {F[F.date>=SPLIT].f5.mean()*100:+.2f}")

# ---------------- PART 2: long entry timing ----------------
res = {k: [] for k in ("T0", "T1", "T2", "T3", "T4")}
for s, t in LONG:
    A = ST[s]; c, o, atr = A["c"], A["o"], A["atr"]; L = A["hi20"][t]
    res["T0"].append(trade(s, t, c[t], 1, False, atr[t]))
    res["T1"].append(trade(s, t + 1, o[t + 1], 1, True, atr[t]))
    if c[t + 1] > c[t]: res["T2"].append(trade(s, t + 1, c[t + 1], 1, False, atr[t + 1]))
    for k in range(1, 6):
        if L <= c[t + k] <= L + 0.5 * atr[t]:
            res["T3"].append(trade(s, t + k, c[t + k], 1, False, atr[t + k])); break
    if c[t] - L <= 1.5 * atr[t]: res["T4"].append(trade(s, t, c[t], 1, False, atr[t]))
print("\nPART 2: LONG entry timing")
table("breakout longs", [("T0 close of the signal day (live)", res["T0"]), ("T1 next day's open", res["T1"]), ("T2 day-1 close, only if above the signal close", res["T2"]),
                         ("T3 retest of the breakout level within 5 days", res["T3"]), ("T4 skip stretched breakouts (close - L > 1.5 ATR)", res["T4"])], len(LONG))

# ---------------- PART 3: short timing ----------------
res = {k: [] for k in ("S0", "S1", "S2")}
for s, t in SHORT:
    A = ST[s]; c, atr = A["c"], A["atr"]
    res["S0"].append(trade(s, t, c[t], -1, False, atr[t]))
    if c[t + 1] < c[t]: res["S1"].append(trade(s, t + 1, c[t + 1], -1, False, atr[t + 1]))
    if A["n"][t] < A["n50"][t]: res["S2"].append(trade(s, t, c[t], -1, False, atr[t]))
print("\nPART 3: SHORT entry timing (breakdowns)")
table("breakdown shorts", [("S0 close of the signal day", res["S0"]), ("S1 day-1 close, only if still lower (continuation)", res["S1"]),
                           ("S2 signal day, only if NIFTY < its 50-day average", res["S2"])], len(SHORT))
