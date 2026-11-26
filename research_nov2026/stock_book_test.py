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
for label, px, want in (("stop", 97.9, "STOP_LOSS"), ("old channel breach (above the stop, below the 10d low): the trail HOLDS", 98.9, None), ("hold", 100.0, None)):
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
check("session 30 -> the OLD rule says TIME_EXIT, the live long rule holds", sb.check_exit_base(p30, d_hold, 100.0) == "TIME_EXIT" and sb.check_exit(p30, d_hold, 100.0) is None)

print("S3: no re-entry on the exit day; no double processing; entry-day positions are not exited")
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7, hwm=200.0))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 103.0, "vol": 5000.0}}, TODAY, vol_adj=1.0)
check("trail exit AND breakout on the same day -> exit only, no re-entry",
      [e["event"] for e in ev_] == ["EXIT"] and ev_[0]["exit_reason"] == "TRAIL_EXIT" and "AAA" not in st["positions"])
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
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.9, "vol": 2000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
check("a breakdown stops out the long and opens the short shadow, independently, same day",
      [(e["event"], e["book"]) for e in ev_] == [("EXIT", "long"), ("ENTRY", "short_shadow")] and "AAA" not in st["positions"] and "AAA" in st["shorts"])

print("TR: 4-ATR trail for longs, base-exit shadow, high-water mark")
P0 = lambda **kw: sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7, **kw)
check("trail: hwm 130, ATR 2 -> level 122; price 121 -> TRAIL_EXIT, 123 -> hold", sb.check_exit(P0(hwm=130.0), DF, 121.0) == "TRAIL_EXIT" and sb.check_exit(P0(hwm=130.0), DF, 123.0) is None)
check("trail: price exactly at the level (122.0) holds (strictly below exits)", sb.check_exit(P0(hwm=130.0), DF, 122.0) is None)
check("stop still comes first (price below entry - 2 ATR)", sb.check_exit(P0(hwm=130.0), DF, 97.0) == "STOP_LOSS")
dh = flat_df(); dh.loc[dh.index[-1], "close"] = 130.0      # the completed 2026-11-06 session closed at 130, after the 2026-11-05 entry
check("hwm derived from completed closes after entry when none is stored", abs(sb._hwm(P0(), dh, 105.0) - 130.0) < 1e-9 and abs(sb._hwm(P0(), DF, 105.0) - 105.0) < 1e-9 and abs(sb._hwm(P0(), DF, 100.0) - 102.0) < 1e-9)
st = fresh(); st["positions"]["AAA"] = sb.asdict(P0(hwm=102.0))
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 110.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
h1 = st["positions"]["AAA"]["hwm"]
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 105.0, "vol": 1000.0}}, "2026-11-10", vol_adj=1.0)
check("hwm is persisted each run and never falls", h1 == 110.0 and st["positions"]["AAA"]["hwm"] == 110.0, f"({h1}, {st['positions']['AAA']['hwm']})")
check("a day-high spike widens the ATR (today's range counts): a wider trail holds where a flat-tape ATR would exit",
      sb.check_exit(P0(hwm=130.0), DF, 121.0, day_high=140.0, day_low=100.0) is None and sb.check_exit(P0(hwm=130.0), DF, 121.0) == "TRAIL_EXIT")
old400 = P0(); old400.entry_date = "2025-10-01"
check("365-day backstop -> TIME_EXIT (housekeeping)", sb.check_exit(old400, DF, 103.0) == "TIME_EXIT")
check("short shadow keeps the OLD rules (channel exit above the 10d high)", sb.check_exit(sb.StockPos("AAA", "2026-11-05", 98.0, 2.0, 100, False, 1.7, side=-1), DF, 101.5) == "CHANNEL_EXIT")

