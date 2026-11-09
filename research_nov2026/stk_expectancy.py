"""Edge = net expectancy above break-even: avg win x win% > avg loss x loss%, after costs.
Re-cuts the stk_triggers.py trades (same signals/exits/costs) as payoff statistics, by side, by year, and in Rs per lot.
Reuses stk_triggers.py's simulation by exec'ing everything above its reporting section."""
import os, sys
src = open("/opt/kronos_bot/research/stk_triggers.py").read().split("span_q = (cal[-1]")[0]
sys.argv = ["x", "5"]
exec(compile(src, "stk_triggers_head", "exec"))

import numpy as np
import pandas as pd


def payoff(T):
    r = T["ret"].values
    if len(r) == 0:
        return None
    w, l = r[r > 0], r[r <= 0]
    aw, al = (w.mean() if len(w) else 0.0), (l.mean() if len(l) else 0.0)
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pr = aw / abs(al) if al != 0 else float("inf")
    be = 1 / (1 + pr) if pr != float("inf") else 0.0
    return dict(n=len(r), win=(r > 0).mean(), aw=aw, al=al, pr=pr, be=be, ex=r.mean(), pf=pf)


def line(label, T):
    p = payoff(T)
    if not p:
        print(f"  {label:<26}{0:>6}"); return
    print(f"  {label:<26}{p['n']:>6}{p['win']*100:>6.0f}%{p['aw']*100:>+8.2f}{p['al']*100:>+8.2f}{p['pr']:>7.2f}{p['be']*100:>7.0f}%{p['ex']*100:>+9.2f}{p['pf']:>6.2f}")


name = os.environ.get("STK_UNIV", "top60").split("/")[-1]
print(f"\n##### universe: {len(names)} stocks ({name}) | {cal[WARM].date()} -> {cal[-1].date()} | cost {COST*100:.2f}% round trip, already deducted")
hdr = f"  {'':<26}{'n':>6}{'win%':>7}{'avg win%':>8}{'avg loss%':>9}{'payoff':>7}{'BE win':>8}{'expect%':>9}{'PF':>6}"
tick = [(v, T) for v, T in ((v, run(v)) for v in S[names[0]]["sig"])]
for v, T in tick:
    print(f"\n[{v}]  payoff = avg win / avg loss;  BE win = win rate needed to break even at that payoff")
    print(hdr)
    line("all trades", T)
    line("  long", T[T.dir == 1])
    line("  short", T[T.dir == -1])

print("\n--- long-only by entry year (expectancy per trade, net; n) ---")
for v, T in tick:
    L = T[T.dir == 1].copy(); L["yr"] = L["entry"].dt.year
    if L.empty: continue
    cells = "  ".join(f"{y}: {g.ret.mean()*100:+.2f}% (n={len(g)})" for y, g in L.groupby("yr"))
    print(f"  {v:<17}{cells}")

print("\n--- Rs per lot for the long-only V1 and V2 trades (lot notional at today's price, cost included) ---")
for v in ("V1 Breakout+Vol", "V2 Breakout"):
    T = dict(tick)[v]; L = T[T.dir == 1].copy()
    L["notional"] = L["stock"].map(lambda s: lots[s] * float(prices[s]["Close"].iloc[-1]))
    L["rs"] = L["ret"] * L["notional"]
    p = payoff(L)
    print(f"  {v:<17} n={len(L)}  expectancy Rs{L.rs.mean():+,.0f}/trade (median trade Rs{L.rs.median():+,.0f}); avg win Rs{L.rs[L.rs>0].mean():+,.0f}, "
          f"avg loss Rs{L.rs[L.rs<=0].mean():+,.0f}; median lot notional Rs{L.notional.median()/1e5:.1f}L; worst trade Rs{L.rs.min():+,.0f}")

print("\n--- tail dependence: long-only V1 expectancy after removing the best k trades ---")
L = dict(tick)["V1 Breakout+Vol"]; L = L[L.dir == 1].sort_values("ret", ascending=False)
for k in (0, 5, 10, 20, 40):
    x = L.iloc[k:]
    print(f"  without best {k:>2}: n={len(x)} expectancy {x.ret.mean()*100:+.2f}%/trade")
