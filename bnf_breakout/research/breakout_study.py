# -*- coding: utf-8 -*-
"""Can a BANKNIFTY breakout or breakdown be anticipated from compression, key
levels and volume? A pre-registered test on 5-minute history.

    python breakout_study.py [DATA_DIR]

PRE-REGISTRATION. The variants below were fixed BEFORE any result was seen, and
every one is reported, so a variant that works by luck cannot be quietly picked
out of a longer list. There are 8 of them. At a 5% bar, one will clear it by
chance roughly one time in three.

WHAT IS MEASURED. Index-level expectancy, not option P&L. Historical option
premiums do not exist (Fyers delists expired contracts), so each setup is run as
the trade an option buyer would effectively be making:

    stop      S   % of spot adverse        (~ the 25% premium stop at delta 0.5)
    target    T   % of spot favourable     (~ the 55% premium target)
    time stop H   bars
    cost      C   % of spot per round trip (commission + spread, ~Rs78 on a
                  Rs11.6k premium, plus slippage)

If stop and target are both inside one bar the STOP is assumed to hit first, the
conservative choice. One trigger per session per variant, so observations are
independent (overlapping triggers inflate n and were exactly how the old
rv_iv p-value of 4.9e-77 was manufactured).

THE BASELINE. Same exits, entered at a random bar in a random direction. A setup
only means something relative to that, because a 2.2:1 reward-to-risk payoff has
a positive expectancy at a hit rate that random entry may already achieve.
"""
import glob, math, os, random, statistics as st, sys

import numpy as np
import pandas as pd

DATA = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\Varun Kamath\AppData\Local\Temp\claude\C--Program-Files-Git\cb28b681-71c2-46aa-822a-29e400e15f68\scratchpad\bnf'

# --- exits, fixed in advance -------------------------------------------------
STOP_PCT = 0.35
TARGET_PCT = 0.77
HOLD_BARS = 24           # 2 hours
COST_PCT = 0.08
# --- entry window ------------------------------------------------------------
FIRST_BAR = 7            # 09:50, once the 09:15-09:45 opening range exists
LAST_BAR = 54            # ~13:45
# --- setup parameters, fixed in advance --------------------------------------
LEVEL_PROX = 0.15        # % of spot
COIL_BARS = 6
COIL_Q = 1 / 3           # bottom third of compression ratio
VOL_BASE_SESSIONS = 20


def load_sessions():
    out = []
    for f in sorted(glob.glob(os.path.join(DATA, '*.csv'))):
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        if len(d) < 60:
            continue
        d['ts'] = pd.to_datetime(d['ts'])
        d = d[(d['ts'].dt.strftime('%H:%M') >= '09:15') & (d['ts'].dt.strftime('%H:%M') <= '15:25')]
        d = d.reset_index(drop=True)
        if len(d) < 60:
            continue
        out.append(dict(day=d['ts'].iloc[0].strftime('%Y-%m-%d'), d=d))
    return out


def prep(sessions):
    """Attach previous-day levels, opening range, volume baseline, compression."""
    vol_hist = []                                   # per-session volume arrays
    prev = None
    for s in sessions:
        d = s['d']
        s['H'] = d['High'].values.astype(float)
        s['L'] = d['Low'].values.astype(float)
        s['C'] = d['Close'].values.astype(float)
        s['V'] = d['Volume'].values.astype(float)
        n = len(d)
        s['n'] = n
        s['pdh'] = prev['H'].max() if prev is not None else None
        s['pdl'] = prev['L'].min() if prev is not None else None
        s['orh'] = s['H'][:6].max()
        s['orl'] = s['L'][:6].min()
        # volume at the same bar of day, median over the previous sessions
        hist = [v for v in vol_hist[-VOL_BASE_SESSIONS:] if v is not None]
        base = np.full(n, np.nan)
        if len(hist) >= 10:
            for b in range(n):
                xs = [h[b] for h in hist if len(h) > b and h[b] > 0]
                if len(xs) >= 8:
                    base[b] = float(np.median(xs))
        s['vbase'] = base
        s['has_vol'] = bool(s['V'].sum() > 0)
        vol_hist.append(s['V'] if s['has_vol'] else None)
        # compression ratio: range of last COIL_BARS bars over what the recent
        # per-bar ranges predict for that many bars
        tr = s['H'] - s['L']
        cr = np.full(n, np.nan)
        for b in range(15, n):
            atr = tr[b - 14:b + 1].mean()
            if atr > 0:
                rng = s['H'][b - COIL_BARS + 1:b + 1].max() - s['L'][b - COIL_BARS + 1:b + 1].min()
                cr[b] = rng / (atr * math.sqrt(COIL_BARS))
        s['cr'] = cr
        prev = s
    return sessions


