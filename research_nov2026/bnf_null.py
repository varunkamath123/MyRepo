"""BANKNIFTY: how much of the result comes from the Kronos READ vs the exit rules around it?
Same simulator as bnf_bars.py (gates off, bar .45, profit-gated decay, ST-flip exit, stop 3%, Rs500/trade).
Only the read stream is changed:
  real          : the actual cached Kronos reads
  inverted      : every LONG<->SHORT swapped (direction carries info => this should do much worse)
  always LONG / always SHORT : direction-blind; Kronos only supplies WHEN (|forecast move| big enough)
  circular shift: reads re-paired with the wrong days (random offsets >=20 bars); keeps the read frequency, clustering
                  and confidence distribution, destroys any link to the price path that followed"""
import sys, json, logging, urllib.parse, time
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
INST, LOT, COST = "BANKNIFTY", 30, 500.0
P = bt.INSTRUMENT_PARAMS[INST]
BAR = 0.45
START = 61
N_SHIFT = int(sys.argv[1]) if len(sys.argv) > 1 else 100
IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}


def upstox_df():
    enc = urllib.parse.quote("NSE_INDEX|Nifty Bank", safe="")
    today = now.date(); start = today - timedelta(days=int(365.25 * 3) + 20)
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


def simulate(df, cache):
    P["kronos_conf_min"] = BAR
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cache[str(ctx["datetime"].iloc[-1])])
    trades, pos = [], None
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            reason = pt.check_exit(INST, pos, df.iloc[:k], px, decay_mode="profit_only")
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                trades.append(dict(entry=pd.Timestamp(pos.entry_date), dir=pos.direction, reason=reason or "OPEN", pnl=pts * LOT - COST))
                pos = None
            continue
        read = cache.get(str(df["datetime"].iloc[k - 1]))
        if not read or read[0] == "NEUTRAL" or read[1] < BAR:
            continue
        pos = pt.Position(instrument=INST, direction=read[0], entry_date=str(dk.date()), entry_price=px, lot_size=LOT,
                          hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=read[1])
    return pd.DataFrame(trades)


def summarize(T):
    if T.empty:
        return 0, 0.0, 0.0, 0.0
    cum = T["pnl"].cumsum()
    return len(T), T.pnl.sum(), (T.pnl > 0).mean() * 100, (cum.cummax() - cum).max()


def transform(cache, keys, how):
    if how == "real":
        return cache
    out = {}
    for k in keys:
        d, c, s, rr, tgt = cache[k]
        if how == "inverted":
            d = {"LONG": "SHORT", "SHORT": "LONG"}.get(d, d)
        elif how == "always LONG" and d != "NEUTRAL":
            d = "LONG"
        elif how == "always SHORT" and d != "NEUTRAL":
            d = "SHORT"
        out[k] = [d, c, s, rr, tgt]
    return out


for prefix, nice in [("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")]:
    df = upstox_df() if prefix == "sweepU" else pd.read_csv(RES / f"sweepY_{INST}_df.csv", parse_dates=["datetime"])
    cache = json.load(open(RES / f"{prefix}_{INST}_signals.json"))
    keys = [str(x) for x in df["datetime"] if str(x) in cache]
    vals = [cache[k] for k in keys]
    n = len(keys)
    span_q = (df["datetime"].iloc[-1] - df["datetime"].iloc[START]).days / 91.3
    print(f"\n=== {nice} | {n} cached reads | {span_q:.1f} quarters | net of Rs{COST:.0f}/trade ===", flush=True)
    print(f"  {'read stream':<22}{'trades':>7}{'/qtr':>6}{'win%':>6}{'net P&L':>11}{'maxDD':>10}")
    t0 = time.time()
    base = None
    for how in ("real", "inverted", "always LONG", "always SHORT"):
        T = simulate(df, transform(cache, keys, how))
        nt, pnl, win, dd = summarize(T)
        if how == "real":
            base = pnl
        print(f"  {how:<22}{nt:>7}{nt/span_q:>6.1f}{win:>5.0f}%{pnl:>+11,.0f}{dd:>10,.0f}", flush=True)
    print(f"  (4 runs took {time.time()-t0:.0f}s)", flush=True)
    rng = np.random.default_rng(11)
    res = []
    for _ in range(N_SHIFT):
        s = int(rng.integers(20, n - 20))
        shifted = {keys[i]: vals[(i + s) % n] for i in range(n)}
        nt, pnl, win, dd = summarize(simulate(df, shifted))
        res.append((nt, pnl))
    r = np.array(res)
    print(f"  circular-shift null ({N_SHIFT} draws): trades/qtr median {np.median(r[:,0])/span_q:.1f} | net P&L median {np.median(r[:,1]):+,.0f}, "
          f"mean {r[:,1].mean():+,.0f}, 5th-95th pct {np.percentile(r[:,1],5):+,.0f} .. {np.percentile(r[:,1],95):+,.0f}", flush=True)
    print(f"  real net {base:+,.0f} beats {(r[:,1] < base).mean()*100:.0f}% of shifted draws (one-sided p = {(r[:,1] >= base).mean():.3f})", flush=True)
