"""Paper-trade review: turn every trade into a lesson.  Reads the live state/logs of both books and prints
  1  BANKNIFTY Kronos book: ledger (entry read, predicted vs actual, exit reason, hold) + what each exit saved or cost (price k days later)
  2  Stock book: every position with return, NIFTY-hedged return, entry price vs that day's official close, Kronos A/B, cohort by entry day
  3  The standing hypotheses and how many paper trades have tested each so far (journal.md has the decision rules)
  python trade_review.py [--save]      --save also writes /opt/kronos_bot/data/reviews/review_<date>.txt
Run it after the close (cron 16:30 IST): the quotes then equal the official closes."""
from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import paper_trader as pt                       # noqa: E402
import stock_book as sb                         # noqa: E402
from brokers.upstox_data import load_daily_ohlcv, get_ltp   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SECTOR = {**{s: "banks/fin" for s in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "HDFCLIFE", "SBILIFE", "JIOFIN")},
          **{s: "autos" for s in ("M&M", "MARUTI", "BAJAJ-AUTO", "EICHERMOT", "TATAMOTORS", "HEROMOTOCO")},
          **{s: "it" for s in ("TCS", "INFY", "HCLTECH", "WIPRO", "TECHM")}, **{s: "consumer" for s in ("ITC", "HINDUNILVR", "NESTLEIND", "TITAN", "TRENT", "BRITANNIA", "TATACONSUMER")}}


def index_series(inst: str) -> pd.Series:
    d = load_daily_ohlcv(inst, bars=215)
    d = d[d["datetime"].dt.dayofweek < 5]
    s = pd.Series(d["close"].values, index=d["datetime"].dt.normalize())
    today = pd.Timestamp(datetime.now(IST).date())
    if today not in s.index:
        try:
            s.loc[today] = float(get_ltp(inst))
        except Exception:
            pass
    return s.sort_index()


def ledger() -> list[dict]:
    ent, out = {}, []
    for l in open(ROOT / "paper_trades.jsonl"):
        r = json.loads(l)
        if r["event"] == "ENTRY":
            ent[(r["instrument"], r["entry_date"])] = r
        elif r["event"] == "EXIT":
            e = ent.get((r["instrument"], r["entry_date"]), {})
            out.append({**r, "conf": e.get("confidence"), "pred": e.get("predicted_profit_inr", r.get("predicted_profit_inr")), "blocked": e.get("would_have_been_blocked")})
            ent.pop((r["instrument"], r["entry_date"]), None)
    return out, ent


def section_index_book():
    closed, openp = ledger()
    print("=" * 118 + "\n1. KRONOS INDEX BOOK: every trade, what was predicted, what happened, what the exit saved or cost\n" + "=" * 118)
    ser = {i: index_series(i) for i in ("NIFTY", "BANKNIFTY")}
    print(f"  {'instr':<10}{'dir':<6}{'entry':<11}{'conf':>5}{'hold':>5}{'pred Rs':>9}{'actual Rs':>10}  {'exit':<17}{'+1d':>9}{'+3d':>9}{'+5d':>9}  (exit value: P&L change had we held k more days; negative = the exit saved us)")
    tot = {"pred": 0.0, "act": 0.0}
    for t in closed:
        s = ser[t["instrument"]]; sd = 1 if t["direction"] == "LONG" else -1
        ed = pd.Timestamp(t["exit_date"]); hold = (ed - pd.Timestamp(t["entry_date"])).days
        later = s[s.index > ed]; ev = []
        for k in (1, 3, 5):
            ev.append(f"{sd * (later.iloc[k - 1] - t['exit_price']) * t['lot_size']:>+9,.0f}" if len(later) >= k else f"{'-':>9}")
        pr = t["pred"] if t["pred"] is not None else float("nan")
        tot["pred"] += pr if pr == pr else 0; tot["act"] += t["pnl_inr"]
        print(f"  {t['instrument']:<10}{t['direction']:<6}{t['entry_date']:<11}{(t['conf'] or 0):>5.2f}{hold:>4}d{pr:>+9,.0f}{t['pnl_inr']:>+10,.0f}  {t['exit_reason']:<17}" + "".join(ev) + ("  [would have been blocked: " + "+".join(t["blocked"]) + "]" if t["blocked"] else ""))
    print(f"  total: predicted Rs{tot['pred']:+,.0f} vs actual Rs{tot['act']:+,.0f}")
    for (inst, ed), e in openp.items():
        px = float(ser[inst].iloc[-1]); sd = 1 if e["direction"] == "LONG" else -1
        print(f"  OPEN {inst} {e['direction']} since {ed} @ {e['entry_price']:,.1f}, conf {e.get('confidence', 0):.2f}, predicted target {e.get('predicted_target', 0):,.0f} "
              f"(R:R {e.get('predicted_risk_reward', 0):.1f}), now {px:,.1f}: unrealised Rs{sd * (px - e['entry_price']) * e['lot_size']:+,.0f}" + (f"  [would have been blocked: {'+'.join(e['would_have_been_blocked'])}]" if e.get("would_have_been_blocked") else ""))
    by = {}
    for t in closed:
        by.setdefault(t["exit_reason"], []).append(t["pnl_inr"])
    print("  by exit reason: " + " | ".join(f"{k}: n={len(v)} total Rs{sum(v):+,.0f}" for k, v in sorted(by.items())))
    by = {}
    for t in closed:
        by.setdefault(t["direction"], []).append(t["pnl_inr"])
    print("  by direction:   " + " | ".join(f"{k}: n={len(v)} total Rs{sum(v):+,.0f}" for k, v in sorted(by.items())))


