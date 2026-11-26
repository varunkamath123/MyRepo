"""Intraday lab: would an intraday futures strategy find a (slight) edge, net of REAL costs?  NIFTY and BANKNIFTY, cleaned 10-minute index bars 2022-01 -> 2026-11 (normal sessions with every slot 0-35 present).
Index = futures proxy; every trade is flat before 15:15 (the index feed freezes in the closing auction since Aug 2026 and an intraday book should not carry overnight).
Cost per round trip: NIFTY 0.0472%, BANKNIFTY 0.0541% (fees + spread measured by depth_recorder); stress at 1.5x.  Rules fixed in advance (6 variants x 2 indices = 12 looks; a result needs |t| > ~2.9 to survive that, and both halves):
  ORB    opening-range breakout: range of the first 30 min (slots 0-2); first close beyond it from 09:45 -> trade that way; exit when the close returns beyond the OPPOSITE end of the range, else 15:15
  MOM    first-30-minute momentum into the close: if |return 09:15 -> 09:45| >= 0.5% trade its sign from 14:45 to 15:15
  FADE   midday large-move fade: a 10-min return of >= 3 trailing-250-session sigmas for that slot (slots 6-30) -> trade AGAINST it for 30 minutes (one position at a time)
  CONT   the same trigger, trade WITH it for 30 minutes
  GAPF   confirmed gap fade: gap >= 30 bp and the first bar closes against it -> trade against the gap from 09:25 to 15:15
  GAPC   confirmed gap continuation: gap >= 30 bp and the first bar closes with it -> trade with the gap from 09:25 to 15:15
Halves: sessions before 2024-07-01 vs from it.  'corr' = correlation of the rule's daily P&L with the index's own close-to-close return (a flat-overnight book should be near 0)."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path("/opt/kronos_bot/repo/futures_v2/intraday")))
import common as C

COST = {"NIFTY": 0.000472, "BANKNIFTY": 0.000541}
SPLIT = pd.Timestamp("2024-07-01")


def sessions(u):
    d = C.load_clean(u, usable_only=False)
    d = d[(d.session_type == "normal") & ~d.bad_ohlc & ~d.post_close & (d.slot <= 35)]
    out = []
    for day, g in d.groupby("session"):
        if len(g) != 36 or list(g.slot) != list(range(36)): continue
        out.append(dict(day=day, o=g.o.values, h=g.h.values, l=g.l.values, c=g.c.values, ret=g.ret_clean_bp.values, gap=float(g.gap_bp.iloc[0])))
    return out


def trades(S, rule, cost):
    # trailing slot sigma (bp) over the previous 250 sessions, no look-ahead
    R = np.vstack([s["ret"] for s in S]); sig = np.full(R.shape, np.nan)
    for i in range(250, len(S)): sig[i] = np.nanstd(R[i - 250:i], axis=0)
    out = []
    for i, s in enumerate(S):
        o, h, l, c = s["o"], s["h"], s["l"], s["c"]
        def add(side, ent, ex):
            out.append(dict(day=s["day"], ret=side * (ex / ent - 1) - cost, side=side))
        if rule == "ORB":
            orh, orl = h[:3].max(), l[:3].min(); side = es = None
            for t in range(3, 35):
                if c[t] > orh: side, es = 1, t; break
                if c[t] < orl: side, es = -1, t; break
            if side is None: continue
            ex = c[35]
            for t in range(es + 1, 36):
                if (side == 1 and c[t] < orl) or (side == -1 and c[t] > orh): ex = c[t]; break
            add(side, c[es], ex)
        elif rule == "MOM":
            r1 = c[2] / o[0] - 1
            if abs(r1) >= 0.005: add(int(np.sign(r1)), c[32], c[35])
        elif rule in ("FADE", "CONT") and i >= 250:
            t = 6
            while t <= 30:
                z = s["ret"][t] / sig[i][t] if sig[i][t] and not np.isnan(s["ret"][t]) else 0
                if abs(z) >= 3:
                    side = int(np.sign(z)) * (-1 if rule == "FADE" else 1); add(side, c[t], c[t + 3]); t += 4
                else:
                    t += 1
        elif rule in ("GAPF", "GAPC"):
            g, b0 = s["gap"], c[0] / o[0] - 1
            if np.isnan(g) or abs(g) < 30: continue
            against = (g > 0 and b0 < 0) or (g < 0 and b0 > 0); withg = (g > 0 and b0 > 0) or (g < 0 and b0 < 0)
            if (rule == "GAPF" and against) or (rule == "GAPC" and withg): add(int(np.sign(g)) * (1 if rule == "GAPC" else -1), c[0], c[35])
    return pd.DataFrame(out)


for u in ("NIFTY", "BANKNIFTY"):
    S = sessions(u); days = pd.Series([s["day"] for s in S]); yrs = (days.iloc[-1] - days.iloc[0]).days / 365.25
    idx_ret = pd.Series([s["c"][-1] / s["o"][0] - 1 for s in S], index=days)       # intraday index move per session (the proxy for 'the market that day')
    print(f"\n{'#' * 118}\n#  {u}: {len(S)} complete sessions {days.iloc[0].date()} -> {days.iloc[-1].date()} ({yrs:.1f} years), cost {COST[u]*100:.4f}%/round trip\n{'#' * 118}")
    # context: where the drift sits intraday
    blocks = (("09:15-09:45", 0, 3), ("09:45-11:15", 3, 12), ("11:15-13:45", 12, 27), ("13:45-15:15", 27, 36))
    cum = np.array([[s["c"][b1 - 1] / (s["o"][b0] if b0 == 0 else s["c"][b0 - 1]) - 1 for s in S] for _, b0, b1 in blocks]) * 1e4
    print("  mean return by time of day, bp (t-stat): " + "   ".join(f"{n} {cum[k].mean():+.1f} ({cum[k].mean()/(cum[k].std()/np.sqrt(len(S))):+.1f})" for k, (n, _, _) in enumerate(blocks)))
    print(f"\n  {'rule':<6}{'trades':>7}{'/yr':>5}{'win%':>6}{'avg win%':>9}{'avg loss%':>10}{'exp%':>8}{'PF':>6}{'t':>6}{'H1 exp':>8}{'H2 exp':>8}{'exp @1.5x cost':>15}{'corr w/ mkt':>12}")
    for rule in ("ORB", "MOM", "FADE", "CONT", "GAPF", "GAPC"):
        T = trades(S, rule, COST[u])
        if len(T) < 30:
            print(f"  {rule:<6}{len(T):>7}"); continue
        w, lo = T.ret[T.ret > 0], T.ret[T.ret <= 0]; pf = w.sum() / abs(lo.sum())
        t = T.ret.mean() / (T.ret.std() / np.sqrt(len(T)))
        dp = T.groupby("day").ret.sum().reindex(days).fillna(0).values; corr = np.corrcoef(dp, idx_ret.values)[0, 1]
        h = lambda x: f"{x.ret.mean()*100:+.3f}" if len(x) else "  n/a"
        print(f"  {rule:<6}{len(T):>7}{len(T)/yrs:>5.0f}{(T.ret>0).mean()*100:>6.0f}{w.mean()*100:>+9.3f}{lo.mean()*100:>+10.3f}{T.ret.mean()*100:>+8.3f}{pf:>6.2f}{t:>6.1f}{h(T[T.day<SPLIT]):>8}{h(T[T.day>=SPLIT]):>8}{(T.ret.mean()-COST[u]*0.5)*100:>+15.3f}{corr:>12.2f}")
