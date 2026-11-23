"""Shorts as INSURANCE: hedge the equal-risk breakout book with a short NIFTY future only while the market is in a downtrend (decided on the PRIOR close, no look-ahead).
Pre-registered (3 rows): hedge 0.5 x the long notional while NIFTY < its 200-day average | while NIFTY < its 50-day average | always (from shorts_lab, for reference)."""
import numpy as np
src = open("/opt/kronos_bot/research/shorts_lab.py").read().split("bh = nret.copy()")[0]
exec(compile(src, "shorts_lab_head", "exec"))
n50 = (nf > nf.rolling(50).mean()).values
dn200 = np.r_[False, ~n200[:-1]]; dn50 = np.r_[False, ~n50[:-1]]
Rb, invb, npb, trb = simulate(N=5, sizing="risk")
drag = 0.003 / 252
print(f"{'strategy':<48}{'CAGR%':>6}{'vol%':>6}{'Sharpe':>7}{'maxDD':>7}{'beta':>7}{'alpha%/y':>8}{'t(a)':>7} | {'H1':>6}{'H2':>6}   share of days hedged")
def row2(lab, R, share):
    r = R[start:]; eq = np.cumprod(1 + r); yrs = len(r) / 252; vol = r.std() * np.sqrt(252)
    m = int(np.searchsorted(cal[start:], pd.Timestamp("2020-01-01")))
    h = lambda a, b: ((np.cumprod(1 + R[start:][a:b])[-1]) ** (252 / max(b - a, 1)) - 1) * 100
    beta, alpha, t = reg(R, start, T)
    print(f"{lab:<48}{(eq[-1]**(1/yrs)-1)*100:>6.1f}{vol*100:>6.1f}{(r.mean()*252)/vol:>7.2f}{(eq/np.maximum.accumulate(eq)-1).min()*100:>7.0f}{beta:>7.2f}{alpha:>8.1f}{t:>7.1f} | {h(0,m):>6.1f}{h(m,len(r)):>6.1f}   {share}")
row2("equal-risk book, unhedged", Rb, "-")
row2("hedge 0.5 always", Rb - 0.5 * invb * nret - 0.5 * drag, "100%")
row2("hedge 0.5 only while NIFTY < 200-DMA", Rb - 0.5 * invb * nret * dn200, f"{dn200[start:].mean()*100:.0f}%")
row2("hedge 0.5 only while NIFTY < 50-DMA", Rb - 0.5 * invb * nret * dn50, f"{dn50[start:].mean()*100:.0f}%")
row2("hedge 1.0 only while NIFTY < 200-DMA", Rb - 1.0 * invb * nret * dn200, f"{dn200[start:].mean()*100:.0f}%")
