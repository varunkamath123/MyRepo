# -*- coding: utf-8 -*-
"""Unit-test overnight_india: the paper maths must be exact and the live path must never double-sell or overspend."""
import os, shutil, sys, tempfile, types
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import overnight_india as OI

P = F = 0


def ck(name, cond, detail=''):
    global P, F
    if cond:
        P += 1
        print(f'  PASS  {name}')
    else:
        F += 1
        print(f'  FAIL  {name}  {detail}')


TMP = os.path.join(tempfile.gettempdir(), 'overnight_test')


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    config.LOG_DIRECTORY = TMP
    OI._bhav_cache.clear()


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def now(self):
        return datetime.fromtimestamp(self.t, OI.IST)


def use_clock(y, mo, d, hh, mm, ss=0):
    c = Clock(OI.IST.localize(datetime(y, mo, d, hh, mm, ss)))
    OI.time = types.SimpleNamespace(time=c.time, sleep=c.sleep)
    OI.now_ist = c.now
    return c


class FakeFyers:
    def __init__(self):
        self.orders, self.placed, self.next_id = {}, [], 100
        self.quote_map, self.funds_avail, self.reject = {}, 100000.0, set()
        self.fill_px, self.on_place, self.cancel_ok = {}, None, True

    def place_order(self, data):
        self.placed.append(dict(data))
        if data['orderTag'] in self.reject:
            return {'s': 'error', 'code': -50, 'message': 'rejected by test'}
        oid = str(self.next_id)
        self.next_id += 1
        o = {'id': oid, 'status': OI.PENDING, 'tradedPrice': 0, 'filledQty': 0, 'qty': data['qty'],
             'symbol': data['symbol'], 'orderTag': data['orderTag']}
        self.orders[oid] = o
        if self.on_place:
            self.on_place(self, o, data)
        return {'s': 'ok', 'id': oid}

    def fill(self, oid, px):
        self.orders[oid].update({'status': OI.FILLED, 'tradedPrice': px, 'filledQty': self.orders[oid]['qty']})

    def orderbook(self):
        return {'s': 'ok', 'orderBook': [dict(o) for o in self.orders.values()]}

    def cancel_order(self, d):
        o = self.orders[d['id']]
        if self.cancel_ok and o['status'] in (OI.PENDING, OI.TRANSIT):
            o['status'] = OI.CANCELLED
        return {'s': 'ok'}

    def quotes(self, d):
        return {'s': 'ok', 'd': [{'n': s, 's': 'ok', 'v': self.quote_map[s]}
                                 for s in d['symbols'].split(',') if s in self.quote_map]}

    def funds(self):
        return {'s': 'ok', 'fund_limit': [{'title': 'Available Balance', 'equityAmount': self.funds_avail}]}

    def tradebook(self):
        return {'s': 'ok', 'tradeBook': []}


