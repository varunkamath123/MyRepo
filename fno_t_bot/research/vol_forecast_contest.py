# -*- coding: utf-8 -*-
"""Which volatility estimator actually forecasts the next move best?

    python research/vol_forecast_contest.py        # on EC2

A pure forecasting contest with NO trading assumptions. The question is only:
standing at the entry bar, which estimator best predicts how much the index
actually moves over the next 240 minutes?

WHY THIS COMES FIRST. Volatility enters this entire system at one line:

    options_bot.py:604
    df['HV'] = df['Returns'].rolling(30).std() * np.sqrt(252 * 75)

That single `hv` is then passed into enter_trade, _rv_iv_blocked and
check_exits, so it sets rv_iv, move_cover and every Black-Scholes price in
paper mode. Replacing it is a one-line change -- but only worth making if
something genuinely forecasts better. If rolling-30 wins here, the whole GARCH
idea stops cheaply, which is the point of running this before building
anything.

THE CONTESTANTS, all strictly walk-forward (information through t-1 only):

    rolling30    the incumbent: equal-weighted std of the last 30 bars
    ewma         RiskMetrics lambda=0.94 -- a RESTRICTED GARCH(1,1) with
                 nothing to fit. This is the control: if full GARCH cannot
                 beat EWMA out of sample, the extra machinery earns nothing.
    prev_rv      yesterday's whole-session realised vol. The crudest possible
                 use of clustering, and already known to carry signal
                 (RV(t-1) -> RV(t) rho +0.593, n=1,258)
    garch11      GARCH(1,1) by maximum likelihood on daily returns, refit
                 every REFIT_EVERY sessions, forecasting one session ahead

JUDGED ON THREE THINGS, because they are not the same question:
    1. rank correlation with the realised forward move  (does it order days?)
    2. mean absolute error of the vol forecast          (is it calibrated?)
    3. separation of PAYABLE from unpayable windows     (does it pay?)

An estimator can win 1 and lose 3. Only 3 is worth money.

NOTE ON SEASONALITY. Intraday 5-min vol is U-shaped -- much larger at the open
and close -- so fitting a model to raw 5-min returns reads that diurnal pattern
as clustering and produces confident nonsense. GARCH here is therefore fitted
on DAILY returns and scaled into the session, which sidesteps the problem
entirely. The intraday refinement is a later question and should only be asked
if the daily version wins.
"""
import glob, math, os, re, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.environ.get('FNO_DATA', '/opt/trading_bot/data')
DIRS = {'NIFTY': 'nifty_5min', 'BANKNIFTY': 'banknifty_5min', 'SENSEX': 'sensex_5min'}
BARS_PER_DAY = 75          # 375 trading minutes / 5
ENTRY_BAR = 9              # ~45 minutes into the session
HORIZON_BARS = 48          # 240 minutes
BREAKEVEN = 0.51           # % of spot an ATM entry must clear, round trip
LAMBDA = 0.94              # RiskMetrics
REFIT_EVERY = 20           # sessions between GARCH re-estimations
MIN_HISTORY = 120          # sessions before the first GARCH fit


# ── statistics ────────────────────────────────────────────────────────────
def rank(v):
    s = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and v[s[j + 1]] == v[s[i]]:
            j += 1
        a = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[s[k]] = a
        i = j + 1
    return r


def spearman(x, y):
    n = len(x)
    if n < 6:
        return 0.0, 1.0, n
    rx, ry = rank(x), rank(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = num / den if den else 0.0
    if abs(rho) >= 1:
        return rho, 0.0, n
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    return rho, 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))), n


