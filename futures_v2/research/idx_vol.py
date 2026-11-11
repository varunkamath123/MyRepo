"""Follow-up to idx_study.py: do the constituent signals predict the SIZE of the next move (breakout readiness) even if not its direction?
Targets: |next 5-day return|, next 5-day high-low range, and P(index touches +/-1.5 ATR within 10 sessions, either side).
Signals: the same S1-S5 + Zc (definitions as in idx_study.py), plus the index's own ATR% (compression) for comparison."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
rng = np.random.default_rng(8); ZW = 250


def nnls_cd(A, b, iters=300):
    n = A.shape[1]; w = np.zeros(n); AtA = A.T @ A; Atb = A.T @ b; d = np.diag(AtA).copy(); d[d == 0] = 1e-12
    for _ in range(iters):
        for j in range(n):
            w[j] = max(0.0, (Atb[j] - AtA[j] @ w + d[j] * w[j]) / d[j])
    return w


def zscore(x):
    return (x - x.rolling(ZW).mean().shift(1)) / x.rolling(ZW).std().shift(1)


def sp(a, b):
    return float(pd.Series(a).rank().corr(pd.Series(b).rank()))


def ic(sig, tgt, h):
    X = pd.concat([sig, tgt], axis=1).dropna(); X.columns = ["s", "f"]
    sub = X.iloc[::h]; bs = []
    for _ in range(1000):
        i = rng.integers(0, len(sub), len(sub)); bs.append(sp(sub.s.values[i], sub.f.values[i]))
    return sp(X.s.values, X.f.values), sp(sub.s.values, sub.f.values), np.percentile(bs, 5), np.percentile(bs, 95)


def build(name, cons, k_lead):
    ix = D["idx"][name]; ix = ix[ix.index < TODAY]; dates = ix.index
    C = pd.DataFrame({s: D["stocks"][s]["Close"] for s in cons}).reindex(dates)
    H = pd.DataFrame({s: D["stocks"][s]["High"] for s in cons}).reindex(dates)
    L = pd.DataFrame({s: D["stocks"][s]["Low"] for s in cons}).reindex(dates)
    ic_, ih, il = ix["Close"], ix["High"], ix["Low"]
    ret, iret = C.pct_change(), ic_.pct_change()
    R = ret.fillna(0.0).iloc[1:]; w = nnls_cd(R.values, iret.iloc[1:].values)
    W = pd.Series(w / w.sum(), index=cons).sort_values(ascending=False); wt = W.reindex(cons)
    hi20, lo20 = H.shift(1).rolling(20).max(), L.shift(1).rolling(20).min()
    S1 = ((C > hi20).astype(float).where(hi20.notna()) * wt).sum(axis=1, min_count=1) - ((C < lo20).astype(float).where(lo20.notna()) * wt).sum(axis=1, min_count=1)
    m50 = C.rolling(50).mean(); S2 = ((C > m50).astype(float).where(m50.notna()) * wt).sum(axis=1, min_count=1)
    lead = list(W.index[:k_lead]); wl = wt[lead] / wt[lead].sum()
    S3 = (C[lead].pct_change(5) * wl).sum(axis=1, min_count=1) - ic_.pct_change(5)
    vol20 = ret.rolling(20).std(); ew = ret.mean(axis=1).rolling(20).std(); n = len(cons)
    S4 = (ew ** 2 * n ** 2 - (vol20 ** 2).sum(axis=1)) / (n * (n - 1) * vol20.mean(axis=1) ** 2)
    S5 = C.pct_change(5).std(axis=1)
    tr = pd.concat([ih - il, (ih - ic_.shift()).abs(), (il - ic_.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    Z = pd.DataFrame({"S1 net breakout breadth": zscore(S1), "S2 trend breadth": zscore(S2), "S3 leader gap": zscore(S3),
                      "S4 implied avg corr": zscore(S4), "S5 dispersion": zscore(S5), "own ATR% (compression=low)": zscore(atr / ic_)})
    Z["Zc composite"] = Z[["S1 net breakout breadth", "S2 trend breadth", "S3 leader gap"]].mean(axis=1)
    cl = ic_.values; touch = np.full(len(cl), np.nan)
    for t in range(len(cl) - 10):
        a = atr.values[t]; hit = 0
        for j in range(1, 11):
            if abs(cl[t + j] - cl[t]) >= 1.5 * a: hit = 1; break
        touch[t] = hit
    tg = pd.DataFrame({"|fwd 5d return|": (ic_.shift(-5) / ic_ - 1).abs() * 100,
                       "fwd 5d range": (ih[::-1].rolling(5).max()[::-1].shift(-1) - il[::-1].rolling(5).min()[::-1].shift(-1)) / ic_ * 100,
                       "P(+/-1.5 ATR in 10d)": pd.Series(touch, index=dates)})
    return Z, tg, atr / ic_ * 100


for name, cons, k in (("NIFTY", D["n50"], 5), ("BANKNIFTY", D["bank"], 3)):
    Z, tg, atrp = build(name, cons, k)
    print(f"\n=== {name}: Spearman IC of each signal with the SIZE of the next move (non-overlapping, 90% bootstrap interval); evaluation {Z['Zc composite'].dropna().index[0].date()} -> {Z.index[-1].date()} ===")
    print(f"  {'signal':<28}" + "".join(f"{c:>30}" for c in tg.columns))
    for col in Z.columns:
        cells = []
        for c, h in zip(tg.columns, (5, 5, 10)):
            r = ic(Z[col], tg[c], h); cells.append(f"{r[1]:>+6.3f} [{r[2]:>+5.2f},{r[3]:>+5.2f}] (ov {r[0]:+.2f})")
        print(f"  {col:<28}" + "".join(f"{c:>30}" for c in cells))
    best = max(Z.columns, key=lambda c: abs(ic(Z[c], tg["fwd 5d range"], 5)[1]))
    q = pd.qcut(Z[best].dropna(), 5, labels=False)
    print(f"\n  strongest on the 5-day range: {best}; quintile -> mean next-5d range % / |return| % / P(touch)")
    for k_ in range(5):
        ii = q[q == k_].index
        print(f"    Q{k_+1}: n={len(ii):>3}  range {tg['fwd 5d range'].loc[ii].mean():>5.2f}  |ret| {tg['|fwd 5d return|'].loc[ii].mean():>5.2f}  P(touch) {tg['P(+/-1.5 ATR in 10d)'].loc[ii].mean()*100:>3.0f}%")
    print(f"  (for scale: average next-5d range {tg['fwd 5d range'].mean():.2f}%, ATR14 {atrp.mean():.2f}% of price)")
