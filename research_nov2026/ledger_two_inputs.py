"""Blocked-vs-passed ledger on BOTH input sets, with identical mechanics:
  sweepU = Upstox spot index candles (zero volume; what the live bot gets)
  sweepY = yfinance index candles (non-zero volume column; what the original backtests used)
Shadow mechanics = paper_trader's own check_exit/_blocked_by; entry at the run-day close on the read through the
previous bar; independent per instrument; ignores the real one-slot rule.  Reads come from the persisted caches."""
import sys, json, logging, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, "/opt/kronos_bot/repo")
import numpy as np
import pandas as pd
import requests
import paper_trader as pt
import backtest as bt

logging.disable(logging.CRITICAL)
RES = Path("/opt/kronos_bot/research")
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}


def upstox_df(inst, years=3):
    enc = urllib.parse.quote(KEYS[inst], safe="")
    today = now.date(); start = today - timedelta(days=int(365.25 * years) + 20)
    rows, win_end = [], today + timedelta(days=1)
    while True:
        ws = max(start, win_end - timedelta(days=300))
        r = requests.get(f"https://api.upstox.com/v2/historical-candle/{enc}/day/{win_end}/{ws}", headers=H, timeout=30)
        r.raise_for_status(); rows += r.json()["data"]["candles"]
        if ws <= start: break
        win_end = ws + timedelta(days=2)
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    df = df.drop(columns=["oi"]).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    return df[df["datetime"].dt.normalize() < pd.Timestamp(today)].reset_index(drop=True)


def ledger(prefix):
    out = []
    for inst in KEYS:
        df = upstox_df(inst) if prefix == "sweepU" else pd.read_csv(RES / f"sweepY_{inst}_df.csv", parse_dates=["datetime"])
        cache = json.load(open(RES / f"{prefix}_{inst}_signals.json"))
        todo = [i for i in range(bt.MIN_CONTEXT_BARS, len(df) - 1) if str(df["datetime"].iloc[i]) not in cache]
        for i in todo:
            d, c, s, rr, tgt = bt.get_signal(df.iloc[: i + 1].copy(), False)
            cache[str(df["datetime"].iloc[i])] = [d, float(c), s, float(rr), float(tgt)]
        if todo:
            json.dump(cache, open(RES / f"{prefix}_{inst}_signals.json", "w"))
        p = bt.INSTRUMENT_PARAMS[inst]; lot = pt.LOT_SIZES[inst]
        pt.get_signal = lambda ctx, force_fallback=False, _c=cache: tuple(_c[str(ctx["datetime"].iloc[-1])])
        pos = None; kind = None; n = len(df)
        for k in range(bt.MIN_CONTEXT_BARS + 1, n):
            ctx = df.iloc[:k].copy(); px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
            if pos is not None:
                reason = pt.check_exit(inst, pos, ctx, px)
                if reason or k == n - 1:
                    pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                    out.append(dict(inst=inst, kind=kind, dir=pos.direction, entry=pd.Timestamp(pos.entry_date),
                                    exit_d=dk, reason=reason or "OPEN", pnl=pts * lot))
                    pos = None
                continue
            key = str(ctx["datetime"].iloc[-1])
            if key not in cache:
                continue
            d_, c_, *_ = cache[key]
            diag = dict(adx=bt.compute_adx(ctx), st=bt.supertrend(ctx, period=p["supertrend_period"], multiplier=p["supertrend_multiplier"]),
                        direction=d_, confidence=c_)
            bb = pt._blocked_by(diag, p)
            if bb or (d_ != "NEUTRAL" and c_ >= p["kronos_conf_min"]):
                kind = bb or "PASSED"
                pos = pt.Position(instrument=inst, direction=d_, entry_date=str(dk.date()), entry_price=px, lot_size=lot,
                                  hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=c_)
    return pd.DataFrame(out)


rng = np.random.default_rng(7)
RES_ALL = {}
for prefix, nice in [("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE volume input (original backtests)")]:
    try:
        L = ledger(prefix)
    except FileNotFoundError as e:
        print(f"\n=== {nice}: missing file {e} ===")
        continue
    RES_ALL[prefix] = L
    mid = L["entry"].sort_values().iloc[len(L) // 2]
    print(f"\n=== {nice} | {len(L)} trades | halves split {mid.date()} ===")
    print(f"  {'group':<8}{'n':>4}{'win%':>6}{'P&L':>11}{'avg':>9}{'H1 n/P&L':>16}{'H2 n/P&L':>16}")
    for g in ["PASSED", "ADX", "ST", "ADX+ST"]:
        x = L[L.kind == g]
        if x.empty:
            print(f"  {g:<8}{0:>4}"); continue
        a, b = x[x.entry < mid], x[x.entry >= mid]
        print(f"  {g:<8}{len(x):>4}{(x.pnl>0).mean()*100:>5.0f}%{x.pnl.sum():>+11,.0f}{x.pnl.mean():>+9,.0f}"
              f"{len(a):>6}/{a.pnl.sum():>+8,.0f}{len(b):>6}/{b.pnl.sum():>+8,.0f}")
    a = L[L.kind != "PASSED"].pnl.values; b = L[L.kind == "PASSED"].pnl.values
    obs = a.mean() - b.mean(); se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    pool = np.concatenate([a, b]); cnt = 0
    for _ in range(20000):
        rng.shuffle(pool)
        cnt += pool[:len(a)].mean() - pool[len(a):].mean() >= obs
    bl = L[L.kind != "PASSED"]; ps = L[L.kind == "PASSED"]
    half = lambda x, lo: x[(x.entry < mid) if lo else (x.entry >= mid)]
    print(f"  BLOCKED n={len(a)} avg {a.mean():+,.0f} vs PASSED n={len(b)} avg {b.mean():+,.0f} | diff {obs:+,.0f}/trade, Welch t {obs/se:.2f}, one-sided perm p {cnt/20000:.3f}")
    print(f"  blocked minus passed by half: H1 {half(bl,True).pnl.mean()-half(ps,True).pnl.mean():+,.0f} | H2 {half(bl,False).pnl.mean()-half(ps,False).pnl.mean():+,.0f} per trade")
    print(f"  all confidence-clearing reads, no trend gates: n={len(L)} P&L {L.pnl.sum():+,.0f} avg {L.pnl.mean():+,.0f}")

if len(RES_ALL) == 2:
    print("\n=== VERDICT INPUTS ===")
    for k, L in RES_ALL.items():
        bl, ps = L[L.kind != "PASSED"], L[L.kind == "PASSED"]
        print(f"  {k}: blocked avg {bl.pnl.mean():+,.0f} (n={len(bl)}) vs passed avg {ps.pnl.mean():+,.0f} (n={len(ps)})")
