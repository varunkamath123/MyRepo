"""Portfolio lab: how should the breakout-long book be BUILT?  NIFTY 50 stocks, 2013-2026 (14y of daily data, today's constituents: survivorship caveat), live signal
(close above the prior-20d high on >= 1.5x the prior-20d volume), live exits (2 ATR stop, 10-day-low channel, 30 sessions), entry at the close, 0.10% round trip.
Capital = 1.0; each position is a fraction of capital; unused capital earns 0 (so every row is return on TOTAL capital).  Daily mark-to-market -> CAGR, vol, Sharpe (rf 0),
max drawdown, exposure.  Variants fixed in advance (11 looks):
  B5   baseline: max 5 positions of 20% each, strongest volume first, any market
  B3 / B8 / B12  the same with 3 / 8 / 12 positions (fraction 1/N each): capacity
  R-str  B5 ranked by breakout strength ((close - level) / ATR)      R-rnd  B5 ranked at random (mean of 10 seeds)
  F200   B5 + new entries only while NIFTY is above its 200-day average
  HEDGE  B5 + a short NIFTY future sized to the long notional every day (market beta stripped), +0.03% per position for the hedge
  RISK   B5 sized for equal risk: size = 1% of capital / (2 ATR as % of price), cap 30% each, total <= 100%
  ALL    F200 + HEDGE + RISK
Halves: 2013-2019 vs 2020-2026."""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
D = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
IST = timezone(timedelta(hours=5, minutes=30)); TODAY = pd.Timestamp(datetime.now(IST).date())
COST, HCOST, WARM = 0.0010, 0.0003, 210
nf = D["idx"]["NIFTY"]["Close"]; nf = nf[nf.index < TODAY]; cal = nf.index; T = len(cal)
nret = nf.pct_change().fillna(0).values; n200 = (nf > nf.rolling(200).mean()).values
names = D["n50"]

