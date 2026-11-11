"""Study 2b (EXPLORATORY, after the primary test failed): is the long-only Zv tail rule better than randomly timed longs?
The indices rose +300% (NIFTY) / +450% (BANKNIFTY) over the sample, so a long-only rule in the market 23% of the time earns a positive
number by drift alone.  Placebo: shift the Zv series circularly by a random lag (>= 60 sessions), run the SAME rule with the SAME exits,
500 draws; the p-value is the share of placebo runs with expectancy >= the observed one.  Thresholds 1.0 / 1.5 / 2.0 are a sensitivity
table, not a search (1.5 was the pre-registered value).  Reported overall and per chronological half."""
import sys
src = open("/opt/kronos_bot/research/idx_study2.py").read().split("for name, cons, k in")[0]
exec(compile(src, "idx_study2_head", "exec"))

N_NULL = 500
for name, cons, k in (("NIFTY", D["n50"], 3), ("BANKNIFTY", D["bank"], 2)):
    Z, fwd, ev, ic_, atr, dates, leaders = build(name, cons, k)
    zv = Z["Zv composite"]; ok = zv.dropna().index; mid = ok[len(ok) // 2]
    valid = zv.loc[ok[0]:]
    drift_day = ic_.loc[ok[0]:].pct_change().mean() * 100
    print(f"\n=== {name} | {ok[0].date()} -> {ok[-1].date()} | halves split {mid.date()} | index drift {drift_day:+.3f}%/day (so a 5-day long earns about {drift_day*5:+.2f}% by drift) ===")
    print(f"  {'rule':<22}{'n':>4}{'avg hold':>9}{'exp%/trd':>9}{'null mean':>10}{'timing alpha':>13}{'p vs random timing':>20}{'   H1 exp / null / p':>26}{'   H2 exp / null / p':>26}")
    for lo in (True, False):
        for thr in (1.0, 1.5, 2.0):
            T = rule(zv, ic_, atr, dates, thr=thr, long_only=lo)
            if len(T) < 20:
                continue
            obs = {"all": T.ret.mean(), "h1": T[T.entry <= mid].ret.mean(), "h2": T[T.entry > mid].ret.mean()}
            nulls = {"all": [], "h1": [], "h2": []}
            for _ in range(N_NULL):
                lag = int(rng.integers(60, len(valid) - 60))
                sh = pd.Series(np.roll(valid.values, lag), index=valid.index).reindex(zv.index)
                Tn = rule(sh, ic_, atr, dates, thr=thr, long_only=lo)
                if len(Tn) == 0:
                    continue
                nulls["all"].append(Tn.ret.mean())
                if (Tn.entry <= mid).any(): nulls["h1"].append(Tn[Tn.entry <= mid].ret.mean())
                if (Tn.entry > mid).any(): nulls["h2"].append(Tn[Tn.entry > mid].ret.mean())
            nm = {k_: np.mean(v) for k_, v in nulls.items()}
            pv = {k_: float(np.mean(np.array(v) >= obs[k_])) for k_, v in nulls.items()}
            tag = f"{'long only' if lo else 'both sides'} z>={thr}" + ("  <- pre-reg" if thr == 1.5 and not lo else "")
            print(f"  {tag:<22}{len(T):>4}{T.held.mean():>9.1f}{obs['all']*100:>+9.2f}{nm['all']*100:>+10.2f}{(obs['all']-nm['all'])*100:>+13.2f}{pv['all']:>20.3f}"
                  f"{obs['h1']*100:>+10.2f}/{nm['h1']*100:>+5.2f}/{pv['h1']:>5.2f}{obs['h2']*100:>+10.2f}/{nm['h2']*100:>+5.2f}/{pv['h2']:>5.2f}")
