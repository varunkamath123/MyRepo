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

## Study 2: constituent VOLUME breadth -> index direction (idx_build_long.py, idx_study2.py, idx_study2b.py; 14 years, 2012-2026)
Pre-registered: signals A (volume-confirmed breakout breadth), B (heavyweight volume breakout), C (net up-volume thrust), Zv = mean of their z-scores;
primary test = Zv vs the forward 5-day return; pass needs all of: IC interval above 0, positive in both halves, placebo p < 0.05, rule expectancy > 0
and PF > 1.2 in both halves.

Primary result: **no statistical edge established** on either index.
- NIFTY: IC -0.009 [-0.08, +0.06] (p=0.31); both-sides rule +0.40%/trade overall but H1 +0.78% / H2 +0.05%.
- BANKNIFTY: IC +0.053 [-0.01, +0.12] (placebo p=0.036); H1 +0.112 / H2 -0.001; rule H1 +1.18% / H2 -0.02%.
- The volume-based signals are no better than the price-only breadth baselines. Whatever there was lived mostly in 2014-2020 and faded.

Exploratory follow-up (study 2b, post hoc, 12 variants looked at): vs randomly timed trades with identical exits (500 circular-shift placebos), the
long-only tail rule (Zv >= +1.5) shows timing alpha of +0.46% (NIFTY, p=0.028) and +0.68% (BANKNIFTY, p=0.010) per trade overall, same sign in both
halves (H2 alpha +0.29% / +0.36%) but not significant in H2 alone (p=0.20 / 0.17, ~35 trades). The short side's alpha is gone in H2.
Treat as a hypothesis for a forward shadow test, not an edge: ~6-8 trades a year per index, NIFTY and BANKNIFTY are not independent confirmations,
and 12 looks put the Bonferroni bar at p < 0.004.
