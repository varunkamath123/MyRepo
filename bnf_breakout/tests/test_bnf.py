# -*- coding: utf-8 -*-
"""Tests for the BANKNIFTY breakout strategy.

    python tests/test_bnf.py

A NOTE ON THE HARNESS, learned the hard way on the parked system: an assertion
helper that takes (got, want) but is called as if it took a boolean passes for
ANY truthy value, so `ck('book grew', book, 110_000)` "passes" for every nonzero
book. Here `eq` compares and `ok` takes a condition, and they are different names
so the mistake cannot be made silently.
"""
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bnf_broker as BR          # noqa: E402
import bnf_chain as CH           # noqa: E402
import bnf_replay as RP          # noqa: E402
import bnf_config                # noqa: E402
import bnf_data as D             # noqa: E402
import bnf_runner as RN          # noqa: E402
import bnf_strategy as S         # noqa: E402

P = F = 0


def eq(name, got, want):
    global P, F
    if got == want:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}\n          got  {got!r}\n          want {want!r}')


def ok(name, cond, detail=''):
    """Takes a CONDITION. numpy booleans are accepted; a number or any other type is
    refused outright, so passing a value where a condition belongs cannot pass."""
    global P, F
    if not isinstance(cond, (bool, np.bool_)):
        F += 1
        print(f'  FAIL  {name}  (ok() was given {type(cond).__name__} {cond!r}, not a condition)')
        return
    if bool(cond):
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  (condition was {cond!r}) {detail}')


def approx(name, got, want, tol=1e-6):
    ok(name, abs(got - want) <= tol, f'got {got}, want {want}')


def mk_cfg(**kw):
    ns = types.SimpleNamespace(**{k: getattr(bnf_config, k) for k in dir(bnf_config) if k.isupper()})
    ns.validate = lambda: []
    # The OI gate needs a chain snapshot that almost no test provides, so it is OFF unless a
    # test asks for it. (Shipped config has it ON; test_oi_gate_ships_on checks that.)
    ns.OI_GATE_ENABLED = False
    ns.OI_GATE_BLOCKS = True          # tests of the gate exercise blocking; shadow has its own tests
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


IST = D.IST
HHMM = [f'{9 + (15 + 5 * i) // 60:02d}:{(15 + 5 * i) % 60:02d}' for i in range(75)]


# ═══ synthetic data ═════════════════════════════════════════════════════════
def mk_session(day, base=55000.0, step=25.0, vol=1.0e7, seed=0, n=75):
    rng = np.random.RandomState(seed)
    C = base + np.cumsum(rng.normal(0, step, n))
    H = C + np.abs(rng.normal(step * 0.5, step * 0.2, n))
    L = C - np.abs(rng.normal(step * 0.5, step * 0.2, n))
    O = np.r_[C[0], C[:-1]]
    V = np.full(n, vol) * (1 + 0.1 * rng.rand(n))
    return dict(day=day, T=HHMM[:n], O=O, H=H, L=L, C=C, V=V)


def mk_history(k=40, last_scale=1.0, end='2026-10-21'):
    """k weekday sessions ENDING on `end`. The strategy now insists the last stored
    session is the one immediately before the day it trades, so a fixture that ended
    in September would (correctly) be rejected as stale."""
    days = []
    d = date.fromisoformat(end)
    while len(days) < k:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [mk_session(dd.isoformat(), step=25.0 * (last_scale if j == k - 1 else 1.0), seed=j)
            for j, dd in enumerate(days)]


def flat_tape(base, n=75, rng_pts=6.0):
    """A dead-flat session: tiny ranges, closes at base."""
    C = np.full(n, float(base))
    return dict(T=HHMM[:n], O=C.copy(), H=C + rng_pts / 2, L=C - rng_pts / 2, C=C,
                V=np.full(n, 2.0e7))


# ═══ 1. CONFIG ══════════════════════════════════════════════════════════════
print('\n--- config ---')
eq('the shipped config is coherent', bnf_config.validate(), [])
eq('paper only', bnf_config.PAPER_TRADE_MODE, True)
eq('capital is the Rs50,000 asked for', bnf_config.STARTING_CAPITAL, 50_000)
eq('BANKNIFTY only', bnf_config.INSTRUMENT, 'BANKNIFTY')
eq('lot size', bnf_config.LOT_SIZE, 30)


# validate() reads module globals, so exercise it by temporarily patching them
def validate_with(**kw):
    old = {k: getattr(bnf_config, k) for k in kw}
    for k, v in kw.items():
        setattr(bnf_config, k, v)
    try:
        return bnf_config.validate()
    finally:
        for k, v in old.items():
            setattr(bnf_config, k, v)


ok('a target at or below the stop is rejected',
   any('target' in p for p in validate_with(TARGET_PREMIUM_PCT=0.2)))
ok('a reversed entry window is rejected',
   any('ENTRY_START' in p for p in validate_with(ENTRY_START='14:00', ENTRY_END='10:00')))
ok('entries closing after the force-close is rejected',
   any('FORCE_CLOSE' in p for p in validate_with(ENTRY_END='15:10')))
ok('an unknown direction is rejected', any('DIRECTION' in p for p in validate_with(DIRECTION='sideways')))
ok('an API gap that would breach 5 req/s is rejected',
   any('5 requests' in p for p in validate_with(API_MIN_GAP_SECONDS=0.05)))
ok('turning paper mode off is refused (no live path exists)',
   any('PAPER_TRADE_MODE' in p for p in validate_with(PAPER_TRADE_MODE=False)))
ok('absurd risk per trade is rejected', len(validate_with(RISK_PCT_OF_BOOK=0.9)) > 0)

# the package must contain no way to place an order
forbidden = ('place_order', 'enter_live_position', 'orders/', '.place', 'modify_order', 'cancel_order')
hits = []
for fn in os.listdir(os.path.dirname(HERE)):
    if fn.endswith('.py'):
        src = open(os.path.join(os.path.dirname(HERE), fn), encoding='utf-8').read()
        for tok in forbidden:
            if tok in src and fn != 'test_bnf.py':
                hits.append((fn, tok))
eq('no order-placement call exists anywhere in the package', hits, [])

# ═══ 2. STRATEGY: indicators ════════════════════════════════════════════════
print('\n--- indicators ---')
H = np.full(30, 102.0)
L = np.full(30, 100.0)
approx('ATR of constant 2-point bars is 2', S.atr(H, L, 29), 2.0)
approx('a perfectly flat 6-bar coil has ratio 2/(2*sqrt(6))', S.compression_ratio(H, L, 29, 6), 2 / (2 * 6 ** 0.5))
eq('compression is undefined before 15 bars', S.compression_ratio(H, L, 10, 6), None)
Hw = H.copy()
Hw[-1] = 110.0
ok('a wide recent range gives a LARGER ratio than a tight one',
   S.compression_ratio(Hw, L, 29, 6) > S.compression_ratio(H, L, 29, 6))

day = dict(pdh=56000.0, pdl=54000.0, hot=True, vbase=np.full(75, 1.0e7))
Hh, Ll = np.full(30, 55010.0), np.full(30, 54990.0)
lv = S.key_levels(day, Hh, Ll, 20)
eq('previous-day levels come through', (lv['PDH'], lv['PDL']), (56000.0, 54000.0))
eq('opening range is the first six bars', (lv['ORH'], lv['ORL']), (55010.0, 54990.0))
ok('day high / low are present', 'DAYH' in lv and 'DAYL' in lv)
lv2 = S.key_levels(day, Hh, Ll, 3)
ok('the opening range does not exist before its sixth bar closes', 'ORH' not in lv2 and 'ORL' not in lv2)
(up, un), (dn, dnn) = S.nearest_levels({'A': 101.0, 'B': 99.0, 'C': 100.0}, 100.0)
eq('a level EQUAL to price is neither above nor below', (up, dn), (101.0, 99.0))

print('\n--- regime & volume baseline ---')
rvs_cold = [0.002] * 60 + [0.0001]
rvs_hot = [0.002] * 60 + [0.01]
eq('last session calmer than its window -> not hot', S.hot_regime(rvs_cold), False)
eq('last session wilder than its window -> hot', S.hot_regime(rvs_hot), True)
eq('too little history -> unknown, not a guess', S.hot_regime([0.1] * 10), None)
sess = [dict(V=np.full(75, 1.0e7 * (1 + 0.01 * k))) for k in range(30)]
vb = S.volume_baseline(sess, 75)
approx('the baseline is the median across prior sessions', vb[10], float(np.median([1.0e7 * (1 + 0.01 * k) for k in range(10, 30)])), 1.0)
ok('under 10 prior sessions -> NaN, never a fabricated baseline',
   bool(np.isnan(S.volume_baseline(sess[:5], 75)).all()))
ok('sessions without volume are ignored',
   bool(np.isnan(S.volume_baseline([dict(V=np.zeros(75))] * 30, 75)).all()))

# ═══ 3. STRATEGY: entry ═════════════════════════════════════════════════════
print('\n--- entry ---')


def coil_bars(n=30, px=55980.0):
    """A tight, high-volume coil pressing up against PDH at 56000."""
    C = np.full(n, px)
    return dict(H=C + 8, L=C - 8, C=C, V=np.full(n, 2.0e7), T=HHMM[:n])


def entry_day(hot=True):
    return dict(pdh=56000.0, pdl=54000.0, hot=hot, vbase=np.full(75, 1.0e7))


