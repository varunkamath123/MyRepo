"""Is Kronos HINDERING the BANKNIFTY book?  Same replay engine as inverse_check.py (live config: bar 0.45, gates off, profit-only decay, ST-flip exit, Rs500/trade, own exits for every variant),
same cached Kronos reads.  The entry DAYS are held at the days Kronos was confident enough to trade (non-NEUTRAL, conf >= 0.45); only the DIRECTION (or, in the last row, the days too) is replaced:
  REAL        Kronos direction
  ALWAYS LONG every eligible read taken long (no model at all: just 'buy when Kronos says there is a read')
  TREND       direction = sign of the 20-day price change (a no-model trend rule)
  RANDOM DIR  random direction on the same days (150 draws -> distribution)
  RANDOM DAYS random days (same entry rate) and random direction (150 draws)
If REAL does not beat these on Rs per trade, the Kronos read is not adding anything to the book, and if it is below them it is subtracting.  Reported for the whole replay and for the live window since 2026-07-01."""
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
RES = Path("/opt/kronos_bot/research"); COST, LOT_, START, BAR = 500.0, {"BANKNIFTY": 30, "NIFTY": 65}, 61, 0.45
IST = timezone(timedelta(hours=5, minutes=30)); now = datetime.now(IST)
H = {"Authorization": "Bearer " + open("/opt/kronos_bot/repo/logs/upstox_token.txt").readline().strip(), "Accept": "application/json"}
KEYS = {"BANKNIFTY": "NSE_INDEX|Nifty Bank", "NIFTY": "NSE_INDEX|Nifty 50"}
LIVE = pd.Timestamp("2026-07-01"); N_DRAWS = 150
rng = np.random.default_rng(11)


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
    df = df[df["datetime"].dt.dayofweek < 5]
    return df[df["datetime"].dt.normalize() < pd.Timestamp(today)].reset_index(drop=True)


def simulate(inst, df, cc):
    P = bt.INSTRUMENT_PARAMS[inst]; P["kronos_conf_min"] = BAR
    pt.get_signal = lambda ctx, force_fallback=False: tuple(cc[str(ctx["datetime"].iloc[-1])])
    out, pos, lot = [], None, LOT_[inst]
    for k in range(START, len(df)):
        px = float(df["close"].iloc[k]); dk = df["datetime"].iloc[k]
        if pos is not None:
            reason = pt.check_exit(inst, pos, df.iloc[:k], px, decay_mode="profit_only")
            if reason or k == len(df) - 1:
                pts = (px - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - px)
                out.append(dict(entry=pd.Timestamp(pos.entry_date), dir=pos.direction, pnl=pts * lot - COST)); pos = None
            continue
        read = cc.get(str(df["datetime"].iloc[k - 1]))
        if not read or read[0] == "NEUTRAL" or read[1] < BAR: continue
        pos = pt.Position(instrument=inst, direction=read[0], entry_date=str(dk.date()), entry_price=px, lot_size=lot, hwm=px, trail_active=False, trail_stop=0.0, entry_confidence=read[1])
    return pd.DataFrame(out)


def summ(T):
    if T is None or len(T) == 0: return (0, 0.0, 0.0, 0.0)
    return (len(T), (T.pnl > 0).mean() * 100, T.pnl.sum(), T.pnl.mean())


