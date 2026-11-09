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
check("one signal-log row per stock evaluated (long signals only on this tape)", len(sigs) == 5 and all(r["long_signal"] and not r["short_signal"] for r in sigs))

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
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
check("second run the same day: no duplicate entry", ev_ == [] and len(st["positions"]) == 1)
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 90.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
check("second run the same day at a crash price: today's entry is not exited", [e for e in ev_ if e["book"] == "long"] == [] and "AAA" in st["positions"])

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

print("R: regime filter, regime tags and the beta-hedged return")
ABOVE = {"nifty": 24000.0, "sma200": 23000.0, "above200": True}
BELOW = {"nifty": 22000.0, "sma200": 23000.0, "above200": False}
st = fresh()
sb.run_day(st, UNI[:2], {s: {"df": DF, "px": 102.0, "vol": 2000.0} for s in ("AAA", "BBB")}, TODAY, vol_adj=1.0, regime=BELOW)
check("NIFTY below 200-DMA -> signals still taken (signal book) but no capital slot", len(st["positions"]) == 2 and not any(p["slot"] for p in st["positions"].values()))
check("positions carry the regime tag and NIFTY entry level", all(p["regime_above200"] is False and p["nifty_entry"] == 22000.0 for p in st["positions"].values()))
e = [json.loads(l) for l in sb.TRADES_LOG.read_text().splitlines()]
check("ENTRY records say the capital slot was blocked by the regime", all(r["capital_blocked_by_regime"] and r["slot"] is False for r in e if r["event"] == "ENTRY"))
st = fresh()
sb.run_day(st, UNI[:2], {s: {"df": DF, "px": 102.0, "vol": 2000.0} for s in ("AAA", "BBB")}, TODAY, vol_adj=1.0, regime=ABOVE)
check("NIFTY above 200-DMA -> capital slots open", all(p["slot"] for p in st["positions"].values()))
st = fresh()
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0, regime=None)
check("regime unknown (fetch failed) -> not filtered, entry still takes a slot", st["positions"]["AAA"]["slot"] is True)
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7, nifty_entry=24000.0, regime_above200=True))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.0, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime={"nifty": 24480.0, "sma200": 23000.0, "above200": True})
x = [r for r in ev_ if r["event"] == "EXIT"][0]
want_ret = ((97.0 - 102.0) / 102.0 - 0.001) * 100
check("hedged return = trade return - NIFTY move - 0.03%", abs(x["nifty_ret_pct"] - 2.0) < 1e-9 and abs(x["hedged_ret_pct"] - (want_ret - 2.0 - 0.03)) < 1e-9,
      f"(ret {x['ret_pct']:+.2f}%, NIFTY {x['nifty_ret_pct']:+.2f}%, hedged {x['hedged_ret_pct']:+.2f}%)")
check("stats carry the hedged sum", abs(st["stats"]["signal"]["sum_hedged_pct"] - x["hedged_ret_pct"]) < 1e-9)
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.0, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime=None)
check("positions without a NIFTY entry level still close cleanly (hedged = None)", [r for r in ev_ if r["event"] == "EXIT"][0]["hedged_ret_pct"] is None)

print("SH: short shadow (mirrored rules, no capital, no Kronos)")
st = fresh()
called = []
ev_ = sb.run_day(st, UNI[:2], {"AAA": {"df": DF, "px": 98.0, "vol": 2000.0}, "BBB": {"df": DF, "px": 98.0, "vol": 1000.0}}, TODAY, vol_adj=1.0,
                 kronos_fn=lambda df: called.append(1) or ("SHORT", 0.9), regime=ABOVE)
check("breakdown + volume -> SHORT shadow position; no volume -> none", list(st["shorts"].keys()) == ["AAA"] and st["positions"] == {})
sp = st["shorts"]["AAA"]
check("short shadow: side -1, not a capital slot, Kronos never consulted", sp["side"] == -1 and sp["slot"] is False and called == [])
e = [json.loads(l) for l in sb.TRADES_LOG.read_text().splitlines()][-1]
check("ENTRY record: book short_shadow, stop = entry + 2 ATR", e["book"] == "short_shadow" and e["direction"] == "SHORT" and abs(e["stop"] - 102.0) < 1e-6, f"({e['stop']})")
check("price == 20d low -> no short signal (must break below)", not sb.evaluate_entry(DF, 99.0, 2000.0, vol_adj=1.0, side=-1)["signal"])
for label, px, want in (("stop (price up 2 ATR)", 102.2, "STOP_LOSS"), ("channel (above the 10d high, below the stop)", 101.5, "CHANNEL_EXIT"), ("hold", 100.0, None)):
    st = fresh()
    st["shorts"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 98.0, 2.0, 100, False, 1.7, side=-1, nifty_entry=24000.0, regime_above200=True))
    ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": px, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime={"nifty": 23760.0, "sma200": 23000.0, "above200": True})
    ex = [r for r in ev_ if r["event"] == "EXIT"]
    check(f"short {label}: {want}", (ex[0]["exit_reason"] == want) if want else (ex == [] and "AAA" in st["shorts"]))
    if want == "STOP_LOSS":
        net = -(102.2 - 98.0) * 100 - 0.001 * 98.0 * 100
        check("short P&L sign, cost, ledger, hedged (NIFTY -1%: the long hedge made money)",
              abs(ex[0]["pnl_inr"] - net) < 1e-6 and st["stats"]["short_shadow"]["n"] == 1 and st["stats"]["signal"]["n"] == 0
              and st["stats"]["capital"]["n"] == 0 and abs(ex[0]["nifty_ret_pct"] + 1.0) < 1e-9, f"({ex[0]['pnl_inr']:+.1f}, hedged {ex[0]['hedged_ret_pct']:+.2f}%)")
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 98.5, "vol": 2000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
check("a breakdown exits the long (channel) and opens the short shadow, independently, same day",
      [(e["event"], e["book"]) for e in ev_] == [("EXIT", "long"), ("ENTRY", "short_shadow")] and "AAA" not in st["positions"] and "AAA" in st["shorts"])