cfg0 = mk_cfg(COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'), VOLUME_MODE='participate')   # the volume tests need the gate ON; shipped config has it off
bars = coil_bars()
st0 = dict(trades_today=0, open_positions=0, last_loss_bar=None)
d = S.evaluate_entry(bars, 29, entry_day(), st0, cfg0)
ok('a coil pressing PDH on volume in a hot regime enters', d.enter is True, d.gates)
eq('anticipating the break means buying a CALL toward resistance', d.direction, 1)
eq('the thesis names the level it is pressing', d.thesis['level'], 'PDH')
ok('invalidation sits below the coil for a CALL', d.thesis['invalidation'] < d.thesis['coil_lo'])
eq('every gate reported', sorted(d.gates), ['capacity', 'coil', 'context', 'level', 'oi', 'regime', 'volume', 'window'])
eq('...and the OI gate says it is off when it is off', d.gates['oi'], (True, 'off'))
eq('a fresh context passes its gate', d.gates['context'], (True, 'fresh'))
stale_day = dict(entry_day(), stale=True, stale_reason='session 2026-10-21 missing and could not be fetched')
dstale = S.evaluate_entry(bars, 29, stale_day, st0, cfg0)
eq('a stale context blocks entry, and says why', (dstale.enter, dstale.blocked_by(), dstale.gates['context'][1]),
   (False, ['context'], 'session 2026-10-21 missing and could not be fetched'))

dA = S.evaluate_entry(bars, 29, entry_day(), st0, mk_cfg(DIRECTION='away', LEVELS=('PDH', 'PDL')))
eq("DIRECTION='away' fades the level: a PUT at resistance", dA.direction, -1)
ok('and its invalidation is ABOVE the level it expects to hold',
   dA.thesis['invalidation'] > dA.thesis['level_value'])

dc = S.evaluate_entry(bars, 29, entry_day(hot=False), st0, cfg0)
eq('a cold regime blocks, and says so', dc.blocked_by(), ['regime'])
dh = S.evaluate_entry(bars, 29, entry_day(hot=None), st0, cfg0)
eq('an unknown regime also blocks rather than guessing', dh.blocked_by(), ['regime'])
dn_ = S.evaluate_entry(bars, 29, entry_day(hot=False), st0, mk_cfg(REQUIRE_HOT_REGIME=False, LEVELS=('PDH', 'PDL')))
ok('the regime filter can be switched off in config', dn_.enter is True)

ok('with day high/low enabled, a flat coil ALWAYS sits at a level (why the tests use PDH/PDL only)',
   S.evaluate_entry(coil_bars(px=55000.0), 29, entry_day(), st0, mk_cfg(COIL_RATIO_MAX=0.812)).gates['level'][0] is True)
far = coil_bars(px=55000.0)
eq('price far from every level -> only the level gate fails',
   S.evaluate_entry(far, 29, entry_day(), st0, cfg0).blocked_by(), ['level'])

wide = coil_bars()
march = np.r_[np.full(24, 55980.0), 55980.0 + 100.0 * np.arange(1, 7)]    # 6 bars marching up 100pts each
wide.update(C=march, H=march + 8, L=march - 8)
ok('a window that MARCHES (range far above its ATR) fails the coil gate',
   'coil' in S.evaluate_entry(wide, 29, entry_day(), st0, cfg0).blocked_by())

lowv = coil_bars()
lowv['V'] = np.full(30, 4.0e6)
eq("volume below normal fails 'participate'",
   S.evaluate_entry(lowv, 29, entry_day(), st0, cfg0).blocked_by(), ['volume'])
eq("...and passes 'dryup'", S.evaluate_entry(lowv, 29, entry_day(), st0, mk_cfg(VOLUME_MODE='dryup', LEVELS=('PDH', 'PDL'))).enter, True)
eq("...and is ignored when VOLUME_MODE is 'off'",
   S.evaluate_entry(lowv, 29, entry_day(), st0, mk_cfg(VOLUME_MODE='off', LEVELS=('PDH', 'PDL'))).enter, True)
nov = coil_bars()
nov['V'] = np.zeros(30)
eq('no volume data at all fails the volume gate loudly, not silently',
   S.evaluate_entry(nov, 29, entry_day(), st0, cfg0).gates['volume'], (False, 'no volume data'))

eq('before the opening range is complete -> window gate fails',
   S.evaluate_entry(coil_bars(), 5, entry_day(), st0, cfg0).gates['window'][0], False)
eq('09:55 bar close is the first allowed decision',
   S.evaluate_entry(coil_bars(), 7, entry_day(), st0, cfg0).gates['window'], (True, '09:55'))
late = coil_bars(n=60)
eq('after the last allowed decision -> window gate fails',
   S.evaluate_entry(late, 56, entry_day(), st0, cfg0).gates['window'][0], False)

eq('at the daily trade cap, capacity blocks',
   S.evaluate_entry(bars, 29, entry_day(), dict(trades_today=2, open_positions=0), cfg0).blocked_by(), ['capacity'])
eq('with a position already open, capacity blocks',
   S.evaluate_entry(bars, 29, entry_day(), dict(trades_today=0, open_positions=1), cfg0).blocked_by(), ['capacity'])
eq('inside the post-loss cooldown, capacity blocks',
   S.evaluate_entry(bars, 29, entry_day(), dict(trades_today=1, open_positions=0, last_loss_bar=27), cfg0).blocked_by(), ['capacity'])
eq('after the cooldown it may enter again',
   S.evaluate_entry(bars, 29, entry_day(), dict(trades_today=1, open_positions=0, last_loss_bar=20), cfg0).enter, True)

# support-side mirror
sup = coil_bars(px=54020.0)
dS = S.evaluate_entry(sup, 29, entry_day(), st0, cfg0)
eq('pressing support -> PUT, toward the level', (dS.enter, dS.direction, dS.thesis['level']), (True, -1, 'PDL'))
ok('and its invalidation is above the coil', dS.thesis['invalidation'] > dS.thesis['coil_hi'])

# ═══ 4. STRATEGY: exits ═════════════════════════════════════════════════════
print('\n--- exits ---')
cfgX = mk_cfg()
dec_up = S.evaluate_entry(bars, 29, entry_day(), st0, cfg0)


def pos_at(px=55980.0, direction=1, entry_bar=29, atr_pts=16.0, inv=None):
    th = dict(level='PDH', level_value=56000.0, direction=direction, coil_hi=px + 8, coil_lo=px - 8,
              invalidation=(px - 8 - 30) if direction > 0 else (px + 8 + 30), atr=atr_pts,
              entry_bar=entry_bar, entry_px=px, mode='toward')
    if inv is not None:
        th['invalidation'] = inv
    dd = S.EntryDecision(True, direction, th, {})
    return S.new_position(dd, 400.0, 1)


def tape(closes, hi_add=4.0, lo_sub=4.0, start=0, T=None):
    n = start + len(closes)
    C = np.r_[np.full(start, closes[0]), np.array(closes, float)]
    return dict(H=C + hi_add, L=C - lo_sub, C=C, V=np.full(n, 1e7), T=(T or HHMM)[:n])


p = pos_at()
eq('premium exactly at the stop -> stop', S.check_premium(p, -0.25, cfgX)['kind'], 'stop')
eq('premium beyond the stop -> stop', S.check_premium(p, -0.40, cfgX)['kind'], 'stop')
eq('just inside the stop -> nothing', S.check_premium(p, -0.249, cfgX), None)
eq('premium at the target -> target', S.check_premium(p, 0.55, cfgX)['kind'], 'target')
eq('between stop and target -> nothing', S.check_premium(p, 0.2, cfgX), None)
approx('the 25% premium stop is 0.350% of spot', S.stop_index_pct(cfgX), 0.350, 0.0005)
approx('the 55% premium target is 0.770% of spot', S.target_index_pct(cfgX), 0.770, 0.0005)

# thesis broken
p = pos_at()
tb = tape([55980, 55980, 55900, 55940], start=29)
tb['T'] = HHMM[:len(tb['C'])]
r = S.on_bar_exit(p, tb, len(tb['C']) - 1, cfgX)
eq('a CALL whose price closes below invalidation -> thesis_broken', r and r['kind'], 'thesis_broken')
p = pos_at(direction=-1)
tp = tape([55980, 55980, 56100, 56050], start=29)
tp['T'] = HHMM[:len(tp['C'])]
eq('a PUT whose price closes above invalidation -> thesis_broken',
   (S.on_bar_exit(p, tp, len(tp['C']) - 1, cfgX) or {}).get('kind'), 'thesis_broken')

# stale: no follow-through within N bars
p = pos_at(entry_bar=10)
fl = tape([55980] * 30)
r = None
for i in range(11, 17):
    r = S.on_bar_exit(p, {k: (v[:i + 1]) for k, v in fl.items()}, i, cfgX)
eq('no follow-through after THESIS_CONFIRM_BARS -> thesis_stale', r and r['kind'], 'thesis_stale')
p = pos_at(entry_bar=10)
r5 = None
for i in range(11, 16):
    r5 = S.on_bar_exit(p, {k: (v[:i + 1]) for k, v in fl.items()}, i, cfgX)
eq('...but not one bar earlier', r5, None)
# confirmed theses are not stale
p = pos_at(entry_bar=10)
up = tape([55980 + 14 * j for j in range(30)])
res = [S.on_bar_exit(p, {k: v[:i + 1] for k, v in up.items()}, i, mk_cfg(THESIS_CONFIRM_BARS=6, TRAIL_ARM_INDEX_PCT=99), False) for i in range(11, 17)]
ok('once the break carries the required distance the thesis is confirmed',
   p['confirmed'] is True and all((x or {}).get('kind') != 'thesis_stale' for x in res))

# trail: ATR-scaled retrace from the best price, on a close
p = pos_at(entry_bar=10, atr_pts=20.0)
seq = [55980, 56010, 56080, 56200, 56210, 56205]
tr = tape(seq, start=10)
last = None
for i in range(11, len(tr['C'])):
    last = S.on_bar_exit(p, {k: v[:i + 1] for k, v in tr.items()}, i, mk_cfg(THESIS_CONFIRM_BARS=99))
eq('a shallow pullback below 1.5 ATR does not trail out', last, None)
tr2 = tape(seq + [56150, 56160, 56165], start=10)
for i in range(11, len(tr2['C'])):
    last = S.on_bar_exit(p, {k: v[:i + 1] for k, v in tr2.items()}, i, mk_cfg(THESIS_CONFIRM_BARS=99))
    if last:
        break
eq('a close back through best - 1.5 ATR -> trail', last and last['kind'], 'trail')
ok('the trail only arms after the required favourable move',
   S.on_bar_exit(pos_at(entry_bar=10), {k: v[:12] for k, v in tape([55980, 55990, 55995], start=10).items()}, 11, mk_cfg()) is None)

# force close
p = pos_at(entry_bar=10)
fc = tape([55980] * 70)
fc['T'] = HHMM[:70]
r = S.on_bar_exit(p, fc, 68, mk_cfg(THESIS_CONFIRM_BARS=999))      # bar 68 opens 14:55, closes 15:00
eq('the bar closing at FORCE_CLOSE ends the trade', r and r['kind'], 'force_close')
eq('a bar closing a minute early does not',
   S.on_bar_exit(pos_at(entry_bar=10), {k: v[:68] for k, v in fc.items()}, 67, mk_cfg(THESIS_CONFIRM_BARS=999)), None)

# replay hard stop / target, and the same-bar ordering rule
p = pos_at()
wide_bar = tape([55980, 55980], start=29)
wide_bar['H'][-1] = 55980 + 500
wide_bar['L'][-1] = 55980 - 500
wide_bar['T'] = HHMM[:31]
eq('a bar that spans BOTH stop and target counts the STOP first (conservative)',
   S.on_bar_exit(p, wide_bar, 30, mk_cfg(), replay=True)['kind'], 'stop')
p = pos_at()
hi_bar = tape([55980, 55980], start=29)
hi_bar['H'][-1] = 55980 + 600
hi_bar['T'] = HHMM[:31]
eq('a bar reaching the target without touching the stop -> target (replay)',
   S.on_bar_exit(p, hi_bar, 30, mk_cfg(), replay=True)['kind'], 'target')
p = pos_at()
r_live = S.on_bar_exit(p, hi_bar, 30, mk_cfg(), replay=False)
ok('live mode does not turn a bar high into a target or stop (the premium poll owns those)',
   (r_live or {}).get('kind') not in ('stop', 'target'), r_live)

# ═══ 5. sizing, book, costs ═════════════════════════════════════════════════
print('\n--- sizing / book / costs ---')
b0 = S.book_state([], cfgX)
eq('empty history -> book is the starting capital', b0['book'], 50_000.0)
eq('...no drawdown, full risk', (b0['drawdown'], b0['risk_mult']), (0.0, 1.0))
b1 = S.book_state([4000, 6000, -3000], cfgX)
eq('wins and losses compound the book', b1['book'], 57_000.0)
eq('...and the peak is tracked separately', b1['peak'], 60_000.0)
approx('...so the drawdown is measured from the peak', b1['drawdown'], 3000 / 60000)
approx('...and risk de-levers linearly: 1 - 0.05/0.25', b1['risk_mult'], 1 - (3000 / 60000) / 0.25)
eq('a catastrophic run floors the book at 25% of start', S.book_state([-90_000], cfgX)['book'], 12_500.0)
eq('...and the multiplier bottoms at the configured floor', S.book_state([-90_000], cfgX)['risk_mult'], 0.25)
eq('compounding off -> the book never moves', S.book_state([9000], mk_cfg(COMPOUND_ENABLED=False))['book'], 50_000.0)
eq('drawdown throttle off -> always full risk', S.book_state([-20000], mk_cfg(DD_THROTTLE_ENABLED=False))['risk_mult'], 1.0)

z = S.size_lots(388.3, b0, cfgX)
eq('Rs50k at a Rs388 premium -> 1 lot (one lot risks ~Rs2.9k of a Rs4k budget)', z['lots'], 1)
approx('...one lot costs premium x lot size', z['one_cost'], 388.3 * 30, 0.01)
z2 = S.size_lots(388.3, S.book_state([30_000], cfgX), cfgX)
eq('after a profit the same entry takes 2 lots', z2['lots'], 2)
ok('realised risk never exceeds the stated budget when sizing above the floor',
   z2['lots'] * z2['one_risk'] <= z2['budget'] + 1e-6)
eq('a huge book is capped at MAX_LOTS',
   S.size_lots(388.3, S.book_state([5_000_000], cfgX), cfgX)['lots'], cfgX.MAX_LOTS)
eq('a lot that costs more than the whole book is skipped, not sized to 1',
   S.size_lots(2000.0, dict(book=40_000.0, drawdown=0.0, risk_mult=1.0, peak=40_000.0), cfgX)['lots'], 0)
eq('a missing premium sizes to zero', S.size_lots(None, b0, cfgX)['lots'], 0)
zt = S.size_lots(388.3, dict(book=100_000.0, drawdown=0.125, risk_mult=0.5, peak=100_000.0), cfgX)
zf = S.size_lots(388.3, dict(book=100_000.0, drawdown=0.0, risk_mult=1.0, peak=100_000.0), cfgX)
ok('a drawdown multiplier shrinks the lots', zt['lots'] < zf['lots'], (zt['lots'], zf['lots']))
zfloor = S.size_lots(388.3, dict(book=50_000.0, drawdown=0.25, risk_mult=0.25, peak=66_000.0), cfgX)
eq('even at the throttle floor it still takes the one-lot minimum', zfloor['lots'], 1)

c1 = S.round_trip_cost(388.3, 388.3, 1, cfgX)
ok('round-trip costs on one lot are of the order of Rs60-100', 55 < c1 < 100, c1)
c3 = S.round_trip_cost(388.3, 388.3, 3, cfgX)
ok('costs grow with size but less than proportionally (brokerage is a fixed Rs40)', c1 < c3 < 3 * c1, (c1, c3))
ok('a flat trade is a net LOSS after costs', (388.3 - 388.3) * 30 - c1 < 0)
ok('a larger sell value raises STT', S.round_trip_cost(388.3, 700.0, 1, cfgX) > S.round_trip_cost(388.3, 400.0, 1, cfgX))

# ═══ 6. data layer ══════════════════════════════════════════════════════════
print('\n--- data ---')


def epoch(hhmm, day='2026-10-22'):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    return datetime(2026, 10, 22, h, m, tzinfo=IST).timestamp()


cands = [[epoch(t), 100, 101, 99, 100.5, 1000] for t in ('09:15', '09:20', '09:25', '09:30')]
b = D.candles_to_bars(cands, datetime(2026, 10, 22, 9, 31, 0, tzinfo=IST))
eq('only CLOSED bars are returned: 09:30 (closes 09:35) is still forming', b['T'], ['09:15', '09:20', '09:25'])
b2 = D.candles_to_bars(cands, datetime(2026, 10, 22, 9, 35, 5, tzinfo=IST))
eq('once its close time has passed the bar is included', b2['T'][-1], '09:30')
b3 = D.candles_to_bars(cands, datetime(2026, 10, 22, 9, 35, 1, tzinfo=IST))
eq('...but not within 2 seconds of the close (the API may not have finalised it)', b3['T'][-1], '09:25')
eq('garbage candles are skipped, not fatal', D.candles_to_bars([['x'], None, [epoch('09:15'), 1, 2, 0, 1]],
   datetime(2026, 10, 22, 12, 0, tzinfo=IST))['T'], ['09:15'])
eq('contiguous bars from the open', D.bars_are_contiguous(HHMM[:10]), True)
eq('a hole is detected', D.bars_are_contiguous(HHMM[:4] + HHMM[5:10]), False)
eq('an empty list is not contiguous', D.bars_are_contiguous([]), False)

tmpd = tempfile.mkdtemp()
for j, dayj in enumerate(('2026-10-19', '2026-10-20', '2026-10-21')):
    with open(os.path.join(tmpd, f'x_{dayj}.csv'), 'w') as fh:
        fh.write('ts,Open,High,Low,Close,Volume\n')
        for i in range(75):
            fh.write(f'{dayj} {HHMM[i]}:00+05:30,100,101,99,100,{1000 + i}\n')
with open(os.path.join(tmpd, 'short.csv'), 'w') as fh:
    fh.write('ts,Open,High,Low,Close,Volume\n2026-10-18 09:15:00+05:30,1,2,0,1,5\n')
hs = D.load_history(tmpd)
eq('three full sessions load; the truncated one is dropped', [s['day'] for s in hs], ['2026-10-19', '2026-10-20', '2026-10-21'])
eq("'before' excludes that day and later so today's partial file cannot leak in",
   [s['day'] for s in D.load_history(tmpd, before='2026-10-21')], ['2026-10-19', '2026-10-20'])
eq('last_n keeps the most recent', [s['day'] for s in D.load_history(tmpd, last_n=1)], ['2026-10-21'])
eq('volume survives the load', float(hs[0]['V'][3]), 1003.0)

# ═══ 7. broker: budget, rate limits, contracts ══════════════════════════════
print('\n--- broker ---')
for m in ("You've reached the Standard plan limit of 5,000 requests/day. Upgrade to Prime",
          "You've reached the Standard plan limit of 5 requests/second", 'request limit reached',
          'Rate limit exceeded', 'Too many requests'):
    eq(f'throttle recognised: {m[:44]!r}', BR.is_rate_limited(m), True)
for m in ('invalid symbol', '', None, 'no data found for this contract', 'symbol not found'):
    eq(f'not a throttle: {str(m)[:40]!r}', BR.is_rate_limited(m), False)

sd = tempfile.mkdtemp()
slept = []
clk = [0.0]
bud = BR.ApiBudget(sd, 5, 0.35, '2026-10-22', sleep=lambda s: (slept.append(round(s, 3)), clk.__setitem__(0, clk[0] + s)),
                   clock=lambda: clk[0])
for _ in range(5):
    bud.record('bars')
eq('calls are counted by label', bud.by_label, {'bars': 5})
eq('a non-essential call is refused at the cap', bud.allow(False)[0], False)
eq('an ESSENTIAL call (an exit) is never refused by the cap', bud.allow(True)[0], True)
bud2 = BR.ApiBudget(sd, 5, 0.35, '2026-10-22')
eq('the count survives a restart', bud2.calls, 5)
eq('a different day starts from zero', BR.ApiBudget(sd, 5, 0.35, '2026-10-23').calls, 0)
bud2.roll('2026-10-23')
eq('roll() resets the counters for a new day', (bud2.calls, bud2.by_label), (0, {}))
bud2.block(1800, now_epoch=1000.0)
eq('after a daily-cap reply even essential calls are refused', bud2.allow(True, now_epoch=1500.0), (False, 'rate_limited'))
eq('...until the backoff ends', bud2.allow(True, now_epoch=2801.0)[0], True)
slept.clear()
bud.pace()
bud.pace()
ok('pacing enforces the minimum gap between calls', len(slept) >= 1 and slept[-1] >= 0.34, slept)


class FakeFyers:
    def __init__(self):
        self.q_calls, self.h_calls = [], 0
        self.premium = {}
        self.listed = lambda sym: sym.startswith('NSE:BANKNIFTY26OCT')
        self.quote_reply = None
        self.candles = []

    def history(self, req):
        self.h_calls += 1
        self.h_days = getattr(self, 'h_days', []) + [req.get('range_from')]
        # a forced reply applies only to PAST days: failing today's bars as well would
        # make every "refused because the context is stale" test pass for the wrong
        # reason (no bars at all), which is what the first version of them did
        if (getattr(self, 'history_reply', None) is not None
                and req.get('range_from') != getattr(self, 'today_str', None)):
            return self.history_reply
        return dict(s='ok', candles=getattr(self, 'by_day', {}).get(req.get('range_from'), self.candles))

    def quotes(self, req):
        sym = req['symbols']
        self.q_calls.append(sym)
        if self.quote_reply is not None:
            return self.quote_reply
        if not self.listed(sym):
            return dict(s='ok', d=[dict(v={})])
        p = self.premium.get(sym, self.premium.get('*', 400.0))
        if isinstance(p, dict):
            return dict(s='ok', d=[dict(v=p)])
        return dict(s='ok', d=[dict(v=dict(lp=p, bid=p - 0.5, ask=p + 0.5))])


ff = FakeFyers()
brk = BR.Broker(ff, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, '2026-10-22', sleep=lambda s: None), None)
q, st = brk.quote('NSE:BANKNIFTY26OCT55800CE', essential=True)
eq('a normal quote parses ltp, bid and ask', (q['ltp'], q['bid'], q['ask']), (400.0, 399.5, 400.5))
ff.premium['NSE:BANKNIFTY26OCTx'] = dict(lp=100.0, bid=0, ask=0)
eq('zero bid/ask are treated as absent', brk.quote('NSE:BANKNIFTY26OCTx', True)[0]['bid'], None)
ff.premium['NSE:BANKNIFTY26OCTy'] = dict(bid=1, ask=2)
eq('a quote with no price is rejected', brk.quote('NSE:BANKNIFTY26OCTy', True)[0], None)
ff.quote_reply = dict(s='error', code=-360, message="You've reached the Standard plan limit of 5,000 requests/day.")
eq('the real daily-cap reply is reported as rate_limited', brk.quote('z', True)[1], 'rate_limited')
eq('...and blocks every later call, even an essential one', brk.quote('z', True)[1], 'rate_limited')
eq('...and the count does not climb while blocked', brk.budget.by_label.get('quote'), 4)
ff.quote_reply = None

