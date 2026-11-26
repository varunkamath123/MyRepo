"""Does the STOCK BOOK's breakout rule work on the INDEX itself?  NIFTY and BANKNIFTY daily bars 2012-01 -> 2026-11 (14.8 years, ~3,650 bars each, no survivorship bias: the index is the index).
Rule = the live stock-book rule with the one part the index cannot supply removed (volume: index volume is the constituents' sum, missing for 39% of BANKNIFTY days, so it is not used):
  LONG   close > prior-20-day high  -> buy the close.   Exit at a close: < prior-10-day low, or <= entry - 2 x ATR14 (ATR at entry), or 30 sessions.  One position, no same-day re-entry.
  SHORT  the mirror: close < prior-20-day low -> sell; exit close > prior-10-day high, or >= entry + 2 ATR14, or 30 sessions.
  Cost per round trip (measured futures costs): NIFTY 0.0472%, BANKNIFTY 0.0541%.
Pre-registered: 4 looks (2 indices x long/short).  PASS needs all of: net expectancy > 0 with t > 2.5 (Bonferroni for 4), profit factor > 1.2 in BOTH halves (split 2019-07-01), and a placebo p < 0.05
(random entries on flat days, same exit rules, same number of trades per year, 1,000 draws).  Sensitivity grid (entry 10/20/50/100, exit 5/10/20) is exploratory and printed to show whether the result is a plateau or a spike.
Portfolio view: the position held at 1x notional when in, cash otherwise -> CAGR / Sharpe / max drawdown vs buy-and-hold, and alpha/beta against the index."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))["idx"]
COST = {"NIFTY": 0.000472, "BANKNIFTY": 0.000541}
SPLIT = pd.Timestamp("2019-07-01")
rng = np.random.default_rng(7)


def prep(df):
    d = df[["Open", "High", "Low", "Close"]].dropna().copy()
    d = d[~d.index.duplicated()]
    pc = d.Close.shift(1)
    tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    return d


def channels(d, n_in, n_out):
    return dict(hi_in=d.High.rolling(n_in).max().shift(1), lo_in=d.Low.rolling(n_in).min().shift(1),
                hi_out=d.High.rolling(n_out).max().shift(1), lo_out=d.Low.rolling(n_out).min().shift(1))


def simulate(d, side, n_in=20, n_out=10, stop_atr=2.0, max_hold=30, cost=0.0, entry_mask=None, daily=False):
    """Close-based signals and exits.  entry_mask (bool array) replaces the breakout trigger for the placebo."""
    ch = channels(d, n_in, n_out); c = d.Close.values; atr = d.atr.values; dates = d.index
    if entry_mask is None:
        trig = (c > ch["hi_in"].values) if side == 1 else (c < ch["lo_in"].values)
        trig = np.where(np.isnan(atr) | np.isnan(ch["hi_in"].values), False, trig)
    else:
        trig = entry_mask
    ex_ch = (lambda i: c[i] < ch["lo_out"].values[i]) if side == 1 else (lambda i: c[i] > ch["hi_out"].values[i])
    trades = []; pos = None; dret = np.zeros(len(d)); inpos = np.zeros(len(d), bool); last_exit = -1
    for i in range(len(d)):
        if pos is not None:
            e, ei, a0 = pos
            dret[i] = side * (c[i] / c[i - 1] - 1); inpos[i] = True
            stop = (c[i] <= e - stop_atr * a0) if side == 1 else (c[i] >= e + stop_atr * a0)
            if stop or ex_ch(i) or i - ei >= max_hold:
                r = side * (c[i] / e - 1) - cost
                trades.append((dates[ei], dates[i], r, i - ei, "STOP" if stop else ("CH" if ex_ch(i) else "TIME"))); pos = None; last_exit = i
                dret[i] -= cost
            continue
        if trig[i] and i != last_exit and not np.isnan(atr[i]):
            pos = (c[i], i, atr[i])
    T = pd.DataFrame(trades, columns=["entry", "exit", "ret", "held", "why"])
    return (T, pd.Series(dret, index=dates), pd.Series(inpos, index=dates)) if daily else T


def stats(T):
    w, lo = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    pf = w.sum() / abs(lo.sum()) if len(lo) and lo.sum() != 0 else np.nan
    t = T.ret.mean() / (T.ret.std() / np.sqrt(len(T))) if len(T) > 2 else np.nan
    return dict(n=len(T), win=(T.ret > 0).mean() * 100, avgw=w.mean() * 100, avgl=lo.mean() * 100, exp=T.ret.mean() * 100, pf=pf, t=t, held=T.held.mean())


def ann(r):
    yrs = len(r) / 252; eq = (1 + r).cumprod()
    return dict(cagr=(eq.iloc[-1] ** (1 / yrs) - 1) * 100, sharpe=r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan, mdd=((eq / eq.cummax()) - 1).min() * 100)


for u in ("NIFTY", "BANKNIFTY"):
    d = prep(D[u]); yrs = (d.index[-1] - d.index[0]).days / 365.25; mret = d.Close.pct_change().fillna(0)
    print(f"\n{'#' * 112}\n#  {u}: {len(d)} bars {d.index[0].date()} -> {d.index[-1].date()} ({yrs:.1f}y); buy&hold CAGR {ann(mret)['cagr']:.1f}% Sharpe {ann(mret)['sharpe']:.2f} maxDD {ann(mret)['mdd']:.0f}%; cost {COST[u]*100:.4f}%/round trip\n{'#' * 112}")
    for side, nm in ((1, "LONG"), (-1, "SHORT")):
        T, dr, ip = simulate(d, side, cost=COST[u], daily=True)
        Tg = simulate(d, side, cost=0.0)
        s, sg = stats(T), stats(Tg)
        h1, h2 = stats(T[T.entry < SPLIT]), stats(T[T.entry >= SPLIT])
        # placebo: random entries on every day with probability p chosen to match trades/year, same exits
        base_mask_p = len(T) / ip.eq(False).sum()
        plac = []
        for _ in range(1000):
            m = rng.random(len(d)) < base_mask_p
            Tp = simulate(d, side, cost=COST[u], entry_mask=m)
            if len(Tp) > 20: plac.append(Tp.ret.mean() * 100)
        plac = np.array(plac); pval = (np.sum(plac >= s["exp"]) + 1) / (len(plac) + 1)
        a, b = ann(dr), ann(mret)
        X = np.vstack([np.ones(len(d)), mret.values]).T; coef, *_ = np.linalg.lstsq(X, dr.values, rcond=None)
        res = dr.values - X @ coef; se = np.sqrt(res.var(ddof=2) * np.linalg.inv(X.T @ X)[0, 0])
        verdict = "PASS" if (s["exp"] > 0 and s["t"] > 2.5 and h1["pf"] > 1.2 and h2["pf"] > 1.2 and pval < 0.05) else "FAIL"
        print(f"\n  {nm}:  n={s['n']} ({s['n']/yrs:.1f}/yr)  win {s['win']:.0f}%  avg win {s['avgw']:+.2f}%  avg loss {s['avgl']:+.2f}%  net exp {s['exp']:+.2f}%/trade (gross {sg['exp']:+.2f}%)  PF {s['pf']:.2f}  t {s['t']:+.1f}  avg hold {s['held']:.1f}d  -> {verdict}")
        print(f"        halves  first (to Jun-2019) n={h1['n']} exp {h1['exp']:+.2f}% PF {h1['pf']:.2f} win {h1['win']:.0f}%   |   second n={h2['n']} exp {h2['exp']:+.2f}% PF {h2['pf']:.2f} win {h2['win']:.0f}%")
        print(f"        placebo (random entries, same exits): mean {plac.mean():+.2f}%  95th pct {np.percentile(plac,95):+.2f}%  strategy {s['exp']:+.2f}%  p={pval:.3f}")
        print(f"        exits: " + ", ".join(f"{k} {v} ({T[T.why==k].ret.mean()*100:+.2f}%)" for k, v in T.why.value_counts().items()))
        print(f"        portfolio at 1x notional: in market {ip.mean()*100:.0f}% of days, CAGR {a['cagr']:+.1f}%, Sharpe {a['sharpe']:.2f}, maxDD {a['mdd']:.0f}%  | alpha {coef[0]*252*100:+.1f}%/yr (t {coef[0]/se:+.1f}), beta {coef[1]:.2f}  | return per unit exposure {a['cagr']/(ip.mean()*100)*100:+.1f}%/yr of time invested")
        by = T.assign(y=T.entry.dt.year).groupby("y").ret.agg(["count", "mean"]); by["mean"] *= 100
        print("        by entry year (n, exp%): " + "  ".join(f"{y}:{int(r['count'])}/{r['mean']:+.1f}" for y, r in by.iterrows()))
    print(f"\n  sensitivity, LONG net exp %/trade (n) and Sharpe at 1x:   [exploratory, not pre-registered]")
    print(f"  {'entry\\exit':<11}" + "".join(f"{'exit '+str(m):>22}" for m in (5, 10, 20)))
    for n_in in (10, 20, 50, 100):
        row = f"  {n_in:<11}"
        for n_out in (5, 10, 20):
            T, dr, ip = simulate(d, 1, n_in=n_in, n_out=min(n_out, n_in), cost=COST[u], daily=True)
            row += f"{T.ret.mean()*100:>+9.2f}% ({len(T):>3}) S{ann(dr)['sharpe']:>5.2f} "
        print(row)
