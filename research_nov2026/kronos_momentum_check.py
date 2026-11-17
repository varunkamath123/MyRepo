"""Is Kronos's daily read mostly a trend-following signal?  Prompted by the live sequence Oct 28 -> Nov 17 2026: SHORT reads at the lows,
NEUTRAL during the rally, then LONG at 81% confidence at a new high.  Cached reads, same inputs as the live bot.
 1  correlation of the signed read (direction x confidence) with the trailing 5- and 20-day return
 2  share of non-neutral reads that point the same way as the trailing 20-day move (base rate = share of up windows)
 3  does the read add anything to the forward 5-day return once the trailing returns are removed (partial rank IC)?
 4  outcomes by confidence bucket, esp. the >=0.70 reads (today's long was 0.81)"""
import json
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")


def rk(x): return pd.Series(x).rank().values


def resid(y, X):
    A = np.column_stack([np.ones(len(y)), X]); b, *_ = np.linalg.lstsq(A, y, rcond=None); return y - A @ b


for prefix, nice in (("sweepU", "UPSTOX zero-volume input (live-matching)"), ("sweepY", "YFINANCE input")):
    for inst in ("BANKNIFTY", "NIFTY"):
        df = pd.read_csv(RES / f"{prefix}_{inst}_df.csv", parse_dates=["datetime"]).reset_index(drop=True)
        cache = json.load(open(RES / f"{prefix}_{inst}_signals.json"))
        rows = []
        for i in range(20, len(df) - 5):
            r = cache.get(str(df["datetime"].iloc[i]))
            if not r: continue
            c0 = float(df["close"].iloc[i])
            rows.append(dict(dir=r[0], conf=r[1], sgn={"LONG": 1, "SHORT": -1}.get(r[0], 0) * r[1], d=1 if r[0] == "LONG" else -1 if r[0] == "SHORT" else 0,
                             tr5=c0 / float(df["close"].iloc[i - 5]) - 1, tr20=c0 / float(df["close"].iloc[i - 20]) - 1, fwd=float(df["close"].iloc[i + 5]) / c0 - 1))
        X = pd.DataFrame(rows)
        print(f"\n=== {nice} | {inst} | {len(X)} bars ===")
        c5 = pd.Series(rk(X.sgn)).corr(pd.Series(rk(X.tr5))); c20 = pd.Series(rk(X.sgn)).corr(pd.Series(rk(X.tr20)))
        print(f"  [1] rank corr of the signed read with trailing 5d return {c5:+.3f}, with trailing 20d return {c20:+.3f}")
        nn = X[X.d != 0]
        same = (np.sign(nn.tr20) == nn.d).mean() * 100; base = (X.tr20 > 0).mean() * 100
        print(f"  [2] non-neutral reads pointing the same way as the 20d trend: {same:.0f}% (n={len(nn)}); up-trend base rate {base:.0f}%")
        for lab, m in (("LONG reads after a rising 20d", (X.d == 1) & (X.tr20 > 0)), ("LONG reads after a falling 20d", (X.d == 1) & (X.tr20 <= 0)),
                       ("SHORT reads after a rising 20d", (X.d == -1) & (X.tr20 > 0)), ("SHORT reads after a falling 20d", (X.d == -1) & (X.tr20 <= 0))):
            g = X[m]
            if len(g) >= 5:
                print(f"      {lab:<34} n={len(g):>3}  mean next-5d {g.fwd.mean()*100:+.2f}%  right direction {(np.sign(g.fwd) == g.d).mean()*100:.0f}%")
        y = rk(X.fwd); s_ = rk(X.sgn)
        raw = float(np.corrcoef(s_, y)[0, 1]); part = float(np.corrcoef(resid(s_, np.column_stack([rk(X.tr5), rk(X.tr20)])), resid(y, np.column_stack([rk(X.tr5), rk(X.tr20)])))[0, 1])
        mom = float(np.corrcoef(rk(X.tr20), y)[0, 1])
        print(f"  [3] IC of the read with the next-5d return {raw:+.3f}; of the trailing-20d return itself {mom:+.3f}; partial IC of the read after removing trailing returns {part:+.3f}")
        print(f"  [4] by confidence: " )
        for lab, lo, hi in (("0.45-0.70", .45, .70), (">= 0.70", .70, 9)):
            for dd, dn in ((1, "LONG"), (-1, "SHORT")):
                g = X[(X.d == dd) & (X.conf >= lo) & (X.conf < hi)]
                if len(g) >= 3:
                    print(f"      {dn:<5} conf {lab:<10} n={len(g):>3}  right direction {(np.sign(g.fwd) == dd).mean()*100:.0f}%  mean next-5d (in the read's direction) {(dd*g.fwd).mean()*100:+.2f}%  mean trailing 20d {g.tr20.mean()*100:+.1f}%")
