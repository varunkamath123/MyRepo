"""Index vs constituents: structure, and a constituent-based z-score for forward breakouts / breakdowns.
Definitions FIXED here before looking at results (textbook values, nothing tuned; 5 signals + 1 composite = the number of looks).

A  structure  : daily-return correlation of each constituent with its index; non-negative least-squares weights (proxy for the
                published index weights, which are knowable in real time); concentration (R^2 of the top-k names); average pairwise
                correlation; weight stability first half vs second half.
B  signals (all computed from data through the close of day t, z-scored against the trailing 250 days):
   S1 net breakout breadth : weighted share of constituents closing above their prior-20-day high  minus  share below their prior-20-day low
   S2 trend breadth        : weighted share of constituents above their own 50-day average
   S3 leader gap           : weighted 5-day return of the top-k weight names (NIFTY k=5, BANKNIFTY k=3) minus the index's own 5-day return
   Zc composite            = mean of z(S1), z(S2), z(S3)            (signs fixed in advance: all +)
   S4 implied avg correlation (20d), S5 dispersion of 5-day returns: reported separately, sign not assumed, not in Zc
   Baseline: the index's own 20-day return z (does the constituent view add anything beyond the index itself?)
   Targets: forward 1/5/10-day index return; first-touch event within 10 sessions of +/-1.5 ATR14 (closes).
C  rule      : Zc >= +1.5 long, <= -1.5 short; exit when Zc crosses 0, after 10 sessions, or a 2 ATR stop; cost 0.03% round trip.
Statistics that overlap are also computed on non-overlapping samples (every h-th day) with a bootstrap 90% interval."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
rng = np.random.default_rng(8)
COST = 0.0003
ZW = 250


def nnls_cd(A, b, iters=300):
    n = A.shape[1]; w = np.zeros(n); AtA = A.T @ A; Atb = A.T @ b; d = np.diag(AtA).copy(); d[d == 0] = 1e-12
    for _ in range(iters):
        for j in range(n):
            w[j] = max(0.0, (Atb[j] - AtA[j] @ w + d[j] * w[j]) / d[j])
    return w


def zscore(x):
    m, s = x.rolling(ZW).mean().shift(1), x.rolling(ZW).std().shift(1)
    return (x - m) / s


def spearman(a, b):
    return float(pd.Series(a).rank().corr(pd.Series(b).rank()))


def ic_block(sig, fwd, h):
    X = pd.concat([sig, fwd], axis=1).dropna(); X.columns = ["s", "f"]
    if len(X) < 30: return None
    full = spearman(X.s.values, X.f.values)
    sub = X.iloc[::h]
    boots = []
    for _ in range(1000):
        i = rng.integers(0, len(sub), len(sub)); boots.append(spearman(sub.s.values[i], sub.f.values[i]))
    return full, spearman(sub.s.values, sub.f.values), np.percentile(boots, 5), np.percentile(boots, 95), len(sub)


def run_index(name, cons, k_lead):
    ix = D["idx"][name]; ix = ix[ix.index < TODAY]
    dates = ix.index
    C = pd.DataFrame({s: D["stocks"][s]["Close"] for s in cons}).reindex(dates)
    H = pd.DataFrame({s: D["stocks"][s]["High"] for s in cons}).reindex(dates)
    L = pd.DataFrame({s: D["stocks"][s]["Low"] for s in cons}).reindex(dates)
    ic_, ih, il = ix["Close"], ix["High"], ix["Low"]
    ret, iret = C.pct_change(), ic_.pct_change()
    print(f"\n{'#'*100}\n#  {name}  |  {len(cons)} constituents | {dates[0].date()} -> {dates[-1].date()} ({len(dates)} sessions)\n{'#'*100}")

    # ---------------- A: structure ----------------
    R = ret.fillna(0.0).iloc[1:]; y = iret.iloc[1:].values
    w_full = nnls_cd(R.values, y); fit_r2 = 1 - np.sum((y - R.values @ w_full) ** 2) / np.sum((y - y.mean()) ** 2)
    W = pd.Series(w_full / w_full.sum(), index=cons).sort_values(ascending=False)
    cor = ret.corrwith(iret)
    print(f"\n[A] structure: weights fitted by non-negative least squares on daily returns (sum before normalising {w_full.sum():.2f}, R^2 {fit_r2:.3f})")
    print(f"  {'name':<12}{'weight%':>8}{'corr w/ index':>15}{'corr 2023':>11}{'corr 2025+':>12}")
    for s in W.index[:10]:
        c23 = ret[s][ret.index.year == 2023].corr(iret[iret.index.year == 2023]); c25 = ret[s][ret.index.year >= 2025].corr(iret[iret.index.year >= 2025])
        print(f"  {s:<12}{W[s]*100:>8.1f}{cor[s]:>15.2f}{c23:>11.2f}{c25:>12.2f}")
    for k in (3, 5, 10):
        top = list(W.index[:k]); wk = nnls_cd(R[top].values, y); r2 = 1 - np.sum((y - R[top].values @ wk) ** 2) / np.sum((y - y.mean()) ** 2)
        print(f"  top-{k} names alone explain {r2*100:.0f}% of the index's daily return variance (all names: {fit_r2*100:.0f}%)")
    mid = len(R) // 2
    wa, wb = nnls_cd(R.values[:mid], y[:mid]), nnls_cd(R.values[mid:], y[mid:])
    print(f"  weight stability: first-half vs second-half fitted weights correlate {np.corrcoef(wa/wa.sum(), wb/wb.sum())[0,1]:.2f}")
    cm = R.corr().values; n = len(cons)
    print(f"  average pairwise correlation among constituents: {(cm.sum()-n)/(n*(n-1)):.2f} overall; " +
          ", ".join(f"{yr}: {(lambda m: (m.sum()-n)/(n*(n-1)))(R[R.index.year==yr].corr().values):.2f}" for yr in sorted(set(R.index.year))))

    # ---------------- B: signals ----------------
    w = W.reindex(cons).values; w = w / w.sum()
    wt = pd.Series(w, index=cons)
    hi20, lo20 = H.shift(1).rolling(20).max(), L.shift(1).rolling(20).min()
    up, dn = (C > hi20).astype(float).where(hi20.notna()), (C < lo20).astype(float).where(lo20.notna())
    S1 = (up * wt).sum(axis=1, min_count=1) - (dn * wt).sum(axis=1, min_count=1)
    S2 = ((C > C.rolling(50).mean()).astype(float).where(C.rolling(50).mean().notna()) * wt).sum(axis=1, min_count=1)
    lead = list(W.index[:k_lead]); wl = wt[lead] / wt[lead].sum()
    S3 = (C[lead].pct_change(5) * wl).sum(axis=1, min_count=1) - ic_.pct_change(5)
    vol20 = ret.rolling(20).std(); ew = ret.mean(axis=1).rolling(20).std()
    S4 = (ew ** 2 * len(cons) ** 2 - (vol20 ** 2).sum(axis=1)) / (len(cons) * (len(cons) - 1) * vol20.mean(axis=1) ** 2)
    S5 = C.pct_change(5).std(axis=1)
    M20 = ic_.pct_change(20)
    Z = pd.DataFrame({"S1 net breakout breadth": zscore(S1), "S2 trend breadth": zscore(S2), "S3 leader gap": zscore(S3),
                      "S4 implied avg corr": zscore(S4), "S5 dispersion": zscore(S5), "own 20d momentum": zscore(M20)})
    Z["Zc composite"] = Z[["S1 net breakout breadth", "S2 trend breadth", "S3 leader gap"]].mean(axis=1)
    tr = pd.concat([ih - il, (ih - ic_.shift()).abs(), (il - ic_.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    cl = ic_.values; ev = np.full(len(cl), np.nan)
    for t in range(len(cl) - 10):
        up_t, dn_t = cl[t] + 1.5 * atr.values[t], cl[t] - 1.5 * atr.values[t]; lab = 0
        for j in range(1, 11):
            if cl[t + j] >= up_t: lab = 1; break
            if cl[t + j] <= dn_t: lab = -1; break
        ev[t] = lab
    ev = pd.Series(ev, index=dates)
    fwd = {h: ic_.shift(-h) / ic_ - 1 for h in (1, 5, 10)}
    ok = Z["Zc composite"].notna()
    print(f"\n[B] signal quality, evaluation sample {Z['Zc composite'].dropna().index[0].date()} -> {dates[-1].date()} ({int(ok.sum())} sessions; z-scores use only the trailing {ZW} days)")
    print(f"  Spearman IC with the forward INDEX return: overlapping / non-overlapping [90% bootstrap interval]")
    print(f"  {'signal':<26}" + "".join(f"{'fwd '+str(h)+'d':>26}" for h in (1, 5, 10)))
    for col in Z.columns:
        cells = []
        for h in (1, 5, 10):
            r = ic_block(Z[col], fwd[h], h)
            cells.append(f"{r[0]:>+6.3f}/{r[1]:>+6.3f} [{r[2]:>+5.2f},{r[3]:>+5.2f}]" if r else "n/a")
        print(f"  {col:<26}" + "".join(f"{c:>26}" for c in cells))
    zc = Z["Zc composite"]
    q = pd.qcut(zc.dropna(), 5, labels=False)
    print("\n  Zc quintiles -> forward index return (%), mean:")
    print(f"  {'quintile':<10}{'n':>5}{'fwd1d':>8}{'fwd5d':>8}{'fwd10d':>8}{'up5d%':>7}{'P(+1.5ATR first)':>18}{'P(-1.5ATR first)':>18}")
    for k in range(5):
        idxk = q[q == k].index
        print(f"  Q{k+1}{' (low)' if k==0 else ' (high)' if k==4 else '':<8}{len(idxk):>5}{fwd[1].loc[idxk].mean()*100:>+8.2f}{fwd[5].loc[idxk].mean()*100:>+8.2f}{fwd[10].loc[idxk].mean()*100:>+8.2f}"
              f"{(fwd[5].loc[idxk] > 0).mean()*100:>7.0f}{(ev.loc[idxk] == 1).mean()*100:>18.0f}{(ev.loc[idxk] == -1).mean()*100:>18.0f}")
    base = ev.loc[zc.dropna().index]
    print(f"  base rates over the sample: P(+1.5 ATR first) {np.mean(base == 1)*100:.0f}%  P(-1.5 ATR first) {np.mean(base == -1)*100:.0f}%  neither {np.mean(base == 0)*100:.0f}%")
    print("\n  Event table (first touch of +/-1.5 ATR within 10 sessions):")
    pre = (ic_ <= H.shape[0] * 0 + ih.shift(1).rolling(20).max())          # index not yet above its prior-20d high
    for lab, m in (("Zc >= +1.5", zc >= 1.5), ("Zc >= +1.5, index NOT yet at a 20d high (leading)", (zc >= 1.5) & pre),
                   ("Zc <= -1.5", zc <= -1.5), ("Zc <= -1.5, index NOT yet at a 20d low (leading)", (zc <= -1.5) & (ic_ >= il.shift(1).rolling(20).min())),
                   ("own 20d momentum z >= +1.5", Z["own 20d momentum"] >= 1.5), ("own 20d momentum z <= -1.5", Z["own 20d momentum"] <= -1.5)):
        e = ev.loc[m[m].index].dropna()
        f5 = fwd[5].loc[e.index]
        print(f"  {lab:<56} n={len(e):>3}  up-first {np.mean(e==1)*100:>3.0f}%  down-first {np.mean(e==-1)*100:>3.0f}%  mean fwd5 {f5.mean()*100:>+5.2f}%")

    # ---------------- C: rule ----------------
    print(f"\n[C] trading rule on the index (future proxy), cost {COST*100:.2f}% round trip, one position at a time")
    print(f"  {'rule':<30}{'n':>4}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'BE win%':>8}{'exp%/trade':>11}{'PF':>6}{'H1':>8}{'H2':>8}{'long%':>8}{'short%':>8}{'in mkt':>7}")
    for rname, sig, thr in (("Zc composite (+/-1.5)", zc, 1.5), ("own 20d momentum z (+/-1.5)", Z["own 20d momentum"], 1.5), ("S1 breakout breadth (+/-1.5)", Z["S1 net breakout breadth"], 1.5)):
        pos, trades, held, ep, ea, et = 0, [], 0, 0.0, 0.0, None
        sv = sig.values
        for t in range(len(cl)):
            if pos:
                held += 1; r = pos * (cl[t] - ep) / ep
                ex = pos * (cl[t] - ep) <= -2 * ea or (pos == 1 and sv[t] < 0) or (pos == -1 and sv[t] > 0) or held >= 10 or t == len(cl) - 1
                if ex:
                    trades.append((et, dates[t], pos, r - COST, held)); pos = 0
                continue
            if not np.isnan(sv[t]) and abs(sv[t]) >= thr and t < len(cl) - 1:
                pos, ep, ea, et, held = (1 if sv[t] > 0 else -1), cl[t], atr.values[t], dates[t], 0
        T = pd.DataFrame(trades, columns=["entry", "exit", "dir", "ret", "held"])
        if T.empty:
            print(f"  {rname:<30}   0"); continue
        w_, l_ = T.ret[T.ret > 0], T.ret[T.ret <= 0]
        pf = w_.sum() / abs(l_.sum()) if len(l_) and l_.sum() != 0 else float("inf")
        pr = (w_.mean() / abs(l_.mean())) if len(w_) and len(l_) else float("nan"); be = 1 / (1 + pr) if pr == pr else float("nan")
        mid_d = T.entry.sort_values().iloc[len(T) // 2]
        f = lambda x: f"{x.ret.mean()*100:+.2f}" if len(x) else "  n/a"
        print(f"  {rname:<30}{len(T):>4}{(T.ret>0).mean()*100:>6.0f}{w_.mean()*100 if len(w_) else 0:>+9.2f}{l_.mean()*100 if len(l_) else 0:>+10.2f}{be*100:>8.0f}{T.ret.mean()*100:>+11.2f}{pf:>6.2f}"
              f"{f(T[T.entry < mid_d]):>8}{f(T[T.entry >= mid_d]):>8}{f(T[T.dir == 1]):>8}{f(T[T.dir == -1]):>8}{T.held.sum()/max(zc.notna().sum(),1)*100:>6.0f}%  (n L/S {int((T.dir==1).sum())}/{int((T.dir==-1).sum())})")
    sub = ic_.loc[zc.dropna().index[0]:]
    print(f"  context: the index moved {(sub.iloc[-1]/sub.iloc[0]-1)*100:+.1f}% over the evaluation sample (buy-and-hold)")


run_index("NIFTY", D["n50"], 5)
run_index("BANKNIFTY", D["bank"], 3)
