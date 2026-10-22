# -*- coding: utf-8 -*-
"""Do PCR, max-pain, IV skew and ADX predict the next hour? (And is it worth recording them?)

    python oi_pcr_adx_study.py OI_INTRADAY.jsonl BNF_5MIN_DIR

Three decision times a session (10:00, 11:30, 13:00), forward 60-minute outcome in bps of
spot. Every signal is tested for DIRECTION (does the sign of the signal predict the sign of
the move?) and, where it makes sense, MAGNITUDE (does its size predict how far price goes?).
Effect sizes are in bps so they can be set against the ~8 bps a round trip costs.

PCR and max-pain from the option chain exist only for the 121 sessions the old bots recorded
(Apr 28 - Oct 21 2026) - Fyers deletes expired chains, so they cannot be rebuilt later.
ADX is derived purely from price, so it is tested on all 420 sessions.

Why PCR is z-scored: it sits near a constant (the parked quad voted bullish on it 61% of the
time and bearish 4%), so its raw level mostly measures the instrument's usual offset. The
z-score compares today's PCR with the same time of day over the PRIOR 20 sessions only.
"""
import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bnf_data  # noqa: E402

TIMES = ('10:00', '11:30', '13:00')
COST_BPS = 8.0


def rho_p(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 10 or x.std() == 0 or y.std() == 0:
        return float('nan'), float('nan')
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    r = float(np.corrcoef(rx, ry)[0, 1])
    t = r * math.sqrt((len(x) - 2) / max(1 - r * r, 1e-12))
    return r, 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))


def hhmm_add(t, m):
    h, mm = int(t[:2]), int(t[3:])
    s = h * 60 + mm + m
    return f'{s // 60:02d}:{s % 60:02d}'


def show(label, sig, fwd, mag=False):
    sig, fwd = np.asarray(sig, float), np.asarray(fwd, float)
    ok = np.isfinite(sig) & np.isfinite(fwd)
    sig, fwd = sig[ok], fwd[ok]
    n = len(sig)
    if n < 20:
        print(f'  {label:46} n={n} (too few)')
        return
    r, p = rho_p(sig, fwd)
    if mag:
        print(f'  {label:46} n={n:4} rho {r:+.3f} (p={p:.2g})')
        return
    pnl = np.sign(sig) * fwd
    big = np.abs(sig) >= np.quantile(np.abs(sig), 2 / 3)
    h = n // 2
    se = pnl.std(ddof=1) / math.sqrt(n)
    print(f'  {label:46} n={n:4} rho {r:+.3f} (p={p:.2g}) hit {100 * (pnl > 0).mean():4.1f}%'
          f'  follow-the-sign mean {pnl.mean():+6.2f} bps (+/-{1.96 * se:.1f})'
          f'  halves {pnl[:h].mean():+.1f}/{pnl[h:].mean():+.1f}  strongest third {pnl[big].mean():+.1f}')


# ── PCR / max-pain / skew ────────────────────────────────────────────────
def load_oi(path):
    rows = defaultdict(list)
    for ln in open(path, encoding='utf-8', errors='ignore'):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not d.get('spot') or d.get('pcr') is None:
            continue
        rows[(d['instrument'], d['ts_str'][:10])].append(
            (d['ts_str'][11:16], float(d['spot']), float(d['pcr']), d.get('max_pain'),
             d.get('iv_skew'), d.get('atm_iv')))
    for k in rows:
        rows[k].sort()
    return rows


def at(series, hhmm):
    last = None
    for r in series:
        if r[0] <= hhmm:
            last = r
        else:
            break
    return last


def oi_study(path):
    rows = load_oi(path)
    for inst in ('BANKNIFTY', 'NIFTY'):
        days = sorted(d for (i, d) in rows if i == inst)
        print(f'\n--- {inst}: PCR / max-pain / skew vs the next 60 minutes ({len(days)} sessions) ---')
        sig = defaultdict(list)
        mags = defaultdict(list)
        for T0 in TIMES:
            hist = {}
            for d in days:
                r = at(rows[(inst, d)], T0)
                if r:
                    hist[d] = r
            for k, d in enumerate(days):
                if d not in hist or k < 21:
                    continue
                prior = [hist[e] for e in days[k - 20:k] if e in hist]
                if len(prior) < 15:
                    continue
                r0 = hist[d]
                r1 = at(rows[(inst, d)], hhmm_add(T0, 60))
                rb = at(rows[(inst, d)], hhmm_add(T0, -60))
                if not r1 or r1[0] <= T0:
                    continue
                fwd = (r1[1] / r0[1] - 1) * 1e4
                pcrs = np.array([p[2] for p in prior])
                z = (r0[2] - pcrs.mean()) / (pcrs.std() + 1e-9)
                sig['PCR z-score (vs prior 20 sessions)'].append((z, fwd))
                if rb:
                    sig['PCR change over the prior hour'].append((r0[2] - rb[2], fwd))
                if r0[3]:
                    sig['max-pain distance (max_pain - spot, %)'].append(((r0[3] - r0[1]) / r0[1] * 100, fwd))
                sk = [p[4] for p in prior if p[4] is not None]
                if r0[4] is not None and len(sk) >= 15:
                    sig['IV skew z-score'].append(((r0[4] - np.mean(sk)) / (np.std(sk) + 1e-9), fwd))
                mags['|PCR z-score| -> size of next move'].append((abs(z), abs(fwd)))
                if rb:
                    mags['|PCR change| -> size of next move'].append((abs(r0[2] - rb[2]), abs(fwd)))
        print(f'  DIRECTION: does the sign of the signal predict the sign of the next hour?')
        for k, v in sig.items():
            a = np.array(v)
            show(k, a[:, 0], a[:, 1])
        print(f'  MAGNITUDE: does the size of the signal predict how far price moves?')
        for k, v in mags.items():
            a = np.array(v)
            show(k, a[:, 0], a[:, 1], mag=True)


