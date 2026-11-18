# Paper-trade learning journal

Purpose: the paper books exist to teach us, so every trade is read against a stated expectation. `review/trade_review.py` (cron 16:30 IST, saved to
`/opt/kronos_bot/data/reviews/`) prints the attribution. This file holds the standing hypotheses, their decision rules, and dated lessons.
Evidence grades: **LEARNED** (backed by enough data to act on) | **SUGGESTED** (consistent, small n) | **UNKNOWN**.
Edge = net expectancy above break-even after costs (win% x avg win > loss% x avg loss).

## Hypotheses under test (status as of Nov 17 2026)
| # | Hypothesis | Evidence so far | Decision rule | Status |
|---|---|---|---|---|
| H1 | BANKNIFTY gates-off config has positive net expectancy | 1 closed (-11.2k, ST_FLIP), 1 open (long, conf 0.81) | revert if the first 10 trades net worse than -75k or include 2+ stop-outs | 1 of 10 |
| H2 | Entries AGAINST SuperTrend are the profit engine of that config | replay: 13/42 trades gave +76k of +118k (Upstox input), 12/37 gave +172k of +219k (yfinance); 12 of 13 were long dip-buys on the Upstox input (8 of 12 on yfinance); only 1-4 were shorts | needs >= 10 live counter-ST trades, longs and shorts separately | 1 (a short, lost) |
| H3 | Kronos's 5-day read is contrarian to the recent 5-day move, and a LONG after a rising 20 days has ~zero edge | replay: signed read vs trailing 5d return corr -0.18..-0.26; LONG after rising 20d: n=124-172, mean next-5d about 0.0%, right direction 49-52% | Nov 17 long (conf 0.81, after a ~15% rally off the Oct 27 low) is a live test; expectation ~0 | open |
| H4 | Kronos's predicted targets / R:R are not calibrated | 8 closed trades: predicted +207k vs actual -63k; winners realised 4-40% of the prediction | never gate on R:R; keep logging the ratio | LEARNED (n=8) |
| H5 | The exits protect more than they cost | exit value: 7 of 8 exits saved P&L at +1 day, 6 of 7 at +3 days, 5 of 7 at +5 days; the one clear miss is the Oct 12 decay-at-a-loss (cost 43k at +5d) | keep; re-test as trades accumulate | SUGGESTED |
| H6 | Payoff, not hit rate, is the live problem | 8 trades: 4W/4L, avg win +5.0k vs avg loss -20.9k (payoff 0.24; break-even win rate 81%) | needs a payoff >= 1 (or a much higher hit rate) to be an edge | LEARNED (n=8, mostly the old config) |
| H7 | Stock breakout longs have positive expectancy after costs | 0 closed; 10 open +0.52% mean but +0.04% after stripping NIFTY | need >= 30 closed signal-book trades, both halves | open |
| H8 | The 15:25 paper fill (last continuous price, ~15:14) is realistic | REVISED on 98 flagged stock-days since Aug 3: the official close sits +0.22% (+/-0.08%) above the 15:14 price (73% up) vs +0.11% for all stocks, so the booked fill is ~0.2% cheaper than a close fill (my first estimate of 0.48% from 10 trades was small-sample noise) | keep executing before the auction (15:14), which is the better timing; compare to research with this 0.2% in mind. Do NOT move fills to the official close | LEARNED (n=98) |
| H9 | Stock entries are clustered, so trade count overstates evidence | Nov 11: 7 entries on one day; banks/fin +2.2% avg, autos -2.2%: ~3 sector bets, not 7 | judge by cohort / sector-adjusted results | SUGGESTED |
| H10 | The short side stays negative | short shadow: 0 closed live; history: breakdown shorts -1.31%/trade, and 5 reversal-short rules on failed breakouts (below the breakout level, >=1 ATR down, stop-and-reverse, red on day 5, down >3% on day 5) ALL lost -1.1..-2.5% per short trade (PF 0.38-0.61, both halves negative, also after hedging NIFTY) and made the combined long+short worse by 0.1-1.1% per signal | revisit at 30 closed | open |
| H11 | NIFTY > 200-DMA filter helps the capital book | NIFTY has been above it throughout | cannot be tested until it falls below | UNKNOWN |
| H12 | Cutting breakout losers earlier would pay | NO as a blanket rule: 5 variants all lowered expectancy (+1.06% live rules vs +0.53..+0.94%). Conditional hint (post hoc, new look): longs down >3% on day 5 finished +8% win / -4.6% mean / 79% stopped (n=48 of 509); those down 0-1% finished +0.91% | pre-register "exit if down >3% on day 5" and judge it on the live cohort (day-5 return is derivable from daily candles) before touching the rules | open |
| H13 | Failed breakouts recover, so buying them (not shorting) is the timing idea | 14y: after a day-3 close back below the breakout level (28% of signals) the stock rose +0.13% day 1, +0.49% day 5, +1.06% day 10 (net NIFTY +0.28% / +0.45%), both halves positive (H1 +0.25%, H2 +0.72% at day 5); after costs it is thin | shadow ledger first; judge on >= 30 live cases | idea |
| H14 | Buying index dips (RSI2 < 10 while above the 200-day average, exit RSI2 > 70 / 10 sessions) has a small positive edge | 14y daily: NIFTY 125 trades (9/yr) +0.29%/trade net of 0.047%, win 66%, PF 1.52, halves +0.15/+0.45, placebo p=0.05 (random-timing null +0.09%); BANKNIFTY +0.26%, PF 1.36, p=0.22. 10-day-low pullback +0.13/+0.20%; z<=-2 ~0 | shadow on both indices (daily 15:14); judge on 30 live trades | candidate |
| H15 | Fading overnight gaps (buy the gap-down / short the gap-up open, exit at the close) works | 14y NIFTY 50 stocks: idiosyncratic gap >= 1% +0.38%/trade net, +0.28% net of NIFTY, PF 1.47, positive both halves and 14 of 15 years; top-3 per day +0.46% (PF 1.57). BUT it decayed: hedged +0.5-0.9% in 2012-14, +0.1-0.2% in 2022-25, -0.01% in 2026, and a realistic-fill test on 109 sessions / 269 trades since Jun 2026 gave -0.08..-0.11% net (PF 0.84-0.90) whatever the entry/exit fill | do not build; revisit if the effect returns | REJECTED for now |