def sessions(n, end=date(2026, 10, 6)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


BHAV_TEXT = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
ABC, EQ, 06-Oct-2026, 100.00, 101.50, 103.00, 100.50, 102.10, 102.00, 101.9, 50000, 5100.25, 900, 20000, 40.0
ABC, BE, 06-Oct-2026, 100.00, 99.00, 99.00, 99.00, 99.00, 99.00, 99.0, 10, 0.01, 1, 10, 100.0
XYZ, EQ, 06-Oct-2026, 50.00, 49.00, 51.00, 48.00, 50.50, 50.40, 50.1, 1000, 5.04, 30, 500, 50.0
BAD, EQ, 06-Oct-2026, -, -, -, -, -, -, -, 0, 0, 0, -, -
"""


def test_parse_and_fetch():
    print('\nbhavcopy parsing and holiday detection')
    fresh()
    d, rows = OI.parse_bhav(BHAV_TEXT)
    ck('trade date read from DATE1', d == date(2026, 10, 6), d)
    ck('EQ rows kept, BE and unparseable rows dropped', set(rows) == {'ABC', 'XYZ'}, rows.keys())
    ck('ABC fields exact', rows['ABC'] == {'prev': 100.0, 'open': 101.5, 'close': 102.0, 'last': 102.1, 'turn_lacs': 5100.25})

    class R:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    OI.nse_get = lambda url: R(200, BHAV_TEXT)
    ck('matching file is stored', OI.fetch_bhav(date(2026, 10, 6)) == 'ok' and OI.load_bhav(date(2026, 10, 6))['XYZ']['open'] == 49.0)
    ck("holiday: NSE serves another session's file", OI.fetch_bhav(date(2026, 10, 2)) == 'holiday')
    ck('holiday remembered', date(2026, 10, 2) in OI.known_holidays())
    ck('holiday not stored as a session', date(2026, 10, 2) not in OI.stored_sessions())
    OI.nse_get = lambda url: R(404, 'not found')
    ck('unpublished file is missing', OI.fetch_bhav(date(2026, 10, 7)) == 'missing')


def write_history(n=260, list_d_at=230):
    days = sessions(n)
    px = {'AAA': 100.0, 'BBB': 100.0, 'CCC': 100.0, 'DDD': 100.0, 'SPL': 1000.0, 'RAW': 1500.0}
    for i, d in enumerate(days):
        rows = {}
        for sym, on in (('AAA', 0.005), ('BBB', 0.001), ('CCC', 0.010), ('DDD', 0.001), ('SPL', 0.002), ('RAW', 0.003)):
            if sym == 'DDD' and i < n - list_d_at:
                continue
            prev = px[sym]
            if sym == 'SPL' and i == n - 40:
                prev = prev / 2  # 1:2 split, exchange-adjusted previous close
            if sym == 'DDD' and i == n - list_d_at:
                prev = 60.0  # listing day: PREV_CLOSE is the issue price
            o = prev * (1 + on)
            if sym == 'RAW' and i == n - 100:
                o = prev / 5 * (1 + on)  # 1:5 split the bhavcopy did NOT adjust: PREV_CLOSE is pre-split
            c = o * (1 - on * 0.5)
            rows[sym] = {'prev': prev, 'open': o, 'close': c, 'last': c, 'turn_lacs': 300.0 if sym == 'CCC' else 9000.0}
            px[sym] = c
        OI.save_bhav(d, rows)
    OI._bhav_cache.clear()
    return days


def test_rank_and_ema():
    print('\nranking, adjusted closes, EMA')
    fresh()
    days = write_history()
    ranked = OI.rank(days, {'AAA', 'BBB', 'CCC', 'DDD', 'SPL', 'RAW'})
    syms = [r['symbol'] for r in ranked]
    ck('highest overnight mean first', syms[0] == 'AAA', syms)
    raw = next(r for r in ranked if r['symbol'] == 'RAW')
    ck('unadjusted split (-80% "gap") dropped from the mean', abs(raw['on_bps'] - 30.0) < 0.5 and raw['obs'] == OI.FORM_SESSIONS - 2, raw)
    raw_series = OI.adjusted_closes('RAW', days[-120:])
    ck('EMA history restarts at the unadjusted split', len(raw_series) == 100 and
       max(abs(raw_series[i + 1] / raw_series[i] - 1) for i in range(99)) < 0.01, len(raw_series))
    last_raw = OI.load_bhav(days[-1])['RAW']['close']
    ck('post-split stock judged on post-split history', OI.decide('RAW', days, {'lp': last_raw * 1.01, 'prev_close_price': last_raw})['decision'] == 'enter')
    ck('split inside the last 63 sessions -> not enough clean history',
       OI.decide('RAW', days[:-50], {'lp': 1.0, 'prev_close_price': 1.0})['decision'] == 'skip_no_history')
    ck('illiquid name (Rs3cr/day) excluded', 'CCC' not in syms, syms)
    ddd = next(r for r in ranked if r['symbol'] == 'DDD')
    ck('listing-day gap excluded from the mean', abs(ddd['on_bps'] - 10.0) < 0.5, ddd)
    ck('first window session has no prior close', all(r['obs'] <= OI.FORM_SESSIONS - 1 for r in ranked))
    spl = OI.adjusted_closes('SPL', days[-80:])
    jumps = [spl[i + 1] / spl[i] - 1 for i in range(len(spl) - 1)]
    ck('split does not break the adjusted series', max(abs(j) for j in jumps) < 0.01, max(jumps))
    ck('adjusted series ends at the last official close', abs(spl[-1] - OI.load_bhav(days[-1])['SPL']['close']) < 1e-9)
    vals = [10, 11, 12, 11, 13, 14, 12, 15]
    try:
        import pandas as pd
        ref = pd.Series(vals, dtype=float).ewm(span=21, adjust=False).mean().iloc[-1]
        ck('EMA matches pandas ewm(adjust=False)', abs(OI.ema(vals) - ref) < 1e-9, (OI.ema(vals), ref))
    except ImportError:
        pass
    last = OI.load_bhav(days[-1])['AAA']['close']
    up = OI.decide('AAA', days, {'lp': last * 1.01, 'prev_close_price': last})
    down = OI.decide('AAA', days, {'lp': last * 0.80, 'prev_close_price': last})
    ck('rising stock above its EMA -> enter', up['decision'] == 'enter', up)
    ck('crashed stock below its EMA -> skip', down['decision'] == 'skip_below_ema', down)
    ck('no quote -> skip', OI.decide('AAA', days, None)['decision'] == 'skip_no_quote')
    ck('too little history -> skip', OI.decide('AAA', days[-30:], {'lp': last})['decision'] == 'skip_no_history')


def day_with(trades, d=date(2026, 10, 6)):
    day = {'date': d.isoformat(), 'trades': trades}
    OI.write_json(OI.day_file(d), day)
    return day


def enter_trade(sym, ltp):
    t = OI.new_trade(sym)
    t.update({'decision': 'enter', 'ltp': ltp})
    return t


def test_live_buys():
    print('\nlive buys: caps, funds floor, fills')
    fresh()
    use_clock(2026, 10, 6, 15, 20)
    fy = FakeFyers()
    fy.on_place = lambda f, o, d: f.fill(o['id'], 100.0 if d['side'] == 1 else 0)
    trades = [enter_trade('AAA', 300.0), enter_trade('BBB', 6000.0), enter_trade('CCC', 4000.0),
              enter_trade('DDD', 4500.0), enter_trade('EEE', 2000.0), enter_trade('GGG', 900.0), OI.new_trade('FFF')]
    trades[-1]['decision'] = 'skip_below_ema'
    day = day_with(trades)
    OI.live_buys(fy, day)
    st = {t['symbol']: t['live']['status'] for t in day['trades']}
    ck('cheap pick bought', st['AAA'] == 'buy_filled', st)
    ck('pick above the per-order cap skipped', st['BBB'] == 'skipped_order_cap', st)
    ck('picks bought while under the daily cap', st['CCC'] == st['DDD'] == 'buy_filled', st)
    ck('daily cap enforced (8,800 spent, 2,000 more would breach 10,000)', st['EEE'] == 'skipped_day_cap', st)
    ck('later cheaper pick still fits under the daily cap', st['GGG'] == 'buy_filled', st)
    ck('below-EMA pick never ordered', st['FFF'] == 'no_entry', st)
    ck('only CNC market buys of 1 share', all(p['productType'] == 'CNC' and p['type'] == 2 and p['qty'] == 1 and p['side'] == 1
                                               and not p['offlineOrder'] for p in fy.placed), fy.placed)
    fresh()
    fy = FakeFyers()
    fy.funds_avail = 41000.0
    day = day_with([enter_trade('AAA', 300.0), enter_trade('BBB', 900.0)])
    OI.live_buys(fy, day)
    ck('funds floor protects the FnO bot capital (41,000 - 300 - 900 < 40,000)',
       day['trades'][0]['live'].get('buy_order_id') and day['trades'][1]['live']['status'] == 'skipped_funds_floor',
       [t['live'] for t in day['trades']])
    fresh()
    fy = FakeFyers()
    fy.funds = lambda: {'s': 'error'}
    day = day_with([enter_trade('AAA', 300.0)])
    OI.live_buys(fy, day)
    ck('unknown funds -> no buys (fail closed)', day['trades'][0]['live']['status'] == 'skipped_funds_floor' and not fy.placed)


def test_entry_gating():
    print('\nentry gating')
    fresh()
    days = write_history()
    OI.write_json(OI.path('picks.json'), {'as_of': days[-1].isoformat(), 'picks': [{'symbol': 'AAA'}, {'symbol': 'BBB'}]})
    fy = FakeFyers()
    fy.on_place = lambda f, o, d: f.fill(o['id'], 101.0)
    for s in ('AAA', 'BBB'):
        last = OI.load_bhav(days[-1])[s]['close']
        fy.quote_map[OI.fy_symbol(s)] = {'lp': last * 1.01, 'prev_close_price': last}
    OI.fyers_client = lambda wait_until=None: fy
    OI.traded_today = lambda f: True
    args = types.SimpleNamespace(force=False, no_broker=False, no_live=False)
    use_clock(2026, 10, 7, 15, 20)
    OI.cmd_entry(args)
    ck('without LIVE_ENABLED nothing is ordered', not fy.placed)
    day = OI.read_json(OI.day_file(date(2026, 10, 7)))
    ck('paper decisions recorded', len(day['trades']) == 2 and day['live'] is False)
    os.remove(OI.day_file(date(2026, 10, 7)))
    open(OI.path('LIVE_ENABLED'), 'w').close()
    OI.cmd_entry(types.SimpleNamespace(force=True, no_broker=False, no_live=False))
    ck('--force never orders, even with LIVE_ENABLED', not fy.placed)
    os.remove(OI.day_file(date(2026, 10, 7)))
    use_clock(2026, 10, 7, 15, 40)
    ck('outside 15:14-15:28 the scheduled entry refuses', OI.cmd_entry(args) == 2 and not fy.placed)
    use_clock(2026, 10, 7, 15, 20)
    OI.cmd_entry(args)
    n = len(fy.placed)
    ck('in-window entry with LIVE_ENABLED orders the above-EMA picks', n == 2, fy.placed)
    OI.cmd_entry(args)
    OI.cmd_entry(types.SimpleNamespace(force=True, no_broker=False, no_live=False))
    ck('re-runs (scheduled or forced) never re-order or overwrite', len(fy.placed) == n and
       all(t['live'].get('buy_order_id') for t in OI.read_json(OI.day_file(date(2026, 10, 7)))['trades']))


def held(sym, amo=None):
    t = enter_trade(sym, 100.0)
    t['live'].update({'status': 'buy_filled', 'qty': 1, 'filled_qty': 1, 'buy_order_id': 'b-' + sym, 'buy_fill': 100.0})
    if amo:
        t['live'].update({'status': 'amo_placed', 'amo_order_id': amo})
    return t


def test_amo_and_morning():
    print('\nAMO placement and morning exits')
    fresh()
    use_clock(2026, 10, 6, 17, 0)
    fy = FakeFyers()
    OI.fyers_client = lambda wait_until=None: fy
    OI.traded_today = lambda f: True
    day_with([held('AAA'), held('BBB')])
    fy.reject = {'ovnamo'}
    OI.cmd_amo(types.SimpleNamespace(force=False))
    st = [t['live']['status'] for t in OI.read_json(OI.day_file(date(2026, 10, 6)))['trades']]
    ck('rejected AMOs are recorded, not retried blindly', st == ['amo_rejected', 'amo_rejected'], st)
    fresh()
    use_clock(2026, 10, 6, 17, 0)
    fy = FakeFyers()
    day_with([held('AAA'), held('BBB')])
    OI.cmd_amo(types.SimpleNamespace(force=False))
    placed = [(p['orderTag'], p['offlineOrder'], p['side'], p['productType']) for p in fy.placed]
    ck('one CNC AMO sell per position', placed == [('ovnamo', True, -1, 'CNC')] * 2, placed)

    # Scenario: AMO pending -> fills in the auction; AMO rejected -> pre-open sell fills;
    # stuck pending sell -> cancelled -> market sell; uncancellable -> flagged, never double-sold.
    fresh()
    use_clock(2026, 10, 7, 9, 2)
    fy = FakeFyers()
    OI.fyers_client = lambda wait_until=None: fy
    fy.orders = {'amoA': {'id': 'amoA', 'status': OI.PENDING, 'tradedPrice': 0, 'filledQty': 0, 'qty': 1},
                 'amoB': {'id': 'amoB', 'status': OI.REJECTED, 'tradedPrice': 0, 'filledQty': 0, 'qty': 1},
                 'amoC': {'id': 'amoC', 'status': OI.PENDING, 'tradedPrice': 0, 'filledQty': 0, 'qty': 1},
                 'amoD': {'id': 'amoD', 'status': OI.PENDING, 'tradedPrice': 0, 'filledQty': 0, 'qty': 1}}
    day_with([held('AAA', 'amoA'), held('BBB', 'amoB'), held('CCC', 'amoC'), held('DDD', 'amoD')])
    real_sleep_until = OI.sleep_until

    def at_open(target):
        real_sleep_until(target)
        fy.fill('amoA', 101.0)
        for o in fy.orders.values():
            if o.get('orderTag') == 'ovnpre':
                fy.fill(o['id'], 102.0)
        fy.cancel_ok = True

    OI.sleep_until = at_open
    original_cancel = fy.cancel_order

    def cancel(d):
        if d['id'] == 'amoD':
            return {'s': 'ok'}
        return original_cancel(d)

    fy.cancel_order = cancel
    fy.on_place = lambda f, o, d: f.fill(o['id'], 99.0) if d['orderTag'] == 'ovnmkt' else None
    rc = OI.cmd_morning(types.SimpleNamespace(force=False))
    OI.sleep_until = real_sleep_until
    res = {t['symbol']: t['live'] for t in OI.read_json(OI.day_file(date(2026, 10, 6)))['trades']}
    tags = [p['orderTag'] for p in fy.placed]
    ck('pending AMO left alone and recorded when it fills', res['AAA']['status'] == 'sold' and res['AAA']['sell_route'] == 'amo'
       and res['AAA']['sell_fill'] == 101.0, res['AAA'])
    ck('rejected AMO replaced by one pre-open sell', res['BBB']['sell_route'] == 'preopen' and tags.count('ovnpre') == 1, (res['BBB'], tags))
    ck('stuck pending sell cancelled then sold at market', res['CCC']['sell_route'] == 'market' and res['CCC']['sell_fill'] == 99.0, res['CCC'])
    ck('uncancellable order flagged, no second sell', res['DDD']['status'] == 'sell_unknown' and tags.count('ovnmkt') == 1, (res['DDD'], tags))
    ck('morning reports failure when any position is unresolved', rc == 1)
    use_clock(2026, 10, 7, 11, 0)
    ck('morning refuses outside 08:55-09:30', OI.cmd_morning(types.SimpleNamespace(force=False)) == 2)


def test_settle_and_report():
    print('\nsettlement and report')
    fresh()
    d0, d1 = date(2026, 10, 5), date(2026, 10, 6)
    OI.save_bhav(d0, {'AAA': {'prev': 99, 'open': 99.5, 'close': 100.0, 'last': 100.2, 'turn_lacs': 9000},
                      'BBB': {'prev': 50, 'open': 50, 'close': 50.0, 'last': 50.0, 'turn_lacs': 9000}})
    OI.save_bhav(d1, {'AAA': {'prev': 100, 'open': 101.0, 'close': 100.5, 'last': 100.5, 'turn_lacs': 9000},
                      'BBB': {'prev': 50, 'open': 49.5, 'close': 49.0, 'last': 49.0, 'turn_lacs': 9000}})
    a, b, s = enter_trade('AAA', 100.0), enter_trade('BBB', 50.0), OI.new_trade('SKP')
    s['decision'] = 'skip_below_ema'
    a['live'].update({'status': 'sold', 'buy_fill': 100.1, 'sell_fill': 101.0, 'sell_route': 'amo', 'sell_date': d1.isoformat()})
    day_with([a, b, s], d0)
    ck('one day file settled', OI.settle(d1) == 1)
    tr = {t['symbol']: t for t in OI.read_json(OI.day_file(d0))['trades']}
    ck('paper entry = official close, exit = next official open', tr['AAA']['paper']['entry'] == 100.0 and tr['AAA']['paper']['exit'] == 101.0)
    ck('gross/net bps exact', tr['AAA']['paper']['gross_bps'] == 100.0 and tr['AAA']['paper']['net_bps'] == 75.0, tr['AAA']['paper'])
    ck('loser booked negative', tr['BBB']['paper']['gross_bps'] == -100.0 and tr['BBB']['paper']['net_bps'] == -125.0)
    ck('skips are never settled', tr['SKP']['paper']['entry'] is None)
    ck('buy slippage vs official close', tr['AAA']['live']['slip_buy_bps'] == 10.0, tr['AAA']['live'])
    ck('sell at the official open = 0 bps slippage', tr['AAA']['live']['slip_sell_bps'] == 0.0)
    ck('settle is idempotent', OI.settle(d1) == 0)
    sm = OI.summarize()
    ck('report: 2 paper trades, mean net -25 bps', sm['paper_trades'] == 2 and sm['paper_net_bps'] == -25.0, sm)
    ck('report: 0.0 bps sell counted as an auction fill', sm['live_sold_at_official_open'] == 1, sm)
    ck('report: execution verdict uses buy+sell slippage', sm['execution_verdict'].startswith('TOO COSTLY (+10.0'), sm)
    ck('report: capital return uses 10 equal slots ((+75 - 125) bps / 10)', abs(sm['paper_return_on_capital_pct'] - (-0.05)) < 1e-9, sm)


if __name__ == '__main__':
    test_parse_and_fetch()
    test_rank_and_ema()
    test_live_buys()
    test_entry_gating()
    test_amo_and_morning()
    test_settle_and_report()
    print(f'\n{P} passed, {F} failed')
    sys.exit(1 if F else 0)
