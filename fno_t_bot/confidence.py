# -*- coding: utf-8 -*-
"""Confidence score — how much should we trust that our P&L is an EDGE?

Published on every position close, so the number moves with the book instead of
being recomputed by hand whenever someone remembers to ask.

P&L is the only success metric worth chasing, but a P&L figure alone cannot
separate skill from one lucky Tuesday. This book's headline was once +Rs1,911
over 30 trades with 543% of the profit from a single session -- a number that
would get capital deployed far too early if quoted without context. The score
is what turns P&L into evidence.

SIX COMPONENTS (100 total), and why each earns its place:

  significance     25  is mean per-trade P&L distinguishable from zero? The
                       single most important question and the most often skipped
  sample power     20  n vs the n needed for 80% power at the OBSERVED effect.
                       Low here means "cannot yet know", not "does not work"
  data integrity   15  share of trades priced at real traded premiums. Earned
                       its place: the Sep 24-28 symbol bug put Black-Scholes
                       fiction into the record and changed three conclusions
  concentration    15  what survives removing the best day and the best trade
  consistency      15  green-day ratio + both chronological halves positive
  multiple testing 10  discount for ~3,072 variants swept. Only out-of-sample
                       survival earns it back -- never more searching

BANDS
   0-20  no evidence of edge — paper only
  20-40  suggestive, badly underpowered
  40-60  real signal, not yet trustworthy for capital
  60-80  deployable at small size
  80+    deployable
"""
from __future__ import annotations

import glob
import json
import math
import os
import statistics as st
from collections import defaultdict

import config

REAL_PREMIUM_FROM = '2026-08-18'     # the day paper switched to real LTPs
VARIANTS_TRIED    = 3072             # the documented sweep count
MAX = {'significance': 25, 'sample_power': 20, 'data_integrity': 15,
       'concentration': 15, 'consistency': 15, 'multiple_testing': 10}
BANDS = [(80, 'DEPLOYABLE'), (60, 'deployable at small size'),
         (40, 'real signal, not yet trustworthy for capital'),
         (20, 'suggestive, badly underpowered'),
         (0,  'no evidence of edge — paper only')]


def _log_dir() -> str:
    """Resolve the log directory regardless of the caller's cwd.

    config.LOG_DIRECTORY is the relative string "logs", which is correct only
    when the process runs from live_bot -- true for the bot, false for any
    analysis script, which then silently scores an empty book as 0/100. Anchor
    to this module's own directory instead.
    """
    d = getattr(config, 'LOG_DIRECTORY', 'logs')
    if os.path.isabs(d) and os.path.isdir(d):
        return d
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), d)
    if os.path.isdir(here):
        return here
    return d if os.path.isdir(d) else here


def load_trades(log_dir: str | None = None) -> list:
    out = []
    for f in sorted(glob.glob(os.path.join(log_dir or _log_dir(),
                                           'FnO_T_Bot_*_trades_*.jsonl'))):
        if '.bak' in f or 'challenger' in f or 'EARLY' in f:
            continue
        day = os.path.basename(f).replace('.jsonl', '').split('_')[-1]
        for line in open(f, encoding='utf-8', errors='ignore'):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('pnl_net') is None or day < REAL_PREMIUM_FROM:
                continue
            d['_day'] = day
            # Sep 24-28 weekly-symbol bug: NIFTY/SENSEX weeklies were BS-priced.
            # BANKNIFTY is monthly-only and took the unaffected code path.
            d['_bs'] = ('2026-09-24' <= day <= '2026-09-28'
                        and d['instrument'] in ('NIFTY', 'SENSEX'))
            out.append(d)
    out.sort(key=lambda z: (z['_day'], z['entry_time']))
    return out