eq('option symbol format (call)', BR.option_symbol(55800, 1, date(2026, 10, 27)), 'NSE:BANKNIFTY26OCT55800CE')
eq('option symbol format (put)', BR.option_symbol(55800, -1, date(2026, 10, 27)), 'NSE:BANKNIFTY26OCT55800PE')
eq('December uses DEC', BR.option_symbol(56000, 1, date(2026, 12, 29)), 'NSE:BANKNIFTY26DEC56000CE')
eq('ATM rounds to the 100 strike gap', BR.atm_strike(55849.0), 55800)
eq('...and up', BR.atm_strike(55851.0), 55900)
eq('the last Tuesday of Oct 2026 is the 27th', BR.last_tuesday(2026, 10), date(2026, 10, 27))
eq('the last Tuesday of Dec 2026 is the 29th', BR.last_tuesday(2026, 12), date(2026, 12, 29))
ec = BR.expiry_candidates(date(2026, 10, 22))
eq('candidates are the monthly expiries, nearest first', ec, [date(2026, 10, 27), date(2026, 11, 24), date(2026, 12, 29)])
ec2 = BR.expiry_candidates(date(2026, 10, 26))
eq('an expiry inside MIN_DAYS_TO_EXPIRY is skipped, rolling to next month', ec2[0], date(2026, 11, 24))

