"""Could we have SHORTED the losers?  NIFTY 50 breakout longs (V1: close above the prior-20d high on >=1.5x volume), 3 years, 0.10% cost per round trip.
Five reversal rules fixed before the run (t0 = signal day, L = the breakout level = prior-20d high at t0):
  A  first close within 5 sessions back BELOW L                  -> short at that close (long is open)
  B  first close within 5 sessions >= 1 ATR below the t0 close   -> short
  C  stop-and-reverse: when the long exits by its 2-ATR stop     -> short at that close
  D  day 5, long still open and below the t0 close               -> short
  E  day 5, long still open and down more than 3%                -> short
Short leg exits (the live rules mirrored): stop 2 ATR above entry, close above the prior-10d high, 30 sessions.
Reported: the short leg alone, its NIFTY-hedged return, and the COMBINED effect per triggered signal: (long exited at the trigger + the short)
versus what the long actually did (live rules).  Halves split at the median signal date."""
import os, sys
os.environ.setdefault("STK_UNIV", "/opt/kronos_bot/research/nifty50_syms.json")
src = open("/opt/kronos_bot/research/stk_triggers.py").read().split("span_q = (cal[-1]")[0]
sys.argv = ["x", "5"]
exec(compile(src, "stk_triggers_head", "exec"))
import numpy as np
import pandas as pd

nf = nifty.copy(); nf.index = pd.to_datetime(nf.index).normalize()
nclose = lambda d: float(nf.asof(pd.Timestamp(d).normalize()))
HEDGE = 0.0003


def long_trades(s):
    A = S[s]; c, atr, n = A["c"], A["atr"], len(A["c"]); sg = A["sig"]["V1 Breakout+Vol"]; hi = HI20[s]; out, pos = [], 0
    for t in range(WARM, n):
        if pos:
            ex = None
            if (c[t] - ep) <= -2 * ea: ex = "STOP"
            elif c[t] < A["lo10"][t]: ex = "CHANNEL"
            elif t - t0 >= 30: ex = "TIME"
            if ex or t == n - 1:
                out.append(dict(t0=t0, t_exit=t, ret=(c[t] - ep) / ep - COST, reason=ex or "OPEN", L=lvl, atr0=ea)); pos = 0
            continue
        if sg[t] == 1:
            pos, ep, ea, t0, lvl = 1, c[t], atr[t], t, hi[t]
    return out


def short_leg(s, t1):
    A = S[s]; c, atr, n = A["c"], A["atr"], len(A["c"]); ep, ea = c[t1], atr[t1]
    for t in range(t1 + 1, n):
        ex = None
        if (c[t] - ep) >= 2 * ea: ex = "STOP"
        elif c[t] > A["hi10"][t]: ex = "CHANNEL"
        elif t - t1 >= 30: ex = "TIME"
        if ex or t == n - 1:
            return t, (ep - c[t]) / ep - COST, ex or "OPEN"
    return None


HI20 = {s: prices[s]["High"].shift(1).rolling(20).max().reindex(S[s]["dates"]).tolist() for s in names}
rows = []
for s in names:
    A = S[s]; c = A["c"]
    for tr in long_trades(s):
        t0, te = tr["t0"], tr["t_exit"]; base = dict(stock=s, t0=t0, date=A["dates"][t0], base=tr["ret"], reason=tr["reason"])
        trig = {}
        for t1 in range(t0 + 1, min(t0 + 5, te) + 1):
            if "A" not in trig and c[t1] < tr["L"]: trig["A"] = t1
            if "B" not in trig and c[t1] <= c[t0] - 1.0 * tr["atr0"]: trig["B"] = t1
        if tr["reason"] == "STOP": trig["C"] = te
        if te > t0 + 5 and c[t0 + 5] < c[t0]: trig["D"] = t0 + 5
        if te > t0 + 5 and c[t0 + 5] <= c[t0] * 0.97: trig["E"] = t0 + 5
        for v, t1 in trig.items():
            sl = short_leg(s, t1)
            if sl is None: continue
            t_end, sret, sreason = sl
            long_part = (c[t1] - c[t0]) / c[t0] - COST
            nret = nclose(A["dates"][t_end]) / nclose(A["dates"][t1]) - 1
            rows.append(dict(base, v=v, t1=t1, short=sret, sreason=sreason, hedged=sret + nret - HEDGE, combined=long_part + sret, held=t_end - t1))
R = pd.DataFrame(rows)
nsig = len({(r.stock, r.t0) for r in R.itertuples()}) if False else sum(len(long_trades(s)) for s in names)
mid = pd.Series(sorted(R.date.unique())).iloc[len(R.date.unique()) // 2]
print(f"NIFTY 50 long breakout signals traded under the live rules: {nsig}; halves split at {pd.Timestamp(mid).date()}\n")
print(f"  {'rule':<46}{'trig':>5}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'short exp%':>11}{'PF':>6}{'H1':>7}{'H2':>7}{'hedged%':>9} | {'long did':>9}{'long->short':>12}{'gain/signal':>12}")
names_v = {"A": "A closes back below the breakout level", "B": "B falls >= 1 ATR below the signal close", "C": "C stop-and-reverse at the long's stop",
           "D": "D day 5, red -> short", "E": "E day 5, down > 3% -> short"}
for v, lab in names_v.items():
    g = R[R.v == v]
    if len(g) < 10:
        print(f"  {lab:<46}{len(g):>5}"); continue
    w, l = g.short[g.short > 0], g.short[g.short <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) else float("inf")
    h = lambda x: f"{x.short.mean()*100:+.2f}" if len(x) else " n/a"
    print(f"  {lab:<46}{len(g):>5}{(g.short>0).mean()*100:>6.0f}{w.mean()*100:>+9.2f}{l.mean()*100:>+10.2f}{g.short.mean()*100:>+11.2f}{pf:>6.2f}{h(g[g.date < mid]):>7}{h(g[g.date >= mid]):>7}{g.hedged.mean()*100:>+9.2f}"
          f" | {g.base.mean()*100:>+8.2f}%{g.combined.mean()*100:>+11.2f}%{(g.combined.mean() - g.base.mean()) * len(g) / nsig * 100:>+11.3f}%")
print("\n  'long did' = mean final return of the triggered longs under the live rules; 'long->short' = long exited at the trigger plus the short leg;"
      "\n  'gain/signal' = the improvement averaged over ALL long signals (live expectancy is +1.06% per signal).")
g = R[R.v == "A"]
print("\n  short-leg exits for rule A: " + ", ".join(f"{k} {v}" for k, v in g.sreason.value_counts().items()) + f"; average hold {g.held.mean():.1f} sessions")
