"""How reliable is Kronos's daily read as a forecast?  Uses the cached per-bar reads (same Kronos, same inputs as the live bot):
sweepU = Upstox zero-volume bars (what live sees), sweepY = yfinance bars.  Forecast = 5 daily bars ahead.
For every bar with a read: forecast move = predicted close / close - 1; outcome = actual close 5 bars later / close - 1.
Overlapping 5-day windows inflate significance, so every statistic is also given on every-5th-bar (non-overlapping) samples."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
H = 5
rng = np.random.default_rng(3)


def ic(x, y):
    if len(x) < 8: return float("nan"), float("nan")
    return float(np.corrcoef(x, y)[0, 1]), float(pd.Series(x).rank().corr(pd.Series(y).rank()))


def ci_nonoverlap(x, y, n_boot=2000):
    """bootstrap 90% CI of the Pearson IC on non-overlapping samples"""
    n = len(x); out = []
    for _ in range(n_boot):
        i = rng.integers(0, n, n)
        out.append(np.corrcoef(x[i], y[i])[0, 1])
    return np.percentile(out, 5), np.percentile(out, 95)


for prefix, nice in (("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")):
    for inst in ("BANKNIFTY", "NIFTY"):
        df = pd.read_csv(RES / f"{prefix}_{inst}_df.csv", parse_dates=["datetime"]).reset_index(drop=True)
        cache = json.load(open(RES / f"{prefix}_{inst}_signals.json"))
        rows = []
        for i in range(len(df) - H):
            r = cache.get(str(df["datetime"].iloc[i]))
            if not r:
                continue
            c0, c1 = float(df["close"].iloc[i]), float(df["close"].iloc[i + H])
            d, conf, _, rr, tgt = r
            rows.append(dict(i=i, dir=d, conf=conf, tgt=tgt, c0=c0, fwd=(c1 / c0 - 1) * 100,
                             fmove=((tgt / c0 - 1) * 100) if tgt and tgt > 0 else np.nan,
                             sgn={"LONG": 1, "SHORT": -1}.get(d, 0) * conf))
        X = pd.DataFrame(rows)
        up = (X.fwd > 0).mean() * 100
        print(f"\n=== {nice} | {inst} | {len(X)} bars with a read ({df['datetime'].iloc[X.i.iloc[0]].date()} -> {df['datetime'].iloc[X.i.iloc[-1]].date()}) ===")
        print(f"  market: {up:.0f}% of 5-day windows were up; mean 5-day move {X.fwd.mean():+.2f}%, sd {X.fwd.std():.2f}%")
        print(f"  reads: LONG {np.mean(X.dir=='LONG')*100:.0f}%  SHORT {np.mean(X.dir=='SHORT')*100:.0f}%  NEUTRAL {np.mean(X.dir=='NEUTRAL')*100:.0f}% | conf>=0.35 in {np.mean(X.conf>=.35)*100:.0f}%, >=0.45 in {np.mean(X.conf>=.45)*100:.0f}%")
        # forecast move vs realized move
        V = X.dropna(subset=["fmove"])
        if len(V) > 30:
            p, s = ic(V.fmove.values, V.fwd.values)
            sub = V.iloc[::H]
            ps, ss = ic(sub.fmove.values, sub.fwd.values)
            lo, hi = ci_nonoverlap(sub.fmove.values, sub.fwd.values)
            print(f"  forecast move vs actual 5-day move (n={len(V)}): corr {p:+.3f}, rank {s:+.3f} | non-overlapping n={len(sub)}: corr {ps:+.3f} (90% CI {lo:+.2f}..{hi:+.2f})")
        # signed read vs outcome, all bars
        p, s = ic(X.sgn.values, X.fwd.values)
        sub = X.iloc[::H]
        ps, _ = ic(sub.sgn.values, sub.fwd.values)
        print(f"  signed read (dir x conf) vs actual: corr {p:+.3f}, rank {s:+.3f} | non-overlapping corr {ps:+.3f}")
        # direction accuracy
        print(f"  {'read':<26}{'n':>5}{'right dir%':>11}{'mean next-5d%':>14}{'avg win/loss when right/wrong':>31}")
        for lab, m in (("LONG (any conf)", X.dir == "LONG"), ("SHORT (any conf)", X.dir == "SHORT"), ("NEUTRAL", X.dir == "NEUTRAL"),
                       ("LONG conf>=0.35", (X.dir == "LONG") & (X.conf >= .35)), ("SHORT conf>=0.35", (X.dir == "SHORT") & (X.conf >= .35)),
                       ("LONG conf>=0.45", (X.dir == "LONG") & (X.conf >= .45)), ("SHORT conf>=0.45", (X.dir == "SHORT") & (X.conf >= .45))):
            g = X[m]
            if len(g) < 5:
                print(f"  {lab:<26}{len(g):>5}"); continue
            s_ = 1 if "LONG" in lab else (-1 if "SHORT" in lab else 0)
            if s_:
                right = (np.sign(g.fwd) == s_).mean() * 100
                pnl = s_ * g.fwd
                aw, al = pnl[pnl > 0].mean(), pnl[pnl <= 0].mean()
                print(f"  {lab:<26}{len(g):>5}{right:>10.0f}%{g.fwd.mean():>+14.2f}{f'{aw:+.2f}% / {al:+.2f}%':>31}")
            else:
                print(f"  {lab:<26}{len(g):>5}{'-':>11}{g.fwd.mean():>+14.2f}")
        long_all, short_all = X[X.dir == "LONG"], X[X.dir == "SHORT"]
        print(f"  mean next-5d: after LONG {long_all.fwd.mean():+.2f}% vs after SHORT {short_all.fwd.mean():+.2f}% (a useful signal needs LONG > SHORT; spread {long_all.fwd.mean()-short_all.fwd.mean():+.2f}%)")