## Lessons log
**Nov 13 short, closed Nov 16 (ST_FLIP, -11,173).** Entered with SuperTrend BULL (allowed since the gates are off), so the SuperTrend-flip exit fired at the next run:
a counter-trend entry is a one-day bet whatever Kronos's 5-day forecast says. Holding would have lost -37.6k (index +2.0% in two days): the exit saved 26.4k.
The replay barely sampled counter-trend shorts, so this trade type is untested, not disproved.

**Nov 17 long (conf 0.81, after a 15% rally).** Prior from 3 years of reads: LONG after a rising 20 days is a coin flip (mean next-5d about 0). High-confidence (>= 0.70)
longs historically did better (right direction 47-69%, mean +0.3..+0.9%) but mostly after declines, not at highs. Expect ~0; record the outcome.

**My own hypothesis was wrong.** After the SHORT reads at the lows and the 81% LONG at the high I suspected Kronos was just extrapolating the trend. Tested on 3 years
of reads: the opposite (contrarian at 5 days, ~neutral vs the 20-day trend). The live sequence is the exception.

**Stock book, day 5.** Booked fills are flattered by ~0.5% per trade (H8); the first cohort is mostly market beta (+0.04% vs NIFTY); sector dispersion dominates (H9).

**Nov 18, "why not exit the losers?"** The stock book has only three exits (2 ATR stop, 10-day-low channel, 30 sessions) and no discretionary loss-cutting; MARUTI (-3.7%) is 0.8% above its stop,
HDFCLIFE 1.9%, INDIGO and M&M 3.2%. On 3 years of NIFTY 50 breakouts every earlier-cut variant lowered expectancy because the edge lives in a fat right tail (42% win, avg win +8.2% vs avg loss -4.2%):
tighter exits raise the payoff but drop the win rate faster. A trade down >3% on day 5 (like MARUTI) usually does stop out (79%), but only 9% of trades look like that: worth ~+0.05% per trade, not
enough to rewrite the rules on a 5-day sample. Tracked as H12.

**Nov 18, "we could have shorted the losers".** Tested five reversal rules on 582 NIFTY 50 breakout signals over 3 years: every one loses (short-leg win rate 14-32%, expectancy -1.1% to -2.5%
per trade, negative in both halves and after hedging NIFTY). Failed breakouts in large caps tend to bounce and take out the short's stop; stocks down >3% on day 5 (MARUTI-like) are the worst shorts
(14% win). The long's own result at those points (-0.6% to -5.8%) is bad, but turning it into a short made it worse. Short shadow ledger keeps collecting forward evidence.

**Nov 18, "it is a timing thing".** Tested timing three ways on 14 years of NIFTY 50 data (4,964 breakouts, 2,533 breakdowns). (1) Where the return accrues: post-breakout drift is slow (+0.35% day 5, +0.70% day 10, +1.66% day 20; net of NIFTY +0.09/+0.25/+0.63) and day 1 is
overnight +0.30% vs intraday -0.17%, so buying the next open gives up the gap (T1 +0.88% vs the live close entry +1.21%). (2) Entry refinements (day-1 confirmation, retest of the level, skip stretched breakouts) do not beat entering on the signal day:
live timing +1.21%/trade, PF 1.44, t=5.4 clustered by week, positive in both halves (+0.57% / +1.78%), +0.43% net of NIFTY (survivorship caveat: today's constituents). (3) Short timing (continuation confirmation, weak-market filter, delayed failure): breakdown shorts
-1.29/-1.34/-1.31% per trade, negative in both halves; breakdowns and failed breakouts BOUNCE. The one real timing edge is execution time of day: flagged breakouts close ~0.22% above their 15:14 price, so trade before the auction.

**Nov 18, "timing = buy low, sell high".** Mean-reversion lab, 8 textbook rules on NIFTY, BANKNIFTY and the NIFTY 50 stocks (2012-2026). BUY-low rules have small positive expectancy (index RSI2 dip-buy +0.29%/trade, stocks +0.38% but only +0.11% after stripping NIFTY);
SELL-high / short rules lose on multi-day holds in every universe (-0.4% to -1.0% per trade, placebo p 0.93-0.99 on the indices: worse than random). The only profitable sell-high is the same-day fade of overnight gaps in stocks, a strong 14-year effect (+0.46%/trade) that
has faded to ~0 recently (realistic fills since Jun 2026: -0.1%). Consistent theme across the whole project: short-term mean reversion works on the LONG side only (dip-buys, failed breakouts, breakdown bounces, Kronos LONG after falls, the BANKNIFTY counter-SuperTrend longs).
