"""BANKNIFTY only: Kronos + complementary triggers, measured on trades/quarter and net P&L.
Decisions use information available at the ~14:55 run (bars through k-1 for Kronos, today's price ~ close_k for
price triggers); entry at close_k; exits checked at later closes.  One position at a time.  Rs500 round-trip cost.
Kronos-type entries are managed by paper_trader.check_exit (live rules); mean-reversion entries by their own exits.
All trigger parameters are TEXTBOOK values fixed here -- nothing is tuned; 12 variants -> beware multiple looks."""
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
INST, LOT, COST = "BANKNIFTY", 30, 500.0
P = bt.INSTRUMENT_PARAMS[INST]
BASE_BAR = P["kronos_conf_min"]
IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}
START = 200                                   # SMA200 warm-up; every variant uses the same window


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
    path = RES / f"{prefix}_{INST}_signals.json"
    cache = json.load(open(path))
    miss = [i for i in range(bt.MIN_CONTEXT_BARS, len(df) - 1) if str(df["datetime"].iloc[i]) not in cache]
    for i in miss:
        d, c, s, rr, tgt = bt.get_signal(df.iloc[: i + 1].copy(), False)
        cache[str(df["datetime"].iloc[i])] = [d, float(c), s, float(rr), float(tgt)]
    if miss:
        json.dump(cache, open(path, "w"))
    return df, cache


def rsi_wilder(close, n=2):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def prep(df):
    c = df["close"]
    f = pd.DataFrame({"close": c, "rsi2": rsi_wilder(c, 2), "sma5": c.rolling(5).mean(), "sma50": c.rolling(50).mean(),
                      "sma200": c.rolling(200).mean(), "min5": c.rolling(5).min(), "max5": c.rolling(5).max()})
    adx, st = [np.nan] * len(df), [None] * len(df)
    for k in range(START, len(df)):
        ctx = df.iloc[:k]
        adx[k] = bt.compute_adx(ctx)
        st[k] = bt.supertrend(ctx, period=P["supertrend_period"], multiplier=P["supertrend_multiplier"])
    f["adx"], f["st"] = adx, st
    return f


VARIANTS = [
    ("K live-like (gates on, bar .45)",       dict(k=(True, 0.45))),
    ("K gates off, bar .45",                  dict(k=(False, 0.45))),
    ("K gates off, bar .35",                  dict(k=(False, 0.35))),
    ("K gates off, bar .25",                  dict(k=(False, 0.25))),
    ("RSI2 mean-reversion alone",             dict(mr=("rsi2",))),
    ("Pullback-in-trend alone",               dict(mr=("pb",))),
    ("RSI2 + Pullback (no Kronos)",           dict(mr=("rsi2", "pb"))),
    ("RSI2 AND Kronos not opposed",           dict(mr=("rsi2",), need="not_opposed")),
    ("RSI2 AND Kronos agrees (>=.25)",        dict(mr=("rsi2",), need="agree")),
    ("K(off,.45) OR RSI2",                    dict(k=(False, 0.45), mr=("rsi2",))),
    ("K(off,.45) OR RSI2 OR Pullback",        dict(k=(False, 0.45), mr=("rsi2", "pb"))),
    ("K(off,.35) OR RSI2 OR Pullback",        dict(k=(False, 0.35), mr=("rsi2", "pb"))),
]


def mr_signal(f, k, kinds):
    r, c = f.iloc[k], f["close"].iloc[k]
    for kind in kinds:
        if kind == "rsi2":
            if r["rsi2"] < 10 and c > r["sma200"]: return "LONG", "RSI2"
            if r["rsi2"] > 90 and c < r["sma200"]: return "SHORT", "RSI2"
        if kind == "pb":
            if c > r["sma50"] and c <= r["min5"]: return "LONG", "PB"
            if c < r["sma50"] and c >= r["max5"]: return "SHORT", "PB"
    return None


