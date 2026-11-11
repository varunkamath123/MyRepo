# Futures strategy v2 (restart, Nov 11 2026)

The Kronos-era strategy is frozen at tag `kronos-fut-v2.0-kronos-era-final` (live paper books keep running from it:
BANKNIFTY Kronos book 14:55 IST, NIFTY 50 stock book 15:25 IST). Everything new lives here and starts from research, not from code.

## Standards (agreed Nov 9-11)
1. **Edge = net expectancy above break-even after costs**: win% x avg win > loss% x avg loss. Always show payoff, break-even win rate, PF.
2. **Both sides, every regime**: report long and short separately, split by NIFTY above/below its 200-DMA, and beta-hedged where relevant.
3. **Pre-register**: definitions fixed in the script docstring before the run; count the looks; overlapping statistics get a
   non-overlapping version with a bootstrap interval.
4. **Evidence ladder**: history study -> shadow ledger (no capital) -> paper book -> anything bigger. One change per tag, tag before deploy.

## Study 1: index vs constituents (idx_build.py, idx_study.py, idx_vol.py; data 2022-11 -> 2026-11)
Question: can constituent state give a forward-looking z-score for an index breakout / breakdown?

Structure (descriptive, solid): NIFTY is carried by few names (HDFCBANK 12%, RELIANCE 9%, ICICIBANK 8%; top-3 explain 76% of daily
variance, top-10 92%). BANKNIFTY is two names: HDFCBANK 26% + ICICIBANK 22%; top-3 explain 89%. Fitted weights are stable
(half-vs-half correlation 0.95 / 0.99). Average pairwise correlation 0.24 (NIFTY, rising from 0.17 in 2023 to 0.28) and 0.39 (BANKNIFTY).

Predictive power of the pre-registered signals (S1 net breakout breadth, S2 trend breadth, S3 leader gap, Zc = mean of the three):
- direction: Spearman IC with the forward 1/5/10-day index return is indistinguishable from zero (Zc: +0.02 / 0.00 / +0.00 NIFTY,
  +0.04 / +0.03 / -0.02 BANKNIFTY; all intervals span zero); quintiles are flat; "Zc >= +1.5, index not yet at a high" preceded
  BANKNIFTY breakdowns more often than breakouts (n=7).
- rule (long Zc >= +1.5, short <= -1.5): NIFTY 16 trades +0.96%/trade (n too small), BANKNIFTY 20 trades -0.42%/trade: not replicated.
- size of the next move: the only strong predictor is the index's own ATR% (volatility clusters: IC +0.36 NIFTY, +0.47 BANKNIFTY vs the
  next 5-day range; low vol stays low, there is no compression-then-expansion effect). Implied correlation and dispersion look
  predictive (IC +0.2) but mostly proxy the volatility level.
- Verdict: breadth / leader z-scores built from constituent PRICES add nothing to the index's own price. The index volume is zero in
  the spot feed, constituent VOLUME is the one input the index does not already contain: the next hypothesis (not yet run).