ff2 = FakeFyers()
brk2 = BR.Broker(ff2, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, '2026-10-22', sleep=lambda s: None), None)
ff2.listed = lambda sym: sym == 'NSE:BANKNIFTY26OCT55900CE'
res, st = brk2.resolve_option(55849.0, 1, date(2026, 10, 22))
eq('a dead ATM strike is not a dead expiry: it falls to the listed neighbour', (res['strike'], res['expiry']), (55900, date(2026, 10, 27)))
n_before = len(ff2.q_calls)
brk2.resolve_option(55849.0, 1, date(2026, 10, 22))
ok('the resolved expiry is cached for the day (fewer probes the second time)', len(ff2.q_calls) - n_before <= 3)
ff3 = FakeFyers()
ff3.listed = lambda sym: False
brk3 = BR.Broker(ff3, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, '2026-10-22', sleep=lambda s: None), None)
eq('nothing listed -> an honest failure', brk3.resolve_option(55800.0, 1, date(2026, 10, 22))[1], 'no listed contract found')
ff4 = FakeFyers()
ff4.quote_reply = dict(s='error', code=-360, message="You've reached the Standard plan limit of 5,000 requests/day.")
brk4 = BR.Broker(ff4, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, '2026-10-22', sleep=lambda s: None), None)
eq('a rate limit while resolving is reported as such, never as "not listed"',
   brk4.resolve_option(55800.0, 1, date(2026, 10, 22)), (None, 'rate_limited'))
ok('...and it stopped probing at once instead of walking every strike', len(ff4.q_calls) == 1, len(ff4.q_calls))

# ═══ 8. the runner: a full simulated day ════════════════════════════════════
print('\n--- runner: simulated trading day ---')
DAY = '2026-10-22'          # a Thursday


def at(hhmm, sec=10, day=DAY):
    y, mo, d_ = (int(x) for x in day.split('-'))
    return datetime(y, mo, d_, int(hhmm[:2]), int(hhmm[3:]), sec, tzinfo=IST)


RELAXED = dict(REQUIRE_HOT_REGIME=False, VOLUME_MODE='off', COIL_RATIO_MAX=99.0, LEVEL_PROX_PCT=50.0,
               API_MIN_GAP_SECONDS=0.0)


class Rig:
    """A runner wired to a fake exchange and a fake clock."""

    def __init__(self, tape_bars, premium=400.0, **cfgkw):
        self.dir = tempfile.mkdtemp()
        self.tape = tape_bars
        self.ff = FakeFyers()
        self.ff.today_str = DAY
        self.ff.premium['*'] = premium
        self.now = at('09:55')
        self.cfgkw = dict(RELAXED)
        self.cfgkw.update(cfgkw)
        self.c = mk_cfg(**self.cfgkw)
        self.holidays = set()                       # dates the fake exchange is shut
        self.holidays_fn = lambda d: d.isoformat() in self.holidays
        self.hist = mk_history(40)
        self.build()

    def build(self):
        self.budget = BR.ApiBudget(self.dir, self.c.MAX_API_CALLS_PER_DAY, 0.0, DAY, sleep=lambda s: None)
        self.broker = BR.Broker(self.ff, self.budget, None, self.c)
        self.r = RN.Runner(self.broker, self.c, clock=lambda: self.now, sleep=lambda s: None,
                           history=self.hist, state_dir=self.dir,
                           is_holiday=self.holidays_fn)
        self.refresh()

    def refresh(self):
        """The fake exchange serves every bar that has OPENED by 'now', including the forming one."""
        cand = []
        t = self.tape
        for i in range(len(t['T'])):
            op = at(t['T'][i], 0)
            if op <= self.now:
                cand.append([op.timestamp(), t['O'][i], t['H'][i], t['L'][i], t['C'][i], t['V'][i]])
        self.ff.candles = cand

    def go(self, hhmm, sec=10):
        self.now = at(hhmm, sec)
        self.refresh()
        return self.r.step()

    def trades(self):
        p = os.path.join(self.dir, 'logs', f'bnf_trades_{DAY}.jsonl')
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []

    def decisions(self):
        p = os.path.join(self.dir, 'logs', f'bnf_decisions_{DAY}.jsonl')
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


# --- 8a. entry, then the premium target ---
rig = Rig(flat_tape(55800))
rig.go('09:40')
eq('before the entry window: no setup scan and NO API calls', rig.budget.calls, 0)
rig.go('09:55')
eq('at 09:55 the window is open but the coil gate has no 15-bar history: NO entry', rig.r.state['position'], None)
dec0 = rig.decisions()
ok('...and the log says WHY: the coil gate is the one blocking', dec0 and 'coil' in dec0[-1]['blocked_by'], dec0)
eq('...with an explicit n/a rather than a silent pass', dec0[-1]['gates']['coil'][1], 'n/a')
rig.go('10:35')
pos = rig.r.state['position']
ok('a setup at the first allowed bar opens a paper position', pos is not None)
eq('...on the CALL side (a flat tape ties up/down, and ties go to the upper level)', pos['opt'], 'CE')
eq('...for one lot (Rs50k book)', pos['lots'], 1)
approx('...filled at the ASK, not the last price', pos['entry_premium'], 400.5)
eq('...and the contract is a real-looking BANKNIFTY monthly', pos['symbol'].startswith('NSE:BANKNIFTY26OCT'), True)
eq('the trade is counted', rig.r.state['trades_today'], 1)
ok('the gates that let it in are recorded with the position', set(pos['entry_gates']) >= {'window', 'level', 'coil'})
ok('the sizing rationale is recorded', 'book=50,000' in pos['sizing'], pos['sizing'])
dc = rig.decisions()
eq('every evaluated bar writes a decision line: the 09:55 "no" and the 10:35 "yes"', (len(dc), dc[0]['enter'], dc[-1]['enter']), (2, False, True))

rig.ff.premium['*'] = dict(lp=630.0, bid=629.0, ask=631.0)        # +57% on the 400.5 fill
rig.go('10:36')
tr = rig.trades()
eq('the premium reaching +55% closes at the target', (len(tr), tr[0]['kind']), (1, 'target'))
approx('...filled at the BID', tr[0]['exit_premium'], 629.0)
approx('...P&L is (exit - entry) x quantity - costs',
       tr[0]['pnl_net'], (629.0 - 400.5) * 30 - S.round_trip_cost(400.5, 629.0, 1, rig.c), 0.01)
eq('the position is gone', rig.r.state['position'], None)
ok('the trade record carries the thesis for later diagnosis', 'invalidation' in tr[0]['thesis'])
ok('and the book before the trade', tr[0]['book_before'] == 50000.0)
ok('and how many API calls the day had used', tr[0]['api_calls_today'] > 0)
ok('a path log of every premium poll exists',
   os.path.exists(os.path.join(rig.dir, 'logs', f'bnf_path_{DAY}.jsonl')))

# --- 8b. the hard stop ---
rig = Rig(flat_tape(55800))
rig.go('10:35')
rig.ff.premium['*'] = dict(lp=300.0, bid=299.0, ask=301.0)         # -25.3% on 400.5
rig.go('10:36')
tr = rig.trades()
eq('a 25% premium drawdown closes at the hard stop', tr[0]['kind'], 'stop')
ok('and it was a loss', tr[0]['pnl_net'] < 0)

# --- 8c. stale thesis: nothing happens for six bars ---
rig = Rig(flat_tape(55800))
rig.go('10:35')
rig.go('11:00', 5)                                     # bar 12 closed: held 5 bars
eq('five bars without follow-through is still within the window', rig.trades(), [])
rig.go('11:05', 5)                                     # bar 13 closed: held 6 bars
tr = rig.trades()
eq('a thesis that never develops is abandoned after the confirm window', tr and tr[0]['kind'], 'thesis_stale')

# --- 8d. thesis broken ---
t_break = flat_tape(55800)
t_break['C'][16:] = 55300.0
t_break['H'][16:] = 55310.0
t_break['L'][16:] = 55290.0
rig = Rig(t_break)
rig.go('10:35')
cur_dir = rig.r.state['position']['dir']
if cur_dir < 0:                                        # make the move go AGAINST the position
    t_break['C'][16:] = 56300.0
    t_break['H'][16:] = 56310.0
    t_break['L'][16:] = 56290.0
    rig.tape = t_break
rig.go('10:40', 5)
tr = rig.trades()
eq('a close beyond the invalidation ends the trade as thesis_broken', tr and tr[0]['kind'], 'thesis_broken')

# --- 8e. force close at the end of the day ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999)
rig.go('10:35')
rig.go('15:00', 5)
tr = rig.trades()
eq('an open position is closed at FORCE_CLOSE', tr and tr[0]['kind'], 'force_close')

# --- 8f. restart safety ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999)
rig.go('10:35')
saved = dict(rig.r.state['position'])
rig.build()                                            # a brand-new process, same state dir
rig.now = at('10:36')
rig.refresh()
rig.r.step()
ok('after a restart the open position is still being managed',
   rig.r.state['position'] is not None and rig.r.state['position']['symbol'] == saved['symbol'])
eq('...and the trade counter was not reset', rig.r.state['trades_today'], 1)
rig.ff.premium['*'] = dict(lp=299.0, bid=298.0, ask=300.0)
rig.go('10:37')
eq('...and it can still be stopped out after the restart', rig.trades()[0]['kind'], 'stop')

