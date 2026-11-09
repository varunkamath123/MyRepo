import sys, os, json, io, logging, contextlib
sys.path.insert(0, "/opt/kronos_bot/repo")
sys.path.insert(0, "/opt/kronos_bot/research/stage_bnf")      # staged NEW paper_trader.py + backtest.py shadow the deployed ones
import pandas as pd
from pathlib import Path
import paper_trader as pt
import backtest as btmod
assert pt.__file__.startswith("/opt/kronos_bot/research/stage_bnf"), pt.__file__
assert btmod.__file__.startswith("/opt/kronos_bot/research/stage_bnf"), btmod.__file__
logging.disable(logging.CRITICAL)

T = "/tmp/bnf_only_test"
os.makedirs(T, exist_ok=True)
pt.STATE_FILE          = f"{T}/paper_state.json"
pt.TRADES_LOG          = f"{T}/paper_trades.jsonl"
pt.SHADOW_STATE_FILE   = Path(f"{T}/shadow_state.json")
pt.SHADOW_TRADES_LOG   = Path(f"{T}/shadow_trades.jsonl")
pt.DECAY_CF_FILE       = Path(f"{T}/decay_cf_state.json")
pt.DECAY_CF_LOG        = Path(f"{T}/decay_cf_trades.jsonl")
pt.BLOCKED_STATE_FILE  = Path(f"{T}/blocked_shadow_state.json")
pt.BLOCKED_TRADES_LOG  = Path(f"{T}/blocked_shadow_trades.jsonl")
pt.DAILY_READS_LOG     = Path(f"{T}/daily_reads.jsonl")
pt.MIROFISH_FILE       = Path(f"{T}/no_mirofish.json")

import datetime as _dt


class FakeDate(_dt.date):
    _today = _dt.date(2026, 11, 9)

    @classmethod
    def today(cls):
        return cls._today


pt.date = FakeDate
real_df = pd.read_csv("/opt/kronos_bot/research/sweepU_BANKNIFTY_df.csv", parse_dates=["datetime"]).tail(150).reset_index(drop=True)
SCEN = {"price": 0.0, "sig": ("NEUTRAL", 0.0, "kronos", 0.0, 0.0), "adx": 30.0, "st": "BEAR"}
pt.get_context     = lambda inst, bars=150: real_df
pt.get_entry_price = lambda inst, ctx: (SCEN["price"], "stub")
pt.get_signal      = lambda df, force_fallback=False: SCEN["sig"]
pt.supertrend      = lambda df, period=10, multiplier=3.0: SCEN["st"]
pt.compute_adx     = lambda df: SCEN["adx"]

ENTRY = 54811.2
NEUTRAL = ("NEUTRAL", 0.0, "kronos", 0.0, 0.0)
SHORT50 = ("SHORT", 0.50, "kronos", 2.0, 54000.0)
SHORT30 = ("SHORT", 0.30, "kronos", 2.0, 54000.0)
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def reset(seed_short=False):
    FakeDate._today = _dt.date(2026, 11, 9)
    for f in os.listdir(T):
        os.remove(f"{T}/{f}")
    pos = {}
    if seed_short:
        pos["BANKNIFTY"] = pt.Position(instrument="BANKNIFTY", direction="SHORT", entry_date="2026-11-09",
                                       entry_price=ENTRY, lot_size=30, hwm=ENTRY, trail_active=False,
                                       trail_stop=0.0, entry_confidence=0.50)
    pt._save_state(pt.PaperState(capital=350000, positions=pos, realised_pnl=0.0, trade_count=0))
    SCEN.update(price=0.0, sig=NEUTRAL, adx=30.0, st="BEAR")


def run(price, sig, adx=30.0, st="BEAR", insts=("BANKNIFTY",)):
    SCEN.update(price=price, sig=sig, adx=adx, st=st)
    FakeDate._today = FakeDate._today + _dt.timedelta(days=1)
    with contextlib.redirect_stdout(io.StringIO()):
        pt.cmd_daily_run(list(insts))
    return pt._load_state(), pt._load_decay_cf(), pt._load_blocked()


