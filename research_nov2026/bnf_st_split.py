"""BANKNIFTY gates-off config: how do entries that AGREE with SuperTrend compare with entries AGAINST it?
Against-SuperTrend entries are what the gates used to block.  With the ST-flip exit still on, a SHORT entered while SuperTrend is BULL
(or a LONG while BEAR) is exited at the next run by ST_FLIP unless something else fires first -- so those trades are roughly one-day trades.
Same replay as bnf_bars.py (cached reads, gates off, profit-only decay, ST-flip exit on, bar 0.45, Rs500/trade), with the SuperTrend state
at entry recorded."""
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
INST, LOT, COST, START = "BANKNIFTY", 30, 500.0, 61
P = bt.INSTRUMENT_PARAMS[INST]; BAR = 0.45
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


def load(prefix):
    df = upstox_df() if prefix == "sweepU" else pd.read_csv(RES / f"sweepY_{INST}_df.csv", parse_dates=["datetime"])
    return df, json.load(open(RES / f"{prefix}_{INST}_signals.json"))


def simulate(df, cache):
    P["kronos_conf_min"] = BAR
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cache[str(ctx["datetime"].iloc[-1])])
    st_series = {}
    trades, pos, meta = [], None, None
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            reason = pt.check_exit(INST, pos, df.iloc[:k], px, decay_mode="profit_only")
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                trades.append(dict(entry=pd.Timestamp(pos.entry_date), dir=pos.direction, reason=reason or "OPEN", pnl=pts * LOT - COST, held=k - meta["k"], **meta["m"]))
                pos = None
            continue
        read = cache.get(str(df["datetime"].iloc[k - 1]))
        if not read or read[0] == "NEUTRAL" or read[1] < BAR:
            continue
        d = read[0]
        st = bt.supertrend(df.iloc[:k], period=P["supertrend_period"], multiplier=P["supertrend_multiplier"])
        pos = pt.Position(instrument=INST, direction=d, entry_date=str(dk.date()), entry_price=px, lot_size=LOT, hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=read[1])
        meta = {"k": k, "m": {"aligned": (d == "LONG" and st == "BULL") or (d == "SHORT" and st == "BEAR"), "st": st}}
    P["kronos_conf_min"] = 0.45
    return pd.DataFrame(trades)


for prefix, nice in (("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")):
    df, cache = load(prefix)
    T = simulate(df, cache)
    print(f"\n=== {nice} | {len(T)} trades | net of Rs{COST:.0f}/trade ===")
    print(f"  {'group':<34}{'n':>4}{'win%':>6}{'avg hold d':>11}{'avg Rs':>9}{'total Rs':>11}  exit reasons")
    for lab, m in (("ALL", T.aligned | ~T.aligned), ("entry agrees with SuperTrend", T.aligned), ("entry AGAINST SuperTrend", ~T.aligned),
                   ("  against ST, SHORT", (~T.aligned) & (T.dir == "SHORT")), ("  against ST, LONG", (~T.aligned) & (T.dir == "LONG")),
                   ("  with ST, SHORT", T.aligned & (T.dir == "SHORT")), ("  with ST, LONG", T.aligned & (T.dir == "LONG"))):
        g = T[m]
        if g.empty:
            print(f"  {lab:<34}{0:>4}"); continue
        rs = ", ".join(f"{k} {v}" for k, v in g.reason.value_counts().items())
        print(f"  {lab:<34}{len(g):>4}{(g.pnl>0).mean()*100:>6.0f}{g.held.mean():>11.1f}{g.pnl.mean():>+9,.0f}{g.pnl.sum():>+11,.0f}  {rs}")
    ag = T[~T.aligned]
    print(f"  against-ST entries that were closed by ST_FLIP within 1 day: {int(((ag.reason == 'ST_FLIP') & (ag.held <= 1)).sum())} of {len(ag)}")