S = {}
for s in names:
    d = D["stocks"][s].reindex(cal).ffill(limit=3)
    o, h, l, c, v = (d[k].values.astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    cs, hs, ls, vs = pd.Series(c), pd.Series(h), pd.Series(l), pd.Series(v)
    tr = pd.concat([hs - ls, (hs - cs.shift()).abs(), (ls - cs.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().values
    hi20, lo10 = hs.shift(1).rolling(20).max().values, ls.shift(1).rolling(10).min().values
    volx = (vs / vs.shift(1).rolling(20).mean()).values
    sig = (c > hi20) & (volx >= 1.5) & ~np.isnan(c)
    ret = np.nan_to_num(np.r_[0.0, c[1:] / c[:-1] - 1])
    S[s] = dict(c=c, atr=atr, lo10=lo10, volx=volx, sig=sig, ret=ret, lvl=hi20)

TR = {}                                           # every possible trade: (stock, t0) -> exit index
for s in names:
    A = S[s]; c = A["c"]
    for t0 in np.where(A["sig"])[0]:
        if t0 < WARM: continue
        ep, ea, te = c[t0], A["atr"][t0], None
        for t in range(t0 + 1, T):
            if np.isnan(c[t]): continue
            if c[t] - ep <= -2 * ea or c[t] < A["lo10"][t] or t - t0 >= 30 or t == T - 1:
                te = t; break
        if te is not None: TR[(s, t0)] = te
SIGS = {}
for (s, t0), te in TR.items():
    A = S[s]; SIGS.setdefault(t0, []).append((s, te, A["volx"][t0], (A["c"][t0] - A["lvl"][t0]) / A["atr"][t0], A["atr"][t0] / A["c"][t0]))


def simulate(N=5, rank="vol", regime=False, hedge=False, sizing="fixed", seed=0, risk=0.01):
    rng = np.random.default_rng(seed); held = {}; R = np.zeros(T); inv = np.zeros(T); npos = np.zeros(T); trades = []
    for t in range(WARM, T):
        gross = sum(p["size"] for p in held.values()); inv[t] = gross; npos[t] = len(held)
        day = sum(p["size"] * S[s]["ret"][t] for s, p in held.items() if p["t0"] < t)
        if hedge: day -= sum(p["size"] for p in held.values() if p["t0"] < t) * nret[t]
        cost, just = 0.0, set()
        for s, p in list(held.items()):
            if p["te"] == t:
                cost += p["size"] * (COST / 2 + (HCOST / 2 if hedge else 0)); trades.append(p["size"] * 0 + (S[s]["c"][t] / S[s]["c"][p["t0"]] - 1 - COST)); del held[s]; just.add(s)
        if t in SIGS and not (regime and not n200[t]):
            cands = [x for x in SIGS[t] if x[0] not in held and x[0] not in just]
            if rank == "vol": cands.sort(key=lambda x: -x[2])
            elif rank == "str": cands.sort(key=lambda x: -x[3])
            else: rng.shuffle(cands)
            for s, te, vx, st, ap in cands:
                size = 1.0 / N if sizing == "fixed" else min(0.30, risk / (2 * ap))
                if len(held) >= N or sum(p["size"] for p in held.values()) + size > 1.0 + 1e-9: continue
                held[s] = dict(size=size, t0=t, te=te); cost += size * (COST / 2 + (HCOST / 2 if hedge else 0))
        R[t] = day - cost
    return R, inv, npos, trades


def metrics(R, inv, npos, trades, lo=WARM, hi=T):
    r = R[lo:hi]; eq = np.cumprod(1 + r); yrs = len(r) / 252
    cagr = eq[-1] ** (1 / yrs) - 1; vol = r.std() * np.sqrt(252); dd = (eq / np.maximum.accumulate(eq) - 1).min()
    return dict(cagr=cagr * 100, vol=vol * 100, sharpe=(r.mean() * 252) / vol if vol else 0, dd=dd * 100, calmar=cagr / abs(dd) if dd else 0, inv=inv[lo:hi].mean() * 100, npos=npos[lo:hi].mean())


mid = int(np.searchsorted(cal, pd.Timestamp("2020-01-01")))
print(f"{len(names)} stocks, {len(TR)} possible trades, {cal[WARM].date()} -> {cal[-1].date()} ({(T-WARM)/252:.1f} years); halves split {cal[mid].date()}")
bh = nret[WARM:]; eqb = np.cumprod(1 + bh)
print(f"NIFTY buy-and-hold: CAGR {(eqb[-1] ** (252 / len(bh)) - 1) * 100:.1f}%, vol {bh.std() * np.sqrt(252) * 100:.1f}%, Sharpe {bh.mean() * 252 / (bh.std() * np.sqrt(252)):.2f}, max drawdown {(eqb / np.maximum.accumulate(eqb) - 1).min() * 100:.0f}%\n")
print(f"  {'variant':<42}{'CAGR%':>7}{'vol%':>6}{'Sharpe':>7}{'maxDD%':>8}{'Calmar':>7}{'invested%':>10}{'avg pos':>8}{'trades/yr':>10}{'win%':>6} | {'CAGR H1':>8}{'CAGR H2':>8}{'Sh H1':>6}{'Sh H2':>6}")
VARIANTS = [("B5  max 5, 20% each, strongest volume", dict(N=5)), ("B3  max 3 positions", dict(N=3)), ("B8  max 8 positions", dict(N=8)), ("B12 max 12 positions", dict(N=12)),
            ("R-str  ranked by breakout strength", dict(N=5, rank="str")), ("R-rnd  ranked at random (10 seeds)", dict(N=5, rank="rnd")),
            ("F200  entries only if NIFTY > 200-DMA", dict(N=5, regime=True)), ("HEDGE  short NIFTY future vs the longs", dict(N=5, hedge=True)),
            ("RISK  equal-risk sizing (1% per trade)", dict(N=5, sizing="risk")), ("ALL  F200 + HEDGE + RISK", dict(N=5, regime=True, hedge=True, sizing="risk"))]
for lab, kw in VARIANTS:
    runs = [simulate(seed=sd, **kw) for sd in (range(10) if kw.get("rank") == "rnd" else [0])]
    ms = [metrics(*r) for r in runs]; m1 = [metrics(*r, lo=WARM, hi=mid) for r in runs]; m2 = [metrics(*r, lo=mid, hi=T) for r in runs]
    avg = lambda L, k: np.mean([x[k] for x in L]); tr = np.concatenate([np.array(r[3]) for r in runs])
    print(f"  {lab:<42}{avg(ms,'cagr'):>7.1f}{avg(ms,'vol'):>6.1f}{avg(ms,'sharpe'):>7.2f}{avg(ms,'dd'):>8.0f}{avg(ms,'calmar'):>7.2f}{avg(ms,'inv'):>10.0f}{avg(ms,'npos'):>8.1f}{len(tr)/len(runs)/((T-WARM)/252):>10.0f}{(tr>0).mean()*100:>6.0f}"
          f" | {avg(m1,'cagr'):>8.1f}{avg(m2,'cagr'):>8.1f}{avg(m1,'sharpe'):>6.2f}{avg(m2,'sharpe'):>6.2f}")
