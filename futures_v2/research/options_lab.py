"""Futures + options, first pass with MODELLED prices (no historical option quotes exist in the data we have): Black-Scholes with India VIX as the implied volatility
(r 6%, dividend yield 1.2%), 21-session (one-month) options held to expiry on NIFTY, 2013-2026.  Bid/ask and skew are NOT in the data: skew is a markup on VIX for OTM puts
(0% / 15% / 30%), and a 0.15%-of-notional round-trip cost is charged per option position.  Read the numbers as 'is the idea plausible', not as a backtest.
O1  variance risk premium: VIX vs the volatility NIFTY then realised over the next month
O2  short ATM straddle held to expiry (the classic premium sale): monthly P&L as % of notional, tail months
O3  buy a 3% OTM put every month (protective put cost) and what it pays in bad months
O4  the equal-risk breakout book with an option hedge on 0.5 x its long notional instead of a futures hedge (pre-registered, 5 rows):
    puts always | puts only when NIFTY < its 50-day average | 97/90 put SPREAD only when below the 50-DMA | the same below-50-DMA put at skew 0% and 30%
    compared with the futures hedge from shorts_lab2 (0.5 x only below the 50-DMA)."""
import math
import numpy as np
src = open("/opt/kronos_bot/research/shorts_lab.py").read().split("bh = nret.copy()")[0]
exec(compile(src, "shorts_lab_head", "exec"))
vixs = pd.read_pickle("/opt/kronos_bot/research/indiavix.pkl")["Close"]
vix = vixs.reindex(cal, method="ffill").values / 100.0
Nc = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
R_, Q_, TT, COSTO = 0.06, 0.012, 21 / 252, 0.0015


def d12(S, K, sig):
    d1 = (math.log(S / K) + (R_ - Q_ + 0.5 * sig * sig) * TT) / (sig * math.sqrt(TT)); return d1, d1 - sig * math.sqrt(TT)


def put(S, K, sig):
    d1, d2 = d12(S, K, sig); return K * math.exp(-R_ * TT) * Nc(-d2) - S * math.exp(-Q_ * TT) * Nc(-d1)


def call(S, K, sig):
    d1, d2 = d12(S, K, sig); return S * math.exp(-Q_ * TT) * Nc(d1) - K * math.exp(-R_ * TT) * Nc(d2)


S0v = nf.values; blocks = list(range(start, T - 22, 21)); mid_b = next(i for i, t in enumerate(blocks) if cal[t] >= pd.Timestamp("2020-01-01"))
# ---- O1 ----
iv = np.array([vix[t] for t in blocks]); rv = np.array([nret[t + 1:t + 22].std() * math.sqrt(252) for t in blocks]); fret = np.array([S0v[t + 21] / S0v[t] - 1 for t in blocks])
print(f"{len(blocks)} monthly blocks, {cal[blocks[0]].date()} -> {cal[blocks[-1]].date()}\n")
print(f"O1 variance risk premium: mean VIX {iv.mean()*100:.1f} vs mean realised vol next month {rv.mean()*100:.1f} (implied > realised in {(iv>rv).mean()*100:.0f}% of months); "
      f"H1 {np.mean((iv-rv)[:mid_b])*100:+.1f} / H2 {np.mean((iv-rv)[mid_b:])*100:+.1f} vol points")
# ---- O2 ----
prem = np.array([(call(S0v[t], S0v[t], vix[t]) + put(S0v[t], S0v[t], vix[t])) / S0v[t] for t in blocks]); pay = np.abs(fret)
pl = prem - pay - COSTO
print(f"\nO2 SHORT ATM straddle, held to expiry, % of notional per month: mean {pl.mean()*100:+.2f}% (premium collected {prem.mean()*100:.2f}%), std {pl.std()*100:.2f}%, winning months {(pl>0).mean()*100:.0f}%, "
      f"median {np.median(pl)*100:+.2f}%")
print(f"   worst 5 months: " + ", ".join(f"{cal[blocks[i]].strftime('%b%Y')} {pl[i]*100:+.1f}%" for i in np.argsort(pl)[:5]) + f"; best month {pl.max()*100:+.1f}%; annualised mean {pl.mean()*12*100:+.1f}% of notional, "
      f"mean/std x sqrt(12) = {pl.mean()/pl.std()*math.sqrt(12):.2f}; H1 {pl[:mid_b].mean()*100:+.2f}% / H2 {pl[mid_b:].mean()*100:+.2f}%")
# ---- O3 ----
print("\nO3 buy a 3% OTM one-month put every month (% of notional):")
for sk in (0.0, 0.15, 0.30):
    pr = np.array([put(S0v[t], 0.97 * S0v[t], vix[t] * (1 + sk)) / S0v[t] for t in blocks]); po = np.maximum(0.97 - (1 + fret), 0)
    net = po - pr - COSTO; bad = fret < -0.05
    print(f"   skew markup {sk*100:>3.0f}%: premium {pr.mean()*100:.2f}%/month = {pr.mean()*1200:.1f}%/yr, pays in {(po>0).mean()*100:.0f}% of months, net {net.mean()*100:+.2f}%/month ({net.mean()*1200:+.1f}%/yr); "
          f"in the {int(bad.sum())} months NIFTY fell more than 5% it paid {po[bad].mean()*100:.1f}% against {pr[bad].mean()*100:.2f}% premium")

# ---- O4 ----
Rb, invb, npb, trb = simulate(N=5, sizing="risk")
n50 = (nf > nf.rolling(50).mean()).values; dn50 = np.r_[False, ~n50[:-1]]


def overlay(kind, cond, sk=0.15, h=0.5):
    R2 = Rb.copy()
    for t in blocks:
        if cond and not dn50[t]: continue
        notional = h * invb[t]
        if notional <= 0: continue
        S, ST = S0v[t], S0v[t + 21]; sg = vix[t] * (1 + sk)
        if kind == "put":
            pr, po = put(S, 0.97 * S, sg) / S, max(0.97 * S - ST, 0) / S
        else:                                                                    # 97/90 put spread
            pr = (put(S, 0.97 * S, sg) - put(S, 0.90 * S, sg * 1.05)) / S; po = (max(0.97 * S - ST, 0) - max(0.90 * S - ST, 0)) / S
        R2[t + 21] += notional * (po - pr - COSTO)
    return R2


print(f"\nO4 the equal-risk breakout book with an option hedge on 0.5 x its long notional (window {cal[start].date()} -> {cal[-1].date()}):")
print(f"  {'strategy':<52}{'CAGR%':>6}{'vol%':>6}{'Sharpe':>7}{'maxDD':>7}{'beta':>7}{'alpha%/y':>8}{'t(a)':>7} | {'H1':>6}{'H2':>6}")
row("equal-risk book, unhedged", Rb)
row("futures hedge 0.5 only below the 50-DMA (reference)", Rb - 0.5 * invb * nret * dn50)
row("3% OTM puts every month (skew 15%)", overlay("put", False))
row("3% OTM puts only below the 50-DMA (skew 15%)", overlay("put", True))
row("97/90 put spread only below the 50-DMA (15%)", overlay("spread", True))
row("   same puts below the 50-DMA, skew 0%", overlay("put", True, sk=0.0))
row("   same puts below the 50-DMA, skew 30%", overlay("put", True, sk=0.30))