# --- 8g. caps and cooldown ---
rig = Rig(flat_tape(55800), MAX_TRADES_PER_DAY=1, THESIS_CONFIRM_BARS=999)
rig.go('10:35')
rig.ff.premium['*'] = dict(lp=630.0, bid=629.0, ask=631.0)
rig.go('10:36')
rig.ff.premium['*'] = 400.0
n_trades = len(rig.trades())
rig.go('10:45', 5)
rig.go('10:50', 5)
eq('MAX_TRADES_PER_DAY stops a second entry', (n_trades, len(rig.trades()), rig.r.state['position']), (1, 1, None))

# --- 8h. a loss triggers the cooldown ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999, COOLDOWN_BARS_AFTER_LOSS=6)
rig.go('10:35')
rig.ff.premium['*'] = dict(lp=299.0, bid=298.0, ask=300.0)
rig.go('10:36')
ok('the stop-out was recorded as a loss for the cooldown', rig.r.state['last_loss_bar'] is not None)
rig.ff.premium['*'] = 400.0
rig.go('10:40', 5)
eq('inside the cooldown there is no re-entry', rig.r.state['position'], None)

# --- 8i. rate limit while holding: keep the position, do not guess ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999)
rig.go('10:35')
rig.ff.quote_reply = dict(s='error', code=-360, message="You've reached the Standard plan limit of 5,000 requests/day.")
secs = rig.go('10:36')
ok('a daily-cap reply while holding leaves the position OPEN', rig.r.state['position'] is not None)
ok('...and backs off rather than hammering', secs >= 60, secs)
eq('...without recording a phantom exit', len(rig.trades()), 0)
rig.ff.quote_reply = None
rig.budget.blocked_until = 0.0
rig.ff.premium['*'] = dict(lp=299.0, bid=298.0, ask=300.0)
rig.go('10:38')
eq('once the cap clears it is priced and stopped normally', rig.trades()[0]['kind'], 'stop')

# --- 8j. budget: exits ignore the cap, entries respect it ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999, MAX_API_CALLS_PER_DAY=3)
rig.go('10:35')
for _ in range(10):
    rig.budget.record('filler')
ok('the daily budget is now exhausted', rig.budget.calls > rig.c.MAX_API_CALLS_PER_DAY)
rig.ff.premium['*'] = dict(lp=299.0, bid=298.0, ask=300.0)
rig.go('10:36')
eq('a stop still executes with the budget exhausted (exits are never refused)', rig.trades()[0]['kind'], 'stop')
rig2 = Rig(flat_tape(55800), MAX_API_CALLS_PER_DAY=0)
rig2.go('10:35')
eq('with the budget spent, NEW entries are refused', rig2.r.state['position'], None)

# --- 8k. market closed ---
rig = Rig(flat_tape(55800))
rig.now = datetime(2026, 10, 24, 11, 0, 0, tzinfo=IST)     # Saturday
rig.refresh()
rig.r.step()
eq('on a weekend there are no API calls at all', rig.budget.calls, 0)

# --- 8l. a gap in the bars is not traded across ---
holed = flat_tape(55800)
for k in ('T', 'O', 'H', 'L', 'C', 'V'):
    holed[k] = (holed[k][:5] + holed[k][6:]) if k == 'T' else np.r_[holed[k][:5], holed[k][6:]]
rig = Rig(holed)
rig.go('10:35')
eq('bars with a missing candle are refused rather than computed across', rig.r.state['position'], None)

# --- 8m. day boundary ---
rig = Rig(flat_tape(55800), THESIS_CONFIRM_BARS=999)
rig.go('10:35')
rig.now = at('09:55', 10, day='2026-10-23')
rig.tape = flat_tape(55800)
rig.refresh()
rig.r.step()
ab = []
pth = os.path.join(rig.dir, 'logs', f'bnf_trades_{DAY}.jsonl')
if os.path.exists(pth):
    ab = [json.loads(x) for x in open(pth)]
eq('a position left over from yesterday is recorded as abandoned, not silently dropped',
   [x['kind'] for x in ab], ['abandoned'])
eq('...and the new day starts clean', rig.r.state['trades_today'] >= 0 and rig.r.state['date'], '2026-10-23')


# --- 8p. the previous session must really be the previous session ---
def day_candles(day, base=55000.0):
    y, mo, d_ = (int(x) for x in day.split('-'))
    out = []
    for i in range(75):
        op = datetime(y, mo, d_, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
        px = base + 3 * i
        out.append([op.timestamp(), px, px + 8, px - 8, px, 1.0e7])
    return out


h_gap = mk_history(40, end='2026-10-19')           # the 20th and 21st are missing
rig = Rig(flat_tape(55800))
rig.hist = h_gap
rig.ff.by_day = {'2026-10-20': day_candles('2026-10-20'), '2026-10-21': day_candles('2026-10-21', 55800.0)}
rig.build()
rig.go('09:40')
eq('two missing sessions are fetched, one API request each', sorted(set(rig.ff.h_days)), ['2026-10-20', '2026-10-21'])
eq('...and the context is NOT stale afterwards', rig.r.ctx['stale'], False)
eq('...and PDH now comes from the session that was fetched, not from Oct 19',
   rig.r.ctx['pdh'], max(8 + 55800.0 + 3 * i for i in range(75)))
eq('...counted under its own label in the API budget', rig.budget.by_label.get('day'), 2)

rig = Rig(flat_tape(55800))
rig.hist = mk_history(40, end='2026-10-19')
rig.ff.history_reply = dict(s='ok', candles=[])
rig.build()
rig.go('10:35')
eq('if the missing session cannot be fetched the context is stale', rig.r.ctx['stale'], True)
eq('...so a setup that would otherwise fire is REFUSED', rig.r.state['position'], None)
dd_ = rig.decisions()
eq('...and the decision log names the context gate as the ONLY blocker', dd_[-1]['blocked_by'] if dd_ else None, ['context'])

rig.ff.history_reply = None
rig.ff.by_day = {'2026-10-20': day_candles('2026-10-20'), '2026-10-21': day_candles('2026-10-21')}
rig.go('10:50')
ok('a stale context is retried after 10 minutes and repairs itself', rig.r.ctx['stale'] is False, rig.r.ctx.get('stale_reason'))
ok('...and once repaired, the very same kind of setup IS taken (the refusal was the context, not the tape)',
   rig.r.state['position'] is not None)

rig = Rig(flat_tape(55800))
rig.hist = mk_history(40, end='2026-10-12')           # 7 missing weekdays
rig.build()
rig.go('09:40')
eq('a gap too long to repair cheaply is stale WITHOUT spending API calls', (rig.r.ctx['stale'], rig.budget.calls), (True, 0))

rig = Rig(flat_tape(55800))
rig.hist = mk_history(40, end='2026-10-19')
rig.ff.history_reply = dict(s='error', code=-360, message="You've reached the Standard plan limit of 5,000 requests/day.")
rig.build()
rig.go('09:40')
eq('a daily-cap reply while repairing leaves it stale rather than guessing', rig.r.ctx['stale'], True)

# an exchange holiday is not a missing session either (Oct 20 2026 really was one)
rig = Rig(flat_tape(55800))
rig.hist = mk_history(40, end='2026-10-19')
rig.holidays = {'2026-10-20'}
rig.ff.by_day = {'2026-10-21': day_candles('2026-10-21')}
rig.build()
rig.go('09:40')
eq('a holiday between the last file and today is not fetched', sorted(set(rig.ff.h_days)), ['2026-10-21'])
eq('...and the context is not stale because of it', rig.r.ctx['stale'], False)

rig = Rig(flat_tape(55800))
rig.holidays = {DAY}
rig.go('10:35')
eq('on a holiday there are no API calls and no position', (rig.budget.calls, rig.r.state['position']), (0, None))

# Saturday/Sunday are not missing sessions
rig = Rig(flat_tape(55800))
rig.hist = mk_history(40, end='2026-10-16')           # Friday; Oct 19 is Monday
rig.ff.by_day = {'2026-10-19': day_candles('2026-10-19'), '2026-10-20': day_candles('2026-10-20'),
                 '2026-10-21': day_candles('2026-10-21')}
rig.build()
rig.go('09:40')
eq('weekends are not treated as missing: only Mon-Wed are fetched', sorted(set(rig.ff.h_days)),
   ['2026-10-19', '2026-10-20', '2026-10-21'])


# --- 8q. a long-running service must pick up each morning's token ---
print('\n--- token handling ---')
for code in (-8, -15, -16, -17, 401):
    eq(f'Fyers auth code {code} is recognised as an auth error', BR.is_auth_error(dict(s='error', code=code)), True)
eq('"token expired" wording is recognised', BR.is_auth_error(dict(s='error', message='Your token has expired')), True)
eq('a daily-cap reply is NOT an auth error', BR.is_auth_error(dict(s='error', code=-360, message="You've reached the Standard plan limit")), False)
eq('an unlisted-contract reply is NOT an auth error', BR.is_auth_error(dict(s='ok', d=[dict(v={})])), False)
eq('a non-dict is not an auth error', BR.is_auth_error(None), False)

ffA = FakeFyers()
ffA.quote_reply = dict(s='error', code=-16, message='could not authenticate the user')
brA = BR.Broker(ffA, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, DAY, sleep=lambda s: None), None)
eq('an auth reply is reported as auth_error, not mistaken for a missing quote', brA.quote('x', True)[1], 'auth_error')
eq('...and flags the broker so the runner rebuilds the client', brA.auth_failed, True)
brN = BR.Broker(None, BR.ApiBudget(tempfile.mkdtemp(), 100, 0.0, DAY, sleep=lambda s: None), None)
eq('with no client at all, calls fail cleanly instead of raising', brN.quote('x', True), (None, 'no client'))

built = []


def fake_reload():
    built.append(1)
    c = FakeFyers()
    c.today_str = DAY
    c.premium['*'] = 400.0
    return c


rig = Rig(flat_tape(55800))
rig.r._reload = fake_reload
rig.r._client_day = None
rig.broker.f = None                                   # the service started before today's token existed
rig.go('09:40')
eq('on the first market-open step of the day the client is built', len(built), 1)
rig.go('09:45')
eq('...and not rebuilt on every step', len(built), 1)
eq('the new client is what the broker now uses', rig.broker.f is not None, True)
rig.broker.auth_failed = True
rig.go('09:50')
eq('an auth reply triggers exactly one rebuild on the next step', len(built), 2)
eq('...and clears the flag', rig.broker.auth_failed, False)
rig.now = at('09:40', day='2026-10-23')
rig.refresh()
rig.r.step()
eq('a new day rebuilds the client again (the morning token is new)', len(built), 3)

failing = {'n': 0}


def bad_reload():
    failing['n'] += 1
    raise BR.TokenError('token is dated yesterday; the morning auth job has not run')


