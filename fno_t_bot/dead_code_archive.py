# -*- coding: utf-8 -*-
"""
Dead Code Archive — FnO_T_Bot
==============================
Catalog of code that has been removed from active files but preserved
here for reference.  Nothing in this file is imported or executed.

Each section records:
  - REMOVED FROM : which file / class
  - REMOVED DATE : approximate date
  - REASON       : why it was removed
  - STATUS       : Candidate for early_bot.py | Superseded | Rejected
"""


# ══════════════════════════════════════════════════════════════════════════════
# [1] vRide — Ride Pre-Window Trend
# ══════════════════════════════════════════════════════════════════════════════
# REMOVED FROM : paper_bot.py — TradingBot class (lines 527-668)
# REMOVED DATE : March 2026
# REASON       : vRide takes pre-window EMA crossovers into the entry window,
#                entering at the tail of an already-established trend.  These
#                trades are NOT reflected in the vX backtest results (which
#                only counts fresh in-window crosses), making live performance
#                unpredictable vs the backtested benchmark.
#                Additionally, the March 17 2026 live paper trade (NIFTY PUT,
#                -₹3,809) demonstrated exactly this failure mode: a pre-window
#                5m BEAR cross in a 15m BULL environment = counter-trend entry.
#                Early session / pre-window trend riding is being redesigned as
#                a separate ORB (Opening Range Breakout) strategy in early_bot.py.
# STATUS       : Candidate for early_bot.py with ORB redesign.
#                Do NOT re-enable in paper_bot.py without backtesting.
# ──────────────────────────────────────────────────────────────────────────────

