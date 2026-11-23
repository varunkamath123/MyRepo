"""Portfolio lab, pass 2: how much of the breakout book is just NIFTY?  Regress each variant's daily return on NIFTY's (beta, annualised alpha, t-stat),
report NIFTY buy-and-hold by half, and test three combinations of the overlays that helped in pass 1 (pre-registered, 3 looks):
  C1  equal-risk 1% per trade, max 5, entries only while NIFTY > 200-DMA
  C2  equal-risk 0.5% per trade, up to 10 positions (more diversification)
  C3  C2 + the 200-DMA entry filter
Uses the same engine and signals as portfolio_lab.py."""
import numpy as np
src = open("/opt/kronos_bot/research/portfolio_lab.py").read().split("mid = int(")[0]
exec(compile(src, "portfolio_lab_head", "exec"))
mid = int(np.searchsorted(cal, pd.Timestamp("2020-01-01")))


def reg(R, lo, hi):
    y, x = R[lo:hi], nret[lo:hi]; X = np.column_stack([np.ones(len(x)), x]); b, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ b; se = np.sqrt(res.var(ddof=2) * np.linalg.inv(X.T @ X).diagonal()); return b[1], b[0] * 252 * 100, b[0] / se[0]


def row(lab, R, inv, npos, trades):
    m, m1, m2 = metrics(R, inv, npos, trades), metrics(R, inv, npos, trades, lo=WARM, hi=mid), metrics(R, inv, npos, trades, lo=mid, hi=T)
    beta, alpha, t = reg(R, WARM, T)
    print(f"  {lab:<44}{m['cagr']:>6.1f}{m['vol']:>6.1f}{m['sharpe']:>7.2f}{m['dd']:>7.0f}{beta:>7.2f}{alpha:>8.1f}{t:>7.1f} | {m1['cagr']:>6.1f}{m2['cagr']:>6.1f}{m1['sharpe']:>6.2f}{m2['sharpe']:>6.2f}{m['inv']:>7.0f}")


bh = nret.copy(); bh[:WARM] = 0
print(f"  {'variant':<44}{'CAGR%':>6}{'vol%':>6}{'Sharpe':>7}{'maxDD':>7}{'beta':>7}{'alpha%/y':>8}{'t(a)':>7} | {'H1 CAGR':>6}{'H2 CAGR':>6}{'ShH1':>6}{'ShH2':>6}{'inv%':>7}")
row("NIFTY buy-and-hold (reference)", bh, np.ones(T), np.ones(T), [])
for lab, kw in (("B5 baseline (20% each, max 5)", dict(N=5)), ("B12 (max 12)", dict(N=12)), ("F200", dict(N=5, regime=True)), ("RISK 1%", dict(N=5, sizing="risk")),
                ("HEDGE (market-neutral)", dict(N=5, hedge=True)), ("C1 RISK 1% + F200", dict(N=5, sizing="risk", regime=True)),
                ("C2 RISK 0.5%, up to 10", dict(N=10, sizing="risk", risk=0.005)), ("C3 RISK 0.5%, up to 10, + F200", dict(N=10, sizing="risk", risk=0.005, regime=True))):
    row(lab, *simulate(**kw))
print("\n  beta = sensitivity of the book's daily return to NIFTY's; alpha = annualised intercept of that regression (return not explained by NIFTY), t(a) its t-stat.")
