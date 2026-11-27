"""Is a downturn 'likely' after a sharp rally like this one?  NIFTY and BANKNIFTY have risen ~14-17% from the Oct 28 low.  Test what followed such states over 2012-2026 (14.9 years).
States (fixed BEFORE the run): A = trailing 20-session return >= +8%;  B = trailing 20-session return >= +10%;  C = close >= 4% above its 50-day average;  D = A and C together.  Baseline = every day.
For each state and index: days (overlapping) and episodes (days within 20 sessions merged), the forward 20-session return (mean, median, % negative), the forward 60-session return,
and the chance of a DRAWDOWN: the lowest close within the next 20 sessions at least 3% / 5% below the signal close (vs the baseline chance).  The decision question: does any state make a drop meaningfully more likely
than the baseline, in both halves of the sample (split 2019-07-01)?  Then a print of where the market is today against the same measures."""
import pickle, sys
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
SPLIT = pd.Timestamp("2019-07-01")


def table(c):
    d = pd.DataFrame({"c": c}); d["r20"] = d.c / d.c.shift(20) - 1; d["s50"] = d.c.rolling(50).mean(); d["s200"] = d.c.rolling(200).mean()
    d["d50"] = d.c / d.s50 - 1; d["d200"] = d.c / d.s200 - 1
    d["f20"] = d.c.shift(-20) / d.c - 1; d["f60"] = d.c.shift(-60) / d.c - 1
    lows = [d.c.iloc[i + 1:i + 21].min() / d.c.iloc[i] - 1 if i + 20 < len(d) else np.nan for i in range(len(d))]
    d["dd20"] = lows
    return d


def episodes(mask, gap=20):
    ids = np.zeros(len(mask), int); eid, last = 0, -10**9
    for i, v in enumerate(mask):
        if v:
            if i - last > gap: eid += 1
            last = i; ids[i] = eid
    return ids


STATES = {"A: 20d ret >= +8%": lambda d: d.r20 >= 0.08, "B: 20d ret >= +10%": lambda d: d.r20 >= 0.10, "C: >= 4% above 50-DMA": lambda d: d.d50 >= 0.04, "D: A and C": lambda d: (d.r20 >= 0.08) & (d.d50 >= 0.04)}
for u in ("NIFTY", "BANKNIFTY"):
    c = DD["idx"][u]["Close"]; c = c[~c.index.duplicated()]; d = table(c).dropna(subset=["f20", "dd20", "r20", "d50"])
    print(f"\n{'=' * 118}\n{u}: {len(d)} days {d.index[0].date()} -> {d.index[-1].date()}\n{'=' * 118}")
    print(f"  {'state':<26}{'days':>6}{'epis':>5}{'fwd20 mean%':>12}{'median%':>8}{'%neg':>6}{'fwd60 mean%':>12}{'P(dd>=3%)':>10}{'P(dd>=5%)':>10}   H1 fwd20 / H2 fwd20   H1 P(dd>=5%) / H2")
    allr = {"all days": pd.Series(True, index=d.index)}
    for k, fn in STATES.items(): allr[k] = fn(d)
    for k, m in allr.items():
        x = d[m]
        if len(x) < 20: print(f"  {k:<26}{len(x):>6}  too few"); continue
        h1, h2 = x[x.index < SPLIT], x[x.index >= SPLIT]; ne = episodes(m.values)[m.values].max() if k != "all days" else 0
        f = lambda y: (y.f20.mean() * 100 if len(y) else float('nan'))
        g = lambda y: ((y.dd20 <= -0.05).mean() * 100 if len(y) else float('nan'))
        print(f"  {k:<26}{len(x):>6}{ne:>5}{x.f20.mean()*100:>+12.2f}{x.f20.median()*100:>+8.2f}{(x.f20<0).mean()*100:>6.0f}{x.f60.mean()*100 if x.f60.notna().any() else float('nan'):>+12.2f}{(x.dd20<=-0.03).mean()*100:>9.0f}%{(x.dd20<=-0.05).mean()*100:>9.0f}%   {f(h1):>+6.2f} / {f(h2):>+6.2f}      {g(h1):>5.0f}% / {g(h2):>3.0f}%")

# ---- where is the market today?
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from brokers.upstox_data import load_daily_ohlcv, get_ltp
print(f"\n{'=' * 118}\nTODAY (live data)\n{'=' * 118}")
for u in ("NIFTY", "BANKNIFTY"):
    x = load_daily_ohlcv(u, bars=320); x = x[x["datetime"].dt.dayofweek < 5]; c = pd.Series(x["close"].values, index=x["datetime"].dt.normalize())
    try: px = float(get_ltp(u))
    except Exception: px = float(c.iloc[-1])
    s50, s200 = c.tail(50).mean(), c.tail(200).mean(); hi = c.tail(250).max(); r20 = px / c.iloc[-21] - 1
    print(f"  {u:<10} {px:>10,.1f}   20-session return {r20*100:+.1f}%   vs 50-DMA {s50:,.0f} ({(px/s50-1)*100:+.1f}%)   vs 200-DMA {s200:,.0f} ({(px/s200-1)*100:+.1f}%)   {(px/hi-1)*100:+.1f}% from the 250-day high ({hi:,.0f})")
    print(f"             hedge trigger (close below the 50-DMA) is {(1-s50/px)*100:.1f}% below the current price -> {s50:,.0f}")
