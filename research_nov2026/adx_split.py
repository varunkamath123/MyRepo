"""Are counter-trend entries at extreme ADX the problem?  BANKNIFTY gates-off replay (the live config), cached reads extended to today.  Each trade is tagged with ADX at the entry decision (bars through the prior session) and
whether it agreed with SuperTrend.  Cells: n, win%, average Rs per trade (net of Rs500), total.  Post-hoc look prompted by the two losing live shorts (ADX 56.6 and 76.1); treat as a lead, not a rule."""
import os, sys
src = open("/opt/kronos_bot/research/inverse_check.py").read().split("for inst in (\"BANKNIFTY\", \"NIFTY\"):")[0]
exec(compile(src, "inverse_head", "exec"))
import pandas as pd

inst = "BANKNIFTY"
for prefix, nice in (("sweepU", "UPSTOX input (live-matching)"), ("sweepY", "YFINANCE input")):
    if prefix == "sweepU":
        df = upstox_df(inst); cache, _ = extend(inst, df)
    else:
        df = pd.read_csv(RES / f"sweepY_{inst}_df.csv", parse_dates=["datetime"]); cache = json.load(open(RES / f"sweepY_{inst}_signals.json"))
    T = simulate(inst, df, cache, False)
    adx, aligned = [], []
    for r in T.itertuples():
        i = int(df.index[df.datetime.dt.normalize() == r.entry][0]); ctx = df.iloc[:i]
        adx.append(bt.compute_adx(ctx)); st = bt.supertrend(ctx, period=10, multiplier=4.0)
        aligned.append((r.dir == "LONG" and st == "BULL") or (r.dir == "SHORT" and st == "BEAR"))
    T["adx"], T["aligned"] = adx, aligned
    print(f"\n=== {nice} | {len(T)} trades | ADX at entry: median {T.adx.median():.0f}, 80th pct {T.adx.quantile(.8):.0f} ===")
    print(f"  {'cell':<44}{'n':>4}{'win%':>6}{'avg Rs':>9}{'total Rs':>11}")
    for lab, m in (("ADX < 25", T.adx < 25), ("ADX 25-45", (T.adx >= 25) & (T.adx < 45)), ("ADX >= 45", T.adx >= 45),
                   ("with SuperTrend, ADX < 45", T.aligned & (T.adx < 45)), ("with SuperTrend, ADX >= 45", T.aligned & (T.adx >= 45)),
                   ("AGAINST SuperTrend, ADX < 45", ~T.aligned & (T.adx < 45)), ("AGAINST SuperTrend, ADX >= 45", ~T.aligned & (T.adx >= 45)),
                   ("  against, ADX >= 45, LONG", ~T.aligned & (T.adx >= 45) & (T.dir == "LONG")), ("  against, ADX >= 45, SHORT", ~T.aligned & (T.adx >= 45) & (T.dir == "SHORT")),
                   ("SHORT, ADX >= 45 (any alignment)", (T.dir == "SHORT") & (T.adx >= 45)), ("LONG, ADX >= 45 (any alignment)", (T.dir == "LONG") & (T.adx >= 45))):
        g = T[m]
        print(f"  {lab:<44}{len(g):>4}" + (f"{(g.pnl>0).mean()*100:>6.0f}{g.pnl.mean():>+9,.0f}{g.pnl.sum():>+11,.0f}" if len(g) else ""))