def section_stock_book():
    print("\n" + "=" * 118 + "\n2. STOCK BOOK: per position, with the market stripped out and the entry convention audited\n" + "=" * 118)
    st = sb._load_state(); uni = {u["symbol"]: u for u in sb._load_universe()}
    q = sb.load_quotes([u["key"] for u in uni.values()])
    nf = index_series("NIFTY"); nnow = float(nf.iloc[-1])
    rows = []
    for book, d in (("long", st["positions"]), ("short", st["shorts"])):
        for sym, p in d.items():
            px = q[uni[sym]["key"]]["px"]; sd = p.get("side", 1)
            ed = pd.Timestamp(p["entry_date"]); n0 = nf.asof(ed) if ed in nf.index else np.nan
            ret = sd * (px / p["entry_price"] - 1) * 100
            hedged = ret - sd * ((nnow / n0 - 1) * 100) if n0 == n0 else np.nan
            gap = np.nan
            try:
                day = sb.load_daily(uni[sym]["key"], bars=40); day["d"] = day["datetime"].dt.normalize()
                cl = day[day["d"] == ed]["close"]
                gap = (p["entry_price"] / float(cl.iloc[0]) - 1) * 100 if len(cl) else np.nan      # entry price vs that day's official close
            except Exception:
                pass
            rows.append(dict(book=book, sym=sym, entry=p["entry_date"], entry_px=p["entry_price"], px=px, ret=ret, hedged=hedged, gap=gap, volx=p["volx"], slot=p["slot"],
                             kdir=p["kronos_dir"], kconf=p["kronos_conf"], sector=SECTOR.get(sym, "other"), stop=p["entry_price"] - sd * sb.STOP_ATR * p["atr"], lot=p["lot"]))
    R = pd.DataFrame(rows)
    if R.empty:
        print("  no open positions"); return
    print(f"  {'':<5}{'symbol':<11}{'entry':<11}{'ret%':>7}{'vs NIFTY%':>10}{'entry vs close%':>16}{'volx':>6}{'kronos':>14}{'sector':>11}{'to stop%':>9}{'book':>9}")
    for _, r in R.sort_values(["entry", "ret"], ascending=[True, False]).iterrows():
        print(f"  {r.book:<5}{r.sym:<11}{r.entry:<11}{r.ret:>+7.2f}{r.hedged:>+10.2f}{r.gap:>+16.2f}{r.volx:>6.2f}{(str(r.kdir) + '/' + str(r.kconf)):>14}{r.sector:>11}{(r.px / r.stop - 1) * 100:>+9.1f}{'CAPITAL' if r.slot else 'signal':>9}")
    L = R[R.book == "long"]
    print(f"\n  longs: n={len(L)}, mean return {L.ret.mean():+.2f}%, mean vs NIFTY {L.hedged.mean():+.2f}%, winners {int((L.ret > 0).sum())}/{len(L)}")
    print(f"  entry price vs that day's official close: mean {L.gap.mean():+.3f}%, median {L.gap.median():+.3f}%, range {L.gap.min():+.2f}% .. {L.gap.max():+.2f}% (n={int(L.gap.notna().sum())}); "
          f"for a long a NEGATIVE number means the 15:25 print was below the close (a better fill than the research's close convention)")
    L = L.assign(ret_close=(L.px / (L.entry_px / (1 + L.gap / 100)) - 1) * 100)
    print(f"  restated as if filled at the official close of the entry day (decide ~15:25, fill in the closing auction): mean {L.ret_close.mean():+.2f}% vs booked {L.ret.mean():+.2f}%"
          f"  -> the booked paper entries are {L.ret.mean() - L.ret_close.mean():+.2f}% per trade better than a realistic close fill")
    print("  by entry day (cohort):  " + " | ".join(f"{d}: n={len(g)} mean {g.ret.mean():+.2f}% (vs NIFTY {g.hedged.mean():+.2f}%)" for d, g in L.groupby("entry")))
    print("  by sector:              " + " | ".join(f"{s}: n={len(g)} mean {g.ret.mean():+.2f}%" for s, g in L.groupby("sector")))
    print("  by Kronos read:         " + " | ".join(f"{k}: n={len(g)} mean {g.ret.mean():+.2f}%" for k, g in L.groupby("kdir")))
    L = L.assign(vb=pd.cut(L.volx, [0, 2.0, 2.5, 99], labels=["1.5-2.0x", "2.0-2.5x", ">2.5x"]))
    print("  by breakout volume:     " + " | ".join(f"{k}: n={len(g)} mean {g.ret.mean():+.2f}%" for k, g in L.groupby("vb", observed=True)))
    print("  nearest stops:          " + ", ".join(f"{r.sym} {(r.px / r.stop - 1) * 100:+.1f}%" for _, r in L.sort_values("ret").head(3).iterrows()))
    for k, s in st["stats"].items():
        if s["n"]:
            print(f"  CLOSED {k}: n={s['n']}, wins {s['wins']}, net Rs{s['pnl_inr']:+,.0f}, {s['sum_ret_pct'] / s['n']:+.2f}%/trade, hedged {s['sum_hedged_pct'] / s['n']:+.2f}%/trade")


def main():
    section_index_book(); section_stock_book()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.save:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main()
        txt = buf.getvalue(); print(txt)
        d = Path("/opt/kronos_bot/data/reviews"); d.mkdir(parents=True, exist_ok=True)
        (d / f"review_{datetime.now(IST).date().isoformat()}.txt").write_text(txt)
    else:
        main()
