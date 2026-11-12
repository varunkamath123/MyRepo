"""Cost and break-even calculator for NIFTY / BANKNIFTY index futures at 10-minute horizons.

  python costs.py                 uses the cleaned dataset for bar volatility if it exists, otherwise a stated fallback
  python costs.py --slip 0.5      override per-side slippage in index points (replace with the spread measured by depth_recorder.py)

Statutory rates and the broker's brokerage are DEFAULTS to verify against a recent contract note; they sit in one dataclass so a change is
one line.  Slippage is the least known input: a market order pays about half the quoted spread per side plus any impact. The first full day of depth
recordings gave a median spread of 4.5 points (NIFTY future) and 15.0 points (BANKNIFTY future), about 2.3x and 2.5x the placeholders first used.

Break-even logic.  A signal s with unit variance and correlation IC with the forward return r (sd sigma_h over the holding period) earns
E[sign(s) * r] = IC * sigma_h * m  when trading every signal (m = E|s| = 0.798 for a Gaussian s), or IC * sigma_h * m_k when trading only
|s| > k (m_k = pdf(k) / (1 - cdf(k)), the mean of |s| beyond the cutoff).  Break-even needs that to equal the round-trip cost.
sigma_h = sigma_bar * sqrt(bars held) assumes independent bars (a simplification; intraday returns are fat-tailed)."""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


@dataclass
class CostModel:
    brokerage_per_order: float = 20.0       # Rs, flat (Upstox: Rs20 or 0.05%, whichever is lower)
    stt_sell_pct: float = 0.02              # % of sell turnover, index futures (rate in force since Oct 2024; verify)
    exch_txn_pct: float = 0.00173           # % of turnover, each side, NSE futures (verify)
    sebi_pct: float = 0.0001                # % of turnover each side (Rs10 per crore)
    stamp_buy_pct: float = 0.002            # % of buy turnover
    gst_pct: float = 18.0                   # on brokerage + exchange + SEBI fees
    # per side, index points: HALF the median top-of-book spread of the near-month future measured by depth_recorder.py on Nov 12 2026
    # (NIFTY 4.5 pts, BANKNIFTY 15.0 pts; one session, 1,495 snapshots). A market order pays about half the spread per side. Re-measure as days accumulate.
    slip_pts: dict = field(default_factory=lambda: {"NIFTY": 2.25, "BANKNIFTY": 7.5})


def round_trip(m: CostModel, underlying: str, price: float, lot: int, lots: int = 1, slip_pts: float | None = None) -> dict:
    notional = price * lot * lots
    brokerage = 2 * m.brokerage_per_order
    stt = notional * m.stt_sell_pct / 100
    exch = 2 * notional * m.exch_txn_pct / 100
    sebi = 2 * notional * m.sebi_pct / 100
    stamp = notional * m.stamp_buy_pct / 100
    gst = (brokerage + exch + sebi) * m.gst_pct / 100
    fees = brokerage + stt + exch + sebi + stamp + gst
    slip = 2 * (m.slip_pts[underlying] if slip_pts is None else slip_pts) * lot * lots
    total = fees + slip
    return {"notional": notional, "brokerage": brokerage, "stt": stt, "exch": exch, "sebi": sebi, "stamp": stamp, "gst": gst, "fees": fees,
            "slip": slip, "total": total, "fees_pct": fees / notional * 100, "total_pct": total / notional * 100, "total_pts": total / (lot * lots)}


def _pdf(k: float) -> float:
    return math.exp(-k * k / 2) / math.sqrt(2 * math.pi)


def _cdf(k: float) -> float:
    return 0.5 * (1 + math.erf(k / math.sqrt(2)))


def m_cut(k: float) -> float:
    """Mean of |s| for a standard normal s, trading only when |s| > k (k=0: every signal, 0.798)."""
    return math.sqrt(2 / math.pi) if k <= 0 else _pdf(k) / (1 - _cdf(k))


def required_ic(cost_pct: float, sigma_h_pct: float, k: float = 0.0) -> float:
    return cost_pct / (sigma_h_pct * m_cut(k))