rig = Rig(flat_tape(55800))
rig.r._reload = bad_reload
rig.r._client_day = None
rig.broker.f = None
secs = rig.go('10:35')
eq('with no usable token nothing is traded and nothing is called', (rig.r.state['position'], rig.budget.calls), (None, 0))
ok('...it backs off and tries again later', secs >= 60, secs)
ok('...and says why in the heartbeat', 'no client' in json.load(open(os.path.join(rig.dir, 'status.json')))['note'])


# --- 8r. the option-chain recorder: logging only, free, and unable to slow the loop ---
print('\n--- option-chain recorder ---')
import threading            # noqa: E402
import time as _time        # noqa: E402


def fake_chain(spot=55800.0):
    rows = [dict(strike=k, call_oi=1000 + k % 700, call_iv=14.0 + (k - 55800) / 5000.0,
                 put_oi=1500 + k % 500, put_iv=15.0 - (k - 55800) / 5000.0) for k in range(52000, 59601, 100)]
    return dict(spot=spot, expiry='2026-10-27', all_expiries=['2026-10-27', '2026-11-24', '2026-12-29', 'x'],
                pcr=1.12, max_pain=55700.0, atm_iv=14.2, iv_skew=1.3, total_call_oi=1_000_000,
                total_put_oi=1_120_000, df=rows)


def mk_rec(loader, **kw):
    d = tempfile.mkdtemp()
    kw.setdefault('CHAIN_RECORD_EVERY_MIN', 15)         # the slot tests below are written for 15-minute slots
    c = mk_cfg(**kw)
    return CH.ChainRecorder(d, c, loader=loader), d


def chain_lines(d, day=DAY):
    p = os.path.join(d, f'bnf_chain_{day}.jsonl')
    return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


rec, d = mk_rec(lambda: fake_chain())
eq('before the open there is no slot', rec.slot_for(at('09:10')), None)
eq('09:15 is slot 0', rec.slot_for(at('09:15')), 0)
eq('09:29 is still slot 0 (15-minute slots)', rec.slot_for(at('09:29')), 0)
eq('09:30 is slot 1', rec.slot_for(at('09:30')), 1)
eq('15:30 is the last slot', rec.slot_for(at('15:30')) is not None, True)
eq('after the close there is no slot', rec.slot_for(at('15:35')), None)

ok('a due slot starts a fetch', rec.trigger(at('09:20'), 55800.0) is True)
rec.wait()
l1 = chain_lines(d)
eq('...and writes exactly one line', len(l1), 1)
r0 = l1[0]
eq('...carrying the summary fields', (r0['pcr'], r0['max_pain'], r0['atm_iv'], r0['iv_skew']), (1.12, 55700.0, 14.2, 1.3))
eq('...the spot and expiry', (r0['spot'], r0['expiry']), (55800.0, '2026-10-27'))
eq('...at most three expiries', len(r0['expiries']), 3)
eq('...and only strikes within +/-15 strikes of ATM', (min(map(int, r0['strikes'])), max(map(int, r0['strikes']))), (54300, 57300))
eq('...stored compactly as [call_oi, call_iv, put_oi, put_iv]', len(r0['strikes']['55800']), 4)
eq('...with the column names recorded alongside', r0['columns'], ['call_oi', 'call_iv', 'put_oi', 'put_iv'])
eq('the same slot is not recorded twice', rec.trigger(at('09:25'), 55800.0), False)
rec.wait()
eq('...so still one line', len(chain_lines(d)), 1)
ok('the next slot is recorded', rec.trigger(at('09:31'), 55800.0) is True)
rec.wait()
eq('...making two', len(chain_lines(d)), 2)
eq('a trigger outside market hours does nothing', rec.trigger(at('15:45'), 55800.0), False)
eq('...nor before the open', rec.trigger(at('08:00'), 55800.0), False)
eq('the success counter', rec.status()['ok'], 2)

# the SHIPPED cadence, and the property that motivated it
import bnf_config as _bcc       # noqa: E402
eq('shipped cadence is 5 minutes', _bcc.CHAIN_RECORD_EVERY_MIN, 5)
r5 = CH.ChainRecorder(tempfile.mkdtemp(), _bcc, loader=lambda: fake_chain())
eq('5-minute slots: 09:19 is slot 0, 09:20 is slot 1', (r5.slot_for(at('09:19')), r5.slot_for(at('09:20'))), (0, 1))
worst = 2 * _bcc.CHAIN_RECORD_EVERY_MIN               # one failed slot, then the next succeeds just before it is read
ok('one failed fetch cannot push the snapshot past the staleness limit that blocks entries',
   worst < _bcc.OI_MAX_SNAPSHOT_AGE_MIN, (worst, _bcc.OI_MAX_SNAPSHOT_AGE_MIN))

# never raises, and a failed slot is not retried until the next one
calls = {'n': 0}


def bad_loader():
    calls['n'] += 1
    raise RuntimeError('NSE blocked the request')


rec, d = mk_rec(bad_loader)
ok('a failing loader does not raise out of trigger()', rec.trigger(at('09:20')) is True)
rec.wait()
eq('...it is counted as a failure', (rec.status()['ok'], rec.status()['fail']), (0, 1))
ok('...with the reason kept', 'NSE blocked' in rec.status()['last_error'])
eq('...and writes no half-built line', chain_lines(d), [])
rec.trigger(at('09:24'))
rec.wait()
eq('a failed slot is NOT retried within the same slot (NSE rate-limits)', calls['n'], 1)
rec.trigger(at('09:31'))
rec.wait()
eq('...but the next slot tries again', calls['n'], 2)

rec, d = mk_rec(lambda: None)
rec.trigger(at('09:20'))
rec.wait()
eq('a None chain (market closed / blocked) is a failure, not an empty line', (rec.status()['fail'], chain_lines(d)), (1, []))
rec, d = mk_rec(lambda: dict(spot=55800.0, df=[]))
rec.trigger(at('09:20'))
rec.wait()
eq('a chain with no strikes is a failure too', rec.status()['fail'], 1)
rec, d = mk_rec(lambda: fake_chain(), CHAIN_RECORD_ENABLED=False)
eq('switched off in config, it never starts a fetch', rec.trigger(at('09:20')), False)

# THE POINT OF THE THREAD: a hung web request must not delay the trading loop
gate = threading.Event()
started = []


def hung_loader():
    started.append(1)
    gate.wait(30)
    return fake_chain()


rec, d = mk_rec(hung_loader)
t0 = _time.monotonic()
r_ = rec.trigger(at('09:20'))
elapsed = _time.monotonic() - t0
ok('trigger() returns immediately while the fetch hangs', r_ is True and elapsed < 0.5, elapsed)
eq('a second trigger in the SAME slot does nothing', rec.trigger(at('09:21')), False)
eq('...and in a later slot it still will not stack a second fetch on a hung one',
   rec.trigger(at('09:31')), False)
eq('only one fetch is in flight', len(started), 1)
rec._started -= 1000                                   # pretend it has hung past CHAIN_FETCH_STALE_SEC
ok('once the hung fetch is stale, the next slot is allowed to proceed', rec.trigger(at('09:46')) is True)
gate.set()
rec.wait(5)

# the runner: triggers it, never depends on it, and survives it blowing up
class Boom:
    def trigger(self, now, spot=None):
        raise RuntimeError('recorder exploded')

    def status(self):
        raise RuntimeError('status exploded')


rig = Rig(flat_tape(55800))
rig.r.chain = Boom()
rig.go('10:35')
ok('a recorder that raises cannot stop the runner trading', rig.r.state['position'] is not None)

rig = Rig(flat_tape(55800))
spy = []


class Spy:
    def trigger(self, now, spot=None):
        spy.append((now.strftime('%H:%M'), spot))
        return False

    def status(self):
        return dict(ok=0, fail=0)


rig.r.chain = Spy()
rig.go('09:40')
rig.go('10:35')
ok('the runner offers the recorder every market-hours step', len(spy) >= 2, spy)
ok('...passing the last spot it knows once it has bars', spy[-1][1] is None or spy[-1][1] > 0)
rig = Rig(flat_tape(55800))
rig.r.chain = Spy()
rig.now = datetime(2026, 10, 24, 11, 0, 0, tzinfo=IST)         # Saturday
rig.refresh()
spy.clear()
rig.r.step()
eq('on a weekend the recorder is not even offered a slot', spy, [])
hb2 = None
rig = Rig(flat_tape(55800))
real_rec, _d = mk_rec(lambda: fake_chain())
rig.r.chain = real_rec
rig.go('09:40')
real_rec.wait()
hb2 = json.load(open(os.path.join(rig.dir, 'status.json')))
eq('...with a chain block in status.json', sorted(hb2['chain']), ['fail', 'last_error', 'last_ok', 'ok'])

# nothing in the DECISION code may consume the chain: it is logging only
src_strategy = open(os.path.join(os.path.dirname(HERE), 'bnf_strategy.py'), encoding='utf-8').read()
src_runner_entry = open(os.path.join(os.path.dirname(HERE), 'bnf_runner.py'), encoding='utf-8').read()
ok('bnf_strategy never imports the chain recorder', 'bnf_chain' not in src_strategy)
import re as _re            # noqa: E402
_used = set(_re.findall(r'self\.chain\.(\w+)', src_runner_entry))
ok('the runner touches the recorder only through trigger / status / snapshot',
   _used <= {'trigger', 'status', 'snapshot'}, _used)


# --- 8s. start-up code that no simulated day exercises ---
# `import logging.handlers` placed INSIDE _setup_logging made `logging` a local name for the
# whole function, so the first line (logging.getLogger) raised UnboundLocalError. The service
# crash-looped on the real host; every test passed, because none of them called main() or
# _setup_logging(). These do.
print('\n--- start-up path ---')
import logging as _lg           # noqa: E402

_ld = tempfile.mkdtemp()
_lgr = RN._setup_logging(_ld)
_lgr.info('start-up smoke line')
for _h in _lgr.handlers:
    _h.flush()
ok('_setup_logging runs without raising and returns a logger', isinstance(_lgr, _lg.Logger))
ok('...using a daily-rotating handler, not a file named for the launch date',
   any(isinstance(h, _lg.handlers.TimedRotatingFileHandler) for h in _lgr.handlers))
eq('...which writes to a stable file name', os.path.exists(os.path.join(_ld, 'bnf.log')), True)
ok('...and the line actually landed in it', 'start-up smoke line' in open(os.path.join(_ld, 'bnf.log')).read())
_lgr.handlers[:] = []

# main() end to end for the paths that need no network: --preflight and --status
import bnf_config as _bc        # noqa: E402
_sd = tempfile.mkdtemp()
_old = (_bc.STATE_DIR, _bc.HISTORY_DIR)
_bc.STATE_DIR, _bc.HISTORY_DIR = _sd, tmpd           # tmpd = the 3-session history built earlier
try:
    rc = RN.main(['--preflight'])
    eq('main --preflight runs to completion (exit 2 here: only 3 sessions of history)', rc, 2)
    eq('main --status runs to completion', RN.main(['--status']), 0)