print("S6: status renders; empty state loads")
st = fresh()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sb.print_status(st)
check("status on an empty book", "no closed trades yet" in buf.getvalue() and "No open positions" in buf.getvalue() and "SHORT SHADOW" in buf.getvalue())
old = {"positions": {}, "stats": {"signal": {"n": 1, "wins": 1, "pnl_inr": 5.0, "cost_inr": 1.0, "sum_ret_pct": 1.0}, "capital": {"n": 0, "wins": 0, "pnl_inr": 0.0, "cost_inr": 0.0, "sum_ret_pct": 0.0}}}
sb.STATE_FILE.write_text(json.dumps(old)); st = sb._load_state()
check("a state file from before the short shadow / hedged columns loads with defaults", st["shorts"] == {} and st["stats"]["signal"]["sum_hedged_pct"] == 0.0 and "short_shadow" in st["stats"])

print("S7: replay equivalence -- live engine run day by day over 3y of NIFTY 50 history (with the NIFTY 200-DMA regime and the short shadow)")
prices, lots, nifty = pickle.load(open("/opt/kronos_bot/research/stk_prices.pkl", "rb"))
names = json.load(open("/opt/kronos_bot/research/nifty50_syms.json"))
uni = [{"symbol": s, "key": s, "lot": lots[s]} for s in names]
nf = nifty.copy(); nf.index = pd.to_datetime(nf.index).normalize()
full, idx = {}, {}
for s in names:
    d = prices[s].copy(); d.index.name = "datetime"; d = d.reset_index(); d.columns = [c.lower() for c in d.columns]
    full[s] = d; idx[s] = {pd.Timestamp(t): i for i, t in enumerate(d["datetime"])}
cal = sorted(set().union(*[set(idx[s]) for s in names]))


def regime_for(D):
    prior = nf[nf.index < D]
    if D not in nf.index or len(prior) < 200:
        return None
    lvl, sma = float(nf.loc[D]), float(prior.tail(200).mean())
    return {"nifty": lvl, "sma200": sma, "above200": lvl > sma}


st = fresh()
for D in cal:
    data = {}
    for s in names:
        j = idx[s].get(D)
        if j is None or j < 200:
            continue
        data[s] = {"df": full[s].iloc[:j], "px": float(full[s]["close"].iloc[j]), "vol": float(full[s]["volume"].iloc[j]),
                   "high": float(full[s]["high"].iloc[j]), "low": float(full[s]["low"].iloc[j])}
    sb.run_day(st, uni, data, str(D.date()), vol_adj=1.0, write_logs=False, regime=regime_for(D))


def line(name, k, extra=""):
    x = st["stats"][k]
    print(f"  {name:<34}{x['n']:>5} closed  win {x['wins']/max(x['n'],1)*100:>3.0f}%  {x['sum_ret_pct']/max(x['n'],1):>+6.2f}%/trade  hedged {x['sum_hedged_pct']/max(x['n'],1):>+6.2f}%/trade{extra}")


line("signal book (long, any regime)", "signal", f"  (+{len(st['positions'])} open)")
line("capital book (3 slots, NIFTY>200-DMA)", "capital")
line("short shadow", "short_shadow", f"  (+{len(st['shorts'])} open)")
print("  research (N50): long 569 trades +1.07% (hedged +0.58%); short 389 trades -1.31% (hedged -0.58%)")
sg, sh = st["stats"]["signal"], st["stats"]["short_shadow"]
check("long signal book: trade count within 3% of research (582 long-only sim)", abs((sg["n"] + len(st["positions"])) - 582) <= 18, f"({sg['n'] + len(st['positions'])})")
check("long signal book expectancy within 0.25 pt of the research +1.06%/trade", abs(sg["sum_ret_pct"] / sg["n"] - 1.06) <= 0.25, f"({sg['sum_ret_pct']/sg['n']:+.2f})")
check("short shadow loses, as the research found (-1.25% short-only sim): within 0.35 pt", abs(sh["sum_ret_pct"] / sh["n"] - (-1.25)) <= 0.35, f"({sh['sum_ret_pct']/sh['n']:+.2f})")
check("short shadow trade count within 8% of the research short-only sim (400)", abs((sh["n"] + len(st["shorts"])) - 400) <= 32, f"({sh['n'] + len(st['shorts'])})")

print()
print("ALL PASSED" if all(results) else f"FAILURES: {results.count(False)} of {len(results)}")