def simulate(df, f, cache, spec):
    kbar = spec["k"][1] if "k" in spec else BASE_BAR
    P["kronos_conf_min"] = kbar                      # check_exit's decay uses the same bar
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cache[str(ctx["datetime"].iloc[-1])])
    trades, pos, typ, held = [], None, None, 0
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            held += 1
            pnl_pct = ((px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)) / pos.entry_price
            reason = None
            if typ == "K":
                reason = pt.check_exit(INST, pos, df.iloc[:k], px)
            else:
                r = f.iloc[k]
                if pnl_pct <= -P["stop_loss_pct"]: reason = "STOP_LOSS"
                elif pos.direction == "LONG" and (r["rsi2"] > 65 or px > r["sma5"]): reason = "MR_EXIT"
                elif pos.direction == "SHORT" and (r["rsi2"] < 35 or px < r["sma5"]): reason = "MR_EXIT"
                elif held >= 7: reason = "TIME_EXIT"
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                trades.append(dict(entry=pd.Timestamp(pos.entry_date), exit=dk, typ=typ, dir=pos.direction, reason=reason or "OPEN",
                                   pnl=pts * LOT - COST))
                pos = None
            continue
        read = cache.get(str(df["datetime"].iloc[k - 1]))
        sig = None
        if "k" in spec and read:
            d, c = read[0], read[1]
            if d != "NEUTRAL" and c >= kbar:
                adx_ok, st_ok = f["adx"].iloc[k] >= 25, ((d == "LONG" and f["st"].iloc[k] == "BULL") or (d == "SHORT" and f["st"].iloc[k] == "BEAR"))
                if not spec["k"][0] or (adx_ok and st_ok):
                    sig = (d, "K")
        if sig is None and "mr" in spec:
            m = mr_signal(f, k, spec["mr"])
            if m:
                need = spec.get("need")
                if need and read:
                    d, c = read[0], read[1]
                    opp = "SHORT" if m[0] == "LONG" else "LONG"
                    if need == "not_opposed" and d == opp and c >= BASE_BAR: m = None
                    if need == "agree" and not (d == m[0] and c >= 0.25): m = None
                elif need and not read:
                    m = None
                if m: sig = m
        if sig:
            d, typ = sig
            pos = pt.Position(instrument=INST, direction=d, entry_date=str(dk.date()), entry_price=px, lot_size=LOT,
                              hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=(read[1] if read else 0.0))
            held = 0
    P["kronos_conf_min"] = BASE_BAR
    return pd.DataFrame(trades)


for prefix, nice in [("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")]:
    df, cache = load(prefix)
    f = prep(df)
    span_q = (df["datetime"].iloc[-1] - df["datetime"].iloc[START]).days / 91.3
    print(f"\n=== {nice} | BANKNIFTY {df['datetime'].iloc[START].date()} -> {df['datetime'].iloc[-1].date()} ({span_q:.1f} quarters) | net of Rs{COST:.0f}/trade ===")
    print(f"  {'variant':<34}{'trades':>7}{'/qtr':>6}{'win%':>6}{'net P&L':>11}{'avg':>8}{'maxDD':>10}{'H1':>10}{'H2':>10}")
    for name, spec in VARIANTS:
        T = simulate(df, f, cache, spec)
        if T.empty:
            print(f"  {name:<34}{0:>7}"); continue
        mid = T["entry"].sort_values().iloc[len(T) // 2]
        cum = T["pnl"].cumsum(); dd = (cum.cummax() - cum).max()
        print(f"  {name:<34}{len(T):>7}{len(T)/span_q:>6.1f}{(T.pnl>0).mean()*100:>5.0f}%{T.pnl.sum():>+11,.0f}{T.pnl.mean():>+8,.0f}{dd:>10,.0f}"
              f"{T[T.entry<mid].pnl.sum():>+10,.0f}{T[T.entry>=mid].pnl.sum():>+10,.0f}")