finally:
    _bc.STATE_DIR, _bc.HISTORY_DIR = _old
    _lg.getLogger('bnf').handlers[:] = []


# --- 8t. the OI / PCR entry gate ---
print('\n--- OI / PCR gate ---')


def mk_snap(pcr=1.1, call_wall=(55300, 900_000), put_wall=(54700, 800_000), age=3.0, spot_grid=(53500, 56501),
            ts='2026-10-22T12:10:00+05:30'):
    """A chain with modest OI everywhere and one big wall on each side."""
    strikes = {}
    for k in range(spot_grid[0], spot_grid[1], 100):
        strikes[str(k)] = [10_000, 14.0, 10_000, 15.0]
    if call_wall:
        strikes[str(call_wall[0])][0] = call_wall[1]
    if put_wall:
        strikes[str(put_wall[0])][2] = put_wall[1]
    return dict(pcr=pcr, age_min=age, strikes=strikes, ts=ts)


PRIOR = [1.0] * 10 + [1.2] * 10                      # mean 1.1, std 0.1
cfgO = mk_cfg(OI_GATE_ENABLED=True)
f = S.oi_features(mk_snap(pcr=1.3), 55000.0, PRIOR, cfgO)
approx('PCR is z-scored against the prior sessions: (1.3 - 1.1) / 0.1 = 2', f['pcr_z'], 2.0, 1e-6)
eq('fewer than OI_PCR_MIN_HISTORY prior sessions -> no z-score, not a guess',
   S.oi_features(mk_snap(pcr=1.3), 55000.0, [1.0] * 5, cfgO)['pcr_z'], None)
eq('the biggest CALL wall above spot', f['wall_up_strike'], 55300.0)
approx('...and its distance as a % of spot', f['wall_up_pct'], 300 / 55000 * 100, 1e-6)
eq('the biggest PUT wall below spot', f['wall_dn_strike'], 54700.0)
approx('...and its distance', f['wall_dn_pct'], 300 / 55000 * 100, 1e-6)
big = mk_snap(call_wall=None)
big['strikes']['55100'][0] = 500_000
big['strikes']['55400'][0] = 900_000
eq('of several call walls the BIGGEST wins, not the nearest', S.oi_features(big, 55000.0, PRIOR, cfgO)['wall_up_strike'], 55400.0)
far = mk_snap(call_wall=None, put_wall=None)
far['strikes']['56000'][0] = 9_000_000                # 1.8% away, outside the 1.5% look-ahead
eq('a wall beyond OI_WALL_LOOKAHEAD_PCT is ignored', S.oi_features(far, 55000.0, PRIOR, cfgO)['wall_up_strike'] != 56000.0, True)
eq('no snapshot -> no features', S.oi_features(None, 55000.0, PRIOR, cfgO), None)

# the gate inside evaluate_entry. coil_bars() presses PDH 56000 => a CALL toward resistance.
bars_c = coil_bars()
day_c = entry_day()
cfgG = mk_cfg(OI_GATE_ENABLED=True, COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))


def oi_feat(pcr_z=1.0, up=1.2, dn=1.2):
    return dict(pcr=1.2, pcr_z=pcr_z, age_min=3.0, n_prior=20, wall_up_strike=56700.0, wall_up_pct=up,
                wall_dn_strike=55000.0, wall_dn_pct=dn)


def ev(oi, bars=bars_c, cfg_=cfgG, day_=day_c):
    return S.evaluate_entry(bars, 29, day_, dict(st0, oi=oi), cfg_)


d1 = ev(oi_feat())
ok('PCR agrees and the way is clear -> enters', d1.enter is True, d1.gates)
ok('...and the OI features travel with the thesis for the trade record', d1.thesis['oi']['pcr_z'] == 1.0)
d2 = ev(oi_feat(pcr_z=-1.0))
eq('PCR disagrees with the trade -> only the OI gate blocks', (d2.enter, d2.blocked_by()), (False, ['oi']))
ok("...and says WHY: 'DISAGREES'", 'DISAGREES' in d2.gates['oi'][1], d2.gates['oi'])
eq('...and the refusal still carries the would-be direction (for measuring the gate)', d2.direction, 1)
ok('...and the would-be thesis', 'invalidation' in d2.thesis and d2.thesis['level'] == 'PDH')
d3 = ev(oi_feat(up=0.3))
eq('a big call wall 0.3% above blocks a CALL toward resistance', (d3.enter, d3.blocked_by()), (False, ['oi']))
ok("...and says 'BLOCKS'", 'BLOCKS' in d3.gates['oi'][1], d3.gates['oi'])
eq('a wall exactly at the clearance distance counts as clear (>=)', ev(oi_feat(up=0.5)).enter, True)
eq('a wall just inside it does not', ev(oi_feat(up=0.4999)).enter, False)
eq('a PUT-side wall nearby does NOT block a CALL', ev(oi_feat(dn=0.1)).enter, True)
eq('no wall in range -> passes, and says so', ev(dict(oi_feat(), wall_up_pct=None, wall_up_strike=None)).gates['oi'][1].endswith('no wall in range'), True)
eq('a direction-signed z below OI_PCR_Z_MIN blocks', ev(oi_feat(pcr_z=0.3), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_PCR_Z_MIN=0.5,
   COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))).enter, False)
eq('...and above it passes', ev(oi_feat(pcr_z=0.6), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_PCR_Z_MIN=0.5,
   COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))).enter, True)

# the mirror: a PUT toward support wants PCR BELOW its norm and a clear path DOWN
sup_b = coil_bars(px=54020.0)
dsup = S.evaluate_entry(sup_b, 29, day_c, dict(st0, oi=oi_feat(pcr_z=-1.0, dn=1.0)), cfgG)
eq('a PUT toward support enters when PCR is below its norm and the way down is clear', (dsup.enter, dsup.direction), (True, -1))
eq('...and is blocked when PCR is ABOVE its norm',
   S.evaluate_entry(sup_b, 29, day_c, dict(st0, oi=oi_feat(pcr_z=+1.0, dn=1.0)), cfgG).blocked_by(), ['oi'])
eq('...and when a put wall sits just below',
   S.evaluate_entry(sup_b, 29, day_c, dict(st0, oi=oi_feat(pcr_z=-1.0, dn=0.2)), cfgG).blocked_by(), ['oi'])
cfgAway = mk_cfg(OI_GATE_ENABLED=True, COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'), DIRECTION='away')
eq("with DIRECTION='away' (a PUT at resistance) the gate asks the PUT questions",
   S.evaluate_entry(bars_c, 29, day_c, dict(st0, oi=oi_feat(pcr_z=-1.0, dn=1.0)), cfgAway).enter, True)

# missing data
eq("no chain data blocks by default ('block')", (ev(None).enter, ev(None).gates['oi']), (False, (False, 'no chain data')))
eq("...and passes when OI_ON_MISSING='pass'",
   ev(None, cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_ON_MISSING='pass', COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))).enter, True)
eq('PCR z unavailable (thin history) is treated as missing, not as agreement', ev(oi_feat(pcr_z=None)).enter, False)
eq('...but the wall condition still applies on its own when PCR is switched off',
   ev(oi_feat(pcr_z=None), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_USE_PCR=False, COIL_RATIO_MAX=0.812,
                                      LEVELS=('PDH', 'PDL'))).enter, True)
eq('OI_USE_PCR off: a disagreeing PCR no longer blocks',
   ev(oi_feat(pcr_z=-2.0), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_USE_PCR=False, COIL_RATIO_MAX=0.812,
                                      LEVELS=('PDH', 'PDL'))).enter, True)
eq('OI_USE_WALLS off: a wall right on top no longer blocks',
   ev(oi_feat(up=0.1), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_USE_WALLS=False, COIL_RATIO_MAX=0.812,
                                  LEVELS=('PDH', 'PDL'))).enter, True)
eq('gate disabled: it passes and says off', ev(None, cfg_=mk_cfg(OI_GATE_ENABLED=False, COIL_RATIO_MAX=0.812,
   LEVELS=('PDH', 'PDL'))).gates['oi'], (True, 'off'))

# the would-be thesis is ONLY carried when OI is the sole blocker
dboth = S.evaluate_entry(bars_c, 29, entry_day(hot=False), dict(st0, oi=oi_feat(pcr_z=-1.0)), cfgG)
eq('if another gate also fails, no would-be setup is reported', (dboth.direction, dboth.thesis), (0, {}))