# ── ADX ───────────────────────────────────────────────────────────────────
def adx_arrays(H, L, C, n=14):
    up, dn = np.diff(H, prepend=H[0]), -np.diff(L, prepend=L[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum.reduce([H - L, np.abs(H - np.r_[C[0], C[:-1]]), np.abs(L - np.r_[C[0], C[:-1]])])

    def wilder(x):
        out = np.empty(len(x))
        out[0] = x[0]
        a = 1.0 / n
        for i in range(1, len(x)):
            out[i] = out[i - 1] + a * (x[i] - out[i - 1])
        return out

    atr = wilder(tr)
    pdi = 100 * wilder(pdm) / np.maximum(atr, 1e-9)
    ndi = 100 * wilder(ndm) / np.maximum(atr, 1e-9)
    dx = 100 * np.abs(pdi - ndi) / np.maximum(pdi + ndi, 1e-9)
    return wilder(dx), pdi, ndi


def adx_study(d):
    sess = bnf_data.load_history(d)
    print(f'\n--- ADX / DI (BANKNIFTY, {len(sess)} sessions, all derived from price) ---')
    bar = {'10:00': 8, '11:30': 26, '13:00': 44}
    A, DIR, FWD, FABS, FRNG = [], [], [], [], []
    for k in range(2, len(sess)):
        s = sess[k]
        if len(s['C']) < 75:
            continue
        H = np.r_[sess[k - 2]['H'], sess[k - 1]['H'], s['H']]
        L = np.r_[sess[k - 2]['L'], sess[k - 1]['L'], s['L']]
        C = np.r_[sess[k - 2]['C'], sess[k - 1]['C'], s['C']]
        off = len(sess[k - 2]['C']) + len(sess[k - 1]['C'])
        adx, pdi, ndi = adx_arrays(H, L, C)
        for T0, i in bar.items():
            j = off + i
            if i + 12 >= len(s['C']):
                continue
            fwd = (s['C'][i + 12] / s['C'][i] - 1) * 1e4
            rng = (s['H'][i + 1:i + 13].max() - s['L'][i + 1:i + 13].min()) / s['C'][i] * 1e4
            A.append(adx[j])
            DIR.append(pdi[j] - ndi[j])
            FWD.append(fwd)
            FABS.append(abs(fwd))
            FRNG.append(rng)
    A, DIR, FWD, FABS, FRNG = (np.array(x) for x in (A, DIR, FWD, FABS, FRNG))
    print(f'  {len(A)} observations; ADX median {np.median(A):.1f}, 80th percentile {np.quantile(A, .8):.1f}')
    print('  MAGNITUDE: does ADX predict how far the next hour goes?')
    show('ADX level -> |next-hour move|', A, FABS, mag=True)
    show('ADX level -> next-hour high-low range', A, FRNG, mag=True)
    print('  DIRECTION: does +DI minus -DI predict the next hour?')
    show('(+DI - -DI), all observations', DIR, FWD)
    hi, lo = A >= 25, A < 25
    show('(+DI - -DI) when ADX >= 25 (a "trend")', DIR[hi], FWD[hi])
    show('(+DI - -DI) when ADX < 25', DIR[lo], FWD[lo])
    top = A >= np.quantile(A, 0.8)
    show('(+DI - -DI) when ADX in its top 20%', DIR[top], FWD[top])
    print('  (a NEGATIVE follow-the-sign mean means the trend REVERSED: the "ADX inversion" noted earlier)')


def main():
    oi_study(sys.argv[1])
    adx_study(sys.argv[2] if len(sys.argv) > 2 else r'C:\Users\Varun Kamath\AppData\Local\Temp\claude\C--Program-Files-Git\cb28b681-71c2-46aa-822a-29e400e15f68\scratchpad\bnf')
    print(f'\nA round trip costs about {COST_BPS:.0f} bps: a follow-the-sign mean has to clear that, in both halves.')


if __name__ == '__main__':
    main()
