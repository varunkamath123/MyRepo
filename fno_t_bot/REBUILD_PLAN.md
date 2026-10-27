# FnO_T_Bot — Rebuild plan and pre-registration

**Written 2026-10-12. Committed before any new engine exists, deliberately.**

The point of writing this first is that the last cycle failed in a specific,
documentable way: build an engine, tune it, watch it degrade, tune it again.
Roughly fifteen parameter changes across forty-one trades. Every change had a
reason at the time. The book is −₹9,664 at confidence 20/100 with a second half
four times worse than the first.

So the rules below are not aspiration. They are the thing that has to be
different, and they are written down now so that a future session can check
whether they were honoured rather than take anyone's word for it.

---

## 1. What is actually established

Only results that survived real prices and a chronological split are listed.
Everything here is a constraint on the rebuild, not a suggestion.

### Direction is not predictable at this horizon

Twelve methods, none of which beat its own base rate by anything exploitable:

| method | sample | result |
|---|---|---|
| ORB (opening-range break) | 507 entries, ~510 sessions | 50–55% right, mean forward move **+0.000%**, halves flip sign |
| MACD histogram (sign, slope) | ~3,700 obs | 49–51%, captured +0.00–0.01% |
| OI drift / PCR drift | 642 obs | 47.4%, −0.025% |
| MaxPain pull | 666 obs | 49.7%, +0.010% |
| phantom-futures P&L sign | ~6,500 obs | 48–57%, captured ≈0.00%, halves flip |
| first break of a held level | 2,437 breaks | continuation 47–55%, payability **falls** after a break |
| volume behind a break | 1,875 obs | instruments disagree on sign; best bucket +0.003% |
| constituent breadth (top-10) | 897 obs | rho −0.012 (p=0.79), −0.037 (p=0.44) |
| weighted constituent breadth | 1,573 obs | rho +0.026 (p=0.295) |
| basket-vs-index divergence | 1,614 obs | rho +0.012 (p=0.63) on direction |
| S/R level quality (8 definitions) | 17,300 touches | **all** 47.9–54.7%; repeated rejection flat |
| the live engines themselves | REV n=27, TREND n=14 | p=0.663, p=0.412; **REV vs TREND p=0.989** |

Supporting: DSR 0.0% across 3,072 swept variants; random-entry baseline
p=0.857; attribute scan AUC 0.44–0.51.

**Constraint: no new engine may depend on calling direction.**

### Magnitude *is* predictable

The one thing with a large-sample, monotone, high-significance result:

```
rv_iv quintile   median best-case excursion   payable @0.51%
Q1 (0.17-0.42)            0.224%                   9.6%
Q3 (0.52-0.62)            0.353%                  28.4%
Q5 (0.76-1.79)            0.436%                  40.4%
rho(rv_iv, excursion) = +0.325   p = 2.6e-28   n = 1,090
rho(rv_iv, SIGNED move) = -0.091            <- direction: nothing
```

Second, weaker, independent magnitude signal: weighted top-10 basket vs index
divergence — payability 22.7% → 40.4% when |divergence| > 0.1%, rho +0.104,
p<0.001, n=1,614. Orthogonal input, so combinable.

**BLOCKER — must be resolved before anything is built on this.** The recorded
project finding behind `RV_IV_MAX` is rho **−0.2269** (n=6,529, p=4.9e-77) —
the opposite sign on what appears to be the same construction (`hv*100/iv`).
Both can be true if high rv_iv means a bigger index move *and* worse long-option
P&L because IV crushes while the move happens. That reconciliation is a
hypothesis, not a finding. **Phase 0 resolves it. Nothing is built on rv_iv
until it is resolved.**

### The payability ceiling

Even with a **perfect** directional oracle, most entries cannot reach
break-even, because the window does not deliver the move the premium charges
for:

```
                 median best-case excursion   payable @0.51% (perfect oracle)
NIFTY                     0.311%                      23.0%
BANKNIFTY                 0.359%                      31.0%
SENSEX                    0.321%                      23.2%
```

Break-even 0.51% is the median `req_move_pct` from this book's own 36
real-priced trades, not an assumption.

**Constraint: direction accuracy has a hard ceiling of ~25-31% payable
windows. Any design must attack the ceiling, not the read.**

Levers measured so far: a wider window (shipped — 09:45→15:00 lifted payability
to 29.1/36.7/28.9%), higher delta (ITM/ATM only, shipped), lower-break-even
structures (debit spreads — **untested**), or selling premium instead of buying.

### Costs decide which structures are even possible