# base shadow: the old rules fire, the trail holds, then the trail closes it later
st = fresh(); st["positions"]["AAA"] = sb.asdict(P0(hwm=102.0))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 98.9, "vol": 1000.0}}, TODAY, vol_adj=1.0)
b = [e for e in ev_ if e["event"] == "BASE_SHADOW"]
p_ = st["positions"]["AAA"]
check("old channel breach, trail holds: position stays open, a BASE_SHADOW event and the stored base exit",
      len(b) == 1 and not [e for e in ev_ if e["event"] == "EXIT"] and p_["base_exit_reason"] == "CHANNEL_EXIT" and p_["base_exit_price"] == 98.9 and p_["base_exit_date"] == TODAY)
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 99.5, "vol": 1000.0}}, "2026-11-10", vol_adj=1.0)
check("the base exit is recorded once (not overwritten when the old rules keep firing)", st["positions"]["AAA"]["base_exit_date"] == TODAY and not [e for e in ev_ if e["event"] == "BASE_SHADOW"])
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.9, "vol": 1000.0}}, "2026-11-11", vol_adj=1.0)
x = [e for e in ev_ if e["event"] == "EXIT"][0]
live_ret = ((97.9 - 102.0) / 102.0 - 0.001) * 100; base_ret = ((98.9 - 102.0) / 102.0 - 0.001) * 100
check("EXIT carries the paired base outcome: base exit 98.9 CHANNEL_EXIT, paired gain = live - base",
      x["base_exit_reason"] == "CHANNEL_EXIT" and x["base_exit_price"] == 98.9 and abs(x["base_ret_pct"] - base_ret) < 1e-9 and abs(x["paired_gain_pct"] - (live_ret - base_ret)) < 1e-9,
      f"(live {live_ret:+.2f}%, base {base_ret:+.2f}%, gain {x['paired_gain_pct']:+.2f}%)")
bs = st["stats"]["base_shadow"]
check("base_shadow ledger books the paired base trade (n=1, base net P&L), signal ledger books the live one",
      bs["n"] == 1 and abs(bs["pnl_inr"] - ((98.9 - 102.0) * 100 - 0.001 * 102.0 * 100)) < 1e-6 and st["stats"]["signal"]["n"] == 1)
# old rules never fired before the live exit -> same outcome
st = fresh(); st["positions"]["AAA"] = sb.asdict(P0(hwm=130.0))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 121.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
x = [e for e in ev_ if e["event"] == "EXIT"][0]
check("trail exit while the old rules were still holding: base = same exit (LIVE_FIRST), paired gain 0, no stray BASE_SHADOW event",
      x["exit_reason"] == "TRAIL_EXIT" and x["base_exit_reason"] == "LIVE_FIRST" and abs(x["paired_gain_pct"]) < 1e-9 and not [e for e in ev_ if e["event"] == "BASE_SHADOW"])
# both fire the same day (stop)
st = fresh(); st["positions"]["AAA"] = sb.asdict(P0(hwm=102.0))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.9, "vol": 1000.0}}, TODAY, vol_adj=1.0)
x = [e for e in ev_ if e["event"] == "EXIT"][0]
check("stop: old and new rules agree the same day -> base_exit_reason STOP_LOSS, paired gain 0, ledgers match",
      x["base_exit_reason"] == "STOP_LOSS" and abs(x["paired_gain_pct"]) < 1e-9 and abs(st["stats"]["base_shadow"]["pnl_inr"] - st["stats"]["signal"]["pnl_inr"]) < 1e-6)
