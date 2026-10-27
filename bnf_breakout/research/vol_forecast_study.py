# -*- coding: utf-8 -*-
"""Can we forecast BANKNIFTY's realised volatility better than the market's implied volatility?

Run:  python vol_forecast_study.py <bars_dir> <oi_intraday.jsonl> <vix5.csv>

PRE-REGISTERED before any result was seen.

  Question A  Out of sample, does a model built from realised-vol features forecast the next window's
              realised vol better than "the implied vol, debiased" (the market's own forecast)?
  Question B  When the model says realised vol will exceed what implied vol suggests, are the
              realised moves in fact larger than what was paid? (a long-straddle PROXY: no option
              prices exist for this, so it ignores theta, vega and the smile.)
  Question C  Does a model of the CHANGE in implied vol beat the time-of-day mean? (small; reported
              only as a by-product, never as a result on its own.)

  Implied sources  (1) BNF  = BANKNIFTY ATM IV from the NSE chain log (oi_intraday.jsonl), 5-minute
                              snapshots, ~125 sessions, nearest series. Monthly-expiry days are dropped
                              (that chain is the expiring series).
                   (2) VIX  = India VIX 5-minute candles from Fyers, 190 sessions. NIFTY's 30-day
                              implied vol, a proxy for BANKNIFTY's.
  Decisions     10:30 and 12:00 (bar closes). Windows: the next 12 bars (60 min) and to the 15:00 close.
                ONE decision per session per time, so observations do not overlap. 8 tests in all.
  Target        y = log(realised vol over the window) = log sqrt(sum of squared 5-minute log returns).
  Models        M_seas  intraday seasonality only.
                M0      debiased implied: y = a + log(implied vol for the window, seasonality-scaled).
                M1      seasonal + realised features (last hour, today so far, yesterday, 5 days, |gap|), NO implied.
                M2      M1 + implied.
                Expanding-window OLS, first 40 sessions never scored, refit every 5. All models are
                scored on the SAME rows.
  Pass          (A) M2 beats M0 in mean squared error of y, bootstrap-over-sessions CI excludes 0 AND
                both chronological halves favour M2, AND the same holds in BOTH implied sources, AND
                in at least 4 of the 8 tests (a single cell among 8 is noise).
                (B) the top third by (M2 forecast - M0 forecast) shows a proxy payoff above the 8 bps
                round-trip cost in BOTH halves, with a CI excluding 0.
                Anything less is reported as 'not found'.
  Cannot tell   Edges smaller than the sample can resolve (~85-150 scored sessions): a null here is
                'not found', not 'absent'.
"""
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
COST_PCT = 0.08                       # round trip, % of spot (REPLAY_COST_INDEX_PCT)
N0, REFIT = 40, 5
rng = np.random.default_rng(23)
BARS = 75
DAYCOUNT = float(os.environ.get('VOL_DAYCOUNT', 252))
END_BAR = 68                          # the bar that closes at 15:00


# ── data ────────────────────────────────────────────────────────────────────
def load_bars(dirs):
    S = {}
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, 'banknifty_5min_*.csv'))):
            x = pd.read_csv(f)
            if len(x) != BARS:
                continue
            day = os.path.basename(f)[15:23]
            day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
            S[day] = dict(O=x['Open'].values, H=x['High'].values, L=x['Low'].values, C=x['Close'].values,
                          V=x['Volume'].values.astype(float))
    return dict(sorted(S.items()))


def load_bnf_iv(path):
    m = {}
    for ln in open(path, encoding='utf-8', errors='ignore'):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get('instrument') != 'BANKNIFTY' or not d.get('atm_iv'):
            continue
        ts = d['ts_str']
        m.setdefault(ts[:10], []).append((int(ts[11:13]) * 60 + int(ts[14:16]), float(d['atm_iv'])))
    return {k: sorted(v) for k, v in m.items()}


def load_vix(path):
    v = pd.read_csv(path).drop_duplicates('epoch')
    v['ts'] = pd.to_datetime(v['epoch'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    out = {}
    for day, g in v.groupby(v['ts'].dt.strftime('%Y-%m-%d')):
        g = g.sort_values('ts')
        if len(g) == BARS:
            out[day] = g['c'].values
    return out


def last_tuesday(y, m):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - 1) % 7)


def is_monthly_expiry(day):
    y, m, d = (int(x) for x in day.split('-'))
    return date(y, m, d) == last_tuesday(y, m)


