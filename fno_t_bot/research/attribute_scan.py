# -*- coding: utf-8 -*-
"""
Systematic attribute -> outcome scan
====================================

The disciplined version of "learn from the wins and losses".

Every configuration change this project made in the week of Aug 17-21 came from
inspecting one or two losing trades and inferring a cause. Deflated Sharpe later
showed the whole set was indistinguishable from noise. The failure was not the
intent -- it was the method: pick a loser, find something unusual about it,
change a parameter. With enough attributes, every trade looks unusual in some
way.

This does the opposite. It tests EVERY logged attribute against outcome at once,
reports all of them including the failures, and applies a multiple-testing
correction based on how many were tested. A feature only counts if it clears
that corrected bar.

Two outcome definitions are used because they can disagree:
  win/loss    -- Mann-Whitney U on the attribute, winners vs losers
  P&L         -- Spearman rank correlation between attribute and pnl_net
"""
from __future__ import annotations
import math
import statistics as st


def _rankdata(x):
    """Average ranks, ties shared."""
    order = sorted(range(len(x)), key=lambda i: x[i])
    ranks = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a, b):
    if len(a) < 4:
        return 0.0, 1.0
    ra, rb = _rankdata(a), _rankdata(b)
    n = len(a)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if da == 0 or db == 0:
        return 0.0, 1.0
    rho = num / (da * db)
    # t approximation
    if abs(rho) >= 1.0:
        return rho, 0.0
    t = rho * math.sqrt((n - 2) / (1 - rho ** 2))
    p = 2 * (1 - _t_cdf(abs(t), n - 2))
    return rho, max(min(p, 1.0), 0.0)


def _t_cdf(t, df):
    """Student-t CDF via incomplete beta; adequate for reporting p-values."""
    x = df / (df + t * t)
    return 1 - 0.5 * _betainc(df / 2.0, 0.5, x)


def _betainc(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(200):
        m = i // 2
        if i == 0:      num = 1.0
        elif i % 2 == 0: num = (m * (b - m) * x) / ((a + 2*m - 1) * (a + 2*m))
        else:            num = -((a + m) * (a + b + m) * x) / ((a + 2*m) * (a + 2*m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30: d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30: c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-8:
            break
    return front * (f - 1.0)


def mannwhitney(a, b):
    """Two-sided Mann-Whitney U with normal approximation."""
    na, nb = len(a), len(b)
    if na < 4 or nb < 4:
        return 0.5, 1.0
    allv = a + b
    r = _rankdata(allv)
    ra = sum(r[:na])
    u1 = ra - na * (na + 1) / 2.0
    u2 = na * nb - u1
    u = min(u1, u2)
    mu = na * nb / 2.0
    sd = math.sqrt(na * nb * (na + nb + 1) / 12.0)
    if sd == 0:
        return 0.5, 1.0
    z = (u - mu) / sd
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    auc = u1 / (na * nb)          # probability a winner exceeds a loser
    return auc, max(min(p, 1.0), 0.0)


def scan(trades, fields, min_n=20):
    rows = []
    for f in fields:
        pairs = [(t[f], t.get('pnl_net', 0.0)) for t in trades
                 if isinstance(t.get(f), (int, float)) and not isinstance(t.get(f), bool)]
        if len(pairs) < min_n:
            continue
        xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
        win = [x for x, y in pairs if y > 0]
        los = [x for x, y in pairs if y <= 0]
        auc, p_mw = mannwhitney(win, los)
        rho, p_sp = spearman(xs, ys)
        rows.append(dict(field=f, n=len(pairs), n_win=len(win), n_los=len(los),
                         auc=auc, p_mw=p_mw, rho=rho, p_sp=p_sp,
                         med_win=st.median(win) if win else float('nan'),
                         med_los=st.median(los) if los else float('nan')))
    return rows


def report(rows, label=''):
    if not rows:
        print('  no field had enough coverage'); return
    k = len(rows) * 2                      # two tests per field
    bonf = 0.05 / k
    print('=' * 100)
    print(f'ATTRIBUTE SCAN {label}   fields={len(rows)}  tests={k}  '
          f'Bonferroni bar p<{bonf:.5f}')
    print('=' * 100)
    print(f"{'attribute':22s} {'n':>4s} {'medWin':>10s} {'medLoss':>10s} "
          f"{'AUC':>6s} {'p(W/L)':>8s} {'rho':>7s} {'p(pnl)':>8s}  flag")
    print('-' * 100)
    for r in sorted(rows, key=lambda x: min(x['p_mw'], x['p_sp'])):
        best = min(r['p_mw'], r['p_sp'])
        flag = ('SURVIVES' if best < bonf else
                'raw<0.05' if best < 0.05 else '')
        print(f"{r['field']:22s} {r['n']:4d} {r['med_win']:10.4f} {r['med_los']:10.4f} "
              f"{r['auc']:6.3f} {r['p_mw']:8.4f} {r['rho']:+7.3f} {r['p_sp']:8.4f}  {flag}")
    surv = [r for r in rows if min(r['p_mw'], r['p_sp']) < bonf]
    raw  = [r for r in rows if bonf <= min(r['p_mw'], r['p_sp']) < 0.05]
    print()
    print(f"  survive correction : {len(surv)}"
          + (f"  -> {', '.join(r['field'] for r in surv)}" if surv else ''))
    print(f"  raw p<0.05 only    : {len(raw)}"
          + (f"  -> {', '.join(r['field'] for r in raw)}" if raw else ''))
    print(f"  expected false positives at raw 0.05 across {k} tests: {0.05*k:.1f}")
    print()
    print("  AUC = P(a winner scores higher than a loser). 0.50 is no information.")