check("EXIT record carries the peak (hwm, peak_ret_pct) for the capture ratio", x["hwm"] == 102.0 and abs(x["peak_ret_pct"]) < 1e-9)
ev_ = sb.run_day(fresh(), UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
e = [r for r in ev_ if r["event"] == "ENTRY"][0]
check("ENTRY record names the exit rule", e["exit_rule"] == "trail4")
st = fresh(); sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0)
check("new position stores hwm = entry price and no base exit yet", st["positions"]["AAA"]["hwm"] == 102.0 and st["positions"]["AAA"]["base_exit_date"] is None)
st = fresh()
st["positions"]["AAA"] = {k: v for k, v in sb.asdict(P0()).items() if k not in ("hwm", "base_exit_date", "base_exit_price", "base_exit_reason")}
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 103.0, "vol": 1000.0}}, TODAY, vol_adj=1.0)
check("a position saved before the trail (no hwm / base keys) loads, gets a hwm and keeps running", st["positions"]["AAA"]["hwm"] == 103.0 and ev_ == [])
buf = io.StringIO()
st = fresh(); st["positions"]["AAA"] = sb.asdict(P0(hwm=110.0, base_exit_date="2026-11-09", base_exit_price=98.9, base_exit_reason="CHANNEL_EXIT"))
with contextlib.redirect_stdout(buf):
    sb.print_status(st)
check("status shows the base-exit shadow ledger, the hwm and the old-exit note on an open position", "BASE-EXIT SHADOW" in buf.getvalue() and "hwm 110.00" in buf.getvalue() and "old exits would have closed" in buf.getvalue())

print("ROT: stale rotation (capital and risk ledgers; the signal ledger is the untouched control)")
OLD = str(DF["datetime"].iloc[-21].date())          # 20 completed sessions after this date -> held 21 sessions today
RECENT = "2026-11-05"
def mkpos(sym, entry, ep, slot=True, risk_lots=0, hwm=None, nifty_entry=None):
    return sb.asdict(sb.StockPos(sym, entry, ep, 2.0, 100, slot, 1.7, risk_lots=risk_lots, hwm=(ep if hwm is None else hwm), nifty_entry=nifty_entry))
def cap_state(entries=(OLD, OLD, OLD), prices=(100.0, 103.0, 101.0), hwms=(None, None, None), nifty=None):
    st = fresh()
    for s, e, ep, h in zip(("AAA", "BBB", "CCC"), entries, prices, hwms):
        st["positions"][s] = mkpos(s, e, ep, slot=True, hwm=h, nifty_entry=nifty)
    return st
DROT = {"AAA": {"df": DF, "px": 101.0, "vol": 1000.0}, "BBB": {"df": DF, "px": 102.0, "vol": 1000.0}, "CCC": {"df": DF, "px": 101.5, "vol": 1000.0}, "DDD": {"df": DF, "px": 102.0, "vol": 2000.0}}
st = cap_state(nifty=24000.0)
ev_ = sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=ABOVE)
rot = [e for e in ev_ if e["event"] == "ROTATE_OUT"]
check("3 capital slots full of stale holders + a new signal -> exactly one rotation, the LOWEST return (BBB -0.97%) replaced by DDD",
      len(rot) == 1 and rot[0]["symbol"] == "BBB" and rot[0]["replaced_by"] == "DDD" and rot[0]["book"] == "capital", f"({[(r['symbol'], round(r['ret_pct'], 2)) for r in rot]})")
check("DDD takes the capital slot, BBB loses it but stays open in the signal ledger, capital slots stay at 3",
      st["positions"]["DDD"]["slot"] is True and st["positions"]["BBB"]["slot"] is False and "BBB" in st["positions"] and st["positions"]["BBB"]["capital_rotated_date"] == TODAY
      and sum(1 for p in st["positions"].values() if p["slot"]) == 3)
want_net = (102.0 - 103.0) * 100 - 0.001 * 103.0 * 100
check("capital ledger books the rotated trade at today's price (net P&L, 1 closed, rotated counter); the signal ledger books nothing yet",
      st["stats"]["capital"]["n"] == 1 and abs(st["stats"]["capital"]["pnl_inr"] - want_net) < 1e-6 and st["stats"]["capital"]["rotated"] == 1 and st["stats"]["signal"]["n"] == 0,
      f"({st['stats']['capital']['pnl_inr']:+.1f} vs {want_net:+.1f})")