```
Oct 6-12, phantom short iron fly, 8 structures over 4 sessions:
  gross theta captured   Rs0-5 per structure
  commission             Rs237-367 per structure
  net                    EVERY ONE negative
```

Four legs is eight orders per round trip. At one lot the fixed per-order
brokerage is 2–3× the decay collected over a five-hour hold at 8 DTE.

**Constraint: cost base must be verified at the intended lot size BEFORE a
structure is considered. This killed the iron fly on arithmetic, not on
thesis.** Capital was raised to ₹1L per index on 2026-10-12 specifically so
that four-leg structures become *measurable*; it does not make them profitable.

### Exits have a sharp discontinuity at +12%

```
peak reached      n    closed green        net
never green       9       0/9          -Rs14,300
0-5%              8       0/8           -Rs6,684
5-12%             5       2/5           -Rs5,386
12-25%            8       8/8           +Rs4,155
>25%              8       8/8          +Rs19,312
```

Every trade that ever reached +12% closed green — 16 of 16. Below it, 2 of 22.
42% of trades reach it; the other 58% lose ₹26,370 between them.

The trail arms at exactly +12%, so it locks in green rather than creating
losers. But its 10-percentage-point giveback is **~0.133% of spot** on an ATM
NIFTY contract against a median retracement of **0.28–0.31%** — it fires on
90–97% of entries in every ADX bucket (rho(ADX, retracement) = −0.06). It is
mis-sized by ~2.3×, not mis-timed, and no day-type signal predicts when it is
safe to loosen.

**Constraint: the exit rule is two rules. Above the proving threshold, exit on
thesis invalidation. Below it, exit fast.**

---

## 2. Dead ends — do not re-test without new data

Re-testing these is how months get spent. Each was falsified on real data at a
sample large enough to matter.

- Every direction method in the table above.
- `chase_pos` as a *cause* of TREND losses (97.5% of signals are >0.85 — it is a constant).
- Retrace depth (192/197 signals sit at 0–25%).
- OI-zone proximity / walls as a tradeable level (p=0.74 and 1.00).
- 17 exit-rule variants on real Breeze premiums — the live stack was already best.
- "Quiet before the move" as the rv_iv mechanism (price-only proxy, wrong sign).
- Long ATM straddle: EV **−38% of premium**, reproducing the recorded −41.6% by
  a different method. Conditioning on top-quintile rv_iv improves it to −21%
  and **does not flip the sign**.
- Intraday short iron fly at 1 lot (costs, above).
- MiroFish as a *trail* condition — it calls direction, and the trail fires
  regardless of direction.

---

## 3. What is reused, not rebuilt

The entry logic is worthless. These are not, and several cannot be recreated:

| asset | why it matters |
|---|---|
| **Premium archive** (`logs/premiums_*.jsonl`) | Real intraday option prices with bid/ask, OI, IV, futures basis. 6 sessions, 6.3 MB, growing. **Un-backfillable** — Fyers delists expired contracts. The single most valuable thing in the project. |
| Counterfactual book | The only reason we know what the gates cost rather than guessing. |
| Confidence scorer | Turns P&L into evidence. Prevents deploying on one lucky fortnight. |
| `PAPER_REQUIRE_REAL_LTP` | Refuses to book a modelled price. Four conclusions reversed when real prices arrived; this is why a fifth did not. |
| Complete 2026 index store | 413/409/409 sessions, gaps backfilled 2026-10-02. |
| Top-10 constituent store | 2,720 files. Did not exist before 2026-10-02. |
| Exit stack | Trailing Stop is the only net-positive exit reason (n=13, +₹12,267). |
| The falsification record above | A map of where not to dig. |

---

## 4. Phase plan

### Phase 0 — resolve blockers. No new engine, no parameter changes.

1. **Resolve the rv_iv sign discrepancy.** Reconcile +0.325 against the
   recorded −0.2269: same construction, same window, same outcome definition.
   The entire magnitude thesis depends on which is right.
2. **Let the archive reach ≥30 sessions** (~6 weeks from 2026-10-12).
3. **Keep REV/TREND running in paper, unchanged.** They are free real-priced
   observations and there is no capital at risk. **Make no parameter changes
   to them.** Their job now is to keep producing counterfactual data, not to
   make money.
4. **MiroFish archive accumulates** (n=3 as of writing; needs ~30).

### Phase 1 — determine on real data whether any structure clears its costs

Using the archive, for every candidate structure, compute P&L on **real
quotes including bid/ask and full commission**, at the intended lot size,
conditioned on magnitude quintile:

- long ATM straddle (2 legs)
- long debit spread (2 legs) — the one long-side structure never tested, and
  the only one that *lowers* the break-even move