class _DEAD_vRide_ARCHIVE:
    """Namespace wrapper — not instantiated anywhere."""

    _VRIDE_ADX_SUSTAIN = 3   # consecutive bars ADX must hold at entry window

    def get_vride_signal(self, df):
        """
        'Ride the Trend' — fires ONCE at entry window open.

        Looks for an EMA crossover that occurred BEFORE the entry window and
        is still valid when the window opens (no reversal, ADX sustained,
        VWAP aligned).  vX handles in-window fresh crossovers; vRide handles
        pre-window established trends.

        Conditions:
          1. A clear EMA crossover existed before entry_start today.
          2. EMA is STILL in the same direction at window open (no reversal).
          3. No counter-crossover between the original cross and window open.
          4. ADX >= per-type threshold for the last _VRIDE_ADX_SUSTAIN bars.
          5. Price is on the correct side of VWAP at window open.

        Returns a signal dict with path='vRide', or None.
        """
        import pandas as pd
        from datetime import datetime
        import pytz
        IST = pytz.timezone('Asia/Kolkata')

        # ── Isolate today's bars ──────────────────────────────────────────────
        today      = datetime.now(IST).date()
        today_mask = df.index.date == today
        tdf        = df[today_mask]
        if len(tdf) < 2:
            return None

        # ── Find the window-open bar (first bar at/after entry_start) ─────────
        win_i = None
        for i, ts in enumerate(tdf.index):
            if ts.time() >= self.entry_start:
                win_i = i
                break
        if win_i is None or win_i < 1:
            return None   # need at least one pre-window bar

        win_row  = tdf.iloc[win_i]
        ef_win   = win_row.get('EMA_fast', float('nan'))
        es_win   = win_row.get('EMA_slow', float('nan'))
        adx_win  = win_row.get('ADX',      float('nan'))
        px_win   = win_row.get('Close',    float('nan'))
        vwap_win = win_row.get('VWAP',     float('nan'))
        if any(pd.isna(v) for v in [ef_win, es_win, adx_win, px_win]):
            return None

        # ── Current EMA direction at window open ──────────────────────────────
        if ef_win > es_win:
            ema_dir = 'CALL'
        elif ef_win < es_win:
            ema_dir = 'PUT'
        else:
            return None   # exactly equal — no clear direction

        # ── Scan pre-window bars backward for most recent EMA crossover ───────
        pre        = tdf.iloc[:win_i]
        cross_i    = None
        cross_type = None
        for i in range(len(pre) - 1, 0, -1):
            ef_j  = pre.iloc[i]['EMA_fast']
            es_j  = pre.iloc[i]['EMA_slow']
            ef_j1 = pre.iloc[i - 1]['EMA_fast']
            es_j1 = pre.iloc[i - 1]['EMA_slow']
            if any(pd.isna(v) for v in [ef_j, es_j, ef_j1, es_j1]):
                continue
            if ef_j1 <= es_j1 and ef_j > es_j:       # bullish cross
                cross_i = i; cross_type = 'CALL'; break
            if ef_j1 >= es_j1 and ef_j < es_j:       # bearish cross
                cross_i = i; cross_type = 'PUT';  break

        if cross_i is None:
            return None   # no pre-window crossover found
        if cross_type != ema_dir:
            return None   # crossover reversed since original cross

        # ── No counter-crossover between original cross and window open ────────
        for i in range(cross_i + 1, len(pre)):
            ef_j  = pre.iloc[i]['EMA_fast']
            es_j  = pre.iloc[i]['EMA_slow']
            ef_j1 = pre.iloc[i - 1]['EMA_fast']
            es_j1 = pre.iloc[i - 1]['EMA_slow']
            if any(pd.isna(v) for v in [ef_j, es_j, ef_j1, es_j1]):
                continue
            if cross_type == 'CALL' and ef_j1 >= es_j1 and ef_j < es_j:
                return None   # counter bearish cross — trend invalidated
            if cross_type == 'PUT'  and ef_j1 <= es_j1 and ef_j > es_j:
                return None   # counter bullish cross — trend invalidated

        # ── ADX sustained for last _VRIDE_ADX_SUSTAIN bars ────────────────────
        adx_thr = self.call_adx_min if cross_type == 'CALL' else self.put_adx_min
        n       = self._VRIDE_ADX_SUSTAIN
        check   = tdf.iloc[max(0, win_i - n + 1): win_i + 1]
        if len(check) < n:
            return None
        if not all(r['ADX'] >= adx_thr for _, r in check.iterrows()):
            return None

        # ── VWAP directional filter ───────────────────────────────────────────
        if not pd.isna(vwap_win) and vwap_win > 0:
            if cross_type == 'CALL' and px_win <= vwap_win:
                return None
            if cross_type == 'PUT'  and px_win >= vwap_win:
                return None

        cross_time = pre.index[cross_i].strftime('%H:%M')

        # ── Signal strength → lot sizing ──────────────────────────────────────
        # Score 0-3: ADX>=35 (+1), EMA spread>=0.04% (+1), VWAP dist>=0.10% (+1)
        # strength>=2: all three market-structure filters agree → enter 2 lots.
        # Backtest (14 months): 2-lot trades show 80% WR vs 65% for 1-lot trades,
        # contributing 60% of total P&L from only 16% of entries. The strength
        # score correctly identifies the highest-conviction setups.
        strength = self._vride_strength(float(px_win), float(ef_win),
                                        float(es_win), float(adx_win), float(vwap_win))
        lots = 2 if strength >= 2 else 1

        return {
            'type'      : cross_type,
            'price'     : float(px_win),
            'adx'       : float(adx_win),
            'path'      : 'vRide',
            'cross_time': cross_time,
            'strength'  : strength,
            'lots'      : lots,
        }

    def _vride_strength(self, px: float, ef: float, es: float,
                        adx: float, vwap: float) -> int:
        """
        Signal strength score for vRide lot sizing (0–3).
          +1  ADX >= 35             (strong trend momentum)
          +1  |EMA spread| >= 0.04% (wide EMA separation)
          +1  |price-VWAP| >= 0.10% (strong VWAP divergence)
        Score >= 2 → 2 lots.  Backtest: +Rs.80,579 vs fixed 1-lot over 193 trades.
        """
        import pandas as pd
        score = 0
        if adx >= 35:                                   score += 1
        if abs(ef - es) / px >= 0.0004:                 score += 1
        if not pd.isna(vwap) and vwap > 0 and abs(px - vwap) / px >= 0.001:
            score += 1
        return score
