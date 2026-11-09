"""Scenario tests + historical replay equivalence for stock_book.py (run on EC2 against staged code)."""
import sys, os, json, pickle, io, contextlib, logging
sys.path.insert(0, "/opt/kronos_bot/repo")
sys.path.insert(0, "/opt/kronos_bot/research/stage_stock")
from pathlib import Path
import numpy as np
import pandas as pd
import stock_book as sb
assert sb.__file__.startswith("/opt/kronos_bot/research/stage_stock"), sb.__file__
logging.disable(logging.CRITICAL)

T = Path("/tmp/stock_book_test"); T.mkdir(exist_ok=True)
sb.STATE_FILE, sb.TRADES_LOG, sb.SIGNALS_LOG = T / "state.json", T / "trades.jsonl", T / "signals.jsonl"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def fresh():
    for f in T.iterdir():
        f.unlink()
    return sb._load_state()


def flat_df(n=60, price=100.0, hi=101.0, lo=99.0, vol=1000.0, end="2026-11-06"):
    dates = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"datetime": dates, "open": price, "high": hi, "low": lo, "close": price, "volume": vol})


UNI = [{"symbol": s, "key": f"k{s}", "lot": 100} for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
TODAY = "2026-11-09"
DF = flat_df()

print("S0: rule functions on a flat tape (hi20=101, avg vol 1000, ATR=2)")
ev = sb.evaluate_entry(DF, 102.0, 1600.0, vol_adj=1.0)
check("breakout + 1.6x volume -> signal", ev["signal"] and abs(ev["hi_in"] - 101.0) < 1e-9 and abs(ev["atr"] - 2.0) < 1e-6, f"({ev})")
check("1.4x volume -> no signal", not sb.evaluate_entry(DF, 102.0, 1400.0, vol_adj=1.0)["signal"])
check("price == 20d high -> no signal (must exceed)", not sb.evaluate_entry(DF, 101.0, 1600.0, vol_adj=1.0)["signal"])
check("exactly 1.5x -> signal (>=)", sb.evaluate_entry(DF, 102.0, 1500.0, vol_adj=1.0)["signal"])
check("live volume adj applies (1.45x raw * 1.03 = 1.49 -> no; 1.47x -> yes)",
      not sb.evaluate_entry(DF, 102.0, 1450.0, vol_adj=1.03)["signal"] and sb.evaluate_entry(DF, 102.0, 1470.0, vol_adj=1.03)["signal"])

print("S1: entry, slot ranking, caps")
st = fresh()
data = {s: {"df": DF, "px": 102.0, "vol": v} for s, v in zip(("AAA", "BBB", "CCC", "DDD", "EEE"), (1600, 2400, 1800, 3000, 1550))}
ev_ = sb.run_day(st, UNI, data, TODAY, vol_adj=1.0)
slots = {s: p["slot"] for s, p in st["positions"].items()}
check("5 signals -> 5 signal-book positions", len(st["positions"]) == 5)
check("capital slots go to the 3 strongest volumes (DDD, BBB, CCC)", {s for s, v in slots.items() if v} == {"DDD", "BBB", "CCC"}, f"({slots})")
check("ENTRY records written with stop = entry - 2 ATR", all(abs(e["stop"] - 98.0) < 1e-6 for e in ev_ if e["event"] == "ENTRY"))
sigs = [json.loads(l) for l in sb.SIGNALS_LOG.read_text().splitlines()]
check("one signal-log row per stock evaluated", len(sigs) == 5 and all(r["signal"] for r in sigs))

print("S2: stop, channel, time exits and P&L / cost")
for label, px, want in (("stop", 97.9, "STOP_LOSS"), ("channel (above the stop, below the 10d low)", 98.9, "CHANNEL_EXIT"), ("hold", 100.0, None)):
    st = fresh()
    st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7))
    ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": px, "vol": 1000.0}}, TODAY, vol_adj=1.0)
    ex = [e for e in ev_ if e["event"] == "EXIT"]
    check(f"{label}: {want}", (ex[0]["exit_reason"] == want) if want else (ex == [] and "AAA" in st["positions"]))
    if want == "STOP_LOSS":
        net = (97.9 - 102.0) * 100 - 0.001 * 102.0 * 100
        check("net P&L = gross - 0.10% of notional; both ledgers updated (slot position)",
              abs(ex[0]["pnl_inr"] - net) < 1e-6 and st["stats"]["signal"]["n"] == 1 and st["stats"]["capital"]["n"] == 1
              and abs(st["stats"]["signal"]["pnl_inr"] - net) < 1e-6, f"({ex[0]['pnl_inr']:+.1f})")
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, False, 1.7))
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
check("signal-only position exit leaves the capital ledger alone", st["stats"]["signal"]["n"] == 1 and st["stats"]["capital"]["n"] == 0)

d_hold = flat_df(n=70, end="2026-11-06")
entry_d = d_hold["datetime"].iloc[-29]                       # 28 complete bars after the entry date -> today is session 29
st = fresh(); st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", str(entry_d.date()), 102.0, 2.0, 100, True, 1.7))
check("session 29 -> still held", sb.check_exit(sb.StockPos(**st["positions"]["AAA"]), d_hold, 100.0) is None)
entry_d = d_hold["datetime"].iloc[-30]                       # 29 bars after -> today is session 30
p30 = sb.StockPos("AAA", str(entry_d.date()), 102.0, 2.0, 100, True, 1.7)
check("session 30 -> TIME_EXIT", sb.check_exit(p30, d_hold, 100.0) == "TIME_EXIT")

