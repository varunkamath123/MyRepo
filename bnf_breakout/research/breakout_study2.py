# -*- coding: utf-8 -*-
"""Second pre-registered pass: a trailing exit, timing-vs-direction, a volatility
regime filter, and an out-of-sample walk-forward.

Run AFTER breakout_study.py, which found that under a fixed 2.2:1 stop/target
every setup loses and none beats a random entry (random -0.058%/trade, best
variant -0.052%). One exit shape is not the whole question -- the live bot earns
its money from trailing exits, not fixed targets -- so this fixes a second family
in advance and asks four things:

  A. TRAILING EXIT. Stop S; once the trade is +A% in favour, trail D% behind the
     best price; no fixed target. Same costs, same one-trigger-per-session rule.
  B. TIMING vs DIRECTION. A setup can add value by saying WHEN (a big move is
     coming) or WHICH WAY (it will be up). Those are different claims. For each
     trigger this reports the forward 2-hour range (timing: is expansion more
     likely than usual?) and the trade taken toward the level, AWAY from it, and
     the average of the two (which is what you get if direction is a coin flip).
  C. VOLATILITY REGIME. The only conditioner that survived scrutiny earlier in
     this project is trailing realised volatility (payable share +26.6 points
     top-vs-bottom quintile against an IV-adjusted break-even, both halves).
     Does restricting setups to a hot regime change anything?
  D. WALK-FORWARD. Choose the setup parameters on the first 60% of sessions,
     report ONLY the last 40%. A grid searched on all the data and reported on
     all the data is how a 4.9e-77 p-value gets manufactured.
"""
import itertools, math, random, statistics as st, sys

import numpy as np

import breakout_study as B

STOP, ARM, TRAIL, HOLD = 0.35, 0.30, 0.25, 24


def sim_trail(s, i, direction, stop=STOP, arm=ARM, trail=TRAIL, hold=HOLD):
    entry = s['C'][i]
    n = s['n']
    end = min(i + hold, n - 2)
    best = 0.0
    for j in range(i + 1, end + 1):
        if direction > 0:
            adv = (entry - s['L'][j]) / entry * 100
            fav = (s['H'][j] - entry) / entry * 100
        else:
            adv = (s['H'][j] - entry) / entry * 100
            fav = (entry - s['L'][j]) / entry * 100
        floor = -stop if best < arm else max(-stop, best - trail)
        if adv >= -floor:                       # exit at the floor level
            return floor - B.COST_PCT, 'trail' if best >= arm else 'stop'
        best = max(best, fav)
    mv = (s['C'][end] - entry) / entry * 100 * direction
    return mv - B.COST_PCT, 'time'


def fwd_range(s, i, hold=HOLD):
    end = min(i + hold, s['n'] - 2)
    return (s['H'][i + 1:end + 1].max() - s['L'][i + 1:end + 1].min()) / s['C'][i] * 100


def first_trigger(sessions, trig):
    out = []
    for s in sessions:
        if s['pdh'] is None:
            continue
        for i in range(B.FIRST_BAR, min(B.LAST_BAR, s['n'] - B.HOLD_BARS - 2)):
            d = trig(s, i)
            if d is None:
                break
            if d != 0:
                out.append((s, i, d))
                break
    return out


def stats(label, rs, base=None):
    if not rs:
        print(f'  {label:52} n=0'); return None
    n = len(rs); m = st.mean(rs)
    sd = st.stdev(rs) if n > 1 else 0
    se = sd / math.sqrt(n) if n > 1 else 0
    h = n // 2
    a = st.mean(rs[:h]) if h else 0
    b = st.mean(rs[h:]) if n - h else 0
    flag = '  <- CI>0 & both halves>0' if (n >= 30 and m - 1.96 * se > 0 and a > 0 and b > 0) else ''
    print(f'  {label:52} n={n:3} mean {m:+.3f}%  CI[{m-1.96*se:+.3f},{m+1.96*se:+.3f}]  halves {a:+.3f}/{b:+.3f}{flag}')
    return m


