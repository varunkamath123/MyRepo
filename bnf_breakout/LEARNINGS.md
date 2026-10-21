# Ten things the data says about being profitable here

Written 2026-10-21, before the strategy was built. Each item names the **market
variable** it came from, the **data**, and the number. Where a conclusion rests on
a small sample, or on a comparison that is not like-for-like, that is said.

The overall shape: **gross edge from price, volume and open-interest structure is
approximately zero; costs and exits decide the sign.** Most of what follows is
about what *not* to expect, because that is what the evidence supports.

---

### 1. Direction is not predictable from price, volume or open-interest structure
*Variables: distance to previous-day high/low, opening range, day high/low;
6-bar range vs ATR (compression); 3-bar volume vs same-time-of-day median;
12-bar trend slope; PCR, max-pain, OI walls, ADX/DI (the `shadow_quad`).*
420 BANKNIFTY sessions, 8 pre-registered variants, two exit families
(`research/breakout_study.py`, `breakout_study2.py`). Every variant is at or below
a **random entry** (−0.058% / −0.057% of spot per trade); the best is −0.052%.
Tuned on the first 60% of sessions and tested on the rest, the best of 36
combinations scored −0.033% in-sample and **−0.050% out-of-sample against −0.071%
for random entry on the same period** (CI includes zero). The OI-based quad fired
89 times with 88 CALLs, and 3 CALLs on Oct 21, a day the market fell 1.2–1.6%.
Earlier work found the same for 12 other direction methods and for 507 ORB
entries (mean +0.000%).

### 2. Compression does not precede expansion
*Variable: range of the last 6 bars ÷ (ATR14 × √6); forward 2-hour range.*
After a compressed coil the next 2 hours' range was **0.486%, against 0.496% for a
random bar** (−0.010%). Quiet tends to stay quiet: yesterday's realised volatility
predicts today's with ρ = +0.593 (n = 1,258 sessions). The "squeeze then
explosion" picture is not what this index does.

### 3. Volume adds nothing to a price setup
*Variable: mean volume of the last 3 bars ÷ median volume at the same bar of day
over the previous 20 sessions (320 sessions carry usable index volume).*
Participating volume −0.085%, drying-up volume −0.082%, a volume-surge
confirmation breakout −0.090%, against −0.086% for the same setup with no volume
condition. All indistinguishable.

### 4. Costs, not signals, decide the sign
*Variables: round-trip cost, hit rate of a 0.77% target.*
A 55% premium target is ~0.77% of spot; a **random** entry reaches it **4.1%** of
the time, and only 3 of the 39 real BANKNIFTY trades did. Round-trip cost on one
lot is ~₹78 on a ~₹11.6k premium (0.67% of the premium, ~0.08% of spot with
slippage). Gross edge ≈ 0 means net ≈ −cost. A strategy therefore needs *rare big
winners and many small controlled losses*, not a high hit rate.

### 5. Exits decide the P&L
*Variables: exit reason, peak and trough of the option premium.*
Real BANKNIFTY book, 39 trades, **+₹175 in total** (break-even): 3 targets
(+₹17,498) and 10 trailing exits (+₹11,974) against 5 stop-losses (−₹20,142) and
6 never-progressed cuts (−₹7,548). Across all instruments (114 trades) trailing
exits earned +₹37,578 and 25% stops lost ₹39,567. The profit is in what you do
after entry.

### 6. Do not cap winners, and do not tighten the stop below ~11%
*Variables: peak and trough of the premium on Oct 21.*
The BANKNIFTY winner (+₹6,485) fell **−10.9%** before running to +56.3%. The
SENSEX winner peaked at +57.0%. The 55% target exited both within ~1 point of the
peak. Replaying lower targets while *also counting the winners they would have
capped*: 10% target −₹15,963 vs actual, 15% −₹8,441, 25% −₹6,565, 35% −₹927 —
every level worse. (My first version of that replay counted only the rescued
losers and showed a gain; it was wrong.)