def breakeven_win_rate(cost_pct: float, win_size_pct: float) -> float:
    """Symmetric payoff: win +x, lose -x, cost c per trade -> break-even win rate 0.5 + c / (2x)."""
    return 0.5 + cost_pct / (2 * win_size_pct)


def bar_stats(underlying: str):
    """(sigma of a 10-min bar in %, per-slot sigma {slot: %}, last close, source)"""
    fallback = {"NIFTY": (0.096, 25000.0), "BANKNIFTY": (0.111, 60000.0)}[underlying]
    try:
        import common as C
        d = C.load_clean(underlying)
        r = d["ret_clean_bp"].dropna() / 100
        per = (d.groupby("slot")["ret_clean_bp"].std() / 100).to_dict()
        return float(r.std()), per, float(d["c"].iloc[-1]), f"clean dataset, {len(d):,} usable bars, {d['ts'].iloc[0].date()} -> {d['ts'].iloc[-1].date()}"
    except Exception as e:
        return fallback[0], {}, fallback[1], f"fallback (clean dataset not available: {type(e).__name__})"


def report(slip: float | None = None):
    m = CostModel()
    lots = {"NIFTY": 65, "BANKNIFTY": 30}
    try:
        import common as C
        for cs in C.ranked_contracts().values():
            lots[cs[0]["underlying"]] = cs[0]["lot"]
    except Exception:
        pass
    for u in ("NIFTY", "BANKNIFTY"):
        sig, per, px, src = bar_stats(u)
        c = round_trip(m, u, px, lots[u], slip_pts=slip)
        print(f"\n{'=' * 96}\n{u}  index {px:,.0f}, lot {lots[u]}, notional Rs{c['notional']:,.0f}  |  bar volatility from: {src}\n{'=' * 96}")
        print(f"  round trip, 1 lot:  brokerage {c['brokerage']:.0f} | STT {c['stt']:.0f} | exchange {c['exch']:.0f} | SEBI {c['sebi']:.1f} | stamp {c['stamp']:.0f} | GST {c['gst']:.0f}"
              f"  = fees Rs{c['fees']:.0f} ({c['fees_pct']:.4f}%)")
        print(f"                      slippage {2 * (m.slip_pts[u] if slip is None else slip):.1f} pts = Rs{c['slip']:.0f}   ->   ALL-IN Rs{c['total']:.0f} = {c['total_pct']:.4f}% = {c['total_pts']:.1f} index points")
        print(f"  10-min bar sigma {sig:.3f}%  ->  all-in cost = {c['total_pct'] / sig:.2f} sigma of one bar")
        print("\n  holding period   sigma_h   cost/sigma_h | required IC if trading: every signal | top 16% (k=1.0) | top 5% (k=1.645) | top 1% (k=2.33)")
        for h, name in ((1, "10 min"), (3, "30 min"), (6, "1 hour"), (12, "2 hours"), (37, "full day")):
            sh = sig * math.sqrt(h)
            print(f"  {name:<14}{sh:>8.3f}%{c['total_pct'] / sh:>12.2f}   | {required_ic(c['total_pct'], sh):>26.3f} | {required_ic(c['total_pct'], sh, 1.0):>16.3f} | {required_ic(c['total_pct'], sh, 1.645):>17.3f} | {required_ic(c['total_pct'], sh, 2.33):>14.3f}")
        print(f"\n  symmetric payoff: win rate needed to break even if the average win is  1x / 2x / 3x / 5x the cost:  "
              + "  ".join(f"{breakeven_win_rate(c['total_pct'], c['total_pct'] * x) * 100:.0f}%" for x in (1, 2, 3, 5)))
        if per:
            print("\n  cost as a multiple of one bar's sigma, by time of day (the open is cheap to trade, midday is expensive):")
            line = []
            for s in (0, 1, 2, 4, 8, 14, 20, 26, 32, 36, 37):
                if s in per:
                    t = 555 + s * 10
                    line.append(f"{t // 60:02d}:{t % 60:02d} {c['total_pct'] / per[s]:.2f}")
            print("   " + "  ".join(line))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--slip", type=float, default=None, help="slippage per side, index points")
    report(ap.parse_args().slip)
