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
START = 61                                    # Kronos reads begin at bar 60


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
    f = pd.DataFrame({"close": c})
    adx, st = [np.nan] * len(df), [None] * len(df)
    for k in range(START, len(df)):
        ctx = df.iloc[:k]
        adx[k] = bt.compute_adx(ctx)
        st[k] = bt.supertrend(ctx, period=P["supertrend_period"], multiplier=P["supertrend_multiplier"])
    f["adx"], f["st"] = adx, st
    return f



def simulate(df, f, cache, adx_gate, st_gate, bar, decay_mode, stop=None):
    P["kronos_conf_min"] = bar
    if stop is not None:
        P["stop_loss_pct"] = stop
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cache[str(ctx["datetime"].iloc[-1])])
    trades, pos = [], None
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            reason = pt.check_exit(INST, pos, df.iloc[:k], px, decay_mode=decay_mode)
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                trades.append(dict(entry=pd.Timestamp(pos.entry_date), dir=pos.direction, reason=reason or "OPEN", pnl=pts * LOT - COST))
                pos = None
            continue
        read = cache.get(str(df["datetime"].iloc[k - 1]))
        if not read or read[0] == "NEUTRAL" or read[1] < bar:
            continue
        d = read[0]
        if adx_gate and not f["adx"].iloc[k] >= 25: continue
        if st_gate and not ((d == "LONG" and f["st"].iloc[k] == "BULL") or (d == "SHORT" and f["st"].iloc[k] == "BEAR")): continue
        pos = pt.Position(instrument=INST, direction=d, entry_date=str(dk.date()), entry_price=px, lot_size=LOT,
                          hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=read[1])
    P["kronos_conf_min"] = BASE_BAR
    P["stop_loss_pct"] = BASE_STOP
    return pd.DataFrame(trades)




BASE_STOP = P["stop_loss_pct"]
for prefix, nice in [("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")]:
    df, cache = load(prefix)
    f = prep(df)
    span_q = (df["datetime"].iloc[-1] - df["datetime"].iloc[START]).days / 91.3
    print()
    print(f"=== {nice} | BANKNIFTY gates OFF, decay profit-only, ST-flip exit on | net of Rs{COST:.0f}/trade ===")
    print(f"  {'bar':<6}{'trades':>7}{'/qtr':>6}{'win%':>6}{'net P&L':>11}{'maxDD':>10}{'H1':>10}{'H2':>10}{'stops':>7}{'LONG':>10}{'SHORT':>10}")
    for bar in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        T = simulate(df, f, cache, False, False, bar, "profit_only")
        if T.empty:
            print(f"  {bar:<6}{0:>7}"); continue
        mid = T["entry"].sort_values().iloc[len(T) // 2]
        cum = T["pnl"].cumsum(); dd = (cum.cummax() - cum).max()
        mark = "  <- live bar" if abs(bar - 0.45) < 1e-9 else ""
        print(f"  {bar:<6}{len(T):>7}{len(T)/span_q:>6.1f}{(T.pnl>0).mean()*100:>5.0f}%{T.pnl.sum():>+11,.0f}{dd:>10,.0f}"
              f"{T[T.entry<mid].pnl.sum():>+10,.0f}{T[T.entry>=mid].pnl.sum():>+10,.0f}{int((T.reason=='STOP_LOSS').sum()):>7}"
              f"{T[T.dir=='LONG'].pnl.sum():>+10,.0f}{T[T.dir=='SHORT'].pnl.sum():>+10,.0f}{mark}")