def simulate(s, i, direction):
    """R in % of spot for one trade entered at the close of bar i."""
    entry = s['C'][i]
    n = s['n']
    end = min(i + HOLD_BARS, n - 2)
    for j in range(i + 1, end + 1):
        fav = (s['H'][j] - entry) / entry * 100 if direction > 0 else (entry - s['L'][j]) / entry * 100
        adv = (entry - s['L'][j]) / entry * 100 if direction > 0 else (s['H'][j] - entry) / entry * 100
        if adv >= STOP_PCT:
            return -STOP_PCT - COST_PCT, 'stop'
        if fav >= TARGET_PCT:
            return TARGET_PCT - COST_PCT, 'target'
    last = s['C'][end]
    mv = (last - entry) / entry * 100 * direction
    return mv - COST_PCT, 'time'


def levels_above(s, i, px):
    cands = []
    for nm, lv in (('PDH', s['pdh']), ('ORH', s['orh']), ('DAYH', s['H'][:i + 1].max())):
        if lv is not None and lv > px:
            cands.append((lv, nm))
    return min(cands) if cands else (None, None)


def levels_below(s, i, px):
    cands = []
    for nm, lv in (('PDL', s['pdl']), ('ORL', s['orl']), ('DAYL', s['L'][:i + 1].min())):
        if lv is not None and lv < px:
            cands.append((lv, nm))
    return max(cands) if cands else (None, None)


def trig_press(s, i, need_coil=False, vmode=None, trend=False):
    """Press against the nearest key level -> trade toward it, before it breaks."""
    px = s['C'][i]
    up, _ = levels_above(s, i, px)
    dn, _ = levels_below(s, i, px)
    du = (up - px) / px * 100 if up else 9e9
    dd = (px - dn) / px * 100 if dn else 9e9
    if min(du, dd) > LEVEL_PROX:
        return 0
    direction = 1 if du <= dd else -1
    if need_coil:
        c = s['cr'][i]
        if not (c == c) or c > s['_cr_cut']:
            return 0
    if vmode:
        vb = s['vbase'][i - 2:i + 1]
        vv = s['V'][i - 2:i + 1]
        if not s['has_vol'] or np.isnan(vb).any() or (vb <= 0).any():
            return None            # cannot evaluate -> skip this session
        rel = float(np.mean(vv / vb))
        if vmode == 'participate' and rel < 1.0:
            return 0
        if vmode == 'dryup' and rel > 0.8:
            return 0
    if trend:
        if i < 20:
            return 0
        slope = s['C'][i] - s['C'][i - 12]
        if (slope > 0) != (direction > 0):
            return 0
    return direction


def trig_confirm(s, i):
    """Confirmation breakout: new 12-bar extreme on a volume surge (the usual way)."""
    if i < 14 or not s['has_vol']:
        return None if not s['has_vol'] else 0
    vb = s['vbase'][i]
    if not (vb == vb) or vb <= 0:
        return None
    if s['V'][i] / vb < 1.5:
        return 0
    if s['C'][i] > s['H'][i - 12:i].max():
        return 1
    if s['C'][i] < s['L'][i - 12:i].min():
        return -1
    return 0


def trig_failed_break(s, i):
    """Fade a failed break: a key level was pierced in the last 3 bars and the
    close is back inside it. Trade the reversal."""
    px = s['C'][i]
    for nm, lv in (('PDH', s['pdh']), ('ORH', s['orh'])):
        if lv is None:
            continue
        if s['H'][i - 3:i + 1].max() > lv * (1 + 0.0005) and px < lv:
            return -1
    for nm, lv in (('PDL', s['pdl']), ('ORL', s['orl'])):
        if lv is None:
            continue
        if s['L'][i - 3:i + 1].min() < lv * (1 - 0.0005) and px > lv:
            return 1
    return 0


def run_variant(name, sessions, trig):
    res = []
    for s in sessions:
        if s['pdh'] is None:
            continue
        hit = None
        for i in range(FIRST_BAR, min(LAST_BAR, s['n'] - HOLD_BARS - 2)):
            d = trig(s, i)
            if d is None:               # variant not evaluable this session
                hit = 'skip'
                break
            if d != 0:
                r, how = simulate(s, i, d)
                hit = (s['day'], r, how, d)
                break
        if hit and hit != 'skip':
            res.append(hit)
    return res