# ── GARCH(1,1), maximum likelihood, scipy only ───────────────────────────
def garch11_fit(rets):
    """(omega, alpha, beta) by MLE on a daily return series, in percent."""
    import numpy as np
    from scipy.optimize import minimize
    r = np.asarray(rets, dtype=float)
    r = r - r.mean()
    v0 = max(r.var(), 1e-10)

    def nll(p):
        om, al, be = p
        if om <= 0 or al < 0 or be < 0 or al + be >= 0.999:
            return 1e10
        s2 = np.empty_like(r)
        s2[0] = v0
        for i in range(1, len(r)):
            s2[i] = om + al * r[i - 1] ** 2 + be * s2[i - 1]
            if s2[i] <= 0:
                return 1e10
        return 0.5 * np.sum(np.log(s2) + r ** 2 / s2)

    best = None
    for a0, b0 in ((0.10, 0.85), (0.05, 0.90), (0.20, 0.70)):
        try:
            res = minimize(nll, [v0 * (1 - a0 - b0), a0, b0], method='Nelder-Mead',
                           options=dict(maxiter=2000, xatol=1e-8, fatol=1e-8))
            if res.success or res.fun < 1e9:
                if best is None or res.fun < best.fun:
                    best = res
        except Exception:
            continue
    if best is None:
        return None
    om, al, be = best.x
    if om <= 0 or al < 0 or be < 0 or al + be >= 0.999:
        return None
    return float(om), float(al), float(be)


def garch11_forecast(params, rets):
    """One-step-ahead sigma (same units as rets) from a fitted model."""
    om, al, be = params
    r = [x - st.mean(rets) for x in rets]
    s2 = st.pvariance(r) if len(r) > 1 else 1e-6
    for i in range(1, len(r)):
        s2 = om + al * r[i - 1] ** 2 + be * s2
    s2 = om + al * r[-1] ** 2 + be * s2
    return math.sqrt(max(s2, 1e-12))


# ── data ──────────────────────────────────────────────────────────────────
def load_session(path):
    out = []
    try:
        fh = open(path, encoding='utf-8', errors='ignore')
    except Exception:
        return out
    with fh:
        head = fh.readline().strip().split(',')
        try:
            ci = next(i for i, h in enumerate(head) if h.strip().lower() == 'close')
        except StopIteration:
            return out
        for ln in fh:
            p = ln.rstrip('\n').split(',')
            if len(p) <= ci:
                continue
            try:
                out.append(float(p[ci]))
            except Exception:
                continue
    return out


def build(inst, sub):
    """Chronological per-session records with everything needed."""
    recs = []
    for f in sorted(glob.glob(os.path.join(DATA, sub, '*.csv'))):
        c = load_session(f)
        if len(c) < ENTRY_BAR + 20:
            continue
        rets = [math.log(c[i + 1] / c[i]) for i in range(len(c) - 1)
                if c[i] > 0 and c[i + 1] > 0]
        if len(rets) < 20:
            continue
        m = re.search(r'(\d{4}-?\d{2}-?\d{2})', os.path.basename(f))
        sp = c[ENTRY_BAR]
        fwd = c[ENTRY_BAR + 1: ENTRY_BAR + 1 + HORIZON_BARS]
        if len(fwd) < HORIZON_BARS // 2:
            continue
        fwd_rets = [math.log(fwd[i + 1] / fwd[i]) for i in range(len(fwd) - 1)
                    if fwd[i] > 0 and fwd[i + 1] > 0]
        if len(fwd_rets) < 10:
            continue
        recs.append(dict(
            day=m.group(1) if m else os.path.basename(f),
            closes=c, rets=rets,
            day_ret_pct=(c[-1] / c[0] - 1.0) * 100.0,
            session_rv=st.stdev(rets) * math.sqrt(len(rets)) * 100.0,
            pre_rets=rets[:ENTRY_BAR],                       # before entry
            fwd_rv=st.stdev(fwd_rets) * math.sqrt(len(fwd_rets)) * 100.0,
            excursion=max(abs(x - sp) for x in fwd) / sp * 100.0,
        ))
    return recs


