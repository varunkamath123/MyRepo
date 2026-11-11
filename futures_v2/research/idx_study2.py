"""Study 2: does constituent VOLUME (which the spot index lacks) give a statistical edge on index direction?   Data 2012 -> 2026, 14 years.
Pre-registered BEFORE the run (nothing tuned; the signal set below is the complete list of looks, x 3 horizons x 2 indices):
  Weights : non-negative least squares of index returns on constituent returns, trailing 250 days, refit every 63 days, applied only AFTER the
            fit window (no look-ahead); names with < 200 observations in the window get weight 0.
  A  volume-confirmed breakout breadth : weighted share of names closing above their prior-20-day high on >= 1.5x the prior-20-day average
                                         volume, minus the share closing below the prior-20-day low on >= 1.5x volume
  B  heavyweight volume breakout       : the same indicator averaged over the top-k weight names (NIFTY k=3, BANKNIFTY k=2)
  C  net up-volume thrust              : 5-day mean of  sum_i w_i * sign(ret_i) * ln(volume_i / prior-20d average)
  Zv composite = mean of z(A), z(B), z(C) (all signs +).   z = (x - trailing-250d mean) / trailing-250d std
  Baselines (price only): S1 breakout breadth without volume, H2 heavyweights without volume, the index's own 20-day momentum.
  Primary hypothesis (one test per index): Zv predicts the forward 5-day index return.
  PASS criteria for the primary hypothesis, all four required:
    C1 non-overlapping Spearman IC (fwd 5d) has a 90% bootstrap interval above 0
    C2 IC (fwd 5d) is positive in BOTH chronological halves of the sample
    C3 placebo p < 0.05 (signal circularly shifted by >= 60 sessions, 1000 draws, two-sided)
    C4 the trading rule below has net expectancy > 0 and profit factor > 1.2 in BOTH halves
  Rule: Zv >= +1.5 long, <= -1.5 short; exit when Zv crosses 0, after 10 sessions, or on a 2 ATR14 stop (closes); 0.03% round trip; one position."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
rng = np.random.default_rng(21)
ZW, WIN, STEP, COST = 250, 250, 63, 0.0003
nifty_close = D["idx"]["NIFTY"]["Close"]; nifty_close = nifty_close[nifty_close.index < TODAY]
nifty_above200 = nifty_close > nifty_close.rolling(200).mean()


def nnls_cd(A, b, iters=300):
    n = A.shape[1]; w = np.zeros(n); AtA = A.T @ A; Atb = A.T @ b; d = np.diag(AtA).copy(); d[d == 0] = 1e-12
    for _ in range(iters):
        for j in range(n):
            w[j] = max(0.0, (Atb[j] - AtA[j] @ w + d[j] * w[j]) / d[j])
    return w


def rolling_weights(ret, iret):
    W = pd.DataFrame(np.nan, index=ret.index, columns=ret.columns)
    for t in range(WIN, len(ret), STEP):
        blk, ib = ret.iloc[t - WIN:t], iret.iloc[t - WIN:t]
        ok = blk.notna().sum() >= 200
        names = ok[ok].index
        w = nnls_cd(blk[names].fillna(0.0).values, ib.fillna(0.0).values)
        if w.sum() > 0:
            W.loc[W.index[t:t + STEP], :] = 0.0
            W.loc[W.index[t:t + STEP], names] = w / w.sum()
    return W


def zscore(x):
    return (x - x.rolling(ZW).mean().shift(1)) / x.rolling(ZW).std().shift(1)


def rk(a):
    return pd.Series(a).rank().values


def ic_block(sig, tgt, h):
    X = pd.concat([sig, tgt], axis=1).dropna(); X.columns = ["s", "f"]
    if len(X) < 60: return None
    ra, rb = rk(X.s.values), rk(X.f.values)
    full = float(np.corrcoef(ra, rb)[0, 1])
    sub = X.iloc[::h]; sa, sb = sub.s.values, sub.f.values
    ic_no = float(np.corrcoef(rk(sa), rk(sb))[0, 1]); bs = []
    for _ in range(1000):
        i = rng.integers(0, len(sub), len(sub)); bs.append(np.corrcoef(rk(sa[i]), rk(sb[i]))[0, 1])
    n = len(ra); null = []
    for _ in range(1000):
        lag = int(rng.integers(60, n - 60)); null.append(np.corrcoef(np.roll(ra, lag), rb)[0, 1])
    p = float(np.mean(np.abs(null) >= abs(full)))
    return dict(full=full, no=ic_no, lo=float(np.percentile(bs, 5)), hi=float(np.percentile(bs, 95)), p=p, n=len(sub))


def build(name, cons, k_lead):
    ix = D["idx"][name]; ix = ix[ix.index < TODAY]; dates = ix.index
    C = pd.DataFrame({s: D["stocks"][s]["Close"] for s in cons}).reindex(dates)
    H = pd.DataFrame({s: D["stocks"][s]["High"] for s in cons}).reindex(dates)
    L = pd.DataFrame({s: D["stocks"][s]["Low"] for s in cons}).reindex(dates)
    V = pd.DataFrame({s: D["stocks"][s]["Volume"] for s in cons}).reindex(dates)
    ic_, ih, il = ix["Close"], ix["High"], ix["Low"]
    ret, iret = C.pct_change(), ic_.pct_change()
    Wr = rolling_weights(ret, iret); has = Wr.notna().any(axis=1)
    Wn = Wr.div(Wr.sum(axis=1), axis=0)
    hi20, lo20 = H.shift(1).rolling(20).max(), L.shift(1).rolling(20).min()
    volx = V / V.shift(1).rolling(20).mean()
    bu, bd = (C > hi20).astype(float), (C < lo20).astype(float)
    vu, vd = bu * (volx >= 1.5), bd * (volx >= 1.5)
    A = (Wn * (vu - vd)).sum(axis=1, min_count=1)
    lead = Wr.rank(axis=1, ascending=False) <= k_lead
    B = ((vu - vd) * lead).sum(axis=1).where(has) / k_lead
    UV = (Wn * np.sign(ret) * np.log(volx.clip(lower=0.25))).sum(axis=1, min_count=1).rolling(5).mean()
    S1 = (Wn * (bu - bd)).sum(axis=1, min_count=1)
    H2 = ((bu - bd) * lead).sum(axis=1).where(has) / k_lead
    M20 = ic_.pct_change(20)
    Z = pd.DataFrame({"A vol-confirmed breadth": zscore(A), "B heavyweight vol-breakout": zscore(B), "C net up-volume thrust": zscore(UV)})
    Z["Zv composite"] = Z.mean(axis=1)
    Z["base: S1 price-only breadth"] = zscore(S1); Z["base: H2 price-only heavyweights"] = zscore(H2); Z["base: own 20d momentum"] = zscore(M20)
    tr = pd.concat([ih - il, (ih - ic_.shift()).abs(), (il - ic_.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    cl = ic_.values; ev = np.full(len(cl), np.nan)
    for t in range(len(cl) - 10):
        u_, d_ = cl[t] + 1.5 * atr.values[t], cl[t] - 1.5 * atr.values[t]; lab = 0
        for j in range(1, 11):
            if cl[t + j] >= u_: lab = 1; break
            if cl[t + j] <= d_: lab = -1; break
        ev[t] = lab
    fwd = {h: ic_.shift(-h) / ic_ - 1 for h in (1, 5, 10)}
    top = [list(Wr.iloc[-1].sort_values(ascending=False).index[:k_lead])]
    return Z, fwd, pd.Series(ev, index=dates), ic_, atr, dates, top[0]


def rule(sig, ic_, atr, dates, thr=1.5, long_only=False):
    cl, sv = ic_.values, sig.values; pos, trades, held, ep, ea, et = 0, [], 0, 0.0, 0.0, None
    for t in range(len(cl)):
        if pos:
            held += 1; r = pos * (cl[t] - ep) / ep
            if pos * (cl[t] - ep) <= -2 * ea or (pos == 1 and sv[t] < 0) or (pos == -1 and sv[t] > 0) or held >= 10 or t == len(cl) - 1:
                trades.append((et, dates[t], pos, r - COST, held)); pos = 0
            continue
        if not np.isnan(sv[t]) and t < len(cl) - 1 and (sv[t] >= thr or (sv[t] <= -thr and not long_only)):
            pos, ep, ea, et, held = (1 if sv[t] > 0 else -1), cl[t], atr.values[t], dates[t], 0
    return pd.DataFrame(trades, columns=["entry", "exit", "dir", "ret", "held"])


def stats(T):
    if len(T) == 0: return dict(n=0)
    w, l = T.ret[T.ret > 0], T.ret[T.ret <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pr = (w.mean() / abs(l.mean())) if len(w) and len(l) else float("nan")
    return dict(n=len(T), win=(T.ret > 0).mean() * 100, aw=w.mean() * 100 if len(w) else 0, al=l.mean() * 100 if len(l) else 0,
                be=100 / (1 + pr) if pr == pr else float("nan"), ex=T.ret.mean() * 100, pf=pf)


for name, cons, k in (("NIFTY", D["n50"], 3), ("BANKNIFTY", D["bank"], 2)):
    Z, fwd, ev, ic_, atr, dates, leaders = build(name, cons, k)
    zv = Z["Zv composite"]; ok = zv.dropna().index
    mid = ok[len(ok) // 2]
    print(f"\n{'#'*110}\n#  {name}  | {len(cons)} names | evaluation {ok[0].date()} -> {ok[-1].date()} ({len(ok)} sessions) | halves split at {mid.date()} | latest top-{k} weights: {leaders}\n{'#'*110}")
    print("\n[1] Spearman IC with the forward index return.  non-overlapping IC [90% bootstrap interval]  (placebo p on the overlapping IC)")
    print(f"  {'signal':<34}" + "".join(f"{'fwd '+str(h)+'d':>34}" for h in (1, 5, 10)) + f"{'fwd5 H1':>10}{'fwd5 H2':>10}")
    res = {}
    for col in Z.columns:
        cells = []
        for h in (1, 5, 10):
            r = ic_block(Z[col], fwd[h], h); res[(col, h)] = r
            cells.append(f"{r['no']:>+6.3f} [{r['lo']:>+5.2f},{r['hi']:>+5.2f}] (p={r['p']:.3f})" if r else "n/a")
        h1 = ic_block(Z[col].loc[:mid], fwd[5].loc[:mid], 5); h2 = ic_block(Z[col].loc[mid:], fwd[5].loc[mid:], 5)
        print(f"  {col:<34}" + "".join(f"{c:>34}" for c in cells) + f"{h1['no']:>+10.3f}{h2['no']:>+10.3f}")
    q = pd.qcut(zv.dropna(), 5, labels=False)
    print("\n[2] Zv quintiles -> forward index return % (mean) and first-touch of +/-1.5 ATR within 10 sessions")
    print(f"  {'quintile':<12}{'n':>5}{'fwd1d':>8}{'fwd5d':>8}{'fwd10d':>8}{'up5d%':>7}{'+1.5ATR first':>15}{'-1.5ATR first':>15}")
    for k_ in range(5):
        ii = q[q == k_].index
        print(f"  Q{k_+1}{' (low)' if k_==0 else ' (high)' if k_==4 else '':<10}{len(ii):>5}{fwd[1].loc[ii].mean()*100:>+8.2f}{fwd[5].loc[ii].mean()*100:>+8.2f}{fwd[10].loc[ii].mean()*100:>+8.2f}"
              f"{(fwd[5].loc[ii] > 0).mean()*100:>7.0f}{(ev.loc[ii] == 1).mean()*100:>15.0f}{(ev.loc[ii] == -1).mean()*100:>15.0f}")
    b = ev.loc[ok]
    print(f"  base: +1.5 ATR first {np.mean(b==1)*100:.0f}%, -1.5 ATR first {np.mean(b==-1)*100:.0f}%; top-minus-bottom quintile fwd5d: "
          f"H1 {(fwd[5].loc[[i for i in q[q==4].index if i<=mid]].mean()-fwd[5].loc[[i for i in q[q==0].index if i<=mid]].mean())*100:+.2f}%  "
          f"H2 {(fwd[5].loc[[i for i in q[q==4].index if i>mid]].mean()-fwd[5].loc[[i for i in q[q==0].index if i>mid]].mean())*100:+.2f}%")
    print("\n[3] trading rule (0.03% round trip): expectancy above break-even?")
    print(f"  {'rule':<36}{'n':>5}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'BE win%':>8}{'exp%/trd':>9}{'PF':>6}{'H1 exp':>8}{'H1 PF':>7}{'H2 exp':>8}{'H2 PF':>7}{'long%':>7}{'short%':>7}")
    rules = {}
    for rname, sig, lo in (("Zv composite (+/-1.5)", zv, False), ("Zv composite, long only", zv, True), ("base: S1 price-only breadth", Z["base: S1 price-only breadth"], False),
                           ("base: own 20d momentum", Z["base: own 20d momentum"], False)):
        T = rule(sig, ic_, atr, dates, long_only=lo); rules[rname] = T
        s, a, c = stats(T), stats(T[T.entry <= mid]), stats(T[T.entry > mid])
        L_, S_ = T[T.dir == 1], T[T.dir == -1]
        f = lambda x: f"{x.ret.mean()*100:+.2f}" if len(x) else "  n/a"
        print(f"  {rname:<36}{s['n']:>5}{s['win']:>6.0f}{s['aw']:>+9.2f}{s['al']:>+10.2f}{s['be']:>8.0f}{s['ex']:>+9.2f}{s['pf']:>6.2f}{a.get('ex', float('nan')):>+8.2f}{a.get('pf', float('nan')):>7.2f}"
              f"{c.get('ex', float('nan')):>+8.2f}{c.get('pf', float('nan')):>7.2f}{f(L_):>7}{f(S_):>7}  (L/S {len(L_)}/{len(S_)})")
    T = rules["Zv composite (+/-1.5)"]
    print("  Zv rule by calendar year (n, expectancy%/trade): " + "  ".join(f"{y}: {len(g)}/{g.ret.mean()*100:+.2f}" for y, g in T.groupby(T.entry.dt.year)))
    ab = nifty_above200.reindex(T.entry).values
    print(f"  Zv rule by NIFTY regime at entry: above 200-DMA n={int(ab.sum())} {T[ab].ret.mean()*100:+.2f}% | below n={int((~ab).sum())} {T[~ab].ret.mean()*100:+.2f}%")
    sub = ic_.loc[ok[0]:]
    print(f"  context: index buy-and-hold over the sample {(sub.iloc[-1]/sub.iloc[0]-1)*100:+.0f}%; the rule is in the market {T.held.sum()/len(ok)*100:.0f}% of sessions")
    r5 = res[("Zv composite", 5)]; h1 = ic_block(zv.loc[:mid], fwd[5].loc[:mid], 5); h2 = ic_block(zv.loc[mid:], fwd[5].loc[mid:], 5)
    s1, s2 = stats(T[T.entry <= mid]), stats(T[T.entry > mid])
    c1, c2, c3 = r5["lo"] > 0, (h1["no"] > 0 and h2["no"] > 0), r5["p"] < 0.05
    c4 = s1.get("n", 0) > 0 and s2.get("n", 0) > 0 and s1["ex"] > 0 and s2["ex"] > 0 and s1["pf"] > 1.2 and s2["pf"] > 1.2
    print(f"\n[VERDICT {name}] C1 IC interval above 0: {'PASS' if c1 else 'FAIL'} ({r5['no']:+.3f} [{r5['lo']:+.2f},{r5['hi']:+.2f}]) | C2 positive in both halves: {'PASS' if c2 else 'FAIL'} "
          f"({h1['no']:+.3f} / {h2['no']:+.3f}) | C3 placebo p<0.05: {'PASS' if c3 else 'FAIL'} (p={r5['p']:.3f}) | C4 rule expectancy>0 & PF>1.2 in both halves: {'PASS' if c4 else 'FAIL'}"
          f"  =>  {'STATISTICAL EDGE' if (c1 and c2 and c3 and c4) else 'NO EDGE ESTABLISHED'}")