check("rotation hedged return = trade return - NIFTY move (0%) - 0.03%", abs(rot[0]["hedged_ret_pct"] - (rot[0]["ret_pct"] - 0.03)) < 1e-9)
e = [r for r in ev_ if r["event"] == "ENTRY"][0]
check("ENTRY record names the stock it rotated out, and ROTATE_OUT is logged before it", e["rotated_out_for_capital"] == "BBB" and [x["event"] for x in ev_] == ["ROTATE_OUT", "ENTRY"])
# later exit of the rotated holder must not double count in the capital ledger
ev2 = sb.run_day(st, UNI[:2], {"BBB": {"df": DF, "px": 97.0, "vol": 1000.0}}, "2026-11-10", vol_adj=1.0, regime=ABOVE)
x = [r for r in ev2 if r["event"] == "EXIT"][0]
check("the rotated holder later stops out: signal ledger books it, capital ledger does NOT book it again, EXIT carries the rotation date",
      st["stats"]["capital"]["n"] == 1 and st["stats"]["signal"]["n"] == 1 and x["slot"] is False and x["capital_rotated_date"] == TODAY)
st = cap_state(hwms=(105.0, 108.0, 106.0))
sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=ABOVE)
check("holders that peaked >= 1.5 ATR (not stale) are never rotated: the new signal is signal-only", st["positions"]["DDD"]["slot"] is False and st["stats"]["capital"]["n"] == 0)
st = cap_state(entries=(RECENT, RECENT, RECENT))
sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=ABOVE)
check("holders younger than 15 sessions are not stale: no rotation", st["positions"]["DDD"]["slot"] is False and st["stats"]["capital"]["n"] == 0)
st = cap_state()
sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=BELOW)
check("NIFTY below its 200-DMA: no capital slot and no rotation", st["positions"]["DDD"]["slot"] is False and st["stats"]["capital"]["n"] == 0)
st = cap_state(entries=(OLD, RECENT, RECENT))
D2 = dict(DROT); D2["DDD"] = {"df": DF, "px": 102.0, "vol": 3000.0}; D2["EEE"] = {"df": DF, "px": 102.0, "vol": 2000.0}
sb.run_day(st, UNI, D2, TODAY, vol_adj=1.0, regime=ABOVE)
check("two signals, one stale holder: the stronger volume (DDD) gets the rotated slot, EEE is signal-only, one rotation",
      st["positions"]["DDD"]["slot"] is True and st["positions"]["EEE"]["slot"] is False and st["stats"]["capital"]["rotated"] == 1 and st["positions"]["AAA"]["slot"] is False)
st = cap_state()
D3 = {k: v for k, v in DROT.items() if k != "BBB"}
sb.run_day(st, UNI, D3, TODAY, vol_adj=1.0, regime=ABOVE)
check("a stale holder with no quote today is skipped; the next-weakest (CCC) is rotated", st["positions"]["CCC"]["slot"] is False and st["positions"]["BBB"]["slot"] is True and st["positions"]["DDD"]["slot"] is True)
sb.ROTATE_STALE = False
st = cap_state()
sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=ABOVE)
check("ROTATE_STALE = False (the revert switch): no rotation, DDD signal-only", st["positions"]["DDD"]["slot"] is False and st["stats"]["capital"]["n"] == 0 and st["stats"]["capital"]["rotated"] == 0)
sb.ROTATE_STALE = True
UNI6 = [{"symbol": s, "key": f"k{s}", "lot": 100} for s in ("A1", "A2", "A3", "A4", "A5", "A6")]
st = fresh()
for s in ("A1", "A2", "A3", "A4", "A5"):
    st["positions"][s] = mkpos(s, OLD, 100.0, slot=False, risk_lots=75)