# ── features ────────────────────────────────────────────────────────────────
def rets(s):
    c, o = s['C'], s['O']
    r = np.empty(BARS)
    r[0] = np.log(c[0] / o[0])
    r[1:] = np.diff(np.log(c))
    return r


def build(S, iv_fn, kd, horizon):
    """One row per session: features, target, implied (raw), for decision bar kd."""
    days = list(S)
    R = {d: rets(S[d]) for d in days}
    rows = []
    for j in range(31, len(days)):
        d = days[j]
        iv = iv_fn(d, kd)
        if iv is None or not np.isfinite(iv):
            continue
        prior = days[max(0, j - 120):j]
        s2 = np.mean([R[p] ** 2 for p in prior], axis=0)          # seasonal r^2 per bar
        end = min(kd + 12, END_BAR) if horizon == 'H12' else END_BAR
        w = slice(kd + 1, end + 1)
        win_s2, day_s2 = s2[w].sum(), s2.sum()
        r = R[d]
        y = np.log(np.sqrt((r[w] ** 2).sum()) * 100 + 1e-6)
        rv_last = np.sqrt((r[max(kd - 11, 0):kd + 1] ** 2).sum()) / np.sqrt(s2[max(kd - 11, 0):kd + 1].sum())
        rv_today = np.sqrt((r[:kd + 1] ** 2).sum()) / np.sqrt(s2[:kd + 1].sum())

        def day_ratio(p):
            return np.sqrt((R[p] ** 2).sum()) / np.sqrt(day_s2)
        rv_1d = day_ratio(days[j - 1])
        rv_5d = np.mean([day_ratio(days[j - i]) for i in range(1, 6)])
        gap = abs(S[d]['O'][0] / S[days[j - 1]]['C'][-1] - 1) * 100
        iv_day = iv / 100 / np.sqrt(DAYCOUNT) * 100                   # % sd per trading day (252 = trading-day basis; 365 = calendar basis)
        iv_h = iv_day * np.sqrt(win_s2 / day_s2)                      # % sd over the window, seasonality-scaled
        ret_h = abs(np.log(S[d]['C'][end] / S[d]['C'][kd])) * 100
        rows.append(dict(day=d, j=j, y=y, f_iv=np.log(iv_h), f_seas=0.5 * np.log(win_s2 * 1e4),
                         f_last=np.log(rv_last + 1e-3), f_today=np.log(rv_today + 1e-3),
                         f_1d=np.log(rv_1d + 1e-3), f_5d=np.log(rv_5d + 1e-3), f_gap=np.log(gap + 0.05),
                         iv_h=iv_h, ret_h=ret_h, rv=np.exp(y)))
    return pd.DataFrame(rows)


def ols(X, y, lam=1e-3):
    A = np.column_stack([np.ones(len(X)), X])
    P = np.eye(A.shape[1]) * lam
    P[0, 0] = 0
    return np.linalg.solve(A.T @ A + P, A.T @ y)


def predict(b, X):
    return b[0] + np.asarray(X) @ b[1:]


MODELS = {
    'M_seas': ['f_seas'],
    'M1': ['f_seas', 'f_last', 'f_today', 'f_1d', 'f_5d', 'f_gap'],
    'M2': ['f_seas', 'f_last', 'f_today', 'f_1d', 'f_5d', 'f_gap', 'f_iv'],
}


def walk_forward(D):
    pred = {k: np.full(len(D), np.nan) for k in list(MODELS) + ['M0']}
    for a in range(N0, len(D), REFIT):
        tr, te = D.iloc[:a], D.iloc[a:a + REFIT]
        for k, cols in MODELS.items():
            b = ols(tr[cols].values, tr['y'].values)
            pred[k][a:a + len(te)] = predict(b, te[cols].values)
        a0 = np.mean(tr['y'].values - tr['f_iv'].values)             # debiased implied: slope fixed at 1
        pred['M0'][a:a + len(te)] = a0 + te['f_iv'].values
    return pred


def boot_ci(x, n=4000):
    x = np.asarray(x, float)
    b = [rng.choice(x, len(x)).mean() for _ in range(n)]
    return np.percentile(b, 2.5), np.percentile(b, 97.5)


