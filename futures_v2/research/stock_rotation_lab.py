"""Opportunity-cost ROTATION for the stock book: when is a held position worse than a fresh signal, so that we should close it to free the slot (not because its exit fired, but because something else is likely to do better)?
Same data and conventions as stock_exit_lab2.py (NIFTY 50 names -> survivorship bias; 2012-2026; cost 0.10% per round trip; entries = the live signal; exits = the live LONG rule: 2 ATR stop, 4-ATR trail below the highest close, no cap).
PART 1  state -> remaining yield.  For positions still open after k = 10 and 20 sessions, bucket by return since entry (and a 'stagnant' flag: peak gain < 1.5 ATR) and measure the REMAINING gross return per remaining session,
        against the per-session yield of a fresh signal (all trades, gross, whole life).  A bucket that yields clearly less than a fresh signal is a rotation candidate (a slot is only worth freeing if a signal is waiting).
PART 2  slot-constrained portfolio (N slots, each 1/N of current equity, first-come, strongest volume first among same-day signals), day by day, with rotation rules fixed BEFORE the run (3 looks x 2 slot counts):
        R1 STALE    when a new signal finds all slots full, replace the open position with the lowest return among those held >= 15 sessions with peak gain < 1.5 ATR
        R2 LOSER    ... the lowest return among those held >= 10 sessions and below their entry price
        R3 LAGGARD  ... the lowest return relative to NIFTY among those held >= 10 sessions and trailing NIFTY (since entry) by more than 3 points
        Swapped trades are closed at that day's close (cost 0.10% round trip like any other trade).  Swap quality: the next-20-session return of the dropped stock vs the replacement, for every swap.
Decision rule: adopt rotation only if (a) portfolio CAGR AND Sharpe beat NO ROTATION at both N=3 and N=5, and (b) replacement minus dropped 20-session return is > 0 in BOTH halves (split 2019-07-01) with >= 30 swaps.  Otherwise slots stay first-come until an exit fires."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

RES = Path("/opt/kronos_bot/research")
DD = pickle.load(open(RES / "idx_data_long.pkl", "rb"))
NIFTY = DD["idx"]["NIFTY"]["Close"]; NIFTY = NIFTY[~NIFTY.index.duplicated()]
COST = 0.001
SPLIT = pd.Timestamp("2019-07-01")


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(); d = d[~d.index.duplicated()]
    pc = d.Close.shift(1); tr = pd.concat([d.High - d.Low, (d.High - pc).abs(), (d.Low - pc).abs()], axis=1).max(axis=1)
    A = dict(idx=d.index, c=d.Close.values, atr=tr.rolling(14).mean().values, hi20=d.High.rolling(20).max().shift(1).values,
             vr=(d.Volume / d.Volume.rolling(20).mean().shift(1)).values)
    A["sig"] = (d.Close.values > A["hi20"]) & (A["vr"] >= 1.5) & ~np.isnan(A["atr"]) & (d.Volume.values > 0)
    A["dmap"] = {t: i for i, t in enumerate(d.index)}
    return A


names = [s for s in DD["n50"] if s in DD["stocks"] and len(DD["stocks"][s]) > 300]
data = {s: prep(DD["stocks"][s]) for s in names}

# ---------------- PART 1: state -> remaining yield
rows = []
for s in names:
    A = data[s]; c, atr = A["c"], A["atr"]; pos = False; last = -1; rec = {}
    for t in range(len(c)):
        if pos:
            held = t - et; hc = max(hc, c[t])
            if held in (10, 20) and held not in rec: rec[held] = (c[t] / ep - 1, hc / ep - 1 < 1.5 * ea / ep)
            if c[t] <= ep - 2 * ea or c[t] < hc - 4 * atr[t]:
                rows.append(dict(sym=s, held=held, gross=c[t] / ep - 1, rec=dict(rec))); pos = False; last = t
            continue
        if A["sig"][t] and t != last: pos, ep, ea, et, hc, rec = True, c[t], atr[t], t, c[t], {}
P = pd.DataFrame(rows); fresh = P.gross.sum() / P.held.sum() * 1e4
print(f"{len(names)} stocks; {len(P)} A1 trades; a FRESH signal yields {fresh:+.1f} bp per session (gross, whole life), +{P.gross.mean()*100:.1f}% over {P.held.mean():.0f} sessions\n")
print("PART 1  remaining yield (bp per remaining session, gross) of positions still open after k sessions, by their return since entry:")
for k in (10, 20):
    L = []
    for _, r in P.iterrows():
        if k in r.rec and r.held > k: L.append((r.rec[k][0], r.rec[k][1], (r.gross - r.rec[k][0]), r.held - k))
    X = pd.DataFrame(L, columns=["st", "stale", "rem", "rd"])
    print(f"  after {k} sessions ({len(X)} positions):   {'bucket':<22}{'n':>5}{'rem bp/day':>11}{'vs fresh':>10}{'mean rem return%':>18}")
    for lab, m in (("state < -2%", X.st < -0.02), ("-2% .. 0%", (X.st >= -0.02) & (X.st < 0)), ("0 .. +3%", (X.st >= 0) & (X.st < 0.03)), ("+3 .. +8%", (X.st >= 0.03) & (X.st < 0.08)), (">= +8%", X.st >= 0.08),
                   ("STAGNANT (peak < 1.5 ATR)", X.stale), ("not stagnant", ~X.stale)):
        g = X[m]
        if len(g): print(f"  {'':<28}{lab:<22}{len(g):>5}{g.rem.sum()/g.rd.sum()*1e4:>+11.1f}{g.rem.sum()/g.rd.sum()*1e4 - fresh:>+10.1f}{g.rem.mean()*100:>+18.2f}")
    print()

# ---------------- PART 2: slot-constrained portfolio with rotation rules
cal = list(NIFTY.index[NIFTY.index >= pd.Timestamp("2012-04-01")])
RULES = {"NONE": None, "R1 STALE": (15, "stale"), "R2 LOSER": (10, "loser"), "R3 LAGGARD": (10, "lag")}


def simulate(N, rule):
    pos, cash, eq, inv, trades, swaps = {}, 1.0, [], [], [], []
    nvals = NIFTY
    for dn, d in enumerate(cal):
        exited = set()
        for sym in list(pos):
            p = pos[sym]; A = data[sym]; i = A["dmap"].get(d)
            if i is None: continue
            c = A["c"][i]; p["hc"] = max(p["hc"], c); p["last"] = c
            if c <= p["ep"] - 2 * p["ea"] or c < p["hc"] - 4 * A["atr"][i]:
                cash += p["stake"] * (c / p["ep"]) * (1 - COST); trades.append((p["d0"], c / p["ep"] - 1 - COST, dn - p["dn0"], "EXIT")); del pos[sym]; exited.add(sym)
        cands = []
        for sym, A in data.items():
            i = A["dmap"].get(d)
            if i is not None and A["sig"][i] and sym not in pos and sym not in exited: cands.append((A["vr"][i], sym, i))
        cands.sort(reverse=True)
        for vr, sym, i in cands:
            if len(pos) >= N:
                if rule is None: continue
                age_min, kind = rule; best, bkey = None, None
                for vs, p in pos.items():
                    if p["dn0"] == dn or dn - p["dn0"] < age_min or p.get("last") is None: continue
                    ret = p["last"] / p["ep"] - 1; nret = nvals.get(d, np.nan) / p["n0"] - 1
                    ok = ((p["hc"] / p["ep"] - 1) < 1.5 * p["ea"] / p["ep"]) if kind == "stale" else (ret < 0 if kind == "loser" else (ret - nret < -0.03))
                    key = ret if kind != "lag" else ret - nret
                    if ok and (bkey is None or key < bkey): best, bkey = vs, key
                if best is None: continue
                p = pos.pop(best); Ad = data[best]; j = Ad["dmap"][d] if d in Ad["dmap"] else None
                cash += p["stake"] * (p["last"] / p["ep"]) * (1 - COST); trades.append((p["d0"], p["last"] / p["ep"] - 1 - COST, dn - p["dn0"], "SWAP"))
                dropped = (Ad["c"][j + 20] / Ad["c"][j] - 1) if (j is not None and j + 20 < len(Ad["c"])) else np.nan
                An = data[sym]; repl = (An["c"][i + 20] / An["c"][i] - 1) if i + 20 < len(An["c"]) else np.nan
                swaps.append((d, best, sym, dropped, repl))
            marks = sum(p["stake"] * (p["last"] / p["ep"]) for p in pos.values()); equity = cash + marks
            stake = min(cash, equity / N)
            if stake <= 0: continue
            A = data[sym]; cash -= stake
            pos[sym] = dict(ep=A["c"][i], ea=A["atr"][i], hc=A["c"][i], last=A["c"][i], stake=stake, dn0=dn, d0=d, n0=nvals.get(d, np.nan))
        marks = 0.0
        for sym, p in pos.items():
            i = data[sym]["dmap"].get(d)
            if i is not None: p["last"] = data[sym]["c"][i]
            marks += p["stake"] * (p["last"] / p["ep"])
        eq.append(cash + marks); inv.append(marks / (cash + marks))
    e = pd.Series(eq, index=cal); r = e.pct_change().fillna(0); first = next((k for k, v in enumerate(inv) if v > 0), 0)
    e, r = e.iloc[first:], r.iloc[first:]; yrs = (e.index[-1] - e.index[0]).days / 365.25
    T = pd.DataFrame(trades, columns=["d0", "ret", "held", "how"]); S = pd.DataFrame(swaps, columns=["d", "dropped", "repl", "fd", "fr"])
    return dict(cagr=((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100, sharpe=r.mean() / r.std() * np.sqrt(252),
                mdd=((e / e.cummax()) - 1).min() * 100, inv=np.mean(inv[first:]) * 100, n=len(T), swaps=S, T=T)


for N in (3, 5):
    print(f"PART 2  N = {N} slots (each 1/{N} of equity); NIFTY buy&hold reference ~CAGR 11-12%, Sharpe 0.76, maxDD -38%")
    print(f"  {'rule':<12}{'CAGR%':>7}{'Sharpe':>8}{'maxDD%':>8}{'invested%':>10}{'trades':>8}{'swaps':>7}{'avg hold':>9}{'mean ret%':>10}   swap quality (20-session fwd: dropped vs replacement)")
    base = None
    for k, rule in RULES.items():
        R = simulate(N, rule); S = R["swaps"].dropna(subset=["fd", "fr"]) if len(R["swaps"]) else R["swaps"]
        sq = ""
        if len(S) >= 5:
            h1, h2 = S[S.d < SPLIT], S[S.d >= SPLIT]
            sq = f"n={len(S)} dropped {S.fd.mean()*100:+.1f}% vs replacement {S.fr.mean()*100:+.1f}% (diff {(S.fr-S.fd).mean()*100:+.1f}; H1 {(h1.fr-h1.fd).mean()*100:+.1f}, H2 {(h2.fr-h2.fd).mean()*100:+.1f})"
        if k == "NONE": base = R
        print(f"  {k:<12}{R['cagr']:>7.1f}{R['sharpe']:>8.2f}{R['mdd']:>8.0f}{R['inv']:>10.0f}{R['n']:>8}{len(R['swaps']):>7}{R['T'].held.mean():>9.0f}{R['T'].ret.mean()*100:>+10.2f}   {sq}")
    print()
