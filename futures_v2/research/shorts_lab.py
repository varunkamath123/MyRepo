"""Shorts, done as PORTFOLIO constructs (the event shorts -- breakdowns, failed breakouts, overbought, gap-ups -- all lost).  NIFTY 50 stocks, 2013-2026.
CAVEAT, and it cuts AGAINST shorts: the universe is today's NIFTY 50, so stocks that fell hard and were dropped from the index are missing -- the short side is understated.
Pre-registered (7 rows):
  HEDGE0.5   the equal-risk breakout book (portfolio_lab 'RISK') with a short NIFTY future at 0.5 x its long notional (its measured beta is ~0.5), ~0.3%/yr hedge cost
  M-L        cross-sectional momentum: rank the 50 stocks by return from t-252 to t-21 sessions, rebalance every 21 sessions: LONG the top 10 (100% of capital)
  M-S        the same ranking: SHORT the bottom 10 (100% of capital)
  M-S|hedged short the bottom 10 and go long NIFTY against it: pure relative underperformance of the losers
  M-LS       dollar-neutral long-short: +50% top 10, -50% bottom 10
  BOOK+SHORT the equal-risk breakout book plus 50% of capital short the bottom-10 momentum basket
  BOOK+LS    the book plus the dollar-neutral momentum long-short at 50% gross
Costs 0.10% per round trip on every name replaced at a rebalance.  Beta / alpha are regressions on NIFTY's daily return."""
import numpy as np
src = open("/opt/kronos_bot/research/portfolio_lab.py").read().split("mid = int(")[0]
exec(compile(src, "portfolio_lab_head", "exec"))
mid = int(np.searchsorted(cal, pd.Timestamp("2020-01-01")))

# ---- momentum baskets, rebalanced every 21 sessions ----
C = np.vstack([S[s]["c"] for s in names]); RET = np.vstack([S[s]["ret"] for s in names])
long_leg, short_leg = np.zeros(T), np.zeros(T); prev_l, prev_s = set(), set()
for t0 in range(WARM + 252, T, 21):
    form = C[:, t0 - 21] / C[:, t0 - 252] - 1
    ok = np.where(~np.isnan(form) & ~np.isnan(C[:, t0]))[0]
    order = ok[np.argsort(form[ok])]
    if len(order) < 30: continue
    bot, top = set(order[:10]), set(order[-10:])
    cost_l = COST * len(top - prev_l) / 10; cost_s = COST * len(bot - prev_s) / 10
    prev_l, prev_s = top, bot
    for t in range(t0 + 1, min(t0 + 22, T)):
        long_leg[t] = RET[list(top), t].mean() - (cost_l if t == t0 + 1 else 0)
        short_leg[t] = RET[list(bot), t].mean() + (cost_s if t == t0 + 1 else 0)          # cost raises the short basket's return (worse for the short)
start = WARM + 252


def reg(R, lo, hi):
    y, x = R[lo:hi], nret[lo:hi]; X = np.column_stack([np.ones(len(x)), x]); b, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ b; se = np.sqrt(res.var(ddof=2) * np.linalg.inv(X.T @ X).diagonal()); return b[1], b[0] * 252 * 100, b[0] / se[0]


def row(lab, R, lo=start):
    r = R[lo:]; eq = np.cumprod(1 + r); yrs = len(r) / 252; vol = r.std() * np.sqrt(252)
    cagr = (eq[-1] ** (1 / yrs) - 1) * 100; dd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
    m = int(np.searchsorted(cal[lo:], pd.Timestamp("2020-01-01")))
    h = lambda a, b: ((np.cumprod(1 + R[lo:][a:b])[-1]) ** (252 / max(b - a, 1)) - 1) * 100
    beta, alpha, t = reg(R, lo, T)
    print(f"  {lab:<44}{cagr:>6.1f}{vol:>6.1f}{(r.mean()*252)/vol:>7.2f}{dd:>7.0f}{beta:>7.2f}{alpha:>8.1f}{t:>7.1f} | {h(0, m):>6.1f}{h(m, len(r)):>6.1f}")


bh = nret.copy()
Rb, invb, npb, trb = simulate(N=5, sizing="risk")
hedge_drag = 0.003 / 252
print(f"momentum sample {cal[start].date()} -> {cal[-1].date()} ({(T-start)/252:.1f} years); the equal-risk book is shown on the same window\n")
print(f"  {'strategy':<44}{'CAGR%':>6}{'vol%':>6}{'Sharpe':>7}{'maxDD':>7}{'beta':>7}{'alpha%/y':>8}{'t(a)':>7} | {'H1':>6}{'H2':>6}")
row("NIFTY buy-and-hold", bh)
row("equal-risk breakout book (RISK)", Rb)
row("HEDGE0.5  book + short NIFTY at 0.5x", Rb - 0.5 * invb * nret - 0.5 * hedge_drag)
row("M-L   momentum long top 10", long_leg)
row("M-S   momentum SHORT bottom 10", -short_leg)
row("M-S|hedged  short bottom 10, long NIFTY", -short_leg + nret)
row("M-LS  dollar-neutral long-short (50/50)", 0.5 * long_leg - 0.5 * short_leg)
row("BOOK+SHORT  book + 50% short bottom 10", Rb - 0.5 * short_leg)
row("BOOK+LS  book + 50% gross neutral momentum", Rb + 0.25 * long_leg - 0.25 * short_leg)
print("\n  beta = sensitivity to NIFTY; alpha = annualised intercept; 'M-S' loses when the bottom-10 basket rises. Survivorship bias understates every short row.")