for inst in ("BANKNIFTY", "NIFTY"):
    df = upstox_df(inst); cache = json.load(open(RES / f"sweepU_{inst}_signals.json"))
    keys = [str(x) for x in df["datetime"]]; close = df["close"].values; pos_of = {k: i for i, k in enumerate(keys)}
    elig = {k for k, v in cache.items() if k in pos_of and v[0] != "NEUTRAL" and v[1] >= BAR}
    print(f"\n{'=' * 118}\n{inst}: {len(df)} bars to {df.datetime.iloc[-1].date()}, {len(elig)} eligible Kronos reads (conf >= {BAR}), {sum(1 for k in elig if cache[k][0]=='LONG')} long / {sum(1 for k in elig if cache[k][0]=='SHORT')} short\n{'=' * 118}")

    def variant(dirfn):
        cc = {k: list(v) for k, v in cache.items()}
        for k in elig:
            cc[k][0] = dirfn(k)
        return cc

    def trend_dir(k):
        i = pos_of[k]; return "LONG" if (i < 20 or close[i] >= close[i - 20]) else "SHORT"

    rows = {}
    rows["REAL (Kronos direction)"] = simulate(inst, df, cache)
    rows["ALWAYS LONG on the same reads"] = simulate(inst, df, variant(lambda k: "LONG"))
    rows["TREND (20d sign) on the same reads"] = simulate(inst, df, variant(trend_dir))
    print(f"  {'variant':<40}{'--- whole replay ---':>40}{'--- live window since 2026-07 ---':>46}")
    print(f"  {'':<40}{'n':>5}{'win%':>6}{'total Rs':>12}{'Rs/trade':>10}{'n':>6}{'win%':>6}{'total Rs':>12}{'Rs/trade':>10}")
    for name, T in rows.items():
        a, b = summ(T), summ(T[T.entry >= LIVE] if len(T) else T)
        print(f"  {name:<40}{a[0]:>5}{a[1]:>6.0f}{a[2]:>+12,.0f}{a[3]:>+10,.0f}{b[0]:>6}{b[1]:>6.0f}{b[2]:>+12,.0f}{b[3]:>+10,.0f}")
    real_all, real_live = summ(rows["REAL (Kronos direction)"]), summ(rows["REAL (Kronos direction)"][lambda x: x.entry >= LIVE])
    # random direction on the same days
    rd = []
    for _ in range(N_DRAWS):
        flips = {k: ("LONG" if rng.random() < 0.5 else "SHORT") for k in elig}
        T = simulate(inst, df, variant(lambda k: flips[k])); rd.append((summ(T)[2], summ(T)[3], summ(T[T.entry >= LIVE])[2] if len(T) else 0.0, summ(T[T.entry >= LIVE])[3] if len(T) else 0.0))
    rd = np.array(rd)
    # random days and direction (same entry rate)
    rate = len(elig) / max(len(df) - START, 1); rdays = []
    for _ in range(N_DRAWS):
        cc = {k: list(v) for k, v in cache.items()}
        for k in keys[START - 1:]:
            if rng.random() < rate: cc[k] = ["LONG" if rng.random() < 0.5 else "SHORT", 1.0, "", 1.0, 0.0]
            else: cc[k] = ["NEUTRAL", 0.0, "", 0.0, 0.0]
        T = simulate(inst, df, cc); rdays.append((summ(T)[2], summ(T)[3], summ(T[T.entry >= LIVE])[2] if len(T) else 0.0, summ(T[T.entry >= LIVE])[3] if len(T) else 0.0))
    rdays = np.array(rdays)
    for name, R in (("RANDOM direction, same days (mean of draws)", rd), ("RANDOM days + direction (mean of draws)", rdays)):
        print(f"  {name:<40}{'':>5}{'':>6}{R[:,0].mean():>+12,.0f}{R[:,1].mean():>+10,.0f}{'':>6}{'':>6}{R[:,2].mean():>+12,.0f}{R[:,3].mean():>+10,.0f}")
        pw = (np.sum(R[:, 1] >= real_all[3]) + 1) / (len(R) + 1); pl = (np.sum(R[:, 3] >= real_live[3]) + 1) / (len(R) + 1)
        print(f"     5th-95th pct Rs/trade: whole {np.percentile(R[:,1],5):+,.0f} .. {np.percentile(R[:,1],95):+,.0f}   live {np.percentile(R[:,3],5):+,.0f} .. {np.percentile(R[:,3],95):+,.0f}   |  P(random >= REAL): whole {pw:.2f}, live {pl:.2f}")
