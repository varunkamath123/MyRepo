"""Drill into the quality flags the dataset build raised: extra-bar sessions, the flat 15:15 bar, daily-high mismatches."""
import json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

for u in ("NIFTY", "BANKNIFTY"):
    d = C.load_clean(u, usable_only=False)
    q = json.load(open(C.DATA / "clean" / f"quality_{u}.json"))
    print(f"\n===== {u} =====")
    # (a) sessions with more than 38 bars
    n = d.groupby("session").size()
    odd = n[(n > 38)]
    print(f"[a] sessions with more than 38 bars: {len(odd)}")
    for s in odd.index[:6]:
        x = d[d.session == s]; extra = x[x.slot > 37]
        print(f"    {s.date()}: {len(x)} bars; bars after 15:25: " + ", ".join(f"{t.strftime('%H:%M')}(o={o:.1f} c={c:.1f})" for t, o, c in zip(extra.ts, extra.o, extra.c)))
    # (b) flat 15:15 bar timeline
    f = d[(d.slot == 36) & d.flat & (d.session_type == "normal")]
    allnorm = d[(d.slot == 36) & (d.session_type == "normal")]
    print(f"[b] flat 15:15 bars: {len(f)} of {len(allnorm)} normal sessions; first {f.session.min().date() if len(f) else '-'}, last {f.session.max().date() if len(f) else '-'}")
    if len(f):
        by_month = f.groupby(f.session.dt.to_period("M")).size()
        tot = allnorm.groupby(allnorm.session.dt.to_period("M")).size()
        print("    by month (flat/sessions): " + "  ".join(f"{m}:{by_month.get(m, 0)}/{tot[m]}" for m in tot.index[-12:]))
        rng = lambda s: ((d[d.slot == s].h - d[d.slot == s].l) / d[d.slot == s].c * 100)
        recent = d.session >= f.session.min()
        print(f"    mean bar range % since the first flat bar: 15:05 {rng(35)[d[d.slot==35].session>=f.session.min()].mean():.3f}  15:15 {rng(36)[d[d.slot==36].session>=f.session.min()].mean():.3f}  15:25 {rng(37)[d[d.slot==37].session>=f.session.min()].mean():.3f}"
              f" | before it: 15:05 {rng(35)[d[d.slot==35].session<f.session.min()].mean():.3f}  15:15 {rng(36)[d[d.slot==36].session<f.session.min()].mean():.3f}  15:25 {rng(37)[d[d.slot==37].session<f.session.min()].mean():.3f}")
    # 1-minute view of the same window (recorder has the last ~4 months)
    p = C.candle_path(f"{u}_IDX", 1)
    if p.exists():
        m = pd.read_csv(p, parse_dates=["ts"]); m["t"] = m.ts.dt.strftime("%H:%M"); m["day"] = m.ts.dt.date
        w = m[(m.t >= "15:10") & (m.t <= "15:29")]
        flat_min = w.groupby("t").apply(lambda g: float(((g.h == g.l)).mean())).round(2)
        print("    1-minute index bars 15:10-15:29, share flat across sessions: " + " ".join(f"{k}:{v:.2f}" for k, v in flat_min.items()))
    # (c) daily mismatches
    print("[c] daily cross-check:")
    for col, v in q["daily_crosscheck"].items():
        print(f"    {col}: {v}")
