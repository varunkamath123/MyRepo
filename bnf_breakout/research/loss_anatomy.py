# -*- coding: utf-8 -*-
"""Anatomy of the losing LIVE paper trades, against the winners.

Run:  python loss_anatomy.py <live_logs_dir> <bars_dir>

  <live_logs_dir>  bnf_trades_<date>.jsonl files (the real trades, Oct 22 onward)
  <bars_dir>       banknifty_5min_<date>.csv for those days (75 bars each)

For every trade it measures, from the index bars (not the option model):
  aligned_day     did the day's open-to-close move go the trade's way?
  held_right      would price have finished the session on the trade's side of the entry (hold to 15:00)?
  broke_level     did a bar close beyond the level within 6 bars of entry (a CONFIRMED break, the thesis)?
  adverse_first   worst adverse move vs best favourable move recorded while held (index %)
  level / coil    the gate readings at entry (distance to the pressed level, coil ratio)
  dte             days to expiry at entry
and then prints the losers as a table, the losers grouped by how they lost, and every feature split
losers vs winners. Samples are small: this describes, it does not prove.
"""
import glob
import json
import os
import re
import sys
import datetime as dt

import numpy as np
import pandas as pd


def load_trades(logs):
    T = []
    for f in sorted(glob.glob(os.path.join(logs, 'bnf_trades_*.jsonl'))):
        for ln in open(f, encoding='utf-8', errors='ignore'):
            try:
                t = json.loads(ln)
            except Exception:
                continue
            if 'pnl_net' in t and t.get('thesis'):
                T.append(t)
    return T


def load_day(bars, day):
    p = os.path.join(bars, f'banknifty_5min_{day.replace("-", "")}.csv')
    x = pd.read_csv(p)
    return x if len(x) == 75 else None


