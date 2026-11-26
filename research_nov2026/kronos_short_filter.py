"""Follow-up to kronos_vs_naive.py: is it the Kronos SHORT reads that hurt the BANKNIFTY / NIFTY book?  Same replay engine and eligible reads (non-NEUTRAL, conf >= 0.45).  Deterministic variants (no random draws):
  REAL                       Kronos direction, as live
  LONG reads only            SHORT reads dropped (treated as NEUTRAL)
  SHORTs only if 20d down    SHORT reads kept only when the close is below the close 20 sessions ago (no counter-trend shorts); LONG reads kept as is
  LONGs only if 20d up       the mirror, LONG reads kept only with the 20-day trend up; SHORT reads kept as is
Also the REAL trades split by direction.  Whole replay and the live window since 2026-07-01.  NOTE the live config was chosen on this replay, so the whole-sample numbers are optimistic; the filters here are post-hoc looks (4 looks per index)."""
import sys
src = open("/opt/kronos_bot/repo/research_nov2026/kronos_vs_naive.py").read().split("for inst in (")[0]
exec(compile(src, "kronos_vs_naive_head", "exec"))

for inst in ("BANKNIFTY", "NIFTY"):
    df = upstox_df(inst); cache = json.load(open(RES / f"sweepU_{inst}_signals.json"))
    keys = [str(x) for x in df["datetime"]]; close = df["close"].values; pos_of = {k: i for i, k in enumerate(keys)}
    elig = {k for k, v in cache.items() if k in pos_of and v[0] != "NEUTRAL" and v[1] >= BAR}

    def up20(k):
        i = pos_of[k]; return i < 20 or close[i] >= close[i - 20]

    def variant(drop):
        cc = {k: list(v) for k, v in cache.items()}
        for k in elig:
            if drop(k, cache[k][0]): cc[k] = ["NEUTRAL", 0.0, "", 0.0, 0.0]
        return cc

    print(f"\n{'=' * 112}\n{inst}: {len(elig)} eligible reads ({sum(1 for k in elig if cache[k][0]=='LONG')} long / {sum(1 for k in elig if cache[k][0]=='SHORT')} short); of the shorts {sum(1 for k in elig if cache[k][0]=='SHORT' and up20(k))} came with the 20-day trend UP\n{'=' * 112}")
    print(f"  {'variant':<34}{'n':>5}{'win%':>6}{'total Rs':>12}{'Rs/trade':>10}{'n':>6}{'win%':>6}{'total Rs':>12}{'Rs/trade':>10}      (left: whole replay, right: live window since 2026-07)")
    R = simulate(inst, df, cache)
    rows = [("REAL (Kronos direction)", R),
            ("LONG reads only", simulate(inst, df, variant(lambda k, d: d == "SHORT"))),
            ("SHORTs only if 20d trend down", simulate(inst, df, variant(lambda k, d: d == "SHORT" and up20(k)))),
            ("LONGs only if 20d trend up", simulate(inst, df, variant(lambda k, d: d == "LONG" and not up20(k))))]
    for name, T in rows:
        a, b = summ(T), summ(T[T.entry >= LIVE] if len(T) else T)
        print(f"  {name:<34}{a[0]:>5}{a[1]:>6.0f}{a[2]:>+12,.0f}{a[3]:>+10,.0f}{b[0]:>6}{b[1]:>6.0f}{b[2]:>+12,.0f}{b[3]:>+10,.0f}")
    print("  REAL trades by direction:")
    for d in ("LONG", "SHORT"):
        g = R[R.dir == d]; a, b = summ(g), summ(g[g.entry >= LIVE] if len(g) else g)
        print(f"    {d:<32}{a[0]:>5}{a[1]:>6.0f}{a[2]:>+12,.0f}{a[3]:>+10,.0f}{b[0]:>6}{b[1]:>6.0f}{b[2]:>+12,.0f}{b[3]:>+10,.0f}")
