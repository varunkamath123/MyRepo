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