def run(name, S, iv_fn, kd, horizon, verdicts):
    D = build(S, iv_fn, kd, horizon)
    if len(D) < N0 + 30:
        print(f'{name}: only {len(D)} usable sessions - skipped')
        return
    pred = walk_forward(D)
    ok = ~np.isnan(pred['M0'])
    Ds = D[ok].reset_index(drop=True)
    y = Ds['y'].values
    err = {k: (y - np.asarray(v)[ok]) ** 2 for k, v in pred.items()}
    n = len(Ds)
    h = n // 2
    print(f'\n--- {name}  decide {"10:30" if kd == 14 else "12:00"}  window {horizon}  (scored sessions {n}) ---')
    print('  MSE of log RV:  ' + '  '.join(f'{k} {err[k].mean():.4f}' for k in ('M_seas', 'M0', 'M1', 'M2')))
    res = {}
    for k in ('M1', 'M2'):
        dlt = err[k] - err['M0']                                       # negative = better than debiased implied
        lo, hi = boot_ci(dlt)
        res[k] = (dlt.mean(), lo, hi, dlt[:h].mean(), dlt[h:].mean())
        print(f'  {k} - M0: {dlt.mean():+.4f}  CI[{lo:+.4f},{hi:+.4f}]  halves {dlt[:h].mean():+.4f}/{dlt[h:].mean():+.4f}'
              f'   {"BEATS implied" if hi < 0 and dlt[:h].mean() < 0 and dlt[h:].mean() < 0 else "not shown"}')
    # Question B: the proxy
    gap = np.asarray(pred['M2'])[ok] - np.asarray(pred['M0'])[ok]
    cut = np.quantile(gap, 2 / 3)
    top = gap >= cut
    payoff = Ds['ret_h'].values - 0.798 * Ds['iv_h'].values - COST_PCT            # % of spot
    ratio = Ds['rv'].values / Ds['iv_h'].values
    for lab, m in (('all', np.ones(n, bool)), ('top third by forecast gap', top), ('rest', ~top)):
        pp = payoff[m] * 100
        lo, hi = boot_ci(pp) if m.sum() > 5 else (np.nan, np.nan)
        first = payoff[m & (np.arange(n) < h)].mean() * 100
        second = payoff[m & (np.arange(n) >= h)].mean() * 100
        print(f'  proxy long-straddle payoff, {lab:<26} n={m.sum():3d}  mean {pp.mean():+7.1f} bps CI[{lo:+6.1f},{hi:+6.1f}]'
              f'  halves {first:+6.1f}/{second:+6.1f}   realised/implied {ratio[m].mean():.2f}')
    verdicts.append((name, kd, horizon, res['M2'], payoff[top].mean() * 100))


def main(bars_dirs, oi_path, vix_path):
    S = load_bars(bars_dirs)
    print(f'{len(S)} bar sessions {list(S)[0]} .. {list(S)[-1]}')
    bnf = load_bnf_iv(oi_path)
    vix = load_vix(vix_path)

    def bnf_fn(d, kd):
        if d not in bnf or is_monthly_expiry(d):
            return None
        t = 9 * 60 + 20 + 5 * kd
        c = [iv for m, iv in bnf[d] if t - 10 <= m <= t + 1]
        return c[-1] if c else None

    def vix_fn(d, kd):
        return float(vix[d][kd]) if d in vix else None

    print(f'IV sessions: BNF {len(bnf)}   VIX {len(vix)}')
    verdicts = []
    for name, fn in (('BNF-IV', bnf_fn), ('VIX', vix_fn)):
        for kd in (14, 33):
            for hz in ('H12', 'EOD'):
                run(name, S, fn, kd, hz, verdicts)

    print('\n================ SUMMARY against the pre-registered bar ================')
    wins = 0
    for name, kd, hz, (m, lo, hi, h1, h2), top_pay in verdicts:
        beat = hi < 0 and h1 < 0 and h2 < 0
        wins += beat
        print(f'  {name:<7} {"10:30" if kd == 14 else "12:00"} {hz:<4} M2-M0 {m:+.4f} CI[{lo:+.4f},{hi:+.4f}] '
              f'{"BEATS" if beat else "no"}   top-third proxy payoff {top_pay:+6.1f} bps (cost already deducted)')
    print(f'\n  tests where M2 beats implied on the pre-registered bar: {wins} of {len(verdicts)} (need >= 4, in both sources)')


if __name__ == '__main__':
    a = sys.argv[1:]
    main(a[0].split(';'), a[1], a[2])