- short credit spread (2 legs)
- short iron fly / condor (4 legs) — now measurable at ₹1L

**Phase 1 kill criterion, pre-registered:** if no structure shows positive
expectancy after real spread and commission across ≥30 sessions, the honest
conclusion is that this instrument/horizon/capital combination does not
support a retail edge, and that gets written down rather than tuned around.

### Phase 2 — build only what Phase 1 identifies

No engine is designed before Phase 1 reports. Whatever it is, it must satisfy
every constraint in section 1 and carry its own pre-registration in this file.

---

## 5. Pre-registration — the process rules

These bind the rebuild. A future session should check them, not trust them.

1. **No parameter change without n ≥ 30 on the specific cohort the change
   affects.** The Oct 5 `PATH_REV_MAX_CHASE` raise was shipped on n=8. It may
   or may not have contributed to the four REV losses that followed — and that
   is the point: it is unknowable.
2. **Any revert trigger must name a field verified to be logged on the passing
   path, not only the blocked path.** The Oct 5 trigger was unenforceable
   because `rev_chase` was recorded only when it blocked. Fixed 2026-10-12.
3. **Maximum one parameter change per 20 trades.** Fifteen changes across
   forty-one trades is why nothing is attributable.
4. **No capital deployed below confidence 60/100.** Currently 20. Note that 9
   of 100 points are permanently unreachable from the 3,072 variants already
   swept, so the realistic ceiling is 91.
5. **Every claim priced on real traded premiums. Never modelled.** The record:
   BS mis-prices real premiums here by 41–123%; a plausible, internally
   consistent model was wrong by 6.7× on the one day it could be checked
   (+₹1,483 modelled vs +₹221 actual); and a short-premium EV table built on an
   expiry-payoff assumption was wrong by ~9× against the first live session.
6. **Replay beats shadow whenever the signal is computable from stored bars.**
   Shadowing ORB would have spent three months accumulating 60 observations;
   replaying it produced 507 in one run and settled it.
7. **State the sample size and the chronological split with every result.** An
   effect that flips sign between halves is noise regardless of its p-value.

---

## 6. Standing open items

- **Breeze session token** — expired 2026-06-15; needs the user's ICICI login.
  It is the only route to pricing the 41 historical trades for real. Not on the
  critical path any more (the archive handles everything forward) but it would
  retire the BS-priced conclusions still sitting live in `config.py`.
- **`RV_IV_GATE_SRC`** still `('chain',)`. VIX-sourced trades are net positive
  (+₹152, n=15) against chain-sourced −₹6,017; extending the gate was checked
  and rejected on n=5. Revisit at n≥8.
- **`DYN_MAX_LOTS_LADDER`** tops out at 4 lots and **`REGIME_CHOPPY_LOTS_CAP`**
  forces 1 lot in CHOPPY regime, which most recent entries logged. Both will
  blunt the ₹1L allocation. Left alone deliberately — raising them is a
  separate, argued risk decision.
- **Trail distance** is mis-sized by ~2.3× against median retracement.
  Candidate fix is a proportional giveback rather than a flat 10 points.
  Blocked on Phase 1 archive data.


## RESOLVED Oct 15 2026 -- the rv_iv sign discrepancy

Phase 0 was blocked on this: one measurement gave rho **+0.325**, the recorded
project finding gave **-0.2269**. Both are correct. The quantity is not stable.

Sample: n=62 real-priced trades, 30 sessions, 2026-07-16 to 2026-10-12. The
four Sep 24-28 NIFTY/SENSEX records are excluded (broken October symbol code
silently fell back to Black-Scholes pricing, measured 41-123% error). Removing
them changes nothing material, which is worth stating -- the contamination was
not the explanation.

### The continuous relationship is TIME-UNSTABLE, and that is measured

Statistic: |rho(1st chronological half) - rho(2nd half)|, 20,000 shuffles of
trade order under H0 that order carries no information.

    cohort        1st half    2nd half     gap     permutation p
    chain IV        -0.667      +0.439     1.106      0.0002      <- real
    all             -0.394      +0.152     0.546      0.0232
    VIX fallback    -0.154      +0.313     0.467      0.2368      <- noise

Pooled rho is -0.172 at p=0.1768: nothing. The sign inversion is not sampling
noise, it is a genuine change over the sample. DO NOT use rv_iv as a continuous
input -- not in sizing, not in conviction, not as a scorer component. That is
exactly the form that inverts.

A third of the observations used the VIX fallback as IV, which carries no
relationship whatsoever (rho +0.032, p=0.8797). Averaging those in is part of
how the pooled number came out ambiguous. RV_IV_GATE_SRC = ('chain',) is
already correct and must stay.

