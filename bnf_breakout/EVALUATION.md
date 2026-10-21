# How this strategy will be judged

Fixed on 2026-10-21, **before any forward result exists**. If the verdict rule is
decided after seeing results it is worthless, so this file is not edited to fit
what happens. A change to it is a new, dated section below, never a rewrite.

## What is being tested

*H0:* this strategy's net P&L per trade is no better than a random entry under the
same exits. *H1:* it is better, and positive.

The honest prior, from `LEARNINGS.md` and `research/`, is **H0**. In-sample replay
over 420 sessions gave the strategy −0.083% of spot per trade (n = 197, win 26%)
against −0.076% for random entry (10 seeds, range −0.088% to −0.057%); it beat 3 of
10 random seeds. The value of running it is a *clean forward verdict*, at ₹50,000
of paper risk and well under 1,000 API requests a day, not an expectation of profit.

## The sample

Replay frequency is ~0.47 trades per session (the entry gates are strict by
design). **n = 30 closed trades therefore takes roughly 64 sessions, about three
months.** That is the price of a verdict that means something. Interim reads:

| closed trades | what it may be used for |
|---|---|
| 1 – 9 | nothing about performance. Confirm the plumbing: fills at ask/bid, costs, logs reconcile, no API-budget alarm. |
| 10 – 29 | sanity only: are setups firing at roughly the replay rate; are exits the expected mix? No tuning. |
| 30 | **the verdict below** |
| 60 | second look if the verdict was inconclusive |

## The verdict at n = 30 closed trades

Computed on net rupee P&L per trade (after costs and the ask/bid fills), with a
bootstrap 95% confidence interval of the mean.

- **PASS** — the CI lower bound is above zero, **and** both chronological halves
  have a positive mean. Only then is the thesis worth taking seriously, and even
  then the next step is a larger paper run, not live money.
- **FAIL** — the mean is at or below zero, **or** the CI upper bound sits below the
  replay's random baseline. Retire the thesis. Do not respond by loosening gates.
- **INCONCLUSIVE** — anything else. Continue to n = 60 with no changes, then decide
  by the same rule.

A mean that is positive but whose CI includes zero is **not** a pass. The old
system's `rv_iv` finding looked like a p-value of 4.9e-77 and was really n = 234.

## Rules while it runs

1. **No config change before n = 30** except fixing a demonstrable bug. Each change
   resets the clock for what it touches.
2. After n = 30, at most **one change per 20 trades**, and only the change the
   diagnosis below points at.
3. A change that was suggested *by* the paper results must be checked on the
   replay's held-out period before it is adopted.
4. **Pause** if the book falls below ₹35,000 (−30%), and say so.

## Config problem or strategy problem?

That is why the two live in separate files. The decision log
(`bnf_decisions_<date>.jsonl`) records every bar's gate states, and the replay
(`python bnf_replay.py`) prints which gate is the *only* one failing most often.

| observation | it is a… | look at |
|---|---|---|
| far fewer setups per session than the replay's ~0.47 | **config** | the gate that is the only failure most often (replay: `coil`, `regime`, `volume`) |
| setups fire, but exits are mostly `thesis_stale` / `thesis_broken` | **strategy** | the thesis: price is not following through |
| setups fire, direction right, still losing | **config** | `HARD_STOP`, `TRAIL_ATR_MULT`, `THESIS_CONFIRM_*`, sizing |
| profitable only with `DIRECTION='away'` | **strategy** | research found pressing toward a level (−0.102%) worse than away (−0.062%); flip it only with the replay as evidence |
| good paper result, bad cost line | **config** | `SLIPPAGE_PCT`, entry timing, lots |

## What would change my mind

A PASS at n = 30 would overturn the conclusion of `LEARNINGS.md` #1–#3, and would
be surprising. A FAIL is the expected outcome and is a *useful* one: it closes this
family of ideas on a forward sample instead of an in-sample one.