DR = {s: {"df": DF, "px": px, "vol": 1000.0} for s, px in (("A1", 101.0), ("A2", 100.5), ("A3", 99.5), ("A4", 100.0), ("A5", 101.5))}
DR["A6"] = {"df": DF, "px": 102.0, "vol": 2000.0}
ev_ = sb.run_day(st, UNI6, DR, TODAY, vol_adj=1.0, regime=ABOVE)
rr = [e for e in ev_ if e["event"] == "ROTATE_OUT"]
per_lot = (99.5 - 100.0) * 100 - 0.001 * 100.0 * 100
check("risk ledger at its 5-position cap + a new signal -> the weakest stale risk holder (A3) is rotated out of the risk ledger only",
      len(rr) == 1 and rr[0]["book"] == "risk" and rr[0]["symbol"] == "A3" and st["positions"]["A3"]["risk_lots"] == 0 and "A3" in st["positions"] and st["positions"]["A6"]["risk_lots"] == 75
      and sum(1 for p in st["positions"].values() if p["risk_lots"] > 0) == 5, f"({[(r['book'], r['symbol']) for r in rr]})")
check("risk ledger books the rotated trade x lots (per-lot net x 75), counter incremented, signal ledger untouched",
      st["stats"]["risk"]["n"] == 1 and abs(st["stats"]["risk"]["pnl_inr"] - per_lot * 75) < 1e-6 and st["stats"]["risk"]["rotated"] == 1 and st["stats"]["signal"]["n"] == 0 and st["stats"]["capital"]["rotated"] == 0,
      f"({st['stats']['risk']['pnl_inr']:+,.0f} vs {per_lot * 75:+,.0f})")
ev3 = sb.run_day(st, UNI6[:3], {"A3": {"df": DF, "px": 95.0, "vol": 1000.0}}, "2026-11-10", vol_adj=1.0, regime=ABOVE)
check("a risk-rotated holder that later exits is not booked again in the risk ledger", st["stats"]["risk"]["n"] == 1 and st["stats"]["signal"]["n"] == 1)
st = fresh()
for s in ("A1", "A2", "A3", "A4", "A5"):
    st["positions"][s] = mkpos(s, OLD, 100.0, slot=False, risk_lots=75)
UB = [dict(u) for u in UNI6[:5]] + [{"symbol": "B1", "key": "kB1", "lot": 100}]
DR2 = {s: {"df": DF, "px": px, "vol": 1000.0} for s, px in (("A1", 101.0), ("A2", 100.5), ("A3", 99.5), ("A4", 100.0), ("A5", 101.5))}
DR2["B1"] = {"df": DF, "px": 400.0, "vol": 2000.0}
ev_ = sb.run_day(st, UB, DR2, TODAY, vol_adj=1.0, regime=ABOVE)
check("a position too big for the gross cap even after freeing one stale holder: no risk rotation, entry is signal-only",
      not [e for e in ev_ if e["event"] == "ROTATE_OUT" and e["book"] == "risk"] and st["positions"]["B1"]["risk_lots"] == 0 and st["stats"]["risk"]["n"] == 0)
buf = io.StringIO()
st = cap_state(nifty=24000.0)
sb.run_day(st, UNI, DROT, TODAY, vol_adj=1.0, regime=ABOVE)
with contextlib.redirect_stdout(buf):
    sb.print_status(st)
check("status shows 'closed by stale rotation' on the capital ledger and the rotated-out note on the position", "closed by stale rotation" in buf.getvalue() and "rotated out of CAPITAL" in buf.getvalue())
st = fresh()
st["positions"]["AAA"] = {k: v for k, v in mkpos("AAA", OLD, 100.0).items() if k not in ("capital_rotated_date", "risk_rotated_date")}
st["stats"]["capital"] = {k: v for k, v in st["stats"]["capital"].items() if k != "rotated"}
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 101.0, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
check("a position / stats saved before rotation existed (no rotated keys) still runs", ev_ == [] and "AAA" in st["positions"])