def main():
    print('=' * 92)
    print('VOLATILITY FORECAST CONTEST — which estimator predicts the next move?')
    print('=' * 92)
    print(f'\n  entry bar {ENTRY_BAR} (~{ENTRY_BAR*5}min in), horizon '
          f'{HORIZON_BARS*5}min, break-even {BREAKEVEN}% of spot')
    print('  all estimators strictly walk-forward: information through t-1 only')

    allrows = []
    for inst, sub in DIRS.items():
        recs = build(inst, sub)
        if len(recs) < MIN_HISTORY + 20:
            print(f'\n  {inst}: only {len(recs)} sessions, skipped')
            continue
        daily = [r['day_ret_pct'] for r in recs]
        params, last_fit = None, -10 ** 9
        ewma_var = st.pvariance(daily[:30]) if len(daily) > 30 else 1.0
        for t in range(len(recs)):
            r = recs[t]
            # keep EWMA marching forward on CLOSED days only
            if t > 0:
                ewma_var = (LAMBDA * ewma_var
                            + (1 - LAMBDA) * daily[t - 1] ** 2)
            if t < MIN_HISTORY:
                continue
            hist = daily[:t]                     # strictly before today
            if params is None or (t - last_fit) >= REFIT_EVERY:
                p = garch11_fit(hist[-400:])
                if p:
                    params, last_fit = p, t
            # --- the four forecasts, each a predicted SESSION vol in % ---
            f = {}
            pre = r['pre_rets']
            bars = recs[t - 1]['rets'][-(30 - len(pre)):] + pre if len(pre) < 30 else pre[-30:]
            f['rolling30'] = (st.stdev(bars) * math.sqrt(BARS_PER_DAY) * 100.0
                              if len(bars) > 2 else None)
            f['ewma'] = math.sqrt(max(ewma_var, 1e-12))
            f['prev_rv'] = recs[t - 1]['session_rv']
            f['garch11'] = garch11_forecast(params, hist[-400:]) if params else None
            if any(v is None for v in f.values()):
                continue
            allrows.append(dict(inst=inst, day=r['day'], f=f,
                                fwd_rv=r['fwd_rv'], exc=r['excursion']))
        print(f'  {inst}: {len(recs)} sessions, GARCH params '
              f'{tuple(round(x,4) for x in params) if params else "none"}')

    if not allrows:
        print('\n  no rows produced')
        return
    allrows.sort(key=lambda r: (r['day'], r['inst']))
    names = ['rolling30', 'ewma', 'prev_rv', 'garch11']
    print(f'\n  {len(allrows)} forecast points   '
          f'{allrows[0]["day"]} .. {allrows[-1]["day"]}')

    print('\n--- 1. does it ORDER days correctly? (rank corr. with realised) ---')
    print(f'  {"estimator":12} {"vs fwd realised vol":>22} {"vs actual excursion":>22}')
    for nm in names:
        x = [r['f'][nm] for r in allrows]
        a, pa, _ = spearman(x, [r['fwd_rv'] for r in allrows])
        b, pb, _ = spearman(x, [r['exc'] for r in allrows])
        print(f'  {nm:12} rho {a:+.3f} (p={pa:.2g}){"":>3} rho {b:+.3f} (p={pb:.2g})')

    print('\n--- 2. is it CALIBRATED? (error against realised forward vol) ---')
    print(f'  {"estimator":12} {"bias":>9} {"MAE":>9} {"RMSE":>9}')
    for nm in names:
        e = [r['f'][nm] - r['fwd_rv'] for r in allrows]
        print(f'  {nm:12} {st.mean(e):>+8.3f} {st.mean([abs(v) for v in e]):>9.3f}'
              f' {math.sqrt(st.mean([v*v for v in e])):>9.3f}')

    print('\n--- 3. does it SEPARATE payable windows? (the only one worth money) ---')
    print(f'  top quintile vs bottom quintile by each estimator,'
          f' payable = excursion >= {BREAKEVEN}%')
    print(f'  {"estimator":12} {"botQ pay":>9} {"topQ pay":>9} {"gap":>8}'
          f' {"1st half":>10} {"2nd half":>10}')
    for nm in names:
        s = sorted(allrows, key=lambda r: r['f'][nm])
        q = max(1, len(s) // 5)
        pay = lambda part: 100.0 * sum(1 for r in part if r['exc'] >= BREAKEVEN) / len(part)
        lo, hi = pay(s[:q]), pay(s[-q:])
        halves = []
        h = len(allrows) // 2
        for part in (allrows[:h], allrows[h:]):
            ps = sorted(part, key=lambda r: r['f'][nm])
            qq = max(1, len(ps) // 5)
            halves.append(pay(ps[-qq:]) - pay(ps[:qq]))
        print(f'  {nm:12} {lo:>8.1f}% {hi:>8.1f}% {hi-lo:>+7.1f}'
              f' {halves[0]:>+9.1f} {halves[1]:>+9.1f}')

    print('\n  A winner must beat rolling30 on BOTH halves of column 3.')
    print('  If EWMA matches garch11, the fitting machinery is not earning its keep.')


if __name__ == '__main__':
    main()