# shadow mode: same inputs, the gate fails, the trade is still taken and the verdict recorded
cfgS = mk_cfg(OI_GATE_ENABLED=True, OI_GATE_BLOCKS=False, COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))
dS = ev(oi_feat(pcr_z=-1.0), cfg_=cfgS)
eq('SHADOW: a disagreeing PCR no longer stops the entry', (dS.enter, dS.blocked_by()), (True, []))
eq('...but the decision says the gate would have blocked', dS.oi_would_block, True)
ok('...and the gate detail says so', 'SHADOW: would block' in dS.gates['oi'][1], dS.gates['oi'])
eq('...and the trade thesis carries the verdict for later scoring', (dS.thesis['oi_would_block'], 'DISAGREES' in dS.thesis['oi_detail']), (True, True))
dS2 = ev(oi_feat(up=0.1), cfg_=cfgS)
eq('SHADOW: a wall right overhead is recorded, not enforced', (dS2.enter, dS2.oi_would_block), (True, True))
dS3 = ev(oi_feat(), cfg_=cfgS)
eq('SHADOW: when the gate agrees, would_block is False', (dS3.enter, dS3.oi_would_block), (True, False))
dS4 = ev(None, cfg_=cfgS)
eq('SHADOW: no chain data -> enters, and records that the gate could not evaluate', (dS4.enter, dS4.oi_would_block), (True, True))
eq('BLOCK mode is unchanged: the same disagreement refuses', (ev(oi_feat(pcr_z=-1.0)).enter, ev(oi_feat(pcr_z=-1.0)).oi_would_block), (False, True))
eq('gate disabled: nothing is ever "would block"', ev(None, cfg_=mk_cfg(OI_GATE_ENABLED=False, COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL'))).oi_would_block, False)
eq('SHADOW still refuses when ANOTHER gate fails', ev(oi_feat(), cfg_=cfgS, day_=entry_day(hot=False)).enter, False)

# config shipped
import bnf_config as _bc2       # noqa: E402
eq('the shipped config evaluates the OI gate', _bc2.OI_GATE_ENABLED, True)
eq('the shipped config has the volume gate OFF since 2026-10-27', _bc2.VOLUME_MODE, 'off')
eq('...in SHADOW mode (logs the verdict, does not block) since 2026-10-24', _bc2.OI_GATE_BLOCKS, False)
eq('...blocking when data is missing', _bc2.OI_ON_MISSING, 'block')
eq('...using both conditions', (_bc2.OI_USE_PCR, _bc2.OI_USE_WALLS), (True, True))
ok('validate() rejects a gate that checks nothing',
   any('gates nothing' in x for x in validate_with(OI_USE_PCR=False, OI_USE_WALLS=False)))
ok("validate() rejects an unknown OI_ON_MISSING", any('OI_ON_MISSING' in x for x in validate_with(OI_ON_MISSING='maybe')))

# expiry day: the nearest chain is the expiring series, not the one the bot trades
eq("an NSE-format expiry equal to today flags the record", CH._is_expiry_day('22-Oct-2026', at('10:00')), True)
eq("...ISO format too", CH._is_expiry_day('2026-10-22', at('10:00')), True)
eq("...a different day does not", CH._is_expiry_day('27-Oct-2026', at('10:00')), False)
eq("...garbage or None does not raise", (CH._is_expiry_day(None, at('10:00')), CH._is_expiry_day('??', at('10:00'))), (False, False))
recE, dE = mk_rec(lambda: dict(fake_chain(), expiry='22-Oct-2026'))
recE.trigger(at('09:20'), 55800.0)
recE.wait()
eq('the recorder marks a snapshot taken on the expiry day', chain_lines(dE)[0]['expiry_day'], True)
recN, dN = mk_rec(lambda: fake_chain())
recN.trigger(at('09:20'), 55800.0)
recN.wait()
eq('...and not on other days', chain_lines(dN)[0]['expiry_day'], False)
fE = S.oi_features(dict(mk_snap(pcr=0.5), expiry_day=True), 55000.0, PRIOR, cfgO)
eq('oi_features carries the flag', fE['expiry_day'], True)
dx = ev(dict(oi_feat(pcr_z=-3.4, dn=0.14), expiry_day=True), cfg_=mk_cfg(OI_GATE_ENABLED=True, COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL')))
eq('on an expiry day the gate is skipped: it passes whatever PCR/walls say', (dx.enter, dx.oi_would_block), (True, False))
ok('...and says why', 'expiry day' in dx.gates['oi'][1], dx.gates['oi'])
dx2 = ev(dict(oi_feat(pcr_z=-1.0), expiry_day=True), cfg_=mk_cfg(OI_GATE_ENABLED=True, OI_SKIP_ON_EXPIRY_DAY=False,
         COIL_RATIO_MAX=0.812, LEVELS=('PDH', 'PDL')))
eq('with the skip switched off the gate judges the expiring chain as before', (dx2.enter, dx2.blocked_by()), (False, ['oi']))
eq('the shipped config skips the gate on expiry days', _bc2.OI_SKIP_ON_EXPIRY_DAY, True)

# chain recorder: latest snapshot, age, restart
rec, d_ = mk_rec(lambda: fake_chain())
eq('before any fetch there is no snapshot', rec.snapshot(at('09:20')), None)
rec.trigger(at('09:20'), 55800.0)
rec.wait()
sn = rec.snapshot(at('09:30'))
approx('a snapshot carries its age in minutes', sn['age_min'], 10.0, 0.05)
eq('...and the PCR and strikes', (sn['pcr'], len(sn['strikes']) > 20), (1.12, True))
rec2 = CH.ChainRecorder(d_, mk_cfg(), loader=lambda: fake_chain())
sn2 = rec2.snapshot(at('09:35'))
ok('after a restart the last snapshot is recovered from the file', sn2 is not None and abs(sn2['age_min'] - 15.0) < 0.05, sn2 and sn2['age_min'])

# PCR history
hd = tempfile.mkdtemp()
hp = os.path.join(hd, 'oi_intraday.jsonl')
with open(hp, 'w') as fh:
    for dd_, pcrs in (('2026-10-19', (0.9, 1.0, 1.1)), ('2026-10-21', (1.2, 1.3, 1.4)), ('2026-10-22', (9.9, 9.9, 9.9))):
        for k, hh in enumerate(('09:20', '10:00', '12:00')):
            fh.write(json.dumps(dict(instrument='BANKNIFTY', ts_str=f'{dd_}T{hh}:00', pcr=pcrs[k])) + '\n')
    fh.write(json.dumps(dict(instrument='NIFTY', ts_str='2026-10-21T10:00:00', pcr=5.5)) + '\n')
ph = CH.PcrHistory(hp, 'BANKNIFTY')
eq('PCR history: the last reading at or before the time, per prior session', ph.prior('2026-10-22', '10:30'), [1.0, 1.3])
eq("...excludes the day itself", 9.9 in ph.prior('2026-10-22', '12:00'), False)
eq("...ignores the other instrument", 5.5 in ph.prior('2026-10-22', '10:30'), False)
eq('...honours n', ph.prior('2026-10-22', '10:30', n=1), [1.3])
eq('a missing file gives an empty history, not an exception', CH.PcrHistory(os.path.join(hd, 'nope.jsonl'), 'BANKNIFTY').prior('2026-10-22', '10:00'), [])


# the runner end to end
class FakeChain:
    def __init__(self, snap=None, age=3.0):
        self.snap, self.age, self.calls = snap, age, 0

    def trigger(self, now, spot=None):
        return False

    def status(self):
        return dict(ok=1, fail=0, last_ok=None, last_error=None)

    def snapshot(self, now):
        self.calls += 1
        if self.snap is None:
            return None
        return dict(self.snap, age_min=self.age, ts=now.isoformat(timespec='seconds'))


class FakeHist:
    def prior(self, day, hhmm, n=20):
        return [1.0] * 15


def oi_rig(snap, age=3.0, **kw):
    r_ = Rig(flat_tape(55800), OI_GATE_ENABLED=True, **kw)
    r_.r.chain = FakeChain(snap, age)
    r_.r.pcr_hist = FakeHist()
    return r_


good = mk_snap(pcr=1.3, call_wall=(56600, 900_000), put_wall=(55000, 900_000), spot_grid=(54000, 57001))
rig = oi_rig(good)
rig.go('10:35')
ok('PCR above its norm and a clear path: the CALL is taken', rig.r.state['position'] is not None)
eq('...the OI features are on the position\'s thesis', rig.r.state['position']['thesis']['oi']['pcr_z'] > 1, True)
dd2 = rig.decisions()[-1]
eq('...and in the decision log', dd2['oi']['wall_up_strike'], 56600.0)

bad = mk_snap(pcr=0.8, call_wall=(56600, 900_000), put_wall=(55000, 900_000), spot_grid=(54000, 57001))
rig = oi_rig(bad)
rig.go('10:35')
eq('PCR below its norm: the same CALL is refused', rig.r.state['position'], None)
dd3 = rig.decisions()[-1]
eq('...blocked by the OI gate alone', dd3['blocked_by'], ['oi'])
eq('...and the refusal is logged with the direction it would have taken', dd3['shadow']['direction'], 1)
ok('...with the thesis fields a later replay needs', {'invalidation', 'coil_hi', 'coil_lo', 'atr', 'entry_px'} <= set(dd3['shadow']))

rig = oi_rig(bad, OI_GATE_BLOCKS=False)
rig.go('10:35')
ok('SHADOW through the runner: the setup the gate dislikes is still traded', rig.r.state['position'] is not None)
dsh = rig.decisions()[-1]
eq('...the decision log records that the gate would have blocked', (dsh['enter'], dsh['oi_would_block'], dsh['blocked_by']), (True, True, []))
eq('...and the open position carries the verdict', rig.r.state['position']['thesis']['oi_would_block'], True)

near_wall = mk_snap(pcr=1.3, call_wall=(55900, 900_000), put_wall=(55000, 900_000), spot_grid=(54000, 57001))
rig = oi_rig(near_wall)
rig.go('10:35')
eq('a call wall 0.18% overhead: the CALL is refused', rig.r.state['position'], None)

rig = Rig(flat_tape(55800), OI_GATE_ENABLED=True)
rig.go('10:35')
eq('gate on but no recorder at all: refused, not silently unfiltered', rig.r.state['position'], None)
eq('...and the log says there was no chain data', rig.decisions()[-1]['gates']['oi'][1], 'no chain data')
rig = Rig(flat_tape(55800), OI_GATE_ENABLED=True, OI_ON_MISSING='pass')
rig.go('10:35')
ok("...with OI_ON_MISSING='pass' it trades without the filter", rig.r.state['position'] is not None)

rig = oi_rig(good, age=45.0)
rig.go('10:35')
eq('a snapshot older than OI_MAX_SNAPSHOT_AGE_MIN is treated as missing', rig.r.state['position'], None)
eq('...and is reported as such', rig.decisions()[-1]['gates']['oi'][1], 'no chain data')

rig = Rig(flat_tape(55800))                           # gate OFF (test default)
fc = FakeChain(good)
rig.r.chain = fc
rig.go('10:35')
eq('with the gate off the snapshot is never even read', fc.calls, 0)
ok('...and the trade is taken as before', rig.r.state['position'] is not None)

# replay: the gate cannot be replayed, so replay turns it off (otherwise every setup would block)
eq('the replay config switches the OI gate off', RP._ReplayCfg().OI_GATE_ENABLED, False)
hist40 = mk_history(40)
t_off, g_off = RP.run(hist40, 'strategy')
ok('the replay evaluated bars (so a quiet result is not an artefact of a blocked gate)', g_off['evaluated'] > 100, g_off['evaluated'])
t_on, g_on = RP.run(hist40, 'strategy', c=mk_cfg(OI_GATE_ENABLED=True))
eq('...whereas leaving the gate on with no history blocks every setup', len(t_on), 0)
asked = []
RP.run(hist40[:25], 'strategy', c=mk_cfg(OI_GATE_ENABLED=True, REQUIRE_HOT_REGIME=False, VOLUME_MODE='off',
                                          COIL_RATIO_MAX=99.0, LEVEL_PROX_PCT=50.0),
       oi_provider=lambda day, hhmm: (asked.append((day, hhmm)) or None))
ok('replay can be given an OI provider, which is asked per bar with (day, time)', len(asked) > 10 and len(asked[0][1]) == 5, asked[:2])

# --- 8n. preflight ---
rig = Rig(flat_tape(55800))
names = {n: okk for n, okk, _ in rig.r.preflight()}
eq('preflight passes with 40 sessions of history', (names['config'], names['history']), (True, True))
rig.r._history = mk_history(10)
names = {n: okk for n, okk, _ in rig.r.preflight()}
eq('...and fails the history check with too little data', names['history'], False)

# --- 8o. status file ---
rig = Rig(flat_tape(55800))
rig.go('10:35')
st_path = os.path.join(rig.dir, 'status.json')
ok('a heartbeat file is written after a step', os.path.exists(st_path))
hb = json.load(open(st_path))
eq('...and it reports the day, the position and the API usage',
   (hb['day'], hb['in_position'], hb['api_calls'] > 0), (DAY, True, True))
ok('...including a per-label breakdown of the calls', hb['api_by_label'].get('bars', 0) >= 1, hb)

print(f'\n{P} passed, {F} failed')
sys.exit(1 if F else 0)