def summarise(name, res, base_mean=None):
    if not res:
        print(f'  {name:46} n=0')
        return None
    r = [x[1] for x in res]
    n = len(r)
    tgt = sum(1 for x in res if x[2] == 'target') / n * 100
    stp = sum(1 for x in res if x[2] == 'stop') / n * 100
    h = n // 2
    a, b = (st.mean(r[:h]) if h else 0), (st.mean(r[h:]) if n - h else 0)
    sd = st.stdev(r) if n > 1 else 0
    se = sd / math.sqrt(n) if n > 1 else 0
    lo, hi = st.mean(r) - 1.96 * se, st.mean(r) + 1.96 * se
    verdict = ''
    if n >= 30 and lo > 0 and a > 0 and b > 0:
        verdict = '  <- clears: CI>0 and both halves>0'
    print(f'  {name:46} n={n:3} tgt {tgt:4.1f}% stop {stp:4.1f}%  mean {st.mean(r):+.3f}%'
          f'  CI[{lo:+.3f},{hi:+.3f}]  halves {a:+.3f}/{b:+.3f}{verdict}')
    return st.mean(r)


def main():
    sessions = prep(load_sessions())
    vs = [s for s in sessions if s['has_vol']]
    print('=' * 118)
    print('BANKNIFTY — can a breakout/breakdown be anticipated?  (pre-registered, 8 variants)')
    print('=' * 118)
    print(f'  {len(sessions)} sessions ({sessions[0]["day"]} .. {sessions[-1]["day"]}); '
          f'{len(vs)} carry usable volume')
    print(f'  exits: stop {STOP_PCT}%  target {TARGET_PCT}%  time {HOLD_BARS} bars  cost {COST_PCT}% '
          f'(reward:risk {TARGET_PCT / STOP_PCT:.1f}:1, break-even hit-rate '
          f'{(STOP_PCT + COST_PCT) / (TARGET_PCT + STOP_PCT) * 100:.0f}%)')

    # compression cut: bottom third of the pooled compression ratio
    allcr = np.concatenate([s['cr'][~np.isnan(s['cr'])] for s in sessions])
    cut = float(np.quantile(allcr, COIL_Q))
    for s in sessions:
        s['_cr_cut'] = cut
    print(f'  compression cut (bottom third): range6 / (ATR14*sqrt(6)) <= {cut:.3f}\n')

    # baseline: a random bar, random direction, one per session
    random.seed(11)
    base = []
    for s in sessions:
        if s['pdh'] is None:
            continue
        i = random.randint(FIRST_BAR, min(LAST_BAR, s['n'] - HOLD_BARS - 3))
        r, how = simulate(s, i, random.choice((1, -1)))
        base.append((s['day'], r, how, 0))
    bm = summarise('V0  BASELINE random bar, random direction', base)

    print()
    summarise('V1  press a key level (no other filter)', run_variant('V1', sessions, lambda s, i: trig_press(s, i)), bm)
    summarise('V2  V1 + price compression', run_variant('V2', sessions, lambda s, i: trig_press(s, i, True)), bm)
    summarise('V3  V2 + volume PARTICIPATING (>=1.0x norm)', run_variant('V3', sessions, lambda s, i: trig_press(s, i, True, 'participate')), bm)
    summarise('V4  V2 + volume DRY-UP (<=0.8x norm)', run_variant('V4', sessions, lambda s, i: trig_press(s, i, True, 'dryup')), bm)
    summarise('V5  CONFIRMATION breakout on volume surge', run_variant('V5', sessions, trig_confirm), bm)
    summarise('V6  fade a FAILED break of PDH/PDL/OR', run_variant('V6', sessions, trig_failed_break), bm)
    summarise('V7  V2 + agree with 12-bar trend', run_variant('V7', sessions, lambda s, i: trig_press(s, i, True, None, True)), bm)

    # the same V2 with the volume-only sessions, for a like-for-like comparison
    print('\n  (V3/V4/V5 can only be evaluated on sessions that carry volume; compare them with V2 on the SAME sessions:)')
    v2v = run_variant('V2v', vs, lambda s, i: trig_press(s, i, True))
    summarise('V2  restricted to volume sessions', v2v, bm)


if __name__ == '__main__':
    main()