def score(trades: list):
    """Return (total, parts, notes). Only real-priced trades count."""
    rel = [t for t in trades if not t.get('_bs')]
    p = [t['pnl_net'] for t in rel]
    parts, notes = {}, {}
    if len(p) < 3:
        return 0, {k: 0 for k in MAX}, {'n': f'only {len(p)} reliable trades'}

    mu = st.mean(p)
    sd = st.pstdev(p) or 1e-9

    try:
        from scipy import stats as sps
        t_, pv = sps.ttest_1samp(p, 0)
    except Exception:
        t_ = mu / (sd / math.sqrt(len(p)))
        pv = 1.0
    if   pv < 0.01 and mu > 0: parts['significance'] = 25
    elif pv < 0.05 and mu > 0: parts['significance'] = 20
    elif pv < 0.10 and mu > 0: parts['significance'] = 12
    elif pv < 0.25 and mu > 0: parts['significance'] = 5
    else:                      parts['significance'] = 0
    notes['significance'] = f"mean Rs{mu:+,.0f}/trade  t={t_:+.2f}  p={pv:.3f}"

    need = ((1.96 + 0.84) ** 2) * (sd ** 2) / (mu ** 2) if mu else 9e9
    parts['sample_power'] = int(max(0, min(20, round(20 * min(len(p) / need, 1.0)))))
    notes['sample_power'] = f"n={len(p)}  need ~{need:,.0f} for 80% power"

    share = len(rel) / len(trades) if trades else 0
    parts['data_integrity'] = int(round(15 * share))
    notes['data_integrity'] = f"{len(rel)}/{len(trades)} real-priced ({share*100:.0f}%)"

    byday = defaultdict(float)
    for t in rel:
        byday[t['_day']] += t['pnl_net']
    tot = sum(p)
    if tot <= 0:
        parts['concentration'] = 0
        notes['concentration'] = f"book not profitable (Rs{tot:+,.0f})"
    else:
        keep = min(tot - max(byday.values()), tot - max(p)) / tot
        parts['concentration'] = int(max(0, min(15, round(15 * keep))))
        notes['concentration'] = (f"Rs{tot:+,.0f}; without best day "
                                  f"Rs{tot-max(byday.values()):+,.0f}")

    green = sum(1 for v in byday.values() if v > 0)
    gr = green / len(byday) if byday else 0
    h = len(p) // 2
    halves_ok = h >= 3 and sum(p[:h]) > 0 and sum(p[h:]) > 0
    parts['consistency'] = int(round(10 * gr)) + (5 if halves_ok else 0)
    notes['consistency'] = (f"{green}/{len(byday)} green days; halves "
                            f"Rs{sum(p[:h]):+,.0f}/Rs{sum(p[h:]):+,.0f}")

    oos = 1.0 if (halves_ok and pv < 0.05) else (0.3 if halves_ok else 0.0)
    disc = max(0.0, 1.0 - math.log10(max(VARIANTS_TRIED, 1)) / 4.0)
    parts['multiple_testing'] = int(round(10 * disc * oos))
    notes['multiple_testing'] = f"{VARIANTS_TRIED:,} variants swept; OOS credit {oos:.1f}"

    return sum(parts.values()), parts, notes


def band(total: int) -> str:
    return next(b for t, b in BANDS if total >= t)


def publish(bot=None, trigger: str = 'position close') -> int | None:
    """Recompute, log one line, and append to the history file.

    Called on every close. Swallows everything -- a measurement must never
    interfere with trading.
    """
    try:
        trades = load_trades()
        total, parts, notes = score(trades)
        rel = [t for t in trades if not t.get('_bs')]
        net = sum(t['pnl_net'] for t in rel)
        line = (f"[CONFIDENCE] {total}/100 — {band(total)} | "
                f"book Rs{net:+,.0f} over {len(rel)} real-priced trades | "
                f"sig {parts['significance']}/25 "
                f"power {parts['sample_power']}/20 "
                f"data {parts['data_integrity']}/15 "
                f"conc {parts['concentration']}/15 "
                f"cons {parts['consistency']}/15 "
                f"mt {parts['multiple_testing']}/10 | {notes.get('significance','')}")
        if bot is not None:
            bot.logger.info("  " + line)
        try:
            os.makedirs(_log_dir(), exist_ok=True)
            from datetime import datetime
            with open(os.path.join(_log_dir(), 'confidence_history.jsonl'),
                      'a', encoding='utf-8') as fh:
                fh.write(json.dumps(dict(
                    ts=datetime.now().isoformat(), trigger=trigger,
                    total=total, band=band(total), net=round(net, 2),
                    n_reliable=len(rel), n_all=len(trades),
                    **{f'c_{k}': v for k, v in parts.items()})) + '\n')
        except Exception:
            pass
        return total
    except Exception as exc:
        try:
            if bot is not None:
                bot.logger.debug(f"  [CONFIDENCE] publish failed: {exc}")
        except Exception:
            pass
        return None