### The BINARY gate at 0.70 survives both halves

    cohort           n   mean P&L   win%    blocked-side halves
    rv_iv >= 0.70   25     -Rs886    28%    -Rs17,880 / -Rs4,260
    rv_iv <  0.70   37     +Rs250    54%

Both chronological halves of the blocked side are negative. By process rule 2
(a rule earns its place only by beating baseline in BOTH halves) the binary
gate PASSES and stays live.

### But its headline benefit is threshold luck, not edge

    block at   keeps  total        block at   keeps  total
    0.55        26   +Rs3,598      0.80        48    -Rs583
    0.60        28     -Rs467      0.85        49    -Rs380
    0.65        32     -Rs113      0.90        54  -Rs9,620
    0.70        37   +Rs9,249      0.95        56  -Rs7,489
    0.75        43     +Rs670      1.00        57  -Rs6,394

A monotone edge improves smoothly as the threshold tightens. This peaks at
exactly the fitted value and collapses either side, and the quintile buckets
alternate sign below 0.69 (+457, -539, +716, then -682, -879). The honest
expectation from the gate is about +Rs250/trade, not +Rs9,249. Anyone quoting
the Rs9k number is quoting an artifact.

### What this does to the research program

"rv_iv is the ONE validated edge" is now too strong and should not be repeated.
It is a validated **filter**: it says when NOT to buy. It never says what to
buy, and it cannot be scaled on. Its surviving effect is +Rs250/trade across 37
trades, which is consistent with the payability ceiling rather than a way
around it -- rv_iv lets the book skip some unpayable days, it does not create
payable ones.

Phase 0 therefore closes with no validated directional OR magnitude edge. The
magnitude thesis cannot be built on rv_iv as a continuous signal.

### Logging gaps this exposed, for the record

  * rv_iv was only written to its own log when it BLOCKED, so the local sample
    was truncated at >= 0.70 -- min 0.700, max 0.833 across 54 rows. A
    correlation measured on a sample cut at the gate is meaningless. Same shape
    as the rev_chase defect found Oct 13. Trade records carry it unconditionally,
    which is why the real sample had to come off EC2.
  * Trade records carry NO price-source field at all, so the log cannot say
    whether a fill was real or Black-Scholes. The Sep 24-28 exclusion had to be
    reconstructed from knowledge of the symbol bug rather than read from data.
    Add px_src to the trade record.

## RESOLVED Oct 15 2026 -- the forward-move claim, and what replaced it

### The recorded rv_iv forward-move finding has the WRONG SIGN

Recorded: "low rv_iv precedes LARGER forward moves, n=6,529, rho -0.2269,
p=4.9e-77, monotone across quintiles." Re-measured from oi_intraday.jsonl
(17,578 stamps, 236 instrument-sessions, 2026-04-28 to 2026-10-15), dependent
variable = forward max excursion as % of spot, which is what an option buyer is
actually paid for:

    basis                              n       rho        p
    overlapping (as recorded)        8,144   +0.2302    ~0
      NIFTY alone                    4,074   +0.2422    ~0
      BANKNIFTY alone                4,070   +0.2052    ~0
    NON-overlapping (1 per session)    234   +0.0867    0.185
      permutation test                 234   +0.0867    0.190

Same magnitude as the recorded number, OPPOSITE sign, on both instruments
independently. And the p=4.9e-77 was never real: ivinfo.py says so in its own
header ("Overlapping windows -> p optimistic"). Adjacent 5-minute bars share
almost their whole forward window, so the true independent n is 234, not 6,529.
At n=234 there is no significant relationship.

The specific claim was MONOTONICITY across quintiles. It is not monotone on
either basis -- independent-sample medians run 0.126, 0.179, 0.205, 0.201,
0.147: up then down.

Joined against the bot's OWN logged rv_iv (the gate's actual scale, 0.229-1.256,
rather than a reconstruction): no relationship at any horizon -- 30min rho
+0.297 p=0.099, 60min +0.045 p=0.811, 120min -0.159 p=0.394, 240min +0.070
p=0.712. The 30-minute sign is POSITIVE, opposite to the gate's premise. n=30
and selected on sessions where a trade was taken, so indicative only.

CONCLUSION. rv_iv is not a magnitude edge. It survives only as a binary P&L
filter at 0.70 (both chronological halves negative on the blocked side, win 28%
vs 54%, n=25/37) and must never be used as a continuous input. See the previous
section.

### What replaced it: payability is a function of HOLDING TIME

