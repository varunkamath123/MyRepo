"""What happens to breakout longs that are red on day 5 (MARUTI, HDFCLIFE, INDIGO, M&M are at day 4-5 now)?  Live rules, NIFTY 50, 3y, net of 0.10%."""
import os, sys
os.environ.setdefault("STK_UNIV", "/opt/kronos_bot/research/nifty50_syms.json")
src = open("/opt/kronos_bot/research/stk_triggers.py").read().split("span_q = (cal[-1]")[0]
sys.argv = ["x", "5"]
exec(compile(src, "stk_triggers_head", "exec"))
import numpy as np, pandas as pd

rows = []
for s in names:
    A = S[s]; c, atr, n = A["c"], A["atr"], len(A["c"]); sg = np.where(A["sig"]["V1 Breakout+Vol"] == 1, 1, 0); pos = 0
    for t in range(WARM, n):
        if pos:
            held = t - et; ex = None
            if held == 5: r5 = (c[t] - ep) / ep
            if (c[t] - ep) <= -2 * ea: ex = "STOP"
            elif c[t] < A["lo10"][t]: ex = "CHANNEL"
            elif held >= 30: ex = "TIME"
            if ex or t == n - 1:
                rows.append((r5 if held >= 5 else np.nan, (c[t] - ep) / ep - COST, ex or "OPEN", held)); pos = 0
            continue
        if sg[t] == 1:
            pos, ep, ea, et, r5 = 1, c[t], atr[t], t, np.nan
T = pd.DataFrame(rows, columns=["r5", "final", "reason", "held"]).dropna(subset=["r5"])
print(f"breakout longs still open on day 5: {len(T)} of {len(rows)}")
print(f"  {'state on day 5':<24}{'n':>5}{'final win%':>11}{'mean final%':>12}{'median final%':>14}{'stopped out%':>13}{'avg hold d':>11}")
for lab, m in (("down more than 3%", T.r5 < -0.03), ("down 1% to 3%", (T.r5 >= -0.03) & (T.r5 < -0.01)), ("down 0% to 1%", (T.r5 >= -0.01) & (T.r5 < 0)), ("up (>= 0)", T.r5 >= 0), ("ALL still open", T.r5 == T.r5)):
    g = T[m]
    print(f"  {lab:<24}{len(g):>5}{(g.final > 0).mean()*100:>11.0f}{g.final.mean()*100:>+12.2f}{g.final.median()*100:>+14.2f}{(g.reason == 'STOP').mean()*100:>13.0f}{g.held.mean():>11.1f}")