### 7. Losers never worked
*Variable: peak premium reached (`max_pnl_pct`).*
BANKNIFTY winners peaked at +29.4% on average; losers at **+2.6%**. A trade that
shows no follow-through early is usually just wrong, which is the basis for the
"thesis stale" exit. **Caveat:** stop-losses averaged −₹4,028 against −₹1,258 for
early cuts, but those are different cohorts, and an earlier attribution of the
"never progressed" rule's cost was itself wrong. This is a hypothesis the paper
run tests, not a result.

### 8. Volatility regime predicts how far price moves, not whether you profit
*Variables: previous session's realised vol vs its trailing median; ATM IV.*
Trailing realised vol separates *payable* days: top-vs-bottom quintile +26.6
points of windows clearing an IV-adjusted break-even (n = 236, halves +26.1 /
+30.4). But the market prices part of it (IV rose 1.34× across those quintiles
while the move rose 2.3×) and at trade level a fixed-risk buyer gains nothing for
free: random entries were *worse* in hot regimes (−0.102%) than cold (−0.076%).
As a continuous input `rv_iv` flips sign between halves (p = 0.0002); as a binary
filter it conflicts (taken trades say block high, blocked signals say 7 of 9 were
winners). Treat the regime as a filter on *when to look*, never as an edge.

### 9. Three correlated bots are one bet, and conviction scores are not conviction
*Variables: entry timestamps; `unified_score`; P&L by path.*
On Oct 21 the three indices entered within **34 seconds** and moved together.
`unified_score` is inverted (ρ = −0.174; its top half lost ₹14,747). The REV path
looks good (+₹400/trade, n = 50) but its 95% CI is [−₹282, +₹1,125] and **without
its best two days it is −₹3,867**. So: one index, one position at a time, size
from the book's risk and drawdown, never from a score.

### 10. The infrastructure failed more often than the strategy
*Variable: Fyers API request counts by consumer.*
The Fyers Standard plan allows **5,000 requests/day**. The parked system exhausted
it at 14:43 (Oct 19) and 13:05 (Oct 21) by polling three indices, three
strategies and every tracked phantom, then could not price an open position. In
one week I found and fixed: an expiry rule that skipped a listed contract, a
throttle guard that did not match Fyers' actual wording (so a rate limit was
misread as "contract not listed" and the wrong answer cached), a `None`-bound
import that logged ₹0 costs, and assertions that passed for any non-zero value.
Hence this design: **one series fetched once per closed bar, an option polled
only while holding it, a persisted daily budget with exits exempt, a preflight
that refuses to trade on bad inputs, and tests that compare values.**

---

## Addendum (2026-10-22): the other inputs to an option's price

`research/iv_study.py` (120 sessions with chain ATM IV, one-hour non-overlapping
windows) and `research/intraday_study.py` (420 sessions, in bps of spot).

* **IV moves against the index (the leverage effect) and the effect is lopsided.**
  BANKNIFTY: -0.68 vol points per +1% of index (rho -0.52, p~0); -1.24 on down
  moves, -0.34 on up moves. A -0.4% hour adds ~+2.1% of premium through vega; a
  +0.4% hour costs a call ~-2.0%. Real, but about one tenth of the delta effect
  (~+28% for a 0.4% move) and roughly 0.06% of spot for the put/call difference
  against ~0.08% of cost.
* **IV drifts down while you wait.** In flat hours (|move| < 0.15%) BANKNIFTY IV
  falls ~0.2 vol points an hour (-1.5% of premium), NIFTY ~0.4 (-3.8%), in every
  hour of the day, on top of theta. A hidden cost of any slow trade.
* **A cheap IV does not predict a rising IV** (rho -0.13, p=0.19 BNF; +0.08, p=0.44
  NIFTY). No "buy cheap vol" effect at a 2-hour horizon.
* **The morning does not predict the afternoon.** First 30 min -> last 30 min:
  +1.4 bps +/- 1.9 (p=0.33); gap -> rest of day: -0.6 bps. All under 3 bps.