n=234 independent instrument-sessions, one observation each, entry ~45 minutes
into the session, forward windows non-overlapping by construction. An ATM entry
needs about 0.51% of spot round trip:

    runway    median exc   mean     p90      CAN reach break-even
     30min      0.139%    0.164%   0.283%        2.1%
     60min      0.196%    0.225%   0.387%        4.3%
    120min      0.259%    0.325%   0.586%       16.2%
    180min      0.336%    0.421%   0.844%       27.8%
    240min      0.404%    0.497%   0.909%       37.2%
    300min      0.471%    0.575%   1.027%       44.9%

This is the payability ceiling restated as something ACTIONABLE. It is not a
signal hypothesis and needs no edge to justify -- it is arithmetic about how far
the index travels in a given time.

Live config is entry_start 09:45, entry_end 14:00, FORCE_CLOSE_TIME 15:00. So
the engine is permitted to open a position with 60 minutes of runway, which has
a 4.3% chance of the market moving far enough to break even BEFORE direction is
considered. The same engine entering at 09:45 gets ~45%. A 10x swing in whether
the trade is arithmetically capable of winning, decided purely by the clock.

Already corroborated inside the book: Jul 16 2026, both Path B PUTs entered
13:40-45 with 45-50 minutes of runway, both force-closed at a loss (-Rs1,033,
-Rs1,479), move_cover 0.40 and 0.45 against the Jul 13 winner's 1.42. The
instrumentation for this (move_cover, runway_min, v1.7.4) was built in July and
the gate was never armed.

Even a full-session hold is marginal at the median: 300 minutes gives 0.471%
against a 0.51% requirement. For a 60-minute hold to pay in 35-50% of windows,
break-even would have to fall to 0.195-0.243% of spot, i.e. 2.1-2.6x cheaper
than an ATM long. That is the quantitative case for debit spreads over naked
long premium, and it is the first time that case has a number attached.

### FALSIFIED THE SAME DAY, by its own pre-registered test

The runway gate below was checked against the live book before being proposed
seriously, and the book rejected it. P&L by entry runway, 63 trades with a
usable runway_min, Sep 24-28 NIFTY/SENSEX excluded:

    runway        n     total      mean    win%
     60-120min   16     -Rs226      -14     44%
    120-180min   19  +Rs10,498     +553     68%   <- the only profitable band
    180-240min   15  -Rs17,022   -1,135     20%   <- the worst band
    240min+      13   -Rs5,577     -429     31%

    require runway >= 180min -> KEEP n=28 -Rs22,599, REMOVE n=35 +Rs10,272
    require runway >= 240min -> KEEP n=13  -Rs5,577, REMOVE n=50  -Rs6,750

More runway is WORSE, which is the opposite of what the excursion table
predicts. The gate would have removed the profit and kept the losses. Do not
ship it.

WHY THE ARITHMETIC DID NOT TRANSFER, which is the useful part: 0.51% of spot is
the break-even for holding an ATM option to a FLAT exit. This bot does not do
that. It exits on a 25% premium stop, a 55% target, or an 18% trail from peak. A
25% premium move on an ATM option needs only about 0.33% of spot, and a trailed
exit can bank considerably less. So realised P&L is decoupled from the
hold-to-flat excursion the table measures, and the table is the wrong
denominator for THIS strategy even though it is correct as arithmetic.

The 120-180min band is an observation, NOT a rule: n=19 fails the n>=30 bar, and
it is one of five bands tried on 63 trades. It is written here so it is not
re-mined and mistaken for a finding.

WHERE THIS POINTS INSTEAD. If exits decouple P&L from excursion, then the exit
parameters -- not entry selection -- are what set whether a trade can pay. The
question to ask next is the stop/target asymmetry: what index move does each
leg of 25% stop / 55% target / 18% trail actually require, and how often is each
reached. That is measurable on data already held.

### SUPERSEDED pre-registration, kept for the record

  Change     gate entries on RUNWAY, not on clock time: require
             runway_min >= R at entry, with R chosen from the table above
  Rationale  arithmetic, not a predicted edge. No signal claim is being made.
  Candidate  R = 180 (27.8% payable) or R = 240 (37.2%), against 4.3% at 60min
  Cost       with FORCE_CLOSE_TIME 15:00, R=240 implies entry_end ~11:00, which
             removes the afternoon entries entirely. State the trade honestly:
             this cuts trade count hard. Count the removed trades' realised P&L
             before arming it.
  Falsifies  if removed-window trades were net POSITIVE in the live book, this
             is wrong and must not ship. Check that first.
  Measure    research/payability_horizon.py, research/rv_iv_forward.py