def main():
    sessions = B.prep(B.load_sessions())
    for s in sessions:
        # previous-session realised vol, and whether it is "hot" vs its own trailing 60
        pass
    rv = []
    prev_rv = {}
    for k, s in enumerate(sessions):
        r = np.diff(np.log(s['C']))
        rv.append(float(np.std(r)))
    for k in range(1, len(sessions)):
        window = rv[max(0, k - 61):k - 1]
        sessions[k]['hot'] = bool(len(window) >= 20 and rv[k - 1] > float(np.median(window)))
    allcr = np.concatenate([s['cr'][~np.isnan(s['cr'])] for s in sessions])
    cut = float(np.quantile(allcr, B.COIL_Q))
    for s in sessions:
        s['_cr_cut'] = cut

    print('=' * 112)
    print('BANKNIFTY breakout anticipation — pass 2: trailing exit, timing vs direction, regime, walk-forward')
    print('=' * 112)
    print(f'  trailing exit: stop {STOP}%, arm at +{ARM}%, trail {TRAIL}% behind best, hold {HOLD} bars, cost {B.COST_PCT}%')

    random.seed(11)
    base_r, base_rng = [], []
    for s in sessions:
        if s['pdh'] is None:
            continue
        i = random.randint(B.FIRST_BAR, min(B.LAST_BAR, s['n'] - B.HOLD_BARS - 3))
        base_r.append(sim_trail(s, i, random.choice((1, -1)))[0])
        base_rng.append(fwd_range(s, i))
    print('\n--- A. trailing exit, one trigger per session ---')
    stats('V0  BASELINE random bar, random direction', base_r)
    variants = {
        'V1  press a key level': lambda s, i: B.trig_press(s, i),
        'V2  V1 + compression': lambda s, i: B.trig_press(s, i, True),
        'V3  V2 + volume participating': lambda s, i: B.trig_press(s, i, True, 'participate'),
        'V4  V2 + volume dry-up': lambda s, i: B.trig_press(s, i, True, 'dryup'),
        'V5  confirmation breakout on volume surge': B.trig_confirm,
        'V6  fade a failed break': B.trig_failed_break,
        'V7  V2 + trend agreement': lambda s, i: B.trig_press(s, i, True, None, True),
    }
    trig_sets = {}
    for nm, fn in variants.items():
        ts = first_trigger(sessions, fn)
        trig_sets[nm] = ts
        stats(nm, [sim_trail(s, i, d)[0] for s, i, d in ts])

    print('\n--- B. TIMING vs DIRECTION (forward 2h range, and toward/away/coin-flip) ---')
    print(f'  baseline forward range: mean {st.mean(base_rng):.3f}%  median {st.median(base_rng):.3f}%')
    print(f'  {"variant":44} {"n":>4} {"fwd range":>10} {"vs base":>8}   {"toward":>8} {"away":>8} {"coin-flip":>9}')
    for nm in ('V1  press a key level', 'V2  V1 + compression', 'V3  V2 + volume participating',
               'V4  V2 + volume dry-up', 'V6  fade a failed break'):
        ts = trig_sets[nm]
        if not ts:
            continue
        rngs = [fwd_range(s, i) for s, i, d in ts]
        tw = [sim_trail(s, i, d)[0] for s, i, d in ts]
        aw = [sim_trail(s, i, -d)[0] for s, i, d in ts]
        print(f'  {nm:44} {len(ts):>4} {st.mean(rngs):>9.3f}% {st.mean(rngs)-st.mean(base_rng):>+7.3f}%'
              f'   {st.mean(tw):>+7.3f}% {st.mean(aw):>+7.3f}% {(st.mean(tw)+st.mean(aw))/2:>+8.3f}%')
    print('  (a setup that predicts WHEN shows a bigger forward range than baseline;')
    print('   one that predicts WHICH WAY shows toward >> away.)')

    print('\n--- C. restrict to a HOT volatility regime (prev session RV above its trailing median) ---')
    hot = [s for s in sessions if s.get('hot')]
    cold = [s for s in sessions if s.get('hot') is False]
    print(f'  hot sessions {len(hot)}, cold sessions {len(cold)}')
    for nm in ('V2  V1 + compression', 'V6  fade a failed break'):
        fn = variants[nm]
        for lab, ss in (('HOT', hot), ('COLD', cold)):
            ts = first_trigger(ss, fn)
            stats(f'{nm} [{lab}]', [sim_trail(s, i, d)[0] for s, i, d in ts])
    hb = [sim_trail(s, random.randint(B.FIRST_BAR, min(B.LAST_BAR, s['n'] - B.HOLD_BARS - 3)),
                    random.choice((1, -1)))[0] for s in hot if s['pdh'] is not None]
    cb = [sim_trail(s, random.randint(B.FIRST_BAR, min(B.LAST_BAR, s['n'] - B.HOLD_BARS - 3)),
                    random.choice((1, -1)))[0] for s in cold if s['pdh'] is not None]
    stats('V0  BASELINE [HOT]', hb)
    stats('V0  BASELINE [COLD]', cb)

    print('\n--- D. WALK-FORWARD: tune on the first 60% of sessions, report only the last 40% ---')
    k = int(len(sessions) * 0.6)
    train, test = sessions[:k], sessions[k:]
    print(f'  train {train[0]["day"]}..{train[-1]["day"]} ({len(train)})   test {test[0]["day"]}..{test[-1]["day"]} ({len(test)})')
    grid = list(itertools.product((0.10, 0.15, 0.25), (0.25, 0.33, 0.50), ('toward', 'away'), (False, True)))
    scored = []
    for prox, q, way, needhot in grid:
        B.LEVEL_PROX = prox
        cutq = float(np.quantile(allcr, q))
        for s in sessions:
            s['_cr_cut'] = cutq
        pool = [s for s in train if (s.get('hot') or not needhot)]
        ts = first_trigger(pool, lambda s, i: B.trig_press(s, i, True))
        rs = [sim_trail(s, i, d if way == 'toward' else -d)[0] for s, i, d in ts]
        if len(rs) >= 30:
            scored.append((st.mean(rs), len(rs), (prox, q, way, needhot)))
    scored.sort(reverse=True)
    print(f'  {len(grid)} combinations tried on TRAIN; best 3 in-sample:')
    for m, n, p in scored[:3]:
        print(f'    train mean {m:+.3f}% n={n}  params prox={p[0]} coil_q={p[1]} dir={p[2]} hot_only={p[3]}')
    if scored:
        m, n, (prox, q, way, needhot) = scored[0]
        B.LEVEL_PROX = prox
        cutq = float(np.quantile(allcr, q))
        for s in sessions:
            s['_cr_cut'] = cutq
        pool = [s for s in test if (s.get('hot') or not needhot)]
        ts = first_trigger(pool, lambda s, i: B.trig_press(s, i, True))
        rs = [sim_trail(s, i, d if way == 'toward' else -d)[0] for s, i, d in ts]
        print('  OUT-OF-SAMPLE, the chosen parameters, never seen during tuning:')
        stats('  chosen setup on the TEST period', rs)
        random.seed(5)
        tb = [sim_trail(s, random.randint(B.FIRST_BAR, min(B.LAST_BAR, s['n'] - B.HOLD_BARS - 3)),
                        random.choice((1, -1)))[0] for s in test if s['pdh'] is not None]
        stats('  random baseline on the same TEST period', tb)


if __name__ == '__main__':
    main()