def lines(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


print("T0: config")
check("ACTIVE_INSTRUMENTS is BANKNIFTY only", pt.ACTIVE_INSTRUMENTS == ["BANKNIFTY"])
check("decay modes: BANKNIFTY profit_only, NIFTY immediate", pt._decay_mode("BANKNIFTY") == "profit_only" and pt._decay_mode("NIFTY") == "immediate" and pt._decay_mode("SENSEX") == "immediate")
P = btmod.INSTRUMENT_PARAMS["BANKNIFTY"]
check("BANKNIFTY params: gates off, bar .45, stop 3%, ST 10/4.0", P["entry_gate_adx"] is False and P["entry_gate_st"] is False
      and P["kronos_conf_min"] == 0.45 and P["stop_loss_pct"] == 0.03 and P["supertrend_multiplier"] == 4.0)

print("T1: gates off -> a confidence-clearing read enters even with ADX<25 and SuperTrend misaligned; flags recorded")
reset(False)
st_, cf, bl = run(ENTRY, SHORT50, adx=20.0, st="BULL")
check("real SHORT entry taken", "BANKNIFTY" in st_.positions and st_.positions["BANKNIFTY"].direction == "SHORT")
e = [r for r in lines(pt.TRADES_LOG) if r["event"] == "ENTRY"][-1]
check("ENTRY record lists would_have_been_blocked ADX+ST", e["would_have_been_blocked"] == ["ADX", "ST"] and abs(e["adx"] - 20.0) < 1e-9 and e["supertrend"] == "BULL", f"({e['would_have_been_blocked']})")
dr = lines(pt.DAILY_READS_LOG)[-1]
check("daily read: gate PASS, would_block ADX+ST, entered", dr["gate"] == "PASS" and dr["would_block"] == ["ADX", "ST"] and dr["entered"] is True, f"({dr['gate']}, {dr['would_block']})")
check("no blocked shadow opened (nothing is blocked any more)", bl["positions"] == {})

print("T2: clean pass -> empty flags")
reset(False)
st_, cf, bl = run(ENTRY, SHORT50, adx=30.0, st="BEAR")
e = [r for r in lines(pt.TRADES_LOG) if r["event"] == "ENTRY"][-1]
check("entry with no would-have-been-blocked flags", "BANKNIFTY" in st_.positions and e["would_have_been_blocked"] == [])

print("T3: reads that must still NOT enter")
reset(False)
st_, cf, bl = run(ENTRY, SHORT30, adx=20.0, st="BULL")
check("confidence 0.30 < 0.45 bar -> no entry, no blocked shadow", st_.positions == {} and bl["positions"] == {})
reset(False)
st_, cf, bl = run(ENTRY, NEUTRAL, adx=30.0, st="BEAR")
check("NEUTRAL read -> no entry", st_.positions == {})
reset(False)
st_, cf, bl = run(ENTRY, ("LONG", 0.449, "kronos", 0.0, 0.0), adx=30.0, st="BULL")
check("0.449 just under the bar -> no entry", st_.positions == {})
reset(False)
st_, cf, bl = run(ENTRY, ("LONG", 0.45, "kronos", 0.0, 0.0), adx=30.0, st="BULL")
check("0.45 at the bar -> entry", "BANKNIFTY" in st_.positions)

print("T4: NIFTY (not active, but its rules are untouched): gates still block, bar 0.55")
reset(False)
SCEN.update(adx=20.0, st="BEAR", sig=("SHORT", 0.60, "kronos", 0.0, 0.0))
d = {}
r = pt.evaluate_signal("NIFTY", real_df, d)
check("NIFTY ADX 20 -> blocked at the ADX gate", r[0] == "NEUTRAL" and d["gate"] == "ADX" and d["would_block"] == [])
SCEN.update(adx=30.0, st="BULL")
d = {}
r = pt.evaluate_signal("NIFTY", real_df, d)
check("NIFTY SuperTrend misaligned -> blocked at the ST gate", r[0] == "NEUTRAL" and d["gate"] == "ST")
SCEN.update(adx=30.0, st="BEAR")
d = {}
r = pt.evaluate_signal("NIFTY", real_df, d)
check("NIFTY all gates pass -> SHORT", r[0] == "SHORT" and d["gate"] == "PASS")
SCEN.update(sig=("SHORT", 0.50, "kronos", 0.0, 0.0))
check("NIFTY conf 0.50 < 0.55 -> NEUTRAL", pt.evaluate_signal("NIFTY", real_df, {})[0] == "NEUTRAL")

print("T5: profit-gated live decay: held while red, fires once green; held_red pair recorded")
reset(True)
st_, cf, bl = run(55300.0, NEUTRAL)
red1 = -(55300.0 - ENTRY) * 30
check("red + unsupported -> HELD (not closed)", "BANKNIFTY" in st_.positions and st_.realised_pnl == 0)
check("streak 1 and held_red recorded with what firing would have paid",
      st_.positions["BANKNIFTY"].unsupported_streak == 1 and abs(cf["held_red"]["BANKNIFTY"]["pnl_if_fired"] - red1) < 1, f"({cf['held_red'].get('BANKNIFTY')})")
check("no old-rule counterfactual (decay did not fire)", cf["open"] == {} and cf["two_day"]["open"] == {})
st_, cf, bl = run(55500.0, NEUTRAL)
check("2nd red unsupported day: still held, held_red keeps the FIRST price", "BANKNIFTY" in st_.positions and cf["held_red"]["BANKNIFTY"]["price"] == 55300.0 and st_.positions["BANKNIFTY"].unsupported_streak == 2)
st_, cf, bl = run(54500.0, NEUTRAL)
held = (ENTRY - 54500.0) * 30
check("green + unsupported -> CONFIDENCE_DECAY closes", "BANKNIFTY" not in st_.positions and abs(st_.realised_pnl - held) < 1, f"({st_.realised_pnl:+,.0f})")
check("held pair settled: fired-at-first-red vs held", cf["held_pairs"]["closed"] == 1 and abs(cf["held_pairs"]["sum_fired"] - red1) < 1 and abs(cf["held_pairs"]["sum_held"] - held) < 1 and cf["held_red"] == {})
rec = [r for r in lines(pt.DECAY_CF_LOG) if r["event"] == "DECAY_HELD_RED_CLOSE"][-1]
check("log record: value_of_holding = held - fired", abs(rec["value_of_holding_inr"] - (held - red1)) < 1 and rec["exit_reason"] == "CONFIDENCE_DECAY", f"({rec['value_of_holding_inr']:+,.0f})")
check("2-day arm opened by the green decay exit", "BANKNIFTY" in cf["two_day"]["open"])

print("T6: held-red pair also settles when the exit is a STOP_LOSS")
reset(True)
st_, cf, bl = run(55300.0, NEUTRAL)
stop_px = ENTRY * 1.031
st_, cf, bl = run(stop_px, NEUTRAL)
check("stop-loss closes it", "BANKNIFTY" not in st_.positions)
rec = [r for r in lines(pt.DECAY_CF_LOG) if r["event"] == "DECAY_HELD_RED_CLOSE"][-1]
check("pair recorded with reason STOP_LOSS", rec["exit_reason"] == "STOP_LOSS" and cf["held_pairs"]["closed"] == 1)

print("T7: a SUPPORTED read at a loss records nothing")
reset(True)
st_, cf, bl = run(55300.0, SHORT50)
check("red but supported -> held, no held_red, streak 0", "BANKNIFTY" in st_.positions and cf["held_red"] == {} and st_.positions["BANKNIFTY"].unsupported_streak == 0)

print("T8: green unsupported day never creates held_red")
reset(True)
st_, cf, bl = run(54500.0, NEUTRAL)
check("green decay exit -> no held_red", cf["held_red"] == {} and "BANKNIFTY" not in st_.positions)

print("T9: opposite read still fires KRONOS_REV even while red; ST flip still exits; no same-day re-entry")
reset(True)
st_, cf, bl = run(55300.0, ("LONG", 0.50, "kronos", 0.0, 0.0))
check("KRONOS_REV at a loss, and the same run does NOT re-enter", "BANKNIFTY" not in st_.positions and lines(pt.TRADES_LOG)[-1]["exit_reason"] == "KRONOS_REV"
      and [r["event"] for r in lines(pt.TRADES_LOG)] == ["EXIT"])
st_, cf, bl = run(55300.0, ("LONG", 0.50, "kronos", 0.0, 0.0))
check("next day the same read DOES enter", "BANKNIFTY" in st_.positions and st_.positions["BANKNIFTY"].direction == "LONG")
reset(True)
st_, cf, bl = run(54600.0, SHORT50, st="BULL")
check("ST_FLIP (green, supported read, SuperTrend flips), no same-run round trip", "BANKNIFTY" not in st_.positions and lines(pt.TRADES_LOG)[-1]["exit_reason"] == "ST_FLIP"
      and [r["event"] for r in lines(pt.TRADES_LOG)] == ["EXIT"])
check("daily reads: nothing logged for the exit day (instrument skipped)", lines(pt.DAILY_READS_LOG) == [])
reset(True)
st_, cf, bl = run(54500.0, NEUTRAL)
st_, cf, bl = run(54400.0, SHORT50, adx=20.0, st="BULL")
check("after a decay exit, next day's qualifying read enters (gates off)", "BANKNIFTY" in st_.positions)

print("T10: --status renders; old state files load")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pt.cmd_status()
out = buf.getvalue()
check("status shows the new section", "LIVE PROFIT-GATED RULE" in out and "2-DAY ARM" in out)
reset(False)
Path(pt.DECAY_CF_FILE).write_text(json.dumps({"open": {}, "closed": 1, "sum_real": -27016.5, "sum_cf": 1006.5,
                                               "two_day": {"open": {}, "closed": 0, "sum_real": 0.0, "sum_arm": 0.0}}))
cf_old = pt._load_decay_cf()
check("old decay_cf file gains held_red / held_pairs and keeps totals", cf_old["held_red"] == {} and cf_old["held_pairs"]["closed"] == 0 and cf_old["closed"] == 1)

print("T11: backtest switches (cached reads, BANKNIFTY, Upstox input)")
cache = json.load(open("/opt/kronos_bot/research/sweepU_BANKNIFTY_signals.json"))
df_bt = pd.read_csv("/opt/kronos_bot/research/sweepU_BANKNIFTY_df.csv", parse_dates=["datetime"])
btmod.get_signal = lambda ctx, force_fallback=False: tuple(cache[str(ctx["datetime"].iloc[-1])]) if str(ctx["datetime"].iloc[-1]) in cache else ("NEUTRAL", 0.0, "x", 0.0, 0.0)
btmod.SIGNAL_CACHE_BARS = 1
tr_new = btmod.backtest_instrument("BANKNIFTY", df_bt)
saved = dict(P)
P.update(entry_gate_adx=True, entry_gate_st=True, decay_mode="immediate")
tr_old = btmod.backtest_instrument("BANKNIFTY", df_bt)
P.update(saved)
check("new config trades more than the old one", len(tr_new) > len(tr_old), f"({len(tr_new)} vs {len(tr_old)})")
dec = [t for t in tr_new if t.exit_reason == "CONFIDENCE_DECAY"]
check("under profit_only no CONFIDENCE_DECAY exit is red", all(t.pnl_inr >= 0 for t in dec), f"({len(dec)} decay exits)")
dec_old = [t for t in tr_old if t.exit_reason == "CONFIDENCE_DECAY"]
check("old config does fire decay at a loss (switch is live)", any(t.pnl_inr < 0 for t in dec_old), f"({sum(t.pnl_inr < 0 for t in dec_old)} red of {len(dec_old)})")

print()
print("ALL PASSED" if all(results) else f"FAILURES: {results.count(False)} of {len(results)}")