print("RB: risk book (equal risk, count and gross caps)")
st = fresh()
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 102.0, "vol": 2000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
# flat tape: ATR 2 -> stop distance 4; lot 100; risk 1% of Rs30L = Rs30,000 -> 30000 / (4 x 100) = 75 lots
check("equal-risk lots = round(1% x capital / (2 ATR x lot)) = 75", st["positions"]["AAA"]["risk_lots"] == 75, f"({st['positions']['AAA']['risk_lots']})")
e = [json.loads(l) for l in sb.TRADES_LOG.read_text().splitlines()][-1]
check("ENTRY record carries risk_lots", e["risk_lots"] == 75)
st = fresh()
UNI6 = [{"symbol": s, "key": f"k{s}", "lot": 100} for s in ("A1", "A2", "A3", "A4", "A5", "A6")]
sb.run_day(st, UNI6, {u["symbol"]: {"df": DF, "px": 102.0, "vol": 2000.0 + i} for i, u in enumerate(UNI6)}, TODAY, vol_adj=1.0, regime=ABOVE)
rl = sorted([p["risk_lots"] for p in st["positions"].values()], reverse=True)
check("6 signals: all in the signal book, only 5 in the risk book (count cap)", len(st["positions"]) == 6 and rl == [75] * 5 + [0], f"({rl})")
st = fresh()
UNI2 = [{"symbol": s, "key": f"k{s}", "lot": 100} for s in ("B1", "B2")]
sb.run_day(st, UNI2, {u["symbol"]: {"df": DF, "px": 400.0, "vol": 2000.0 + i} for i, u in enumerate(UNI2)}, TODAY, vol_adj=1.0, regime=ABOVE)
rl = sorted([p["risk_lots"] for p in st["positions"].values()], reverse=True)
check("gross notional cap (1.3 x Rs30L): the second Rs30L position is refused", rl == [75, 0], f"({rl})")
st = fresh()
st["positions"]["AAA"] = sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, False, 1.7, risk_lots=75))
ev_ = sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.9, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
per_lot = (97.9 - 102.0) * 100 - 0.001 * 102.0 * 100
check("risk ledger P&L = per-lot net x lots; signal ledger stays per lot; capital ledger untouched (slot False)",
      abs(st["stats"]["risk"]["pnl_inr"] - per_lot * 75) < 1e-6 and abs(st["stats"]["signal"]["pnl_inr"] - per_lot) < 1e-6 and st["stats"]["capital"]["n"] == 0
      and st["stats"]["risk"]["n"] == 1, f"(risk Rs{st['stats']['risk']['pnl_inr']:+,.0f} = {st['stats']['risk']['pnl_inr']/sb.RISK_CAPITAL*100:+.2f}% of capital; the stop was sized to lose ~1%)")
st = fresh()
st["positions"]["AAA"] = {k: v for k, v in sb.asdict(sb.StockPos("AAA", "2026-11-05", 102.0, 2.0, 100, True, 1.7)).items() if k != "risk_lots"}
sb.run_day(st, UNI[:1], {"AAA": {"df": DF, "px": 97.9, "vol": 1000.0}}, TODAY, vol_adj=1.0, regime=ABOVE)
check("a position saved before the risk book existed (no risk_lots key) exits cleanly and is not counted in it", st["stats"]["risk"]["n"] == 0 and st["stats"]["signal"]["n"] == 1)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sb.print_status(fresh())
check("status prints the risk book line", "RISK BOOK" in buf.getvalue())

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
print("  research (N50, OLD exits): long 569 trades +1.07% (hedged +0.58%); short 389 trades -1.31% (hedged -0.58%); the shorts still use the old exits")
sg, sh = st["stats"]["signal"], st["stats"]["short_shadow"]
bsl = st["stats"]["base_shadow"]
print(f"  base-exit shadow (same entries, OLD exits): {bsl['n']} closed, win {bsl['wins']/max(bsl['n'],1)*100:.0f}%, {bsl['sum_ret_pct']/max(bsl['n'],1):+.2f}%/trade")