def feats(t, bars):
    day = t['entry_time'][:10]
    x = load_day(bars, day)
    th = t['thesis']
    d = th['direction']
    i0 = th['entry_bar']
    e = th['entry_px']
    lv = th['level_value']
    C = x['Close'].values
    H = x['High'].values
    L = x['Low'].values
    day_move = (C[-1] / x['Open'].iloc[0] - 1) * 100
    end = 68                                    # the bar that closes at 15:00
    held_right = (C[end] - e) * d > 0
    broke = any(((C[j] - lv) * d > 0) for j in range(i0 + 1, min(i0 + 7, 75)))
    # first-30-min path: did price go with or against the trade
    r30 = (C[min(i0 + 6, 74)] / e - 1) * 100 * d
    lvl = re.search(r'([A-Z]+)@([0-9.]+)%', t['entry_gates']['level'][1])
    coil = re.search(r'([0-9.]+)<=', t['entry_gates']['coil'][1])
    exp = dt.date.fromisoformat(t['expiry'])
    et = t['exit_time']
    mins = int(et[11:13]) * 60 + int(et[14:16]) - (9 * 60 + 15)
    xi = max(0, min(74, mins // 5 - 1))                  # the bar that had just closed when the exit was sent
    post_exit = (C[end] - C[xi]) * d / C[xi] * 100 if xi < end else 0.0
    return dict(
        post_exit=post_exit,
        day=day, time=t['entry_time'][11:16], side=t['opt'], level=th['level'], net=t['pnl_net'], kind=t['kind'],
        held=t['held_min'], peak=t['max_pnl_pct'], trough=t['min_pnl_pct'], best_fav=t['index_best_fav'],
        worst_adv=t['index_worst_adv'], aligned_day=((day_move * d) > 0), day_move=day_move,
        held_right=bool(held_right), broke_level=bool(broke), r30=r30,
        dist=float(lvl.group(2)) if lvl else np.nan, coil=float(coil.group(1)) if coil else np.nan,
        dte=(exp - dt.date.fromisoformat(day)).days, second=(not t['entry_gates']['capacity'][1].startswith('trades=0')),
        oi_wb=bool(th.get('oi_would_block')), premium=t['entry_premium'], cost=t['costs'])


def main(logs, bars):
    rows = [feats(t, bars) for t in load_trades(logs)]
    D = pd.DataFrame(rows)
    D['loser'] = D.net <= 0
    L, W = D[D.loser], D[~D.loser]
    pd.set_option('display.width', 220)
    print(f'{len(D)} live trades: {len(W)} winners (net Rs{W.net.sum():+,.0f}), {len(L)} losers (net Rs{L.net.sum():+,.0f})')
    cols = ['day', 'time', 'side', 'level', 'kind', 'net', 'held', 'peak', 'trough', 'best_fav', 'worst_adv', 'aligned_day', 'held_right', 'broke_level', 'coil', 'dist', 'dte']
    print('\nTHE LOSERS:')
    print(L[cols].round(2).to_string(index=False))

    print('\nHOW THEY LOST')
    big = L[L.net <= -1000]
    small = L[L.net > -1000]
    print(f'  small ({len(small)} trades, net Rs{small.net.sum():+,.0f}): mostly costs; exits {dict(small.kind.value_counts())}; '
          f'average held {small.held.mean():.0f} min, average peak premium {small.peak.mean():+.1f}%, average worst {small.trough.mean():+.1f}%')
    print(f'  big   ({len(big)} trades, net Rs{big.net.sum():+,.0f}): exits {dict(big.kind.value_counts())}; '
          f'average held {big.held.mean():.0f} min, average peak premium {big.peak.mean():+.1f}%, average worst {big.trough.mean():+.1f}%, '
          f'average index adverse move {big.worst_adv.mean():.2f}% vs best favourable {big.best_fav.mean():.2f}%')
    print(f'  the big losers are {big.net.sum() / L.net.sum() * 100:.0f}% of all loss money; none of them ever had a peak above {big.peak.max():+.1f}% premium')

    print('\nLOSERS vs WINNERS')
    def pct(s):
        return f'{s.mean() * 100:.0f}%'
    rows2 = [
        ('side was right for the whole day (aligned with open->close)', pct(L.aligned_day), pct(W.aligned_day)),
        ('price finished the session on the trade side of the entry', pct(L.held_right), pct(W.held_right)),
        ('a bar CLOSED beyond the pressed level within 6 bars (the break confirmed)', pct(L.broke_level), pct(W.broke_level)),
        ('index move in the first 30 min after entry, in the trade direction (mean %)', f'{L.r30.mean():+.2f}', f'{W.r30.mean():+.2f}'),
        ('coil ratio at entry (mean; lower = tighter)', f'{L.coil.mean():.2f}', f'{W.coil.mean():.2f}'),
        ('distance to the pressed level at entry (mean %)', f'{L.dist.mean():.3f}', f'{W.dist.mean():.3f}'),
        ('days to expiry at entry (mean)', f'{L.dte.mean():.0f}', f'{W.dte.mean():.0f}'),
        ('second trade of the day', pct(L.second), pct(W.second)),
        ('entered after 13:00', pct(L.time >= '13:00'), pct(W.time >= '13:00')),
        ('pressed a HIGH level (ORH / DAYH / PDH), i.e. a CALL toward resistance', pct(L.level.isin(['ORH', 'DAYH', 'PDH'])), pct(W.level.isin(['ORH', 'DAYH', 'PDH']))),
        ('OI shadow gate would have blocked it', pct(L.oi_wb), pct(W.oi_wb)),
        ('average premium paid (Rs)', f'{L.premium.mean():.0f}', f'{W.premium.mean():.0f}'),
    ]
    print(f'  {"feature":<82}{"losers":>9}{"winners":>9}')
    for a, b, c in rows2:
        print(f'  {a:<82}{b:>9}{c:>9}')
    print('\nAFTER THE EXIT: index move from the exit bar to the 15:00 close, in the trade direction (%)')
    for lab, g in (('big losers', big), ('small losers', small), ('winners', W)):
        vals = ' '.join(f'{v:+.2f}' for v in g.post_exit)
        print(f'  {lab:<13} mean {g.post_exit.mean():+.2f}%   price went the trade way afterwards in {(g.post_exit > 0.1).sum()} of {len(g)}   ({vals})')
    sk = big[(big.post_exit > 0.1) | big.held_right]
    print(f"  big losers where the market later went the trade's way (stopped out, then right): {len(sk)} of {len(big)} "
          f'= Rs{sk.net.sum():+,.0f} of the Rs{big.net.sum():+,.0f}')
    print('\nBIG-LOSER SETUPS: ' + '; '.join(
        f'{r.day[5:]} {r.time} {r.side} {r.level} dist {r.dist:.2f}% coil {r.coil:.2f} dte {r.dte} second={r.second}' for r in big.itertuples()))
    return D


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
