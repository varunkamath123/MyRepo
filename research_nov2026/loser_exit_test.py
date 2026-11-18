"""Why not exit the losers?  Part 1: how close each open stock position is to its exits (live).  Part 2: would cutting losers EARLIER have paid
(NIFTY 50, long-only breakout + volume, 3 years, 0.10% cost)?  Variants fixed before the run (5 looks):
  base : stop 2 ATR, channel exit (10-day low), 30 sessions (the live rules)
  S1.5 : stop 1.5 ATR            S1.0 : stop 1.0 ATR
  R5   : base + exit on day 5 if the position is below its entry
  A3   : base + exit on day 3 if it is down more than 1 ATR
  R10  : base + exit on day 10 if the position is below its entry"""
import os, sys
sys.path.insert(0, "/opt/kronos_bot/repo")
os.environ.setdefault("STK_UNIV", "/opt/kronos_bot/research/nifty50_syms.json")
src = open("/opt/kronos_bot/research/stk_triggers.py").read().split("span_q = (cal[-1]")[0]
sys.argv = ["x", "5"]
exec(compile(src, "stk_triggers_head", "exec"))
import numpy as np
import pandas as pd
import stock_book as sb

print("\n=== part 1: live positions vs their exits ===")
uni = {u["symbol"]: u for u in sb._load_universe()}; st = sb._load_state()
q = sb.load_quotes([u["key"] for u in uni.values()])
print(f"  {'symbol':<10}{'entry':>10}{'now':>10}{'ret%':>7}{'stop':>10}{'to stop%':>9}{'chan exit':>10}{'to chan%':>9}{'days':>5}  next exit if...")
for sym, p in sorted(st["positions"].items(), key=lambda kv: (q[uni[kv[0]]["key"]]["px"] / kv[1]["entry_price"])):
    px = q[uni[sym]["key"]]["px"]; df = sb.load_daily(uni[sym]["key"], bars=60)
    chan = float(df["low"].tail(sb.CHANNEL_OUT).min()); stop = p["entry_price"] - sb.STOP_ATR * p["atr"]
    held = int((df["datetime"].dt.normalize() > pd.Timestamp(p["entry_date"])).sum()) + 1
    print(f"  {sym:<10}{p['entry_price']:>10,.1f}{px:>10,.1f}{(px / p['entry_price'] - 1) * 100:>+7.2f}{stop:>10,.1f}{(px / stop - 1) * 100:>+9.1f}{chan:>10,.1f}{(px / chan - 1) * 100:>+9.1f}{held:>5}  "
          f"{'price < ' + format(max(stop, chan), ',.1f')} ({'stop' if stop >= chan else 'channel'})")


def sim(s, sig, stop_atr=2.0, cut=None):
    A = S[s]; c, atr, n = A["c"], A["atr"], len(A["c"]); pos, out = 0, []
    for t in range(WARM, n):
        if pos:
            held = t - et; ex = None
            if (c[t] - ep) <= -stop_atr * ea: ex = "STOP"
            elif c[t] < A["lo10"][t]: ex = "CHANNEL"
            elif cut and held == cut[0] and ((cut[1] == "red" and c[t] < ep) or (cut[1] == "atr" and (c[t] - ep) <= -cut[2] * ea)): ex = "CUT"
            elif held >= 30: ex = "TIME"
            if ex or t == n - 1:
                out.append((A["dates"][et], (c[t] - ep) / ep - COST, ex or "OPEN", held)); pos = 0
            continue
        if sig[t] == 1:
            pos, ep, ea, et = 1, c[t], atr[t], t
    return out


def run_variant(**kw):
    rows = []
    for s in names:
        sg = np.where(S[s]["sig"]["V1 Breakout+Vol"] == 1, 1, 0).tolist()
        rows += sim(s, sg, **kw)
    return pd.DataFrame(rows, columns=["entry", "ret", "reason", "held"])


def stats(T, label):
    w, l = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    mid = T.entry.sort_values().iloc[len(T) // 2]
    pf = w.sum() / abs(l.sum())
    h = lambda x: f"{x.ret.mean()*100:+.2f}"
    print(f"  {label:<44}{len(T):>5}{(T.ret>0).mean()*100:>6.0f}{w.mean()*100:>+9.2f}{l.mean()*100:>+10.2f}{w.mean()/abs(l.mean()):>7.2f}{T.ret.mean()*100:>+9.2f}{pf:>6.2f}"
          f"{h(T[T.entry < mid]):>8}{h(T[T.entry >= mid]):>8}{T.held.mean():>7.1f}  {dict(T.reason.value_counts())}")


print("\n=== part 2: would cutting losers earlier have paid? (NIFTY 50, long-only breakout+volume, 3y, net of 0.10%) ===")
print(f"  {'variant':<44}{'n':>5}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'payoff':>7}{'exp%':>9}{'PF':>6}{'H1':>8}{'H2':>8}{'hold d':>7}  exits")
for label, kw in (("base: stop 2 ATR, channel, 30d (LIVE)", {}), ("S1.5: stop 1.5 ATR", {"stop_atr": 1.5}), ("S1.0: stop 1.0 ATR", {"stop_atr": 1.0}),
                  ("R5: also exit on day 5 if below entry", {"cut": (5, "red", 0)}), ("A3: also exit on day 3 if down > 1 ATR", {"cut": (3, "atr", 1.0)}),
                  ("R10: also exit on day 10 if below entry", {"cut": (10, "red", 0)})):
    stats(run_variant(**kw), label)
