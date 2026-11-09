"""Would a dumb, standard, UNTUNED trend rule have captured the Oct 28 - Nov 4 rally, and how do such rules do over 3y?
Close-to-close, signal at day t's close -> position over day t+1; no costs; 1 lot (NIFTY 65, BANKNIFTY 30).
Rules use textbook parameters and are not tuned."""
import sys, logging
sys.path.insert(0, "/opt/kronos_bot/repo")
import numpy as np
import pandas as pd
import backtest as bt

bt.log.setLevel(logging.WARNING)
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
ST = {"NIFTY": (10, 3.0), "BANKNIFTY": (10, 4.0)}          # the bot's own SuperTrend params


def st_series(df, period, mult):
    out = []
    for i in range(len(df)):
        out.append(bt.supertrend(df.iloc[: i + 1], period=period, multiplier=mult) if i >= period + 1 else "BULL")
    return pd.Series(out, index=df.index)


def donchian(df, n_in=20, n_out=10):
    pos, cur = [], 0
    hi_in = df["close"].shift(1).rolling(n_in).max(); lo_in = df["close"].shift(1).rolling(n_in).min()
    hi_out = df["close"].shift(1).rolling(n_out).max(); lo_out = df["close"].shift(1).rolling(n_out).min()
    for i in range(len(df)):
        c = df["close"].iloc[i]
        if cur == 0:
            if c > hi_in.iloc[i]: cur = 1
            elif c < lo_in.iloc[i]: cur = -1
        elif cur == 1 and c < lo_out.iloc[i]:
            cur = -1 if c < lo_in.iloc[i] else 0
        elif cur == -1 and c > hi_out.iloc[i]:
            cur = 1 if c > hi_in.iloc[i] else 0
        pos.append(cur)
    return pd.Series(pos, index=df.index)


def evaluate(df, pos, lot):
    pos = pos.shift(1).fillna(0)                                   # act next bar
    pnl = pos * df["close"].diff().fillna(0) * lot
    chg = pos.diff().fillna(0).ne(0)
    ntr = int((pos.diff().abs() > 0).sum() / 1)
    cum = pnl.cumsum()
    dd = float((cum.cummax() - cum).max())
    recent = float(pnl[(df["datetime"] >= "2026-10-29") & (df["datetime"] <= "2026-11-04")].sum())
    # trades = runs of constant non-zero position
    runs, start = [], None
    for i, p in enumerate(pos):
        if p != 0 and (start is None or pos.iloc[i] != pos.iloc[start]):
            if start is not None:
                runs.append(pnl.iloc[start:i].sum())
            start = i
        elif p == 0 and start is not None:
            runs.append(pnl.iloc[start:i].sum()); start = None
    if start is not None:
        runs.append(pnl.iloc[start:].sum())
    wins = sum(1 for r in runs if r > 0)
    return float(pnl.sum()), len(runs), wins, dd, recent


rules = {}
tot = {}
for inst in ["NIFTY", "BANKNIFTY"]:
    df = pd.read_csv(f"/opt/kronos_bot/research/sweepY_{inst}_df.csv", parse_dates=["datetime"]).reset_index(drop=True)
    lot = LOT[inst]
    st = st_series(df, *ST[inst])
    ma50 = df["close"].rolling(50).mean()
    P = {
        "buy & hold (long)":            pd.Series(1, index=df.index),
        "SuperTrend always-in (L/S)":   st.map({"BULL": 1, "BEAR": -1}),
        "SuperTrend long-only":         st.map({"BULL": 1, "BEAR": 0}),
        "Donchian 20/10 (L/S)":         donchian(df),
        "Donchian 20/10 long-only":     donchian(df).clip(lower=0),
        "50-DMA (L/S)":                 np.sign(df["close"] - ma50).fillna(0),
        "50-DMA long-only":             (df["close"] > ma50).astype(int).where(ma50.notna(), 0),
    }
    print(f"\n[{inst}] {len(df)} bars {df['datetime'].iloc[0].date()} -> {df['datetime'].iloc[-1].date()}  (1 lot = {lot})")
    print(f"  {'rule':<30}{'P&L 3y':>11}{'trades':>7}{'win%':>6}{'maxDD':>10}{'Oct29-Nov4':>12}")
    for name, pos in P.items():
        pnl, n, w, dd, rec = evaluate(df, pos, lot)
        print(f"  {name:<30}{pnl:>+11,.0f}{n:>7}{(w/n*100 if n else 0):>5.0f}%{dd:>10,.0f}{rec:>+12,.0f}")
        t = tot.setdefault(name, [0.0, 0.0, 0])
        t[0] += pnl; t[1] += rec; t[2] += n

print("\nCOMBINED (NIFTY + BANKNIFTY, one lot each, no costs)")
print(f"  {'rule':<30}{'P&L 3y':>11}{'Oct29-Nov4':>12}{'trades':>8}")
for name, (p, r, n) in tot.items():
    print(f"  {name:<30}{p:>+11,.0f}{r:>+12,.0f}{n:>8}")
print("\nReference (Kronos system, same inputs): live gates -266k / 40 trades (to Oct 26); no trend gates +13k (Upstox input) / +102k (yfinance input); in the Oct29-Nov4 window it took 0 trades.")
