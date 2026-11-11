# Intraday toolkit (10-minute NIFTY / BANKNIFTY futures)

Built Nov 11 2026 as the data and economics foundation for a 10-minute futures strategy. No strategy yet; this is what a strategy stands on.

| Piece | What it does | Runs |
|---|---|---|
| `candle_recorder.py` | 1-minute and 10-minute bars, with volume and open interest for the futures, for both indices and the near / next / far futures. Cross-checks 10m against resampled 1m. | cron 16:00 IST (`--today`); `--backfill` for history |
| `depth_recorder.py` | One batched quote every 15 s: 5-level depth, total buy/sell quantity, volume, OI, average price (4 futures + 2 indices). Gzips the day at the end. Holiday-aware, lock-protected. | cron 09:10 IST, exits 15:45 IST |
| `build_dataset.py` | Validated, flagged 10-minute index dataset, 2022-01 -> today, cross-checked against Upstox daily bars. | cron Saturdays; manual |
| `costs.py` | Fee model (brokerage, STT, exchange, SEBI, stamp, GST + slippage), break-even maths, required predictive correlation by holding period and selectivity. | manual |
| `test_intraday.py` | 37 offline checks. | before every change |

Data lives outside the repo: `/opt/kronos_bot/data/intraday/` (layout in `common.py`). **It is on one instance disk: snapshot or copy it somewhere.**
Futures history only exists for contracts that are still listed (Upstox returns nothing for expired ones), so the recorder is the only way
to build a continuous futures history; the backfill started with Aug 26 2026 (the Nov contract's listing).

Load for research: `common.load_clean("NIFTY")` (usable bars only; `ret_clean_bp` is the safe return column, `gap_bp` the overnight gap on the first bar).

## What the data checks found (read before trusting any 10-minute index result)
1. **Cash trading is continuous only until 15:15 since 2026-08-03.** 15:15 -> ~15:28 is a call-auction window: no volume and no price movement in
   stocks or in the index built from them, then a single auction print (2-3% of the day's volume in one minute) that equals the official close
   exactly. Index 10-minute bars at 15:15 and 15:25 are flagged `frozen_close` and not usable (68 sessions so far). Futures trade normally through
   the window (15:15-15:40) and are the source of truth near the close.
2. The close is a different price from the 15:15 print: median 0.14-0.30% apart on HDFCBANK / RELIANCE / SBIN (max 0.5-2%), i.e. 1.5-3 sigma of a
   10-minute bar. "Decide on the close, fill at the close" is therefore optimistic; the honest test decides on 15:14 data and fills at the auction.
3. Sessions in Apr-May 2025 carry flat prints stamped 15:35 / 15:55 (`post_close`, dropped).
4. Weekend / evening special sessions (Muhurat, Budget Saturdays, DR drills) are flagged `weekend_special` / `special_hours`.
5. Daily cross-check: opens match exactly in all 1,190 sessions; a handful of old sessions have missing bars (highs/lows off); the daily close
   differs from the last 10-minute close only because of check 1/2.

## Economics (costs.py defaults; verify statutory rates against a contract note)
All-in round trip for one lot at current levels: NIFTY about Rs605 = 0.037% (fees 0.029% + 1 point slippage per side), BANKNIFTY about Rs698 = 0.039%.
One 10-minute bar has sigma 0.099% (NIFTY) / 0.126% (BANKNIFTY): cost is 0.38 / 0.31 of a bar's sigma. To break even holding one hour the signal needs a
correlation with the forward return of 0.19 / 0.16 if it trades every signal, 0.074 / 0.061 if it only trades its strongest 5%. Replace the slippage
placeholder with the spread measured by the depth recorder after a few weeks.

## Cron (EC2, UTC)
```
40 3 * * 1-5  cd /opt/kronos_bot/repo/futures_v2/intraday && /opt/kronos_bot/venv/bin/python depth_recorder.py >> /opt/kronos_bot/data/intraday/logs/depth_cron.log 2>&1
30 10 * * 1-5 cd /opt/kronos_bot/repo/futures_v2/intraday && /opt/kronos_bot/venv/bin/python candle_recorder.py --today >> /opt/kronos_bot/data/intraday/logs/candles_cron.log 2>&1
30 4 * * 6    cd /opt/kronos_bot/repo/futures_v2/intraday && /opt/kronos_bot/venv/bin/python build_dataset.py >> /opt/kronos_bot/data/intraday/logs/build_cron.log 2>&1
```