def ref_trail(s):
    """Independent reference for the live LONG rule (simple-mean ATR14 instead of the engine's Wilder ATR): same entries, 2-ATR stop, 4-ATR trail from the highest close."""
    d = full[s]; c, h, l, v = (d[k].values.astype(float) for k in ("close", "high", "low", "volume")); n = len(c)
    pc = np.r_[np.nan, c[:-1]]; tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc))); tr[0] = h[0] - l[0]
    atr = pd.Series(tr).rolling(14).mean().values; rets, op, pos, last = [], 0, False, -1
    for t in range(200, n):
        if pos:
            hc = max(hc, c[t])
            if c[t] <= ep - 2 * ea or c[t] < hc - 4 * atr[t]:
                rets.append(c[t] / ep - 1 - 0.001); pos = False; last = t
            continue
        if t != last and c[t] > h[t - 20:t].max() and v[t - 20:t].mean() > 0 and v[t] / v[t - 20:t].mean() >= 1.5:
            pos, ep, ea, hc = True, c[t], atr[t], c[t]
    return rets, int(pos)


rr, ro = [], 0
for s in names:
    r_, o_ = ref_trail(s); rr += r_; ro += o_
ref_n, ref_mean = len(rr) + ro, float(np.mean(rr)) * 100
eng_n, eng_mean = sg["n"] + len(st["positions"]), sg["sum_ret_pct"] / sg["n"]
print(f"  reference (independent numpy sim of the trail rule): {len(rr)} closed + {ro} open, {ref_mean:+.2f}%/trade   engine: {sg['n']} closed + {len(st['positions'])} open, {eng_mean:+.2f}%/trade")
check("long signal book (4-ATR trail): trade count within 5% of the independent reference sim", abs(eng_n - ref_n) <= max(6, 0.05 * ref_n), f"(engine {eng_n}, reference {ref_n})")
check("long signal book (4-ATR trail): expectancy within 0.8 pt of the independent reference", abs(eng_mean - ref_mean) <= 0.8, f"(engine {eng_mean:+.2f}, reference {ref_mean:+.2f})")
check("every closed live long has a paired base-exit outcome (ledger counts match)", bsl["n"] == sg["n"], f"({bsl['n']} vs {sg['n']})")
cp, rk = st["stats"]["capital"], st["stats"]["risk"]
print(f"  capital ledger: {cp['n']} closed ({cp.get('rotated', 0)} by stale rotation), win {cp['wins']/max(cp['n'],1)*100:.0f}%, {cp['sum_ret_pct']/max(cp['n'],1):+.2f}%/trade, Rs{cp['pnl_inr']:+,.0f} per lot;   risk ledger: {rk['n']} closed ({rk.get('rotated', 0)} by rotation), Rs{rk['pnl_inr']:+,.0f}")
check("stale rotation leaves the SIGNAL ledger untouched (same 368 closed trades and +3.58%/trade as before rotation existed)", sg["n"] == 368 and abs(eng_mean - 3.58) <= 0.01, f"({sg['n']}, {eng_mean:+.2f})")
check("stale rotation fired in the 3y replay (capital ledger)", cp.get("rotated", 0) > 0, f"({cp.get('rotated', 0)})")
check("short shadow loses, as the research found (-1.25% short-only sim): within 0.35 pt", abs(sh["sum_ret_pct"] / sh["n"] - (-1.25)) <= 0.35, f"({sh['sum_ret_pct']/sh['n']:+.2f})")
check("short shadow trade count within 8% of the research short-only sim (400)", abs((sh["n"] + len(st["shorts"])) - 400) <= 32, f"({sh['n'] + len(st['shorts'])})")

print()
print("ALL PASSED" if all(results) else f"FAILURES: {results.count(False)} of {len(results)}")