print("S3: no re-entry on the exit day; no double processing; entry-day positions are not exited")
st = fresh()
st["positions"]["AAA"] = sb.asdict(p30)
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": d_hold, "px": 103.0, "vol": 5000.0}}, TODAY, vol_adj=1.0)
check("time exit AND breakout on the same day -> exit only, no re-entry",
      [e["event"] for e in ev_] == ["EXIT"] and "AAA" not in st["positions"])
st = fresh()
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 90.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
check("second run the same day: no exit of today's entry, no duplicate entry", ev_ == [] and len(st["positions"]) == 1)

print("S4: slots free up")
st = fresh()
for s in ("AAA", "BBB", "CCC"):
    st["positions"][s] = sb.asdict(sb.StockPos(s, "2026-11-05", 102.0, 2.0, 100, True, 1.7))
ev_ = sb.run_day(st, UNI, {"DDD": {"df": DF, "px": 102.0, "vol": 2000.0}, "AAA": {"df": DF, "px": 97.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
check("AAA stopped out and DDD signals the same day -> DDD takes the freed capital slot",
      "AAA" not in st["positions"] and st["positions"]["DDD"]["slot"] is True)
st = fresh()
for s in ("AAA", "BBB", "CCC"):
    st["positions"][s] = sb.asdict(sb.StockPos(s, "2026-11-05", 102.0, 2.0, 100, True, 1.7))
sb.run_day(st, UNI, {"DDD": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
check("3 capital positions open -> DDD is signal-only", st["positions"]["DDD"]["slot"] is False)

print("S5: Kronos A/B read is logged, never gates")
st = fresh()
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0, kronos_fn=lambda df: ("SHORT", 0.6))
e = [json.loads(l) for l in sb.TRADES_LOG.read_text().splitlines()][-1]
check("Kronos disagrees -> still enters, read recorded", "AAA" in st["positions"] and e["kronos_dir"] == "SHORT" and e["kronos_agrees"] is False and e["kronos_conf"] == 0.6)
st = fresh()
def boom(df): raise RuntimeError("model down")
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0, kronos_fn=boom)
check("Kronos failure -> entry still taken", "AAA" in st["positions"] and st["positions"]["AAA"]["kronos_dir"] is None)

print("S6: status renders; empty state loads")
st = fresh()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sb.print_status(st)
check("status on an empty book", "no closed trades yet" in buf.getvalue() and "No open positions" in buf.getvalue())

print("S7: replay equivalence -- live engine run day by day over 3y of NIFTY 50 history vs the research long-only simulation")
prices, lots, nifty = pickle.load(open("/opt/kronos_bot/research/stk_prices.pkl", "rb"))
names = json.load(open("/opt/kronos_bot/research/nifty50_syms.json"))
uni = [{"symbol": s, "key": s, "lot": lots[s]} for s in names]
full, idx = {}, {}
for s in names:
    d = prices[s].copy(); d.index.name = "datetime"; d = d.reset_index(); d.columns = [c.lower() for c in d.columns]
    full[s] = d; idx[s] = {pd.Timestamp(t): i for i, t in enumerate(d["datetime"])}
cal = sorted(set().union(*[set(idx[s]) for s in names]))
st = fresh()
n_ent = 0
for D in cal:
    data = {}
    for s in names:
        j = idx[s].get(D)
        if j is None or j < 200:
            continue
        data[s] = {"df": full[s].iloc[:j], "px": float(full[s]["close"].iloc[j]), "vol": float(full[s]["volume"].iloc[j]),
                   "high": float(full[s]["high"].iloc[j]), "low": float(full[s]["low"].iloc[j])}
    n_ent += sum(e["event"] == "ENTRY" for e in sb.run_day(st, uni, data, str(D.date()), vol_adj=1.0, write_logs=False))
sg, cp = st["stats"]["signal"], st["stats"]["capital"]
mean = sg["sum_ret_pct"] / sg["n"]
print(f"  live engine, signal book : {sg['n']} closed (+{len(st['positions'])} open), win {sg['wins']/sg['n']*100:.0f}%, {mean:+.2f}%/trade, INR {sg['pnl_inr']/sg['n']:+,.0f}/trade")
print(f"  live engine, capital book: {cp['n']} closed, win {cp['wins']/max(cp['n'],1)*100:.0f}%, {cp['sum_ret_pct']/max(cp['n'],1):+.2f}%/trade, total INR {cp['pnl_inr']:+,.0f}")
print(f"  research long-only (stk_v1_robust [3]): 582 trades, +1.06%/trade")
check("trade count within 3% of the research simulation", abs((sg["n"] + len(st["positions"])) - 582) <= 18, f"({sg['n'] + len(st['positions'])} vs 582)")
check("expectancy within 0.25 pt of the research +1.06%/trade", abs(mean - 1.06) <= 0.25, f"({mean:+.2f})")

print()
print("ALL PASSED" if all(results) else f"FAILURES: {results.count(False)} of {len(results)}")
