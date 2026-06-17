# Daily Review Format — FnO_T_Bot

Triggered when user types: "Today's Answers"

Answer all 5 questions using EC2 bot logs + Fyers API. Always end with P&L and current Fyers balance.

---

## Q1: "what were the rules that governed the market today ?"
What the market actually did — not what the bot did. The objective conditions:
- Regime at open (CHOPPY / TRENDING / CAUTIOUS), GIFT Nifty direction, opening gap
- OR range established (high / low / width), which side had momentum
- ADX trajectory through the day (building / waning / flat), DI spread
- 15m ST direction and whether it held or flipped
- PCR, MaxPain gravity, IVSkew — what the OI structure was signalling
- Key price levels that acted as support/resistance
- Summary: was there a tradeable move today? Which direction? When did it develop?

## Q2: "was I able to predict, capitalize and profit from it ? (assuming potential to do so existed in the market)"
Bot performance against the market opportunity:
- Did the pre-market bias (GIFT / regime / PCR) correctly predict direction?
- Did a signal fire? Which path (PATH-A ORB / PATH-REV / none)?
- If signal fired: did it capture the move, or did it enter too early / too late / wrong direction?
- If no signal fired: was there a valid move the bot structurally could not capture (entry window missed, OR not broken, ADX too low)?
- P&L for the day (Rs) — win / loss / no trade
- Honest verdict: capitalised / partially capitalised / missed / no opportunity

## Q3: "what can I do to improve (if any)"
Gap analysis — specific and actionable:
- What gate or condition, if different, would have changed the outcome?
- Any signal that was present but ignored (e.g. 15m ST opposing, htf_align 0/20)?
- Any signal that was absent but would have helped (e.g. OI CONFIRM required)?
- Candidate rule to investigate or backtest — one concrete thing
- If no trade: what would have needed to be true for a valid entry to exist?

## Q4: "what would a 'perfect model' have achieved today ?"
Theoretical best outcome given today's data — no hindsight on direction, only on execution:
- Entry: optimal path, direction, time, strike
- Exit: optimal exit method (target / trail / EOD), time, price
- Perfect P&L (Rs) — what was achievable on this day
- Gap between perfect P&L and actual P&L — and why

## Q5: "do I cover all the necessary data points to capitalize on such market moves the next time?"
System completeness check:
- Were all relevant signals available to the bot at entry time? (ADX, OI, VWAP, ST, PCR)
- Was any data missing, stale, or not yet integrated (e.g. VIX, broader market, sector)?
- Is the entry window, OR bars, or checkpoint timing correct for this type of day?
- Is there a structural gap in the bot — a move type it cannot capture by design?
- One yes/no verdict: system is complete for this move type, or needs addition X

---

## Always append at end of every "Today's Answers"
- **P&L today**: Rs X (win / loss / no trade)
- **Fyers balance**: run capital_status.py --fyers → report ground truth balance + capital gate status
