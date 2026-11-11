"""Tests for the intraday toolkit (no network): cost arithmetic, break-even maths, resampling, dataset cleaning, depth record format.
Run:  python test_intraday.py"""
import gzip
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import costs
import build_dataset as B
import candle_recorder as R
import depth_recorder as D

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def session(day: str, n: int = 38, start_min: int = 555, seed: int = 0, step: int = 10, price: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.Timestamp(day) + pd.to_timedelta(start_min + np.arange(n) * step, unit="min")
    c = price + np.cumsum(rng.normal(0, 0.1, n)); o = np.r_[price, c[:-1]]
    h = np.maximum(o, c) + 0.05; l = np.minimum(o, c) - 0.05
    return pd.DataFrame({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": 0, "oi": 0})


print("costs")
m = costs.CostModel()
c = costs.round_trip(m, "NIFTY", 25000, 65, slip_pts=0)
check("notional", abs(c["notional"] - 1_625_000) < 1e-6)
check("STT is 0.02% of sell turnover", abs(c["stt"] - 325.0) < 1e-6, f"({c['stt']})")
check("stamp duty is 0.002% of buy turnover", abs(c["stamp"] - 32.5) < 1e-6)
check("two flat brokerage orders", c["brokerage"] == 40.0)
exch = 2 * 1_625_000 * 0.00173 / 100; sebi = 2 * 1_625_000 * 0.0001 / 100; gst = (40 + exch + sebi) * 0.18
check("GST only on brokerage + exchange + SEBI", abs(c["gst"] - gst) < 1e-9)
check("fees add up", abs(c["fees"] - (40 + 325 + exch + sebi + 32.5 + gst)) < 1e-9, f"(Rs{c['fees']:.1f} = {c['fees_pct']:.4f}%)")
c2 = costs.round_trip(m, "NIFTY", 25000, 65, slip_pts=1.0)
check("slippage = 2 sides x 1 pt x lot", abs(c2["slip"] - 130.0) < 1e-9 and abs(c2["total"] - c["fees"] - 130.0) < 1e-9)
c3 = costs.round_trip(m, "BANKNIFTY", 60000, 30, lots=3, slip_pts=0); c1 = costs.round_trip(m, "BANKNIFTY", 60000, 30, slip_pts=0)
check("3 lots: turnover-based charges x3, brokerage stays 2 orders", abs(c3["stt"] - 3 * c1["stt"]) < 1e-9 and c3["brokerage"] == c1["brokerage"] == 40.0)

print("break-even maths")
check("m_cut(0) = sqrt(2/pi)", abs(costs.m_cut(0) - math.sqrt(2 / math.pi)) < 1e-12)
check("m_cut(1.645) ~ 2.06 (mean |s| beyond the 1.645 cutoff)", abs(costs.m_cut(1.645) - 2.0627) < 0.01, f"({costs.m_cut(1.645):.4f})")
check("required IC falls as selectivity rises", costs.required_ic(0.03, 0.2, 0) > costs.required_ic(0.03, 0.2, 1.0) > costs.required_ic(0.03, 0.2, 1.645))
check("required IC = cost / (sigma m)", abs(costs.required_ic(0.03, 0.2, 0) - 0.03 / (0.2 * math.sqrt(2 / math.pi))) < 1e-12)
check("break-even win rate, win = 2x cost -> 75%", abs(costs.breakeven_win_rate(0.03, 0.06) - 0.75) < 1e-12)

print("resampling 1m -> 10m")
one = session("2026-11-02", n=375, step=1, seed=3)
r10 = R.resample_10m(one)
check("38 buckets; first 09:15, last 15:25", len(r10) == 38 and r10.ts.iloc[0].strftime("%H:%M") == "09:15" and r10.ts.iloc[-1].strftime("%H:%M") == "15:25")
check("last bucket holds only 5 one-minute bars", abs(r10.o.iloc[-1] - one.o.iloc[-5]) < 1e-12)
check("self-consistency: 0 mismatches", R.consistency(one, r10)["px_mismatch"] == 0 and R.consistency(one, r10)["vol_mismatch"] == 0)
r10b = r10.copy(); r10b.loc[5, "c"] += 0.5
check("a corrupted bar is caught", R.consistency(one, r10b)["px_mismatch"] == 1)

print("cleaning")
frames = [session("2026-11-02", seed=1), session("2026-11-03", seed=2)]
s3 = session("2026-11-04", seed=3).drop(index=[10]).reset_index(drop=True)                     # one missing slot
s4 = session("2026-11-05", n=20, seed=4)                                                          # short session
s5 = session("2026-11-08", n=6, start_min=18 * 60 + 15, seed=5)                                   # Sunday Muhurat evening
s6 = session("2026-11-06", seed=6)
s6.loc[20, ["h", "l"]] = [s6.loc[20, "o"] - 1.0, s6.loc[20, "o"] + 1.0]                          # bad OHLC (high below low)
s6.loc[36, ["o", "h", "l", "c"]] = s6.loc[35, "c"]                                                # flat bar at 15:15 (slot 36)
raw = pd.concat(frames + [s3, s4, s5, s6, frames[0].iloc[:3]])                                   # + duplicate timestamps
d, rep = B.clean(raw)
st = d.drop_duplicates("session").set_index("session")["session_type"]
check("duplicates removed", rep["duplicate_timestamps"] == 3 and d["ts"].is_unique)
check("session types", st.loc["2026-11-02"] == "normal" and st.loc["2026-11-04"] == "normal" and st.loc["2026-11-05"] == "short"
      and st.loc["2026-11-08"] == "weekend_special", f"({dict(st)})")
check("missing slot -> gap_before on the next bar only", int(d[d.session == "2026-11-04"]["gap_before"].sum()) == 1)
check("gap bar has no return", d[(d.session == "2026-11-04") & d.gap_before]["ret_bp"].isna().all())
check("bad OHLC flagged", int(d["bad_ohlc"].sum()) == 1 and bool(d[(d.session == "2026-11-06") & (d.slot == 20)]["bad_ohlc"].iloc[0]))
fl = d[(d.session == "2026-11-06") & (d.slot == 36)].iloc[0]
nx = d[(d.session == "2026-11-06") & (d.slot == 37)].iloc[0]
check("flat bar flagged; its return and the next bar's return are not 'clean'", fl["flat"] and np.isnan(fl["ret_clean_bp"]) and np.isnan(nx["ret_clean_bp"]) and not np.isnan(nx["ret_bp"]))
check("usable = normal sessions without bad OHLC", int(d["usable"].sum()) == int(((d.session_type == "normal") & ~d.bad_ohlc).sum()) and not d[d.session_type == "short"]["usable"].any())
check("first bar of a session has no return", d[d.slot == 0]["ret_bp"].isna().all() or d[(d.slot == 0) & (d.session_type == "normal")]["ret_bp"].isna().all())
dly = pd.DataFrame({"ts": [pd.Timestamp("2026-11-02"), pd.Timestamp("2026-11-03")], "o": [frames[0].o.iloc[0], frames[1].o.iloc[0] * 1.01],
                    "h": [frames[0].h.max(), frames[1].h.max()], "l": [frames[0].l.min(), frames[1].l.min()], "c": [frames[0].c.iloc[-1], frames[1].c.iloc[-1]]})
_, rep2 = B.clean(raw, dly)
check("daily cross-check catches a 1% open mismatch", rep2["daily_crosscheck"]["o"]["over_0.05pct"] == 1 and rep2["daily_crosscheck"]["c"]["over_0.05pct"] == 0, f"({rep2['daily_crosscheck']['o']})")
check("flat-bar rate by slot reported", "15:15" in rep["flat_bar_slots"] or rep["flat_bar_rate_overall"] > 0)

print("depth record format")
q = {"last_price": 25235.0, "volume": 6164925, "oi": 12175085.0, "average_price": 25346.5, "total_buy_quantity": 219180.0, "total_sell_quantity": 229905.0,
     "last_trade_time": "1794391799858", "ohlc": {"open": 25320.0, "high": 25466.6, "low": 25210.0, "close": 25300.0},
     "depth": {"buy": [{"quantity": 65, "price": 25235.0, "orders": 1}] * 6, "sell": [{"quantity": 130, "price": 25236.5, "orders": 2}] * 5}}
rec = D.record(q, "NIFTY26NOVFUT", "2026-11-11T15:30:00.000+05:30", True)
check("future record has 5 levels per side and totals", len(rec["b"]) == 5 and len(rec["a"]) == 5 and rec["tbq"] == 219180.0 and rec["oi"] == 12175085.0 and rec["b"][0] == [25235.0, 65, 1])
check("spread computable from the record", abs(rec["a"][0][0] - rec["b"][0][0] - 1.5) < 1e-9)
ri = D.record(q, "NIFTY_IDX", "t", False)
check("index record has no depth fields", "b" not in ri and ri["ltp"] == 25235.0)
with tempfile.TemporaryDirectory() as td:
    p = Path(td); (p / "X.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")
    D.compress_day(p)
    check("compress_day: plain file replaced by .gz that reads back", not (p / "X.jsonl").exists() and len(gzip.open(p / "X.jsonl.gz", "rt").read().splitlines()) == 2)

print()
print("ALL PASSED" if all(results) else f"FAILURES: {results.count(False)} of {len(results)}")
sys.exit(0 if all(results) else 1)
