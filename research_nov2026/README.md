# Research scripts, Nov 2026 (Kronos futures)

Throwaway analysis scripts behind the Nov 9 BANKNIFTY-only config and the stock-futures study.
They run on the EC2 box (`/opt/kronos_bot/research`, hard-coded paths) against cached Kronos reads and
price data; none of them touch live state. Kept for reproducibility, not as a library.

- `bnf_*.py`: BANKNIFTY-only replays (trigger variants, gates/decay/exit/bar sensitivity, wrong-day and
  inverted-read controls, same-day re-entry). `bnf_only_test.py` is the scenario test for the live switches.
- `ledger_two_inputs.py`, `trend_baselines.py`: blocked-vs-passed ledger on Upstox vs yfinance input; trend baselines.
- `stk_*.py`, `n50_*`: F&O stock futures. Price-only entry indications on the 60 most liquid names and on the
  NIFTY 50 (set `STK_UNIV` to a JSON list of symbols), robustness checks, and Kronos reads / filter on breakout days.
