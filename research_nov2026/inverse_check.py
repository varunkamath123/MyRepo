"""Should we do the OPPOSITE of the Kronos signal?  The live book lost 72k over 10 trades while BANKNIFTY and NIFTY rose, so a literal sign flip of those trades would show +72k (before costs, and with exits that
would not have fired at the same prices).  Test it properly on the cached reads, extended to today:
 1  by calendar quarter: mean next-5-day return in the read's direction (negative = following the read lost), hit rate, rank IC  (both indices)
 2  full strategy replay (gates off, profit-only decay, ST-flip exit, bar 0.45, Rs500/trade, the BANKNIFTY live config) with the REAL reads and with every LONG<->SHORT swapped, so the inverted trades
    get their own stops / exits: totals by half-year and for the live window since 2026-07-01
Missing reads (cache ends mid-October) are computed with Kronos for the extra days; the extended cache is saved."""
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
RES = Path("/opt/kronos_bot/research"); COST, LOT_, START = 500.0, {"BANKNIFTY": 30, "NIFTY": 65}, 61
IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}
KEYS = {"BANKNIFTY": "NSE_INDEX|Nifty Bank", "NIFTY": "NSE_INDEX|Nifty 50"}


def upstox_df(inst):
    enc = urllib.parse.quote(KEYS[inst], safe=""); today = now.date(); start = today - timedelta(days=int(365.25 * 3) + 20)
    rows, win_end = [], today + timedelta(days=1)
    while True:
        ws = max(start, win_end - timedelta(days=300))
        r = requests.get(f"https://api.upstox.com/v2/historical-candle/{enc}/day/{win_end}/{ws}", headers=H, timeout=30); r.raise_for_status(); rows += r.json()["data"]["candles"]
        if ws <= start: break
        win_end = ws + timedelta(days=2)
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    df = df.drop(columns=["oi"]).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    df = df[df["datetime"].dt.dayofweek < 5]                                   # drop the Sunday special session (the cache never had it for earlier years either)
    return df[df["datetime"].dt.normalize() < pd.Timestamp(today)].reset_index(drop=True)


def extend(inst, df):
    path = RES / f"sweepU_{inst}_signals.json"; cache = json.load(open(path)); miss = 0
    for i in range(bt.MIN_CONTEXT_BARS, len(df)):
        k = str(df["datetime"].iloc[i])
        if k not in cache:
            d, c, s, rr, tgt = bt.get_signal(df.iloc[: i + 1].copy(), False); cache[k] = [d, float(c), s, float(rr), float(tgt)]; miss += 1
    if miss: json.dump(cache, open(path, "w"))
    return cache, miss


def simulate(inst, df, cache, invert):
    P = bt.INSTRUMENT_PARAMS[inst]; P["kronos_conf_min"] = 0.45
    flip = {"LONG": "SHORT", "SHORT": "LONG", "NEUTRAL": "NEUTRAL"}
    cc = {k: ([flip[v[0]]] + v[1:]) if invert else v for k, v in cache.items()}
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cc[str(ctx["datetime"].iloc[-1])])
    out, pos, lot = [], None, LOT_[inst]
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            reason = pt.check_exit(inst, pos, df.iloc[:k], px, decay_mode="profit_only")
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                out.append(dict(entry=pd.Timestamp(pos.entry_date), dir=pos.direction, reason=reason or "OPEN", pnl=pts * lot - COST)); pos = None
            continue
        read = cc.get(str(df["datetime"].iloc[k - 1]))
        if not read or read[0] == "NEUTRAL" or read[1] < 0.45: continue
        pos = pt.Position(instrument=inst, direction=read[0], entry_date=str(dk.date()), entry_price=px, lot_size=lot, hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=read[1])
    return pd.DataFrame(out)


for inst in ("BANKNIFTY", "NIFTY"):
    df = upstox_df(inst); cache, miss = extend(inst, df)
    print(f"\n{'=' * 112}\n{inst}: {len(df)} bars to {df.datetime.iloc[-1].date()}, {miss} reads computed to extend the cache\n{'=' * 112}")
    rows = []
    for i in range(bt.MIN_CONTEXT_BARS, len(df) - 5):
        r = cache.get(str(df["datetime"].iloc[i]))
        if r and r[0] != "NEUTRAL":
            d = 1 if r[0] == "LONG" else -1; c0 = float(df.close.iloc[i])
            rows.append(dict(date=df["datetime"].iloc[i], d=d, conf=r[1], fwd=float(df.close.iloc[i + 5]) / c0 - 1))
    X = pd.DataFrame(rows); X["q"] = X.date.dt.to_period("Q"); X["pnl"] = X.d * X.fwd * 100
    print("[1] following the read, by quarter (5-day return in the read's direction):")
    print(f"  {'quarter':<8}{'n':>5}{'mean %':>8}{'hit%':>6}{'LONG n/mean':>14}{'SHORT n/mean':>14}")
    for q, g in X.groupby("q"):
        L, S_ = g[g.d == 1], g[g.d == -1]
        print(f"  {str(q):<8}{len(g):>5}{g.pnl.mean():>+8.2f}{(g.pnl>0).mean()*100:>6.0f}{len(L):>6}/{L.pnl.mean() if len(L) else float('nan'):>+6.2f}{len(S_):>8}/{S_.pnl.mean() if len(S_) else float('nan'):>+6.2f}")
    h1, rec = X[X.date < "2026-07-01"], X[X.date >= "2026-07-01"]
    se = h1.pnl.std() / np.sqrt(len(h1)); se2 = rec.pnl.std() / np.sqrt(max(len(rec), 1))
    print(f"  before 2026-07: n={len(h1)} mean {h1.pnl.mean():+.2f}% (se {se:.2f}) | since 2026-07-01: n={len(rec)} mean {rec.pnl.mean():+.2f}% (se {se2:.2f}) -> difference {rec.pnl.mean()-h1.pnl.mean():+.2f} "
          f"= {(rec.pnl.mean()-h1.pnl.mean())/np.sqrt(se**2+se2**2):+.1f} standard errors")
    print("\n[2] full strategy replay (own exits for the inverted trades), Rs per period of ENTRY:")
    R, I = simulate(inst, df, cache, False), simulate(inst, df, cache, True)
    for T_ in (R, I):
        T_["per"] = T_.entry.dt.year.astype(str) + "H" + np.where(T_.entry.dt.month <= 6, "1", "2")
    pers = sorted(set(R.per) | set(I.per))
    print(f"  {'period':<8}{'real n':>7}{'real Rs':>10}{'inverted n':>11}{'inverted Rs':>12}")
    for p_ in pers:
        a, b = R[R.per == p_], I[I.per == p_]
        print(f"  {p_:<8}{len(a):>7}{a.pnl.sum():>+10,.0f}{len(b):>11}{b.pnl.sum():>+12,.0f}")
    live = pd.Timestamp("2026-07-01")
    a, b = R[R.entry >= live], I[I.entry >= live]
    print(f"  TOTAL    {len(R):>7}{R.pnl.sum():>+10,.0f}{len(I):>11}{I.pnl.sum():>+12,.0f}")
    print(f"  since 2026-07-01 (the live window): real {len(a)} trades Rs{a.pnl.sum():+,.0f} | inverted {len(b)} trades Rs{b.pnl.sum():+,.0f}  (inverted LONG {int((b.dir=='LONG').sum())} / SHORT {int((b.dir=='SHORT').sum())})")
