"""
FnO_T_Bot — Futures & Options Trading Bot
Instruments: NIFTY, BANKNIFTY and SENSEX (options only)
Strategy  : Trend Momentum — EMA 9/21 crossover + ADX > 25 + VWAP filter
            Per-instrument windows/ADX thresholds from config.INSTRUMENT_STRATEGY (vX)
Mode       : Paper (PAPER_TRADE_MODE=True) or Live (PAPER_TRADE_MODE=False)

Usage:
  python options_bot.py NIFTY       # trade NIFTY options
  python options_bot.py BANKNIFTY   # trade BANKNIFTY options
  python options_bot.py SENSEX      # trade SENSEX options (BSE)
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytz
from scipy.stats import norm
from ta.trend import ADXIndicator, EMAIndicator
from ta.momentum import RSIIndicator

import config
import shared_state
import sgx_nifty
import signal_scorer
import post11_scorer
import unified_scorer
import reversal_scout
import breakout_scout
import max_pain_trap
import near_miss_tracker
import trade_probability
from fyers_auth import FyersAuth

# ── Market regime (optional — graceful fallback if module not found) ──────────
try:
    from market_regime import RegimeAnalyzer as _RegimeAnalyzer
    _REGIME_AVAIL = True
except ImportError:
    _REGIME_AVAIL = False
from reversal_guard import compute_reversal_risk
from exit_scorer import compute_exit_score as _exit_score
from oi_zones import load_zones, get_zone_signal, describe_zones, zones_age_description
from nse_holidays import (
    is_market_open_today,
    is_within_market_hours,
    is_valid_entry_window,
    market_status,
)

IST = pytz.timezone('Asia/Kolkata')

# Path F entry window cap: OTM options (2 strikes out) need ≥90 min runway before
# force-close at 14:30. Cap entries at 13:00 regardless of instrument's vX end time.
_PATH_F_END = dtime(*map(int, config.PATH_F_ENTRY_END.split(':')))


# ─── Transaction cost helper ─────────────────────────────────────────────────

def round_trip_costs(entry_price: float, exit_price: float,
                     lot_size: int) -> float:
    """Accurate NSE options round-trip costs for 1 lot."""
    buy_val  = entry_price * lot_size
    sell_val = exit_price  * lot_size

    brokerage = config.BROKERAGE_PER_ORDER * 2
    exchange  = (buy_val + sell_val) * config.NSE_EXCHANGE_CHARGE_RATE
    sebi      = (buy_val + sell_val) * config.SEBI_CHARGES_RATE
    gst       = (brokerage + exchange + sebi) * config.GST_RATE
    stt       = sell_val * config.STT_RATE
    stamp     = buy_val  * config.STAMP_DUTY_RATE

    return brokerage + exchange + sebi + gst + stt + stamp


# ─── Black-Scholes (paper mode pricing) ─────────────────────────────────────

def bs_price(option_type: str, S: float, K: float,
             T: float, sigma: float) -> float:
    r = config.RISK_FREE_RATE
    if T <= 0:
        return max(S - K, 0.0) if option_type == 'CALL' else max(K - S, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if option_type == 'CALL':
        return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))
    return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))


# ─── Bot ─────────────────────────────────────────────────────────────────────

class TradingBot:

    def __init__(self, instrument: str):
        assert instrument in config.INSTRUMENTS, \
            f"Unknown instrument '{instrument}'. Choose: {list(config.INSTRUMENTS)}"

        self.instrument  = instrument
        self.inst_cfg    = config.INSTRUMENTS[instrument]
        self.lot_size    = self.inst_cfg['lot_size']
        self.strike_gap  = self.inst_cfg['strike_gap']
        self.capital     = float(self.inst_cfg['capital'])
        # Per-instrument live_mode: determined by capital-gated threshold logic.
        # capital_gate.resolve_live_mode() reads JSONL P&L at startup, compares
        # against CAPITAL_GATE_BNF_LIVE / CAPITAL_GATE_SENSEX_LIVE thresholds,
        # and returns True/False.  NIFTY is always live (anchor instrument).
        # If CAPITAL_GATE_ENABLED=False, falls back to manual live_mode flag.
        # Global PAPER_TRADE_MODE=True overrides everything → paper regardless.
        try:
            from capital_gate import resolve_live_mode as _resolve_live
            _inst_live = _resolve_live(instrument)
        except Exception as _cg_err:
            # Graceful fallback: use manual flag if capital_gate unavailable
            _inst_live = config.INSTRUMENT_STRATEGY.get(
                instrument, {}).get('live_mode', False)
            logging.getLogger(__name__).warning(
                f'[CAPITAL-GATE] fallback to manual live_mode ({_inst_live}): {_cg_err}'
            )
        self.live = (not config.PAPER_TRADE_MODE) and _inst_live

        self.positions    = []
        self.trade_log    = []
        self._persisted_trade_keys: set = set()   # trades already written to JSONL
        self.trades_today = 0
        self.daily_pnl    = 0.0
        self.total_pnl    = 0.0
        self.current_date = None
        self.fyers        = None
        self.logger       = self._setup_logger()

        # ── Per-instrument strategy overrides (vX: config.INSTRUMENT_STRATEGY) ──
        _strat = config.INSTRUMENT_STRATEGY.get(instrument, {})
        self.call_adx_min  = _strat.get('call_adx_min',  config.MOMENTUM_ADX_THRESHOLD)
        self.put_adx_min   = _strat.get('put_adx_min',   config.MOMENTUM_ADX_THRESHOLD)
        self.skip_tuesday  = _strat.get('skip_tuesday',  False)
        self.skip_thursday = _strat.get('skip_thursday', False)
        # Tuesday CALL: elevated ADX + DI-spread gate (replaces blanket block)
        self.tuesday_call_adx_min   = _strat.get('tuesday_call_adx_min',  self.call_adx_min)
        self.tuesday_call_di_spread = _strat.get('tuesday_call_di_spread', 0)
        # Tuesday PUT: raised ADX + DI-spread gate (standard PUT filters are too lax on Tue)
        self.tuesday_put_adx_min   = _strat.get('tuesday_put_adx_min',   self.put_adx_min)
        self.tuesday_put_di_spread = _strat.get('tuesday_put_di_spread', 0)
        _es = _strat.get('entry_start', '11:00')
        _ee = _strat.get('entry_end',   '14:45')
        self.entry_start   = dtime(*map(int, _es.split(':')))
        self.entry_end     = dtime(*map(int, _ee.split(':')))
        self._last_scan_ts     = None   # throttle scan logs to once per 5-min bar
        self._bot_id           = f'MAIN_{instrument}'   # shared_state key
        # ── Regime + Quality state ─────────────────────────────────────────
        self._regime               = 'MIXED'   # TRENDING / CHOPPY / MIXED
        self._regime_date          = None      # date when regime was last detected
        self._quality_state        = 'NORMAL'  # NORMAL / REDUCED
        self._quality_consecutive_wins = 0

        # ── Challenger (shadow OI-guided strategy) state ──────────────────────
        # Champion  = current live strategy (ATM strike, vX fresh-crossover only)
        # Challenger = same signal, OI-guided strike; no real orders; logs shadow P&L
        self.challenger_positions    = []
        self.challenger_trade_log    = []
        self.challenger_daily_pnl    = 0.0
        self.challenger_total_pnl    = 0.0
        self.challenger_trades_today = 0

        # ── OI Zones (pre-session support/resistance context) ─────────────────
        # Loaded from data/oi_zones/latest_{instrument}.json (written by
        # oi_zones_eod.py at 15:35 IST every trading day).
        # Used to weight entry decisions:
        #   BOOST  → price just broke through a major OI wall → +1 lot
        #   TAKE   → price in clear OI space → standard sizing
        #   REDUCE → price approaching a significant wall → cap to 1 lot
        #   SKIP   → price hugging adverse wall, tight box, or max-pain opposing → block
        self._oi_zones = load_zones(instrument, max_age_days=3)
        if self._oi_zones:
            # Logger not set up yet — print to stdout; logger picks up on first run()
            print(f"  [OI] {instrument}: {describe_zones(self._oi_zones)}")
        else:
            print(f"  [OI] {instrument}: No OI zones found — run oi_zones_eod.py after market close")

        # ── OI intraday snapshot log (PCR drift analysis) ────────────────────
        # nse_oi.py buffers every fresh NSE fetch in memory (≤5-min cadence).
        # We also persist to JSONL so the data survives restarts / Friday runs.
        # Only NIFTY and BANKNIFTY contribute; SENSEX (BSE) is excluded by nse_oi.
        try:
            import nse_oi as _nse_oi_init
            _oi_hist = os.path.join(config.LOG_DIRECTORY, 'oi_intraday.jsonl')
            _nse_oi_init.set_history_path(_oi_hist)
        except Exception:
            pass   # non-fatal — snapshot log is for analysis only

        # MaxPain Trap: set trade log path so weekly_analyzer can learn per-instrument
        try:
            _mp_log = os.path.join(config.LOG_DIRECTORY, 'mp_trap_learnings.jsonl')
            max_pain_trap.set_log_path(_mp_log)
        except Exception:
            pass   # non-fatal

        # ── Near-miss tracker ─────────────────────────────────────────────────
        # On no-trade days, records the bar that came closest to firing a signal.
        # "Closest" = highest ADX seen during the entry window.
        self._nm_best_adx  = 0.0   # peak ADX seen in entry window today
        self._nm_best      = {}    # snapshot of that best bar
        self._nm_pending   = []    # near-miss events awaiting +30/60/90 min outcome
        self._path_e_fired = False  # Path E (HTF Grind): one trade per day
        self._path_b_fired    = False  # MRB (Path B): once per day per instrument

        # ── Path A (ORB) state — reset daily ─────────────────────────────────────
        self._last_bar        = None    # Last processed 5-min indicator bar (used by exit scorer)
        self._or_high         = None    # Opening Range high
        self._or_low          = None    # Opening Range low
        self._or_ready        = False   # True once OR is computed (after 09:30)
        self._or_width_ok     = True    # True if OR width passes per-day gate
        self._gap_type        = None    # 'GAP_FADE_UP' / 'GAP_AND_GO_UP' / etc.
        self._gap_prev_close  = None    # previous session close (gap-rev recovery calc)
        self._gap_open_price  = None    # today's open price   (gap-rev recovery calc)
        self._gap_rev_dir     = None    # 'CALL' (fade-dn) / 'PUT' (fade-up) / None
        self._path_a_fired             = False  # ORB: once per day
        self._path_a_reentry_available = False  # True after PATH-A stop-loss → allows re-entry
        self._dynamic_or               = False  # True when fallback Dynamic OR is active
        self._daily_regime             = None   # RegimeSnapshot (computed at day start)

        # ── PATH_REV (MaxPain Snap Reversal) state — reset daily ──────────────
        self._morning_dir       = None   # 'PUT' or 'CALL' based on morning DI dominance
        self._morning_adx_peak  = 0.0   # peak ADX seen during ORB window
        self._morning_di_peak   = 0.0   # peak |DI+ − DI−| during ORB window
        self._path_rev_fired    = False  # one PATH_REV entry per day
        self._ivskew_hist       = []    # [(ts_float, ivskew), …] for 30-min drift
        self._skip_bnf_today    = False  # set daily: Monday before BNF monthly expiry
        self._st15m             = None   # cached 15m SuperTrend (+1/-1) from get_htf_context

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_logger(self) -> logging.Logger:
        os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
        log_file = os.path.join(
            config.LOG_DIRECTORY,
            f"{config.BOT_NAME}_{self.instrument}_{datetime.now(IST).strftime('%Y%m%d')}.log"
        )
        name = f"{config.BOT_NAME}_{self.instrument}"
        logger = logging.getLogger(name)
        if not logger.handlers:
            fmt = '%(asctime)s [%(levelname)s] %(message)s'
            logger.setLevel(logging.INFO)
            logger.addHandler(logging.FileHandler(log_file, encoding='utf-8'))
            logger.addHandler(logging.StreamHandler())
            for h in logger.handlers:
                h.setFormatter(logging.Formatter(fmt))
        return logger

    def connect(self) -> bool:
        """Load saved Fyers token (same-day) or exit with instructions."""
        token_file = os.path.join(config.LOG_DIRECTORY, 'token.txt')
        if os.path.exists(token_file):
            with open(token_file) as f:
                lines = f.read().strip().split('\n')
            if len(lines) >= 2:
                try:
                    token_date = datetime.fromisoformat(lines[1]).date()
                    if token_date == datetime.now(IST).date():
                        auth = FyersAuth()
                        auth.access_token = lines[0]
                        self.fyers = auth.get_fyers_client()
                        if auth.test_connection():
                            mode = "LIVE" if self.live else "PAPER"
                            self.logger.info(
                                f"Fyers connected [{mode} MODE]. Token: today."
                            )
                            return True
                except (ValueError, IndexError):
                    pass

        self.logger.error(
            "No valid Fyers token.\n"
            "  Manual  : python fyers_auth.py\n"
            "  Auto    : python fyers_auto_auth.py"
        )
        return False

    def _recover_positions(self) -> None:
        """
        On startup, query Fyers for any open positions belonging to this instrument.
        Re-registers them into self.positions and shared_state so the bot can manage
        them correctly after a mid-session restart.

        Prevents orphaned positions when services are restarted during market hours.
        Only runs in LIVE mode — paper mode has no real positions to recover.

        SAFETY GUARD: Only recovers if current time is within this bot's own entry
        window. Before the window open, any open positions must have been placed by
        the early_bot — the main bot must NOT take ownership of those.
        """
        if not self.live or self.fyers is None:
            return
        now = datetime.now(IST)
        now_t = now.time()
        # Only recover if we're within or past our unified entry window (ORB starts at 09:30).
        # The unified bot owns positions from 09:30 onward (Path A ORB + main session).
        _unified_start = dtime(9, 30)
        if now_t < _unified_start:
            self.logger.info(
                f"[RECOVER] Skipping — time {now_t.strftime('%H:%M')} is before "
                f"unified entry window 09:30. No positions should exist yet."
            )
            return
        try:
            import re as _re
            resp = self.fyers.positions()
            if resp.get('s') != 'ok':
                self.logger.warning(f"[RECOVER] Fyers positions() failed: {resp}")
                return
            prefix    = self.inst_cfg.get('option_prefix', '')
            recovered = 0
            for p in resp.get('netPositions', []):
                sym = p.get('symbol', '')
                qty = p.get('netQty', 0)
                if not sym.startswith(prefix) or qty == 0:
                    continue
                opt_type  = 'CALL' if sym.endswith('CE') else 'PUT'
                avg_price = float(p.get('netAvg', 0))
                ltp       = float(p.get('ltp', avg_price))
                m         = _re.search(r'(\d+)(CE|PE)$', sym)
                strike    = int(m.group(1)) if m else 0
                cur_pnl_pct = ((ltp - avg_price) / avg_price) if avg_price > 0 else 0.0
                position = {
                    'instrument'       : self.instrument,
                    'type'             : opt_type,
                    'entry_time'       : datetime.now(IST),
                    'entry_price'      : avg_price,
                    'entry_underlying' : 0.0,   # unknown at recovery time
                    'strike'           : strike,
                    'option_symbol'    : sym,
                    'lot_size'         : abs(qty),
                    'hv_at_entry'      : 0.18,  # fallback HV; trail/SL still works
                    'highest_pnl_pct'  : max(0.0, cur_pnl_pct),
                }
                self.positions.append(position)
                self.trades_today += 1
                shared_state.register_position(
                    self.instrument, self._bot_id, opt_type, self.logger
                )
                recovered += 1
                self.logger.info(
                    f"[RECOVER] Re-registered open position: {sym} | "
                    f"avg=₹{avg_price:.2f} | ltp=₹{ltp:.2f} | qty={qty}"
                )
            if recovered == 0:
                self.logger.info(f"[RECOVER] No open {self.instrument} positions to recover.")
        except Exception as e:
            self.logger.error(f"[RECOVER] Position recovery failed: {e}")

    # ── Market Data ───────────────────────────────────────────────────────────

    def _get_futures_symbol(self) -> str:
        """Derive current-month index futures symbol for Fyers API.

        Format: NSE:NIFTY25MAYFUT / NSE:BANKNIFTY25MAYFUT / BSE:SENSEX25MAYFUT
        Rolls to next month once we're past the last Thursday of the current month
        (NIFTY/BNF monthly expiry day).
        """
        import calendar as _cal
        _now = datetime.now(IST)
        yr, mo = _now.year, _now.month
        # Last Thursday of current month
        _last_thu = max(
            d for d in range(28, 32)
            if d <= _cal.monthrange(yr, mo)[1]
            and datetime(yr, mo, d).weekday() == 3
        )
        if _now.day > _last_thu:          # past expiry → roll to next month
            mo += 1
            if mo > 12:
                mo, yr = 1, yr + 1
        _mon = ['JAN','FEB','MAR','APR','MAY','JUN',
                'JUL','AUG','SEP','OCT','NOV','DEC'][mo - 1]
        _yy  = str(yr)[2:]
        _pfx = {'NIFTY': 'NSE:NIFTY', 'BANKNIFTY': 'NSE:BANKNIFTY',
                'SENSEX': 'BSE:SENSEX'}.get(self.instrument, 'NSE:NIFTY')
        return f"{_pfx}{_yy}{_mon}FUT"

    def get_index_data(self) -> pd.DataFrame | None:
        """Fetch 5-min candles: last 3 calendar days + today for EMA warm-up context."""
        try:
            now       = datetime.now(IST)
            today     = now.date()
            # Pull 7 calendar days so EMA_slow(21) is fully warmed up at session open,
            # even after long weekends or market holidays (e.g. Good Friday + weekend = 4
            # consecutive non-trading days; days=3 would return zero historical bars).
            from_date = (now - timedelta(days=7)).strftime('%Y-%m-%d')
            to_date   = now.strftime('%Y-%m-%d')

            resp = self.fyers.history({
                "symbol"     : self.inst_cfg['index_symbol'],
                "resolution" : "5",
                "date_format": "1",
                "range_from" : from_date,
                "range_to"   : to_date,
                "cont_flag"  : "1",
            })

            if resp.get('s') != 'ok':
                self.logger.error(
                    f"[DATA-FAIL] Fyers API error for {self.instrument}: "
                    f"status={resp.get('s')} msg={resp.get('message', resp)}"
                )
                return None

            candles = resp.get('candles', [])
            if not candles:
                self.logger.error(
                    f"[DATA-FAIL] Empty candles response for {self.instrument}. "
                    f"API status=ok but no data. from={from_date} to={to_date}"
                )
                return None

            df = pd.DataFrame(
                candles, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume']
            )
            df['ts'] = (pd.to_datetime(df['ts'], unit='s', utc=True)
                        .dt.tz_convert(IST))
            df.set_index('ts', inplace=True)

            # Stale data guard: latest bar must be from today
            latest_date = df.index[-1].date()
            if latest_date != today:
                self.logger.error(
                    f"[DATA-STALE] Latest bar is {latest_date}, expected {today}. "
                    f"API may be returning cached/historical data. Skipping cycle."
                )
                return None

            # Partial-bar guard: Fyers returns the currently-forming candle as the
            # last row. Bars are labelled by open-time; a bar that opened at T is
            # fully closed only once T + 5 min has elapsed.  Computing EMA/ADX/VWAP
            # on an incomplete bar diverges from the backtest, which always uses
            # fully-closed historical bars.  Drop the last bar when it hasn't closed.
            _now_ist      = datetime.now(IST)
            _last_open    = df.index[-1]
            _last_close   = _last_open + pd.Timedelta(minutes=5)
            if _last_close > _now_ist:
                self.logger.debug(
                    f"[BAR-PARTIAL] {self.instrument}: dropped forming bar "
                    f"{_last_open.strftime('%H:%M')}–{_last_close.strftime('%H:%M')} "
                    f"(now={_now_ist.strftime('%H:%M:%S')})"
                )
                df = df.iloc[:-1]
                if len(df) == 0:
                    self.logger.error(
                        f"[DATA-FAIL] {self.instrument}: no bars remain after "
                        f"partial-bar drop. Too early in session?"
                    )
                    return None

            n_today = (df.index.date == today).sum()
            self.logger.debug(
                f"[DATA-OK] {self.instrument}: {len(df)} bars fetched "
                f"({from_date}..{to_date}), {n_today} bars today."
            )

            # ── Optional: India VIX for goldilocks gate + reversal guard ─────
            # Fetches same 3-day window of VIX 5-min bars from Fyers and merges
            # as df['VIX']. Enables:
            #   (1) reversal_guard component 6 (VIX spike detection)
            #   (2) USE_VIX_FILTER goldilocks gate (added below in main loop)
            # Silent fail: VIX unavailable leaves df unchanged — both filters dormant.
            if config.USE_VIX_FILTER:
                try:
                    _vix_resp = self.fyers.history({
                        "symbol"     : "NSE:INDIAVIX-INDEX",
                        "resolution" : "5",
                        "date_format": "1",
                        "range_from" : from_date,
                        "range_to"   : to_date,
                        "cont_flag"  : "1",
                    })
                    if _vix_resp.get('s') == 'ok' and _vix_resp.get('candles'):
                        _vdf = pd.DataFrame(
                            _vix_resp['candles'],
                            columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume']
                        )
                        _vdf['ts'] = (pd.to_datetime(_vdf['ts'], unit='s', utc=True)
                                      .dt.tz_convert(IST))
                        _vdf.set_index('ts', inplace=True)
                        df['VIX'] = _vdf['Close'].reindex(df.index, method='ffill')
                        self.logger.debug(
                            f"[VIX] Fetched {len(_vdf)} VIX bars. "
                            f"Latest: {_vdf['Close'].iloc[-1]:.2f}"
                        )
                except Exception:
                    pass   # VIX unavailable — goldilocks filter dormant, no crash

            # ── Futures volume (PATH_A_FUT_VOL_ENABLED) ──────────────────────
            # Fetches current-month index futures 5-min bars and merges FUT_VOL
            # into the index df.  Silent-fail: column absent → gate dormant.
            if getattr(config, 'PATH_A_FUT_VOL_ENABLED', False):
                try:
                    _fut_sym  = self._get_futures_symbol()
                    _fut_resp = self.fyers.history({
                        "symbol"     : _fut_sym,
                        "resolution" : "5",
                        "date_format": "1",
                        "range_from" : from_date,
                        "range_to"   : to_date,
                        "cont_flag"  : "1",
                    })
                    if _fut_resp.get('s') == 'ok' and _fut_resp.get('candles'):
                        _fdf = pd.DataFrame(
                            _fut_resp['candles'],
                            columns=['ts', 'Open', 'High', 'Low', 'Close', 'FUT_VOL']
                        )
                        _fdf['ts'] = (pd.to_datetime(_fdf['ts'], unit='s', utc=True)
                                      .dt.tz_convert(IST))
                        _fdf.set_index('ts', inplace=True)
                        df['FUT_VOL'] = _fdf['FUT_VOL'].reindex(df.index, method='ffill')
                        self.logger.debug(
                            f"[FUT-VOL] {self.instrument}: {_fut_sym} | "
                            f"latest={int(_fdf['FUT_VOL'].iloc[-1]):,}"
                        )
                    else:
                        self.logger.debug(
                            f"[FUT-VOL] {self.instrument}: API status "
                            f"{_fut_resp.get('s')} for {_fut_sym} — gate dormant"
                        )
                except Exception as _fv_err:
                    self.logger.debug(f"[FUT-VOL] fetch failed: {_fv_err} — gate dormant")

            return df

        except Exception as e:
            self.logger.error(f"[DATA-FAIL] get_index_data exception: {e}", exc_info=True)
            return None

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['EMA_fast'] = EMAIndicator(
            close=df['Close'], window=config.MOMENTUM_EMA_FAST
        ).ema_indicator()
        df['EMA_slow'] = EMAIndicator(
            close=df['Close'], window=config.MOMENTUM_EMA_SLOW
        ).ema_indicator()
        adx = ADXIndicator(
            high=df['High'], low=df['Low'], close=df['Close'], window=14
        )
        df['ADX']      = adx.adx()
        df['DI_plus']  = adx.adx_pos()   # +DI: bullish directional strength
        df['DI_minus'] = adx.adx_neg()   # -DI: bearish directional strength
        df['RSI']     = RSIIndicator(df['Close'], 14).rsi()
        df['Returns'] = df['Close'].pct_change()
        # HV: rolling(30) + default 0.18 — aligned with bot.py backtest.
        # Was rolling(20)/0.15, which produced option prices ~10-15% lower than
        # the backtest's Black-Scholes values, making live P&L comparisons invalid.
        df['HV']      = df['Returns'].rolling(30).std() * np.sqrt(252 * 75)
        df['HV']      = df['HV'].bfill().fillna(0.18)
        df['Volume_MA'] = df['Volume'].rolling(20).mean()

        # 5m SuperTrend — used as alternate entry trigger (Path C: ST flip)
        # Same period/multiplier as 15m context for consistency.
        df['ST_5m'] = self._compute_supertrend(
            df, config.SUPERTREND_PERIOD, config.SUPERTREND_MULTIPLIER
        )

        # ATR14 — True Range rolling mean (14 bars).
        # Needed by signal_scorer Consolidation Quality component (Gap 9).
        # Computed inline (no new import) using same TR formula as _compute_supertrend.
        _tr = pd.concat([
            df['High'] - df['Low'],
            (df['High'] - df['Close'].shift(1)).abs(),
            (df['Low']  - df['Close'].shift(1)).abs(),
        ], axis=1).max(axis=1)
        df['ATR14'] = _tr.rolling(14).mean()

        # Daily VWAP — resets per calendar day (critical: now that we fetch 3 days of data,
        # we must group by date so today's VWAP is not polluted by prior days' prices).
        # For index feeds where volume=0, falls back to per-day expanding mean of typical price.
        _tp        = (df['High'] + df['Low'] + df['Close']) / 3
        _date_col  = df.index.date
        if df['Volume'].sum() > 0:
            # Volume available: proper intraday VWAP per day
            df['VWAP'] = (
                (_tp * df['Volume']).groupby(_date_col).cumsum()
                / df['Volume'].groupby(_date_col).cumsum().replace(0, float('nan'))
            )
        else:
            # Index feed (volume=0): per-day expanding mean of typical price
            df['VWAP'] = _tp.groupby(_date_col).transform(
                lambda x: x.expanding().mean()
            )

        # VWAP Standard Deviation bands (Gap 2) — volatility-adjusted extension zones.
        # Per-day expanding std of (Close - VWAP). Price at ±2sd = statistically
        # over/under-extended vs intraday fair value (bad entry, good reversal target).
        # Used by reversal_guard VWAP stretch component and logged in SCAN context.
        _vwap_diff  = df['Close'] - df['VWAP']
        _vwap_std   = _vwap_diff.groupby(_date_col).transform(
            lambda s: s.expanding().std()
        )
        df['VWAP_1up'] = df['VWAP'] + _vwap_std
        df['VWAP_2up'] = df['VWAP'] + 2 * _vwap_std
        df['VWAP_1dn'] = df['VWAP'] - _vwap_std
        df['VWAP_2dn'] = df['VWAP'] - 2 * _vwap_std

        # ── Intraday SMA levels (PATH_A_SMA_ENABLED) ─────────────────────────
        # SMA_fast (20-bar = 100 min): short-term trend reference.
        # SMA_slow (50-bar = 250 min): structural bias; used for trend-alignment +1 strength.
        if getattr(config, 'PATH_A_SMA_ENABLED', False):
            _sf = getattr(config, 'PATH_A_SMA_FAST', 20)
            _ss = getattr(config, 'PATH_A_SMA_SLOW', 50)
            df['SMA_fast'] = df['Close'].rolling(_sf, min_periods=max(1, _sf // 2)).mean()
            df['SMA_slow'] = df['Close'].rolling(_ss, min_periods=max(1, _ss // 2)).mean()

        # ── Previous Day High / Low / Close (PATH_A_PDH_PDL_ENABLED) ─────────
        # Key price-memory S/R levels.  Broadcast as constants to all today's rows.
        if getattr(config, 'PATH_A_PDH_PDL_ENABLED', False):
            try:
                _today  = df.index[-1].date()
                _prev   = df[pd.Series(df.index.date, index=df.index) < _today]
                if not _prev.empty:
                    _by_day = _prev.groupby(pd.Series(_prev.index.date, index=_prev.index))
                    _last   = sorted(_by_day.groups)[-1]
                    _ld     = _by_day.get_group(_last)
                    df['PDH'] = float(_ld['High'].max())
                    df['PDL'] = float(_ld['Low'].min())
                    df['PDC'] = float(_ld['Close'].iloc[-1])
            except Exception:
                pass   # non-fatal — proximity logging simply absent

        return df

    # ── Multi-Timeframe: 15min SuperTrend (monitoring) ─────────────────────

    @staticmethod
    def _compute_supertrend(df: pd.DataFrame, period: int = 10,
                            multiplier: float = 3.0) -> pd.Series:
        """SuperTrend indicator: +1=bullish, -1=bearish."""
        hl2   = (df['High'] + df['Low']) / 2
        tr    = pd.concat([
            df['High'] - df['Low'],
            (df['High'] - df['Close'].shift(1)).abs(),
            (df['Low']  - df['Close'].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr   = tr.rolling(period).mean()
        up    = (hl2 - multiplier * atr).values.copy()
        dn    = (hl2 + multiplier * atr).values.copy()
        close = df['Close'].values.copy()
        trend = np.ones(len(df))
        for i in range(1, len(df)):
            if close[i - 1] > up[i - 1]:
                up[i] = max(up[i], up[i - 1])
            if close[i - 1] < dn[i - 1]:
                dn[i] = min(dn[i], dn[i - 1])
            if trend[i - 1] == 1:
                trend[i] = -1 if close[i] < up[i] else 1
            else:
                trend[i] = 1 if close[i] > dn[i] else -1
        return pd.Series(trend, index=df.index, name='SuperTrend')

    def get_htf_context(self) -> dict:
        """
        Fetch 15min data + compute SuperTrend for multi-timeframe context.
        Used for logging/monitoring. Only used as a signal filter when
        USE_SUPERTREND_FILTER is enabled (currently disabled — 23 trades
        over 13 months was too few in v8 backtest).
        """
        # HTF uses SuperTrend only — EMA is dropped at this timeframe because
        # it lags even more on 15m bars.  ST is ATR-based, adapts to volatility,
        # and flips cleanly (+1/-1) with no ambiguous crossover lag.
        ctx = {'supertrend_15m': None}
        try:
            now = datetime.now(IST)
            resp = self.fyers.history({
                "symbol"     : self.inst_cfg['index_symbol'],
                "resolution" : "15",
                "date_format": "1",
                "range_from" : (now - timedelta(days=5)).strftime('%Y-%m-%d'),
                "range_to"   : now.strftime('%Y-%m-%d'),
                "cont_flag"  : "1",
            })
            if resp.get('s') != 'ok' or not resp.get('candles'):
                return ctx
            df15 = pd.DataFrame(
                resp['candles'], columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume']
            )
            df15['ts'] = pd.to_datetime(df15['ts'], unit='s', utc=True).dt.tz_convert(IST)
            df15.set_index('ts', inplace=True)

            st = self._compute_supertrend(
                df15, config.SUPERTREND_PERIOD, config.SUPERTREND_MULTIPLIER
            )
            ctx['supertrend_15m'] = int(st.iloc[-1])   # +1 or -1
        except Exception as e:
            self.logger.debug(f"HTF context error: {e}")
        return ctx

    # ── Option Chain: OI, IV, PCR (live sentiment) ────────────────────────

    def get_option_chain_context(self, underlying_price: float) -> dict:
        """
        Fetch option chain context for the current instrument.

        NIFTY / BANKNIFTY : NSE public website via nse_oi (jugaad-data).
                            No auth required. Results cached 5 min.
        SENSEX            : BSE F&O via Fyers API (bse_oi module).
                            Uses the same authenticated session as price data.
                            Results cached 5 min.

        Returns dict with:
          pcr      : put/call OI ratio  (>1.2 bullish, <0.7 bearish)
          max_pain : strike with min buyer pain (pin target near expiry)
          atm_iv   : average ATM implied volatility (%)
          iv_skew  : put_iv - call_iv at ATM (positive = fear premium)
          oi_bias  : 'bullish' | 'bearish' | 'neutral'
          strikes  : {strike: {call_oi, call_iv, put_oi, put_iv}}
        """
        empty = {'pcr': None, 'max_pain': None, 'atm_iv': None,
                 'iv_skew': None, 'oi_bias': 'neutral', 'strikes': {}}
        try:
            if self.instrument == 'SENSEX':
                import bse_oi
                return bse_oi.get_oc_context(self.fyers, underlying_price)
            import nse_oi
            return nse_oi.get_oc_context(self.instrument, underlying_price)
        except Exception as e:
            self.logger.warning(f"[OC] oi fetch EXCEPTION: {e}")
            return empty

    # ── Signal ────────────────────────────────────────────────────────────────

    def _get_day_cfg(self, day_abbr: str) -> dict:
        """Return the complete per-day ORB config for day_abbr (Mon/Tue/Wed/Thu/Fri).

        Reads from config.PATH_A_DAY_CONFIG[day_abbr], filling any missing keys with
        the legacy per-day dicts / global PATH_A_* defaults so the caller always gets
        a complete dict regardless of which keys are present in config.

        Keys returned:
          or_bars      — number of 5-min bars defining the Opening Range (per-day optimised)
          enabled      — whether ORB is active on this day
          adx_min      — per-day ADX floor for entry
          or_width_max — max H-L width as fraction of price (None = no limit)
          no_call      — suppress CALL signals
          no_put       — suppress PUT signals
          stop         — stop-loss fraction (0.50 = 50%)
          target       — profit target fraction (0.80 = 80%)
          trail_act    — trailing activation threshold
          trail_dist   — trailing distance
          checkpoint   — time string for the 12PM loss-stop checkpoint
        """
        raw = getattr(config, 'PATH_A_DAY_CONFIG', {}).get(day_abbr, {})
        # Default entry_end = PATH_A_LATE_END ('12:00') — same outer bound the main
        # loop already enforces via _orb_window.  Per-day override (e.g. Wed '10:55')
        # adds a hard cutoff INSIDE get_path_a_signal() for early-exit day models.
        _late_end = getattr(config, 'PATH_A_LATE_END', '12:00')
        return {
            'or_bars'             : raw.get('or_bars',      config.PATH_A_ORB_BARS),
            'enabled'             : raw.get('enabled',      True),
            'adx_min'             : raw.get('adx_min',
                                            config.PATH_A_DAY_ADX_MIN.get(day_abbr,
                                                                           config.PATH_A_ADX_MIN)),
            'or_width_max'        : raw.get('or_width_max',
                                            config.PATH_A_OR_WIDTH_MAX.get(day_abbr)),
            'no_call'             : raw.get('no_call',
                                            day_abbr in config.PATH_A_NO_CALL_DAYS),
            'no_put'              : raw.get('no_put',             False),
            'oi_confirm_required' : raw.get('oi_confirm_required', False),
            'entry_end'           : raw.get('entry_end',          _late_end),
            'stop'                : raw.get('stop',      self.inst_cfg.get('path_a_stop',        config.PATH_A_STOP)),
            'target'              : raw.get('target',    self.inst_cfg.get('path_a_target',       config.PATH_A_TARGET)),
            'trail_act'           : raw.get('trail_act', self.inst_cfg.get('path_a_trail_act',    config.PATH_A_TRAIL_ACT)),
            'trail_dist'          : raw.get('trail_dist',self.inst_cfg.get('path_a_trail_dist',   config.PATH_A_TRAIL_DIST)),
            'checkpoint'          : raw.get('checkpoint',         config.PATH_A_FORCE_CLOSE),
        }

    def _get_oi_direction_bias(self,
                               sig_type: str,
                               px: float,
                               oc: dict) -> 'tuple[str, str]':
        """Score the live OI context for a pending signal and return a direction bias.

        Combines four independent data sources into a single score:
          1. Live PCR      — call/put buyer dominance (5-min cached from nse_oi)
          2. PCR drift     — 30-min trend in PCR from the intraday snapshot buffer
          3. MaxPain       — spot position vs gravity anchor (DTE≤2 only)
          4. IV skew       — put_iv > call_iv = fear premium; call_iv > put_iv = greed

        Each component contributes ±1 to the score (total range −4 to +4):
          Positive score = OI context supports sig_type direction.
          Negative score = OI context contradicts sig_type direction.

        Returns:
          (bias, reason_str) where bias ∈ {'CONFIRM', 'NEUTRAL', 'REJECT'}
            CONFIRM  : score ≥ +2 — proceed with full confidence
            NEUTRAL  : score −1..+1 — allowed on most days; blocked on days where
                       oi_confirm_required=True (Tue, Wed in default config)
            REJECT   : score ≤ −2 — hard-block when OI_DIRECTION_BIAS_REJECT=True

        SENSEX: nse_oi returns None for BSE instruments; PCR drift is unavailable.
        Only IV skew and MaxPain (from bse_oi fallback) contribute → score capped
        at ±2, usually NEUTRAL — graceful, never hard-blocks SENSEX signals.
        """
        score   = 0
        reasons = []
        is_call = (sig_type == 'CALL')
        dte     = getattr(config, 'DAYS_TO_EXPIRY', 2)

        # ── 1. Live PCR ───────────────────────────────────────────────────────
        live_pcr     = oc.get('pcr')
        pcr_call_max = getattr(config, 'OI_PCR_CALL_CONFIRM_MAX', 0.90)
        pcr_put_min  = getattr(config, 'OI_PCR_PUT_CONFIRM_MIN',  1.10)

        if live_pcr is not None:
            if is_call:
                if live_pcr < pcr_call_max:
                    score += 1
                    reasons.append(f'PCR={live_pcr:.2f} CALL-dominant ✓')
                elif live_pcr > pcr_put_min:
                    score -= 1
                    reasons.append(f'PCR={live_pcr:.2f} PUT-dominant ✗')
                else:
                    reasons.append(f'PCR={live_pcr:.2f} neutral')
            else:
                if live_pcr > pcr_put_min:
                    score += 1
                    reasons.append(f'PCR={live_pcr:.2f} PUT-dominant ✓')
                elif live_pcr < pcr_call_max:
                    score -= 1
                    reasons.append(f'PCR={live_pcr:.2f} CALL-dominant ✗')
                else:
                    reasons.append(f'PCR={live_pcr:.2f} neutral')
        else:
            reasons.append('PCR N/A')

        # ── 2. PCR drift (30-min intraday trend) ─────────────────────────────
        # Falling PCR = call buyers accumulating = bullish momentum → confirms CALL
        # Rising PCR  = put buyers accumulating = bearish momentum → confirms PUT
        drift_thresh = getattr(config, 'OI_PCR_DRIFT_THRESHOLD',      0.05)
        lookback     = getattr(config, 'OI_PCR_DRIFT_LOOKBACK_MINS',  30)
        try:
            import nse_oi as _nse_oi_m
            drift = _nse_oi_m.get_pcr_drift(self.instrument, lookback)
        except Exception:
            drift = None

        if drift is not None:
            if is_call:
                if drift < -drift_thresh:
                    score += 1
                    reasons.append(f'PCR-drift={drift:+.3f} CALL-building ✓')
                elif drift > drift_thresh:
                    score -= 1
                    reasons.append(f'PCR-drift={drift:+.3f} PUT-building ✗')
                else:
                    reasons.append(f'PCR-drift={drift:+.3f} flat')
            else:
                if drift > drift_thresh:
                    score += 1
                    reasons.append(f'PCR-drift={drift:+.3f} PUT-building ✓')
                elif drift < -drift_thresh:
                    score -= 1
                    reasons.append(f'PCR-drift={drift:+.3f} CALL-building ✗')
                else:
                    reasons.append(f'PCR-drift={drift:+.3f} flat')
        else:
            reasons.append('PCR-drift N/A (<2 snapshots)')

        # ── 3. MaxPain gravity (DTE ≤ 2 only) ────────────────────────────────
        # Near expiry, options writers have strong incentive to pin price at
        # MaxPain.  Price significantly above MaxPain → gravity pulls DOWN
        # (tailwind for PUT, headwind for CALL), and vice versa.
        live_mp = oc.get('max_pain')
        mp_pct  = getattr(config, 'OI_MAXPAIN_GRAVITY_PCT', 0.008)

        if live_mp and live_mp > 0 and px > 0:
            if dte <= 2:
                mp_dist = (px - live_mp) / live_mp   # positive = spot ABOVE MaxPain
                if is_call:
                    if mp_dist > mp_pct:
                        score -= 1
                        reasons.append(
                            f'MaxPain={live_mp:,.0f} spot {mp_dist*100:.1f}%↑ gravity headwind ✗')
                    elif mp_dist < -mp_pct:
                        score += 1
                        reasons.append(
                            f'MaxPain={live_mp:,.0f} spot {abs(mp_dist)*100:.1f}%↓ gravity tailwind ✓')
                    else:
                        reasons.append(f'MaxPain={live_mp:,.0f} spot in pin zone')
                else:
                    if mp_dist < -mp_pct:
                        score -= 1
                        reasons.append(
                            f'MaxPain={live_mp:,.0f} spot {abs(mp_dist)*100:.1f}%↓ gravity headwind ✗')
                    elif mp_dist > mp_pct:
                        score += 1
                        reasons.append(
                            f'MaxPain={live_mp:,.0f} spot {mp_dist*100:.1f}%↑ gravity tailwind ✓')
                    else:
                        reasons.append(f'MaxPain={live_mp:,.0f} spot in pin zone')
            else:
                reasons.append(f'MaxPain={live_mp:,.0f} DTE={dte}>2 (gravity inactive)')

        # ── 4. IV skew (fear/greed premium at ATM) ────────────────────────────
        # iv_skew = put_iv − call_iv at ATM (positive = fear premium)
        # Fear premium (skew > threshold): market hedging downside → PUT favoured
        # Greed premium (skew < -threshold): market chasing upside → CALL favoured
        iv_skew      = oc.get('iv_skew')
        skew_thresh  = getattr(config, 'OI_IV_SKEW_THRESHOLD', 2.0)

        if iv_skew is not None:
            if iv_skew > skew_thresh:        # fear premium
                if is_call:
                    score -= 1
                    reasons.append(f'IVskew={iv_skew:+.1f}% fear ✗ CALL')
                else:
                    score += 1
                    reasons.append(f'IVskew={iv_skew:+.1f}% fear ✓ PUT')
            elif iv_skew < -skew_thresh:     # greed premium
                if is_call:
                    score += 1
                    reasons.append(f'IVskew={iv_skew:+.1f}% greed ✓ CALL')
                else:
                    score -= 1
                    reasons.append(f'IVskew={iv_skew:+.1f}% greed ✗ PUT')
            else:
                reasons.append(f'IVskew={iv_skew:+.1f}% neutral')
        else:
            reasons.append('IVskew N/A')

        # ── Map score → bias ──────────────────────────────────────────────────
        if   score >= 2:
            bias = 'CONFIRM'
        elif score <= -2:
            bias = 'REJECT'
        else:
            bias = 'NEUTRAL'

        reason_str = f'[score {score:+d}/4] ' + ' | '.join(reasons)
        return bias, reason_str

    def _compute_or(self, df: 'pd.DataFrame', now: 'datetime') -> None:
        """Compute today's Opening Range from the first PATH_A_ORB_BARS candles.

        Called once per day at the first bar at or after 09:30. Sets:
          self._or_high, self._or_low  — the range boundaries
          self._or_width_ok            — True if width passes per-day gate
          self._or_ready               — True when range is established
          self._gap_type               — gap classification vs previous close
        """
        today    = now.date()
        today_df = df[df.index.date == today]

        # ── Per-day OR bar count (from PATH_A_DAY_CONFIG or global fallback) ──
        day_abbr  = now.strftime('%a')
        _dcfg     = self._get_day_cfg(day_abbr)
        n         = _dcfg['or_bars']   # e.g. Mon=4, Fri=5 (global default=5)

        if len(today_df) < n:
            return   # not enough bars yet

        or_bars = today_df.head(n)
        self._or_high = float(or_bars['High'].max())
        self._or_low  = float(or_bars['Low'].min())
        or_width  = (self._or_high - self._or_low) / self._or_low
        width_max = _dcfg['or_width_max']
        if width_max is not None and or_width > width_max:
            self._or_width_ok = False
            self.logger.info(
                f"  [PATH-A] {self.instrument}: OR width {or_width*100:.3f}% "
                f"> {width_max*100:.2f}% limit ({day_abbr}) — ORB blocked today"
            )
        else:
            self._or_width_ok = True

        # ── Gap classification ───────────────────────────────────────────────
        prev_df = df[df.index.date < today]
        if len(prev_df) > 0 and len(today_df) > 0:
            prev_close  = float(prev_df.iloc[-1]['Close'])
            open_price  = float(today_df.iloc[0]['Open'])
            latest_px   = float(today_df.iloc[-1]['Close'])
            gap_pct     = (open_price - prev_close) / prev_close
            if gap_pct > 0.003:
                self._gap_type = ('GAP_FADE_UP'    if latest_px < open_price
                                  else 'GAP_AND_GO_UP')
            elif gap_pct < -0.003:
                self._gap_type = ('GAP_FADE_DN'    if latest_px > open_price
                                  else 'GAP_AND_GO_DN')
            else:
                self._gap_type = 'INSIDE_OPEN'
            # Store for gap-reversal ADX supplement (checked dynamically each cycle)
            self._gap_prev_close = prev_close
            self._gap_open_price = open_price
            # FADE_DN = gap-dn + price recovering → recovery direction is CALL
            # FADE_UP = gap-up + price recovering → recovery direction is PUT
            if self._gap_type == 'GAP_FADE_DN':
                self._gap_rev_dir = 'CALL'
            elif self._gap_type == 'GAP_FADE_UP':
                self._gap_rev_dir = 'PUT'
            else:
                self._gap_rev_dir = None   # AND_GO or INSIDE — no supplement

        self._or_ready = True
        suppress_note  = '' if self._or_width_ok else ' [WIDTH BLOCKED]'
        self.logger.info(
            f"  [PATH-A] {self.instrument}: OR {self._or_low:.1f}–{self._or_high:.1f} "
            f"| Width {or_width*100:.3f}% | Gap: {self._gap_type}{suppress_note}"
        )

    def _otm_degree_from_sr(self, sig_type: str, px: float) -> tuple:
        """
        Return (otm_degree, reason_str) based on distance to next significant
        OI support/resistance level in the breakout direction.

        Logic:
          CALL → look at resistance levels ABOVE current price
          PUT  → look at support levels BELOW current price

        OTM degree by distance (in strike_gaps) to nearest MAJOR or WALL:
          WALL < 2 gaps     → 0  (wall will pin the move before OTM goes ITM)
          WALL 2–4 gaps     → 1  (some room; wall is the natural target)
          WALL 4+ gaps      → 2  (wall is far — free run)
          MAJOR < 1.5 gaps  → 0  (close resistance; respect it)
          MAJOR 1.5–3 gaps  → 1  (moderate room to run)
          MAJOR 3+ gaps     → 2  (clear runway)
          No MAJOR/WALL     → 2  (free run — no significant ceiling/floor)

        Returns (0, reason) if:
          - SENSEX (no BSE OI data)
          - OI zones not loaded or stale
        """
        if self.instrument == 'SENSEX' or not self._oi_zones:
            return 0, 'no-OI-data'

        gap = self.strike_gap

        if sig_type == 'CALL':
            # Resistance levels strictly above current price, sorted nearest-first
            candidates = sorted(
                [(float(r.get('strike', 0)), r.get('strength', 'MINOR'))
                 for r in self._oi_zones.get('resistance', [])
                 if float(r.get('strike', 0)) > px],
                key=lambda x: x[0]
            )
        else:
            # Support levels strictly below current price, sorted nearest-first (descending)
            candidates = sorted(
                [(float(s.get('strike', 0)), s.get('strength', 'MINOR'))
                 for s in self._oi_zones.get('support', [])
                 if float(s.get('strike', 0)) < px],
                key=lambda x: x[0], reverse=True
            )

        # Only MAJOR and WALL carry meaningful stopping power
        sig = [(s, st) for s, st in candidates if st in ('MAJOR', 'WALL')]

        if not sig:
            return 2, 'free-run (no MAJOR/WALL ahead)'

        nearest_strike, nearest_strength = sig[0]
        dist_gaps = (abs(nearest_strike - px)) / gap

        if nearest_strength == 'WALL':
            if dist_gaps < 2:
                return 0, f'WALL {nearest_strike:.0f} ({dist_gaps:.1f}g away) → ATM'
            if dist_gaps < 4:
                return 1, f'WALL {nearest_strike:.0f} ({dist_gaps:.1f}g away) → OTM+1'
            return 2,     f'WALL {nearest_strike:.0f} ({dist_gaps:.1f}g away) → OTM+2'
        else:  # MAJOR
            if dist_gaps < 1.5:
                return 0, f'MAJOR {nearest_strike:.0f} ({dist_gaps:.1f}g away) → ATM'
            if dist_gaps < 3:
                return 1, f'MAJOR {nearest_strike:.0f} ({dist_gaps:.1f}g away) → OTM+1'
            return 2,     f'MAJOR {nearest_strike:.0f} ({dist_gaps:.1f}g away) → OTM+2'

    def _try_dynamic_or(self, df: 'pd.DataFrame', now: 'datetime') -> bool:
        """Fallback: when original OR is too wide, find a tight post-open consolidation.

        Scans the last PATH_A_DYNAMIC_OR_BARS × 5-min bars (from 09:30 onwards).
        If their combined High–Low spread ≤ PATH_A_DYNAMIC_OR_MAX_WIDTH, that zone
        becomes the Dynamic OR and _or_width_ok is re-enabled.

        Must be called before PATH_A_DYNAMIC_OR_SEARCH_END (default 10:30) to
        ensure a minimum 30-min entry window before the 11:00 ORB cutoff.

        Returns True when a new dynamic OR is established; False otherwise.
        Already-established dynamic OR (_dynamic_or=True) returns False immediately.
        """
        if not getattr(config, 'PATH_A_DYNAMIC_OR_ENABLED', False):
            return False
        if self._dynamic_or or self._path_a_fired:
            return False

        _end_str = getattr(config, 'PATH_A_DYNAMIC_OR_SEARCH_END', '10:30')
        _end     = dtime(*[int(x) for x in _end_str.split(':')])
        if now.time() >= _end:
            return False

        n_bars    = getattr(config, 'PATH_A_DYNAMIC_OR_BARS', 5)
        max_width = getattr(config, 'PATH_A_DYNAMIC_OR_MAX_WIDTH', 0.0020)

        today     = now.date()
        today_df  = df[df.index.date == today]
        # Only bars from 09:30 onwards — skip the volatile opening bars
        post_open = today_df[today_df.index.time >= dtime(9, 30)]

        if len(post_open) < n_bars:
            return False   # not enough bars yet — keep waiting

        window    = post_open.iloc[-n_bars:]
        dyn_high  = float(window['High'].max())
        dyn_low   = float(window['Low'].min())
        dyn_mid   = (dyn_high + dyn_low) / 2
        dyn_width = (dyn_high - dyn_low) / dyn_mid

        if dyn_width > max_width:
            return False   # still too wide — keep polling

        # ── Tight consolidation found ─────────────────────────────────────────
        self._or_high     = dyn_high
        self._or_low      = dyn_low
        self._or_width_ok = True
        self._dynamic_or  = True
        self.logger.info(
            f"  [PATH-A-DYN] {self.instrument}: Dynamic OR established "
            f"{dyn_low:.1f}–{dyn_high:.1f} | "
            f"Width {dyn_width*100:.3f}% | "
            f"{n_bars}-bar consolidation (09:30+) | "
            f"Search window closes {_end_str} | "
            f"Stop {getattr(config, 'PATH_A_DYNAMIC_OR_STOP', 0.35)*100:.0f}% "
            f"(vs standard {config.PATH_A_STOP*100:.0f}%)"
        )
        return True

    def _check_gap_rev_adx(self, sig_type: str, px: float, adx: float,
                            row: 'pd.Series', normal_adx_min: float) -> bool:
        """Gap-Reversal ADX supplement — returns True if gap-fade context allows
        bypassing the normal ADX floor.

        Criteria (ALL must pass):
          1. sig_type matches the gap-reversal direction (_gap_rev_dir)
          2. Gap magnitude ≥ GAP_REV_MIN_GAP_PCT
          3. Price has recovered ≥ GAP_REV_RECOVERY_PCT of the original gap
          4. ADX ≥ reduced GAP_REV_ADX_MIN
          5. DI-spread in recovery direction ≥ GAP_REV_DI_SPREAD_MIN

        Rationale: after a gap open, the 14-period ADX averages the initial
        gap-direction bars with the recovery bars, suppressing the reading to
        17–24 even when DI dominance is strong and sustained.  This check uses
        price recovery + DI spread as the trend-confirmation proxy instead.
        """
        if sig_type != self._gap_rev_dir:
            return False
        if not self._gap_prev_close or not self._gap_open_price:
            return False

        gap_pts     = abs(self._gap_open_price - self._gap_prev_close)
        gap_pct_abs = gap_pts / self._gap_prev_close
        min_gap     = getattr(config, 'GAP_REV_MIN_GAP_PCT', 0.005)
        if gap_pct_abs < min_gap:
            return False

        # Recovery: price must have retraced ≥ GAP_REV_RECOVERY_PCT of the gap
        rec_pct   = getattr(config, 'GAP_REV_RECOVERY_PCT', 0.50)
        if sig_type == 'CALL':   # gap-dn → recovery = price rising toward prev_close
            rec_threshold = self._gap_open_price + gap_pts * rec_pct
            recovered     = px >= rec_threshold
        else:                    # gap-up → recovery = price falling toward prev_close
            rec_threshold = self._gap_open_price - gap_pts * rec_pct
            recovered     = px <= rec_threshold

        gap_adx_min = getattr(config, 'GAP_REV_ADX_MIN',        18)
        gap_di_min  = getattr(config, 'GAP_REV_DI_SPREAD_MIN',  12)
        di_plus     = float(row.get('DI_plus',  float('nan')))
        di_minus    = float(row.get('DI_minus', float('nan')))
        di_spread   = ((di_plus - di_minus) if sig_type == 'CALL'
                       else (di_minus - di_plus))

        ok = recovered and adx >= gap_adx_min and di_spread >= gap_di_min
        if ok:
            self.logger.info(
                f"  [GAP-REV] {self.instrument} {sig_type}: ADX supplement | "
                f"gap={gap_pct_abs*100:.2f}% | "
                f"recovery={px:.0f}≥{rec_threshold:.0f} ({rec_pct*100:.0f}%) ✓ | "
                f"ADX={adx:.1f}≥{gap_adx_min} ✓ | "
                f"DI-spread={di_spread:.1f}≥{gap_di_min} ✓ | "
                f"(normal floor was {normal_adx_min})"
            )
        else:
            fail = []
            if not recovered:
                fail.append(f"recovery {px:.0f}<{rec_threshold:.0f}")
            if adx < gap_adx_min:
                fail.append(f"ADX {adx:.1f}<{gap_adx_min}")
            if di_spread < gap_di_min:
                fail.append(f"DI-spread {di_spread:.1f}<{gap_di_min}")
            self.logger.debug(
                f"  [GAP-REV] {self.instrument} {sig_type}: supplement failed — "
                + " | ".join(fail)
            )
        return ok

    def get_path_a_signal(self, df: 'pd.DataFrame',
                          row: 'pd.Series', now: 'datetime') -> 'dict | None':
        """Path A — Opening Range Breakout signal (09:30–11:00).

        Fires when price breaks above OR_high or below OR_low with:
          ADX ≥ per-day minimum | VWAP aligned | no CALL on Thu | gap context gate
        Once per day (_path_a_fired set by caller).
        """
        if not self._or_ready or self._path_a_fired:
            return None

        # If original OR was too wide, attempt to find a Dynamic OR from
        # post-open consolidation.  _try_dynamic_or() updates _or_high/_or_low
        # and sets _or_width_ok=True if a tight zone is found.
        if not self._or_width_ok:
            if not self._try_dynamic_or(df, now):
                return None
            # Dynamic OR established — fall through with elevated requirements

        _px  = float(row.get('Close', float('nan')))
        _adx = float(row.get('ADX',   float('nan')))
        if pd.isna(_px) or pd.isna(_adx):
            return None

        buf          = config.PATH_A_BUFFER
        _call_break  = _px > self._or_high * (1.0 + buf)
        _put_break   = _px < self._or_low  * (1.0 - buf)
        if not _call_break and not _put_break:
            return None

        sig_type = 'CALL' if _call_break else 'PUT'
        day_abbr = now.strftime('%a')
        _dcfg    = self._get_day_cfg(day_abbr)

        # ── Day-level enabled gate ───────────────────────────────────────────
        if not _dcfg['enabled']:
            self.logger.info(
                f"  [PATH-A-SKIP] {self.instrument}: ORB disabled on {day_abbr} "
                f"(PATH_A_DAY_CONFIG['{day_abbr}']['enabled']=False)"
            )
            return None

        # ── Per-day direction suppression ────────────────────────────────────
        if sig_type == 'CALL' and _dcfg['no_call']:
            self.logger.info(
                f"  [PATH-A-SKIP] {self.instrument} CALL suppressed on {day_abbr} "
                f"(day config no_call=True)"
            )
            return None
        if sig_type == 'PUT' and _dcfg['no_put']:
            self.logger.info(
                f"  [PATH-A-SKIP] {self.instrument} PUT suppressed on {day_abbr} "
                f"(day config no_put=True)"
            )
            return None

        # ── Per-day entry window hard cutoff ─────────────────────────────────
        # entry_end defaults to PATH_A_LATE_END ('12:00') — same outer bound as
        # the main loop.  Days with an early-exit model (e.g. Wed '10:55') set a
        # tighter cutoff so no new signals fire past the force-close time.
        # Gap-reversal supplement: extend window to GAP_REV_ENTRY_EXT for the
        # recovery direction on fade days (recovery completes 2–4 hr after open).
        _entry_end_t = dtime(*[int(x) for x in _dcfg['entry_end'].split(':')])
        _past_window = now.time() >= _entry_end_t
        if (_past_window
                and getattr(config, 'GAP_REV_ENABLED', False)
                and sig_type == self._gap_rev_dir):
            _ext_str   = getattr(config, 'GAP_REV_ENTRY_EXT', '12:30')
            _ext_end_t = dtime(*[int(x) for x in _ext_str.split(':')])
            _past_window = now.time() >= _ext_end_t
        if _past_window:
            self.logger.debug(
                f"  [PATH-A-SKIP] {self.instrument}: past per-day entry window end "
                f"({_dcfg['entry_end']}) on {day_abbr}"
            )
            return None

        # ── Per-day ADX minimum ──────────────────────────────────────────────
        adx_min = _dcfg['adx_min']
        # Dynamic OR raises the ADX floor (original OR already rejected today)
        if self._dynamic_or:
            adx_min = max(adx_min, getattr(config, 'PATH_A_DYNAMIC_OR_ADX_MIN', 30))
        if _adx < adx_min:
            # Normal ADX gate failed — try gap-reversal supplement before blocking
            if (getattr(config, 'GAP_REV_ENABLED', False)
                    and self._check_gap_rev_adx(sig_type, _px, _adx, row, adx_min)):
                pass   # supplement passed — fall through to remaining signal checks
            else:
                self.logger.info(
                    f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                    f"ADX {_adx:.1f} < {adx_min} ({day_abbr}"
                    f"{', DYN-OR' if self._dynamic_or else ''})"
                )
                return None

        # ── Gap context (informational only) ─────────────────────────────────
        # GAP_FADE_UP/DN no longer blocks signals. A confirmed OR breakout
        # (price closing above OR high / below OR low) is the real evidence
        # that the fade has reversed. Gap type is logged via PATH-A line.

        # ── VWAP filter ──────────────────────────────────────────────────────
        if config.USE_VWAP_FILTER and 'VWAP' in df.columns:
            _vwap = float(row.get('VWAP', float('nan')))
            if not pd.isna(_vwap) and _vwap > 0:
                if sig_type == 'CALL' and _px < _vwap:
                    self.logger.info(
                        f"  [PATH-A-SKIP] {self.instrument} CALL: "
                        f"px {_px:.0f} < VWAP {_vwap:.0f}"
                    )
                    return None
                if sig_type == 'PUT' and _px > _vwap:
                    self.logger.info(
                        f"  [PATH-A-SKIP] {self.instrument} PUT: "
                        f"px {_px:.0f} > VWAP {_vwap:.0f}"
                    )
                    return None

        # ── Minimum OR Extension Gate ────────────────────────────────────────
        # Filter entries right at the OR boundary (thin extension = fakeout risk).
        # May 4 example: entry at ext=0.063% with OI wall 0.16% above → immediate reversal.
        # PATH_A_MIN_OR_EXTENSION (config) = 0.10% default. Set 0.0 to disable.
        break_pct = ((_px - self._or_high) / self._or_high if sig_type == 'CALL'
                     else (self._or_low - _px) / self._or_low)
        _min_ext = getattr(config, 'PATH_A_MIN_OR_EXTENSION', 0.0)
        if _min_ext > 0 and break_pct * 100 < _min_ext:
            self.logger.info(
                f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                f"OR ext {break_pct*100:.3f}% < min {_min_ext:.2f}% "
                f"— extension too thin, waiting for confirmed move"
            )
            return None

        # ── Strength scoring (0–5) ───────────────────────────────────────────
        # Core 3 (unchanged): ADX≥35, OR extension>0.1%, VWAP distance≥0.1%
        # New +1: futures volume high (broad participation)
        # New +1: price on correct side of SMA_slow (structural trend alignment)
        strength = 0
        if _adx >= 35:
            strength += 1
        # break_pct already computed above
        if break_pct > 0.001:   # > 0.1% clean breakout
            strength += 1
        if 'VWAP' in df.columns:
            _vwap = float(row.get('VWAP', float('nan')))
            if not pd.isna(_vwap) and _vwap > 0 and abs(_px - _vwap) / _vwap >= 0.001:
                strength += 1

        # ── Futures volume conviction bonus ───────────────────────────────────
        if getattr(config, 'PATH_A_FUT_VOL_ENABLED', False) and 'FUT_VOL' in df.columns:
            _lkbk   = getattr(config, 'PATH_A_FUT_VOL_LOOKBACK', 20)
            _hi_thr = getattr(config, 'PATH_A_FUT_VOL_HIGH_RATIO', 1.5)
            _lo_thr = getattr(config, 'PATH_A_FUT_VOL_LOW_RATIO', 0.70)
            _fv     = df['FUT_VOL'].dropna()
            if len(_fv) >= 2:
                _cur_vol = float(_fv.iloc[-1])
                _avg_vol = float(_fv.iloc[-_lkbk:].mean())
                if _avg_vol > 0:
                    _vr = _cur_vol / _avg_vol
                    if _vr >= _hi_thr:
                        strength += 1
                        self.logger.info(
                            f"  [FUT-VOL] {self.instrument} {sig_type}: "
                            f"vol_ratio={_vr:.2f} ≥ {_hi_thr} → +1 str (conviction ORB)"
                        )
                    elif _vr < _lo_thr:
                        self.logger.info(
                            f"  [FUT-VOL] {self.instrument} {sig_type}: "
                            f"vol_ratio={_vr:.2f} < {_lo_thr} → thin breakout (no bonus)"
                        )
                    else:
                        self.logger.debug(
                            f"  [FUT-VOL] {self.instrument}: vol_ratio={_vr:.2f} (normal)"
                        )

        # ── SMA_slow trend-alignment bonus + PDH/PDL proximity warning ────────
        if getattr(config, 'PATH_A_SMA_ENABLED', False) and 'SMA_slow' in df.columns:
            _sma_s = float(row.get('SMA_slow', float('nan')))
            if not pd.isna(_sma_s) and _sma_s > 0:
                _aligned = (sig_type == 'CALL' and _px > _sma_s) or \
                           (sig_type == 'PUT'  and _px < _sma_s)
                if _aligned:
                    strength += 1
                    self.logger.info(
                        f"  [SMA] {self.instrument} {sig_type}: "
                        f"px {_px:.0f} {'>' if sig_type=='CALL' else '<'} "
                        f"SMA_slow {_sma_s:.0f} → +1 str (structural alignment)"
                    )
                else:
                    _prox = getattr(config, 'PATH_A_SMA_PROX_PCT', 0.003)
                    _dist = abs(_px - _sma_s) / _sma_s
                    if _dist < _prox:
                        self.logger.info(
                            f"  [SMA] {self.instrument} {sig_type}: "
                            f"px {_px:.0f} within {_dist*100:.2f}% of SMA_slow {_sma_s:.0f}"
                            f" — counter-SMA entry (headwind)"
                        )

        # ── PDH/PDL proximity warning ─────────────────────────────────────────
        if getattr(config, 'PATH_A_PDH_PDL_ENABLED', False):
            _pdhpdl_prox = getattr(config, 'PATH_A_PDH_PDL_PROX', 0.002)
            if sig_type == 'CALL' and 'PDH' in df.columns:
                _pdh = float(row.get('PDH', float('nan')))
                if not pd.isna(_pdh) and _pdh > 0:
                    _dp = (_pdh - _px) / _pdh  # how far below PDH we are
                    if 0 < _dp < _pdhpdl_prox:
                        self.logger.info(
                            f"  [PDH] {self.instrument} CALL: px {_px:.0f} within "
                            f"{_dp*100:.2f}% of PDH {_pdh:.0f} — potential resistance"
                        )
            elif sig_type == 'PUT' and 'PDL' in df.columns:
                _pdl = float(row.get('PDL', float('nan')))
                if not pd.isna(_pdl) and _pdl > 0:
                    _dp = (_px - _pdl) / _pdl   # how far above PDL we are
                    if 0 < _dp < _pdhpdl_prox:
                        self.logger.info(
                            f"  [PDL] {self.instrument} PUT: px {_px:.0f} within "
                            f"{_dp*100:.2f}% of PDL {_pdl:.0f} — potential support (watch)"
                        )

        # ── Dynamic OR: minimum strength gate ────────────────────────────────────
        # Fallback mode requires 2-lot quality — no weak 1-lot entries when the
        # original OR already failed once today.
        if self._dynamic_or:
            _dyn_min_str = getattr(config, 'PATH_A_DYNAMIC_OR_MIN_STRENGTH', 2)
            if strength < _dyn_min_str:
                self.logger.info(
                    f"  [PATH-A-SKIP] {self.instrument} {sig_type}: DYN-OR needs "
                    f"strength≥{_dyn_min_str}, got {strength} — waiting for stronger setup"
                )
                return None

        # ── Dynamic OR: DI alignment gate ────────────────────────────────────────
        # By the time a Dynamic OR fires (09:45+), the day's directional bias is
        # often already established via DI+/DI−.  Require the signal direction to
        # align with the dominant DI to prevent counter-trend entries on settled days.
        # Evidence: Apr 27 DYN-OR PUT fired with DI+=33, DI−=14 (bullish day) → -₹550.
        # Gate: if |DI+ − DI−| ≥ threshold, only the dominant direction is allowed.
        if self._dynamic_or and getattr(config, 'PATH_A_DYNAMIC_OR_DI_GATE', True):
            _di_plus  = float(row.get('DI_plus',  float('nan')))
            _di_minus = float(row.get('DI_minus', float('nan')))
            _di_spread_min = getattr(config, 'PATH_A_DYNAMIC_OR_DI_MIN_SPREAD', 15)
            if not pd.isna(_di_plus) and not pd.isna(_di_minus):
                _di_spread = _di_plus - _di_minus   # positive = bull dominant
                if _di_spread >= _di_spread_min and sig_type == 'PUT':
                    self.logger.info(
                        f"  [PATH-A-SKIP] {self.instrument} PUT: DYN-OR DI gate — "
                        f"DI+ {_di_plus:.1f} > DI− {_di_minus:.1f} "
                        f"(spread {_di_spread:.1f} ≥ {_di_spread_min}) → bull bias, no PUT"
                    )
                    return None
                if -_di_spread >= _di_spread_min and sig_type == 'CALL':
                    self.logger.info(
                        f"  [PATH-A-SKIP] {self.instrument} CALL: DYN-OR DI gate — "
                        f"DI− {_di_minus:.1f} > DI+ {_di_plus:.1f} "
                        f"(spread {abs(_di_spread):.1f} ≥ {_di_spread_min}) → bear bias, no CALL"
                    )
                    return None

        # ── Late window gate (time ≥ PATH_A_END up to PATH_A_LATE_END) ──────────
        # OR survived intact past 11:30 → breakout is decisive (3.5h of S/R
        # validation). But shorter runway + higher fakeout risk → 4 hard guards:
        #   1. ADX ≥ PATH_A_LATE_ADX_MIN (32) — confirmed trend, not drift
        #   2. |DI+ − DI-| ≥ PATH_A_LATE_DI_SPREAD (12) — directional dominance
        #   3. ST5 aligned with direction (PATH_A_LATE_ST5_REQUIRED) — no counter-trend
        #   4. Lots capped at 1 (PATH_A_LATE_LOTS) — short runway, conservative
        # Unified scorer still gates at 55/100 after this passes.
        _pa_end  = dtime(*[int(x) for x in config.PATH_A_END.split(':')])
        _is_late = now.time() >= _pa_end
        if _is_late:
            _late_adx = getattr(config, 'PATH_A_LATE_ADX_MIN', 32)
            _late_str = getattr(config, 'PATH_A_LATE_MIN_STRENGTH', 1)
            if _adx < _late_adx or strength < _late_str:
                self.logger.info(
                    f"  [PATH-A-SKIP] {self.instrument} {sig_type}: late window "
                    f"ADX {_adx:.1f} (need ≥{_late_adx}) "
                    f"str {strength} (need ≥{_late_str})"
                )
                return None

            # ── Late window guard 2: DI spread ───────────────────────────────
            _late_di_req = getattr(config, 'PATH_A_LATE_DI_SPREAD', 12)
            if _late_di_req > 0:
                _di_p = float(row.get('DI_plus',  float('nan')))
                _di_m = float(row.get('DI_minus', float('nan')))
                if not pd.isna(_di_p) and not pd.isna(_di_m):
                    _di_spd = abs(_di_p - _di_m)
                    if _di_spd < _late_di_req:
                        self.logger.info(
                            f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                            f"late window DI spread {_di_spd:.1f} < {_late_di_req} "
                            f"— insufficient directional dominance"
                        )
                        return None

            # ── Late window guard 3: ST5 direction ───────────────────────────
            if getattr(config, 'PATH_A_LATE_ST5_REQUIRED', True):
                _st5_val = str(row.get('ST_5m', '')).upper()
                if _st5_val in ('BULL', 'BEAR'):
                    _st5_ok = (
                        (sig_type == 'CALL' and _st5_val == 'BULL') or
                        (sig_type == 'PUT'  and _st5_val == 'BEAR')
                    )
                    if not _st5_ok:
                        self.logger.info(
                            f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                            f"late window ST5={_st5_val} — counter-trend late entry blocked"
                        )
                        return None

            # ── Late window guard 4: 15m SuperTrend must align ───────────────
            # HTF ST gives 0/20 in SCORER but does not hard-block. In the late
            # window (short runway), a directly opposing 15m trend is sufficient
            # to kill the trade — extreme 5m ADX still compensated the SCORER
            # for Jun 11 NF (15m BEAR + CALL, ADX=42 → 55/100, -₹2,240).
            if getattr(config, 'PATH_A_LATE_HTF_REQUIRED', True):
                _st15 = getattr(self, '_st15m', None)
                if _st15 is not None:
                    _st15_ok = (
                        (sig_type == 'CALL' and _st15 == 1) or
                        (sig_type == 'PUT'  and _st15 == -1)
                    )
                    if not _st15_ok:
                        _st15_str = 'BULL' if _st15 == 1 else 'BEAR'
                        self.logger.info(
                            f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                            f"late window 15m-ST={_st15_str} opposes — HTF hard gate"
                        )
                        return None

            # ── Late window guard 5: CHOPPY regime skip ──────────────────────
            # INSIDE_OPEN + CHOPPY + 12:xx entry = theta trap (no momentum
            # to reach 55% target in ≤2h before 14:30 force-close).
            if getattr(config, 'REGIME_CHOPPY_LATE_ORB_SKIP', False):
                if getattr(self, '_regime', 'MIXED') == 'CHOPPY':
                    self.logger.info(
                        f"  [PATH-A-SKIP] {self.instrument} {sig_type}: "
                        f"CHOPPY regime — late-window ORB suppressed"
                    )
                    return None

        # ── OTM degree: driven by distance to next significant S/R level ────
        # Primary signal: how far is the next MAJOR/WALL OI level in the
        # breakout direction?  More room = more OTM.
        # ADX guard: override to ATM if trend is too weak to trust S/R distance.
        _otm = 0
        _otm_reason = 'OTM disabled'
        if getattr(config, 'PATH_A_OTM_ENABLED', False):
            _otm, _otm_reason = self._otm_degree_from_sr(sig_type, _px)
            _min_adx = getattr(config, 'PATH_A_OTM_MIN_ADX', 25)
            if _otm > 0 and _adx < _min_adx:
                _otm = 0
                _otm_reason = (f'downgraded ATM: ADX {_adx:.1f} < {_min_adx} '
                               f'(trend unconfirmed)')

            # ── Gap-and-go momentum boost ─────────────────────────────────────
            # A GAP_AND_GO day whose OR then breaks in the SAME direction is a
            # momentum stack: the market opened strong and the OR confirmed that
            # strength.  This warrants one extra OTM degree vs pure S/R distance.
            # Rules:
            #   - Boost only when S/R already grants room (_otm >= 1).
            #     If S/R returned ATM (wall/major close), that structural cap wins.
            #   - Cap at OTM+2 (never go beyond 2-strike OTM).
            #   - Counter-gap breakout (OR fails the gap direction) = gap-failure
            #     signal; valid trade but no boost.
            _gap_boost_on = getattr(config, 'PATH_A_GAP_OTM_BOOST', True)
            _gap_aligned  = (
                (sig_type == 'CALL' and self._gap_type == 'GAP_AND_GO_UP') or
                (sig_type == 'PUT'  and self._gap_type == 'GAP_AND_GO_DN')
            )
            _gap_counter  = (
                (sig_type == 'PUT'  and self._gap_type == 'GAP_AND_GO_UP') or
                (sig_type == 'CALL' and self._gap_type == 'GAP_AND_GO_DN')
            )
            if _gap_boost_on and _gap_aligned and _otm >= 1:
                _old_otm = _otm
                _otm     = min(_otm + 1, 2)
                _otm_reason = (f'gap-&-go boost OTM+{_old_otm}→OTM+{_otm} '
                               f'[{self._gap_type}]; {_otm_reason}')
            elif _gap_aligned and _otm == 0:
                # Gap confirms direction but wall/major is too close — log only
                _otm_reason += f' | gap-aligned ({self._gap_type}): ATM kept (S/R cap)'
            elif _gap_counter:
                # OR broke opposite the gap = gap failure = bearish/bullish trap signal
                _otm_reason += f' | counter-gap ({self._gap_type}): gap-failure trade'
        _otm_lbl = f' OTM+{_otm}' if _otm else ' ATM'

        # ── Late window guard 4: cap lots at 1 ──────────────────────────────────
        _late_lots_cap = getattr(config, 'PATH_A_LATE_LOTS', 1)
        _lots = (min(2 if strength >= 2 else 1, _late_lots_cap)
                 if _is_late else (2 if strength >= 2 else 1))

        _tag = ' (LATE)' if _is_late else ''
        self.logger.info(
            f"  [PATH-A{_tag}] {self.instrument} {sig_type} ORB ✓ | "
            f"px {_px:.1f} breaks {'above' if sig_type == 'CALL' else 'below'} "
            f"OR {'high' if sig_type == 'CALL' else 'low'} "
            f"{self._or_high if sig_type == 'CALL' else self._or_low:.1f} "
            f"| ADX {_adx:.1f} | gap={self._gap_type} | str={strength} | lots={_lots}"
            f" |{_otm_lbl} [{_otm_reason}]"
            + (f" | late-guards: DI-spd={abs(float(row.get('DI_plus', 0)) - float(row.get('DI_minus', 0))):.1f} ST5={str(row.get('ST_5m',''))}"
               if _is_late else "")
        )
        return {
            'type'       : sig_type,
            'price'      : _px,
            'adx'        : float(_adx),
            'strength'   : strength,
            'lots'       : _lots,
            'path'       : 'A',
            'otm_strikes': _otm,           # 0=ATM, 1=1-strike OTM, 2=2-strike OTM
            'otm_reason' : _otm_reason,    # why this OTM degree was chosen (for analysis)
            'gap_type'   : self._gap_type, # gap context at OR time (for JSONL analysis)
            'dynamic_or' : self._dynamic_or,  # True = fallback dynamic OR (not original ORB)
            'late_entry' : _is_late,       # True = fired in late ORB window (for analysis)
        }

    def _check_pivot(self, df: 'pd.DataFrame', htf: dict, oc: dict,
                     now: 'datetime', current_price: float, hv: float) -> 'dict | None':
        """Pivot logic: if open position is losing AND a reversal signal fires → close + pivot.

        Rules:
          - Profitable (pnl > round-trip cost %): hold regardless of any new signal.
          - Losing + same-direction signal: hold (don't churn into same strike).
          - Losing + opposite-direction signal: close and return the reversal signal.
          - Losing + no signal: hold (let stop manage).

        Returns the pivot signal dict if a close+pivot should happen, else None.
        """
        if not self.positions:
            return None

        pos = self.positions[0]
        elapsed_days = (datetime.now(IST) - pos['entry_time']).total_seconds() / 86400

        # Current option price (live LTP or Black-Scholes)
        if self.live and pos.get('option_symbol'):
            from fyers_orders import get_ltp
            current_opt = get_ltp(self.fyers, pos['option_symbol'])
            if current_opt is None:
                T_rem = max(config.DAYS_TO_EXPIRY - elapsed_days, 0.01) / 365
                current_opt = bs_price(pos['type'], current_price,
                                       pos['strike'], T_rem, hv)
        else:
            T_rem = max(config.DAYS_TO_EXPIRY - elapsed_days, 0.01) / 365
            current_opt = bs_price(pos['type'], current_price,
                                   pos['strike'], T_rem, hv)

        pnl_pct      = (current_opt - pos['entry_price']) / pos['entry_price']
        entry_value  = pos['entry_price'] * pos['lot_size']
        cost_pct     = (round_trip_costs(pos['entry_price'], current_opt, pos['lot_size'])
                        / entry_value) if entry_value > 0 else 0.015

        # ── Profitable: hold, no pivot ───────────────────────────────────────
        if pnl_pct > cost_pct:
            return None

        # ── Losing: check for best available signal ──────────────────────────
        signal = None
        _t = now.time()
        _orb_start = dtime(9, 30)
        _orb_end   = dtime(*[int(x) for x in
                             getattr(config, 'PATH_A_LATE_END', '12:00').split(':')])

        # Try Path A (ORB) if in window — includes late extension to 12:00
        if _orb_start <= _t < _orb_end and not self._path_a_fired:
            signal = self.get_path_a_signal(df, df.iloc[-1], now)

        # Try Paths C/D (CONT, ST_FLIP)
        if signal is None:
            signal = self.get_signal(df)

        # Try Path E (HTF Grind)
        if signal is None:
            signal = self.get_path_e_signal(df, htf)

        if signal is None:
            return None   # no signal — let stop manage

        # ── Same direction: hold (don't churn) ──────────────────────────────
        if signal['type'] == pos['type']:
            self.logger.info(
                f"  [PIVOT-HOLD] {self.instrument}: {pos['type']} position "
                f"({pnl_pct*100:+.1f}%) — Path {signal.get('path','?')} "
                f"agrees with direction, holding"
            )
            return None

        # ── Opposite direction: pivot ────────────────────────────────────────
        self.logger.info(
            f"  [PIVOT] {self.instrument}: {pos['type']} position "
            f"({pnl_pct*100:+.1f}%) losing — Path {signal.get('path','?')} "
            f"fires {signal['type']} reversal → closing + pivoting"
        )
        return signal

    def get_signal(self, df: pd.DataFrame) -> dict | None:
        min_bars = config.MOMENTUM_EMA_SLOW + config.EMA_CROSSOVER_LOOKBACK + 1
        if len(df) < min_bars:
            self.logger.warning(
                f"  [vX-SKIP] {self.instrument}: only {len(df)} bars, need {min_bars}. "
                f"EMA warm-up incomplete."
            )
            return None

        row = df.iloc[-1]
        nan_cols = [c for c in ['EMA_fast', 'EMA_slow', 'ADX'] if pd.isna(row.get(c))]
        if nan_cols:
            self.logger.warning(
                f"  [vX-SKIP] {self.instrument}: NaN in indicators {nan_cols} "
                f"at {df.index[-1]}. Skipping."
            )
            return None

        # ADX pre-filter (minimum of call/put thresholds) — INFO so it shows in prod logs
        _adx_pre = min(self.call_adx_min, self.put_adx_min)
        if row['ADX'] <= _adx_pre:
            self.logger.info(
                f"  [vX-SKIP] {self.instrument} ADX {row['ADX']:.1f} ≤ {_adx_pre} "
                f"(pre-filter). No trend strength yet."
            )
            return None

        # Volume filter (skip when volume data unavailable, e.g. index feed)
        if config.USE_VOLUME_FILTER:
            vol_ok = (not pd.isna(row['Volume_MA'])) and row['Volume_MA'] > 0
            if vol_ok and row['Volume'] < row['Volume_MA'] * config.VOLUME_MULTIPLIER:
                return None

        # ── Path B (MRB) ARCHIVED EMA 9/21 crossover [replaced Apr 2026] ─────────
        # Structural issue in morning-momentum regimes (Apr 2026 pattern: big move
        # 09:15–10:30, consolidation 11:00+): the EMA cross fires before the entry
        # window and is always stale (>3 bars) by 11:00. Path B (MRB) uses the
        # 09:15–10:55 OHLC range as reference — no freshness constraint needed.
        # Re-enable EMA cross: set config.PATH_B_ENABLED = False
        if not config.PATH_B_ENABLED:
            lookback = config.EMA_CROSSOVER_LOOKBACK
            window   = df.iloc[-(lookback + 1):]

            bull = any(
                window['EMA_fast'].iloc[j-1] <= window['EMA_slow'].iloc[j-1] and
                window['EMA_fast'].iloc[j]   >  window['EMA_slow'].iloc[j]
                for j in range(1, len(window))
            )
            bear = any(
                window['EMA_fast'].iloc[j-1] >= window['EMA_slow'].iloc[j-1] and
                window['EMA_fast'].iloc[j]   <  window['EMA_slow'].iloc[j]
                for j in range(1, len(window))
            )

            signal_type = None
            _ema_dir = 'BEAR' if row['EMA_fast'] < row['EMA_slow'] else 'BULL'

            if bull and row['EMA_fast'] > row['EMA_slow'] and row['Close'] > row['EMA_fast']:
                signal_type = 'CALL'
            elif bear and row['EMA_fast'] < row['EMA_slow'] and row['Close'] < row['EMA_fast']:
                signal_type = 'PUT'

            if signal_type is None:
                if _ema_dir == 'BEAR':
                    if not bear:
                        self.logger.info(
                            f"  [vX-SKIP] {self.instrument} BEAR but no fresh cross in last "
                            f"{lookback} bars. Cross too old or not yet confirmed."
                        )
                    elif row['Close'] >= row['EMA_fast']:
                        self.logger.info(
                            f"  [vX-SKIP] {self.instrument} PUT blocked: "
                            f"Close {row['Close']:.0f} >= EMA_fast {row['EMA_fast']:.0f}"
                        )
                elif _ema_dir == 'BULL':
                    if not bull:
                        self.logger.info(
                            f"  [vX-SKIP] {self.instrument} BULL but no fresh cross in last "
                            f"{lookback} bars. Cross too old or not yet confirmed."
                        )
                    elif row['Close'] <= row['EMA_fast']:
                        self.logger.info(
                            f"  [vX-SKIP] {self.instrument} CALL blocked: "
                            f"Close {row['Close']:.0f} <= EMA_fast {row['EMA_fast']:.0f}"
                        )
            else:
                _adx_thr = self.call_adx_min if signal_type == 'CALL' else self.put_adx_min
                if row['ADX'] <= _adx_thr:
                    self.logger.info(
                        f"  [vX-SKIP] {self.instrument} {signal_type} blocked: "
                        f"ADX {row['ADX']:.1f} ≤ {_adx_thr}"
                    )
                    signal_type = None
                if signal_type is not None and config.USE_VWAP_FILTER and 'VWAP' in df.columns:
                    vwap = row.get('VWAP', float('nan'))
                    if not pd.isna(vwap) and vwap > 0:
                        if signal_type == 'CALL' and row['Close'] < vwap:
                            self.logger.info(
                                f"  [vX-SKIP] {self.instrument} CALL blocked: "
                                f"price {row['Close']:.0f} < VWAP {vwap:.0f}"
                            )
                            signal_type = None
                        elif signal_type == 'PUT' and row['Close'] > vwap:
                            self.logger.info(
                                f"  [vX-SKIP] {self.instrument} PUT blocked: "
                                f"price {row['Close']:.0f} > VWAP {vwap:.0f}"
                            )
                            signal_type = None

            if signal_type:
                strength = 0
                if row['ADX'] >= 35:
                    strength += 1
                _di_spread = abs(row.get('DI_plus', 0) - row.get('DI_minus', 0))
                if _di_spread >= 5.0:
                    strength += 1
                if 'VWAP' in df.columns:
                    _vwap = row.get('VWAP', float('nan'))
                    if not pd.isna(_vwap) and _vwap > 0:
                        if abs(row['Close'] - _vwap) / _vwap >= 0.001:
                            strength += 1
                return {
                    'type'    : signal_type,
                    'price'   : row['Close'],
                    'adx'     : row['ADX'],
                    'strength': strength,
                    'lots'    : 2 if strength >= 2 else 1,
                    'path'    : 'vX',
                }

        # ── TMS: Trend Momentum Score (primary main-session signal) ────────────
        # Replaces the EMA freshness gate. Direction comes from EMA position
        # (EMA_fast > EMA_slow for CALL); cross TIMING is irrelevant. Momentum is
        # confirmed by scoring 5 independent components — all must agree.
        # Fires when score >= TMS_THRESHOLD (config, default 5 / 7 max).
        if config.TMS_ENABLED:
            _tms_ema_dir: 'str | None' = None
            _ef = float(row['EMA_fast'])
            _es = float(row['EMA_slow'])
            if _ef > _es:
                _tms_ema_dir = 'CALL'
            elif _ef < _es:
                _tms_ema_dir = 'PUT'

            if _tms_ema_dir is not None:
                _tms_adx  = float(row['ADX'])
                _tms_dip  = float(row.get('DI_plus',  0) or 0)
                _tms_dim  = float(row.get('DI_minus', 0) or 0)
                _tms_score = 0

                # 1. ADX strength (max +2)
                if _tms_adx >= config.TMS_ADX_HIGH:
                    _tms_score += 2
                elif _tms_adx >= config.TMS_ADX_LOW:
                    _tms_score += 1

                # 2. ADX slope — rising vs N bars ago (+1)
                _slope_bars = config.TMS_SLOPE_BARS
                if len(df) >= _slope_bars + 1:
                    _tms_adx_prev = float(df['ADX'].iloc[-(_slope_bars + 1)])
                    if _tms_adx > _tms_adx_prev:
                        _tms_score += 1

                # 3. DI dominant spread (max +2)
                _tms_di_dom = ((_tms_dip - _tms_dim) if _tms_ema_dir == 'CALL'
                               else (_tms_dim - _tms_dip))
                if _tms_di_dom >= config.TMS_DI_HIGH:
                    _tms_score += 2
                elif _tms_di_dom >= config.TMS_DI_LOW:
                    _tms_score += 1

                # 4. EMA spread widening vs N bars ago (+1)
                if len(df) >= _slope_bars + 1:
                    _spread_now  = abs(_ef - _es)
                    _spread_prev = abs(
                        float(df['EMA_fast'].iloc[-(_slope_bars + 1)]) -
                        float(df['EMA_slow'].iloc[-(_slope_bars + 1)])
                    )
                    if _spread_now > _spread_prev:
                        _tms_score += 1

                # 5. 5m SuperTrend aligned (+1)
                if 'ST_5m' in df.columns:
                    _tms_st = row.get('ST_5m', 0)
                    if _tms_ema_dir == 'CALL' and _tms_st == 1:
                        _tms_score += 1
                    elif _tms_ema_dir == 'PUT' and _tms_st == -1:
                        _tms_score += 1

                self.logger.info(
                    f"  [TMS] {self.instrument} {_tms_ema_dir}: "
                    f"score={_tms_score}/7 | ADX={_tms_adx:.1f} "
                    f"| DI_dom={_tms_di_dom:.1f} | EMA={'>' if _ef > _es else '<'}"
                )

                if _tms_score >= config.TMS_THRESHOLD:
                    _tms_adx_thr = (self.call_adx_min if _tms_ema_dir == 'CALL'
                                    else self.put_adx_min)
                    if _tms_adx <= _tms_adx_thr:
                        self.logger.info(
                            f"  [TMS-SKIP] {self.instrument} {_tms_ema_dir}: "
                            f"ADX {_tms_adx:.1f} ≤ {_tms_adx_thr} threshold"
                        )
                    else:
                        _tms_vwap_ok = True
                        if config.USE_VWAP_FILTER and 'VWAP' in df.columns:
                            _tv = row.get('VWAP', float('nan'))
                            if not pd.isna(_tv) and _tv > 0:
                                if _tms_ema_dir == 'CALL' and row['Close'] < _tv:
                                    _tms_vwap_ok = False
                                    self.logger.info(
                                        f"  [TMS-SKIP] {self.instrument} CALL: "
                                        f"price {row['Close']:.0f} < VWAP {_tv:.0f}"
                                    )
                                elif _tms_ema_dir == 'PUT' and row['Close'] > _tv:
                                    _tms_vwap_ok = False
                                    self.logger.info(
                                        f"  [TMS-SKIP] {self.instrument} PUT: "
                                        f"price {row['Close']:.0f} > VWAP {_tv:.0f}"
                                    )

                        if _tms_vwap_ok:
                            _tms_strength = 0
                            if _tms_adx >= 35:
                                _tms_strength += 1
                            if abs(_tms_dip - _tms_dim) >= 5.0:
                                _tms_strength += 1
                            _tv2 = row.get('VWAP', float('nan'))
                            if not pd.isna(_tv2) and _tv2 > 0:
                                if abs(row['Close'] - _tv2) / _tv2 >= 0.001:
                                    _tms_strength += 1
                            return {
                                'type'    : _tms_ema_dir,
                                'price'   : row['Close'],
                                'adx'     : _tms_adx,
                                'tms'     : _tms_score,
                                'strength': _tms_strength,
                                'lots'    : 2 if _tms_strength >= 2 else 1,
                                'path'    : 'vX',
                            }

        # ── Path C (CONT): EMA Spread Widening (Continuation) ──────────────────
        # DISABLED Apr 2026 (PATH_C_ENABLED=False): no live edge confirmed.
        # Phantom trade risk on non-ORB days. Re-enable after 30+ ORB live days.
        # Original design: spread widening 3 bars + ADX≥35 + 12:00+ start.
        # Backtest (275 days NIFTY): 69 trades, 48% WR standalone → ~58% with HTF.
        _now_t = df.index[-1].time()
        _cont_adx_min = 35   # higher conviction required for continuation entries
        _cont_start   = dtime(12, 0)

        if getattr(config, 'PATH_C_ENABLED', False) and _now_t >= _cont_start and len(df) >= 4:
            _ef = float(row['EMA_fast'])
            _es = float(row['EMA_slow'])
            _cont_dir: str | None = None
            if _ef > _es:
                _cont_dir = 'CALL'
            elif _ef < _es:
                _cont_dir = 'PUT'

            if _cont_dir is not None and float(row['ADX']) > _cont_adx_min:
                # VWAP filter
                _cont_vwap_ok = False
                if config.USE_VWAP_FILTER and 'VWAP' in df.columns:
                    _cv = row.get('VWAP', float('nan'))
                    if not pd.isna(_cv) and _cv > 0:
                        _cont_vwap_ok = (
                            (_cont_dir == 'CALL' and row['Close'] > _cv) or
                            (_cont_dir == 'PUT'  and row['Close'] < _cv)
                        )
                else:
                    _cont_vwap_ok = True   # filter disabled

                if _cont_vwap_ok:
                    # EMA spread widening: last 3 bars each wider than the previous
                    _spreads = [
                        abs(float(df['EMA_fast'].iloc[-3]) - float(df['EMA_slow'].iloc[-3])),
                        abs(float(df['EMA_fast'].iloc[-2]) - float(df['EMA_slow'].iloc[-2])),
                        abs(float(df['EMA_fast'].iloc[-1]) - float(df['EMA_slow'].iloc[-1])),
                    ]
                    _widening = _spreads[0] < _spreads[1] < _spreads[2]

                    if _widening:
                        # Guard: ensure no fresh in-window crossover exists
                        # (Mode A would have returned already if there was one —
                        #  this is a double-check in case ADX gated Mode A out)
                        _lb       = config.EMA_CROSSOVER_LOOKBACK
                        _win_cont = df.iloc[-(_lb + 1):]
                        _has_fresh = any(
                            ((_win_cont['EMA_fast'].iloc[j-1] <= _win_cont['EMA_slow'].iloc[j-1] and
                              _win_cont['EMA_fast'].iloc[j]   >  _win_cont['EMA_slow'].iloc[j]) or
                             (_win_cont['EMA_fast'].iloc[j-1] >= _win_cont['EMA_slow'].iloc[j-1] and
                              _win_cont['EMA_fast'].iloc[j]   <  _win_cont['EMA_slow'].iloc[j]))
                            for j in range(1, len(_win_cont))
                        )

                        if not _has_fresh:
                            _cont_strength = 0
                            if float(row['ADX']) >= 40:
                                _cont_strength += 1
                            if _spreads[2] > _spreads[1] * 1.05:   # spread grew ≥5%
                                _cont_strength += 1
                            if 'VWAP' in df.columns:
                                _cvc = row.get('VWAP', float('nan'))
                                if not pd.isna(_cvc) and _cvc > 0:
                                    if abs(row['Close'] - _cvc) / _cvc >= 0.001:
                                        _cont_strength += 1
                            self.logger.info(
                                f"  [CONT] {self.instrument} {_cont_dir} spread widening "
                                f"({_spreads[0]:.1f}→{_spreads[1]:.1f}→{_spreads[2]:.1f}) "
                                f"ADX={row['ADX']:.1f} strength={_cont_strength}"
                            )
                            return {
                                'type'    : _cont_dir,
                                'price'   : row['Close'],
                                'adx'     : row['ADX'],
                                'strength': _cont_strength,
                                'lots'    : 2 if _cont_strength >= 2 else 1,
                                'path'    : 'CONT',
                            }

        # ── Path D (ST_FLIP): 5m SuperTrend direction change ─────────────────────
        # DISABLED Apr 2026 (PATH_D_ENABLED=False): no live edge confirmed.
        # Phantom trade risk on non-ORB days. Re-enable after 30+ ORB live days.
        # Original design: 5m ST flip + ADX + VWAP — catches structural breakouts
        # that vX misses because the EMA cross is stale.
        if getattr(config, 'PATH_D_ENABLED', False) and 'ST_5m' in df.columns and len(df) >= 2:
            _st_curr = int(row.get('ST_5m', 0))
            _st_prev = int(df.iloc[-2].get('ST_5m', 0))

            if _st_curr != _st_prev and _st_curr in (1, -1):
                st_type = 'CALL' if _st_curr == 1 else 'PUT'
                _st_adx_thr = self.call_adx_min if st_type == 'CALL' else self.put_adx_min

                if row['ADX'] <= _st_adx_thr:
                    self.logger.info(
                        f"  [ST-SKIP] {self.instrument} {st_type} flip blocked: "
                        f"ADX {row['ADX']:.1f} ≤ {_st_adx_thr}"
                    )
                else:
                    _st_vwap_ok = True
                    if config.USE_VWAP_FILTER and 'VWAP' in df.columns:
                        _sv = row.get('VWAP', float('nan'))
                        if not pd.isna(_sv) and _sv > 0:
                            if st_type == 'CALL' and row['Close'] < _sv:
                                self.logger.info(
                                    f"  [ST-SKIP] {self.instrument} CALL flip blocked: "
                                    f"price {row['Close']:.0f} < VWAP {_sv:.0f}"
                                )
                                _st_vwap_ok = False
                            elif st_type == 'PUT' and row['Close'] > _sv:
                                self.logger.info(
                                    f"  [ST-SKIP] {self.instrument} PUT flip blocked: "
                                    f"price {row['Close']:.0f} > VWAP {_sv:.0f}"
                                )
                                _st_vwap_ok = False

                    if _st_vwap_ok:
                        st_strength = 0
                        if row['ADX'] >= 35:
                            st_strength += 1
                        _ef = row['EMA_fast']
                        _es = row['EMA_slow']
                        if _es != 0 and abs(_ef - _es) / _es >= 0.0004:
                            st_strength += 1
                        if 'VWAP' in df.columns:
                            _sv2 = row.get('VWAP', float('nan'))
                            if not pd.isna(_sv2) and _sv2 > 0:
                                if abs(row['Close'] - _sv2) / _sv2 >= 0.001:
                                    st_strength += 1
                        return {
                            'type'    : st_type,
                            'price'   : row['Close'],
                            'adx'     : row['ADX'],
                            'strength': st_strength,
                            'lots'    : 2 if st_strength >= 2 else 1,
                            'path'    : 'ST_FLIP',
                        }

        return None

    def get_path_b_signal(self, df: pd.DataFrame,
                       htf: dict, oc: dict) -> dict | None:
        """
        Path B — Morning Range Breakout (replaces EMA 9/21 crossover).

        Range: High/Low of all 09:15–10:55 bars (before the 11:00 entry window).
        Entry: Close > MR_high × (1+buffer) → CALL
               Close < MR_low  × (1-buffer) → PUT

        Filters applied in order:
          1. ADX ≥ config.PATH_B_ADX_MIN (uniform, no CALL/PUT asymmetry)
          2. VWAP aligned (CALL above, PUT below)
          3. 15m SuperTrend aligned (if PATH_B_HTF_REQUIRED)
          4. PCR gate (NIFTY/BANKNIFTY only — suppress against strong OI bias)
          5. MaxPain gravity guard (within 0.5% → suppress)

        OI snap: if a MAJOR/WALL OI level is within PATH_B_OI_SNAP_DIST of the
        OHLC range edge, the boundary is snapped to that level (true market wall).

        Once per day (caller sets _path_b_fired = True when signal is returned).
        """
        now   = datetime.now(IST)
        today = now.date()

        # ── Compute morning range ────────────────────────────────────────────
        _range_end = dtime(*map(int, config.PATH_B_RANGE_END.split(':')))
        today_df  = df[df.index.date == today]
        mr_bars   = today_df[today_df.index.time <= _range_end]

        if len(mr_bars) < 3:
            return None   # not enough morning bars yet

        mr_high = float(mr_bars['High'].max())
        mr_low  = float(mr_bars['Low'].min())

        # ── OI wall snap (NIFTY / BANKNIFTY only — SENSEX has no BSE OI) ───
        if self._oi_zones and self.instrument != 'SENSEX':
            _snap = config.PATH_B_OI_SNAP_DIST
            for _r in self._oi_zones.get('resistance', []):
                if _r.get('strength') in ('MAJOR', 'WALL'):
                    _strike = float(_r.get('strike', 0))
                    if _strike > 0 and abs(_strike - mr_high) / mr_high < _snap:
                        self.logger.info(
                            f"  [PATH-B] {self.instrument}: MR_high snapped "
                            f"{mr_high:.0f}→{_strike:.0f} (OI {_r['strength']})"
                        )
                        mr_high = _strike
                        break
            for _s in self._oi_zones.get('support', []):
                if _s.get('strength') in ('MAJOR', 'WALL'):
                    _strike = float(_s.get('strike', 0))
                    if _strike > 0 and abs(_strike - mr_low) / mr_low < _snap:
                        self.logger.info(
                            f"  [PATH-B] {self.instrument}: MR_low snapped "
                            f"{mr_low:.0f}→{_strike:.0f} (OI {_s['strength']})"
                        )
                        mr_low = _strike
                        break

        row  = df.iloc[-1]
        _px  = float(row['Close'])
        _adx = float(row.get('ADX', 0) or 0)
        _vwap = row.get('VWAP', float('nan'))
        if isinstance(_vwap, (int, float)):
            _vwap = float(_vwap)

        # ── Breakout check ──────────────────────────────────────────────────
        _buf        = config.PATH_B_BUFFER
        _call_break = _px > mr_high * (1.0 + _buf)
        _put_break  = _px < mr_low  * (1.0 - _buf)

        if not _call_break and not _put_break:
            # Log range proximity on every new bar (throttled by caller's _last_scan_ts)
            _to_top  = mr_high - _px
            _to_bot  = _px - mr_low
            _rng_pct = (mr_high - mr_low) / mr_low * 100 if mr_low > 0 else 0
            self.logger.info(
                f"  [PATH-B-SCAN] {self.instrument}: "
                f"MR=[{mr_low:.0f},{mr_high:.0f}] ({_rng_pct:.1f}%) | "
                f"Px={_px:.0f} | "
                f"{_to_bot:.0f}pt↑bot / {_to_top:.0f}pt→top | "
                f"ADX={_adx:.1f}(need>{config.PATH_B_ADX_MIN})"
            )
            return None

        sig_type = 'CALL' if _call_break else 'PUT'

        # ── Break freshness guard (Jul 13, from Path B's first live fire) ────
        # A breakout entry belongs at the boundary. When ADX lags a drift-led
        # break, this signal otherwise fires 30-60 min late at full extension
        # (Jul 13 SENSEX: boundary crossed ~12:15 @ ADX 10.6, fired 12:55 at
        # +0.39% beyond the range — the day's top tick, -25.8%). Require the
        # last inside-range close to be within PATH_B_MAX_BREAK_AGE_BARS bars.
        _max_age = getattr(config, 'PATH_B_MAX_BREAK_AGE_BARS', 3)
        if _max_age > 0 and len(today_df) >= 2:
            _thr_hi = mr_high * (1.0 + _buf)
            _thr_lo = mr_low  * (1.0 - _buf)
            _closes = today_df['Close'].tolist()
            _age = None
            for _i in range(len(_closes) - 2, -1, -1):   # skip current bar
                _c = float(_closes[_i])
                if (_call_break and _c <= _thr_hi) or (_put_break and _c >= _thr_lo):
                    _age = len(_closes) - 1 - _i
                    break
            if _age is None or _age > _max_age:
                _age_str = f"{_age}" if _age is not None else f">{len(_closes)}"
                self.logger.info(
                    f"  [PATH-B-SKIP] {self.instrument} {sig_type}: stale break — "
                    f"boundary crossed {_age_str} bars ago (max {_max_age}); "
                    f"edge is at the boundary, not the chase"
                )
                return None

        # ── ADX filter ──────────────────────────────────────────────────────
        if _adx < config.PATH_B_ADX_MIN:
            self.logger.info(
                f"  [PATH-B-SKIP] {self.instrument} {sig_type} breakout blocked: "
                f"ADX {_adx:.1f} < {config.PATH_B_ADX_MIN}"
            )
            return None

        # ── VWAP filter ─────────────────────────────────────────────────────
        if config.USE_VWAP_FILTER and not pd.isna(_vwap) and _vwap > 0:
            if sig_type == 'CALL' and _px < _vwap:
                self.logger.info(
                    f"  [PATH-B-SKIP] {self.instrument} CALL: px {_px:.0f} < VWAP {_vwap:.0f}"
                )
                return None
            if sig_type == 'PUT' and _px > _vwap:
                self.logger.info(
                    f"  [PATH-B-SKIP] {self.instrument} PUT: px {_px:.0f} > VWAP {_vwap:.0f}"
                )
                return None

        # ── 15m SuperTrend alignment ─────────────────────────────────────────
        if config.PATH_B_HTF_REQUIRED:
            _st15 = htf.get('supertrend_15m')
            if sig_type == 'CALL' and _st15 != 1:
                self.logger.info(
                    f"  [PATH-B-SKIP] {self.instrument} CALL: 15m ST not BULL (st15={_st15})"
                )
                return None
            if sig_type == 'PUT' and _st15 != -1:
                self.logger.info(
                    f"  [PATH-B-SKIP] {self.instrument} PUT: 15m ST not BEAR (st15={_st15})"
                )
                return None

        # ── PCR directional gate (NIFTY/BANKNIFTY only) ─────────────────────
        if oc and self.instrument != 'SENSEX':
            _pcr = oc.get('pcr')
            if _pcr is not None:
                if sig_type == 'PUT' and _pcr > config.PATH_B_PCR_BULL_GATE:
                    self.logger.info(
                        f"  [PATH-B-SKIP] {self.instrument} PUT: "
                        f"PCR={_pcr:.2f} > {config.PATH_B_PCR_BULL_GATE} "
                        f"(heavy put writing — bullish OI, bearish breakout suspect)"
                    )
                    return None
                if sig_type == 'CALL' and _pcr < config.PATH_B_PCR_BEAR_GATE:
                    self.logger.info(
                        f"  [PATH-B-SKIP] {self.instrument} CALL: "
                        f"PCR={_pcr:.2f} < {config.PATH_B_PCR_BEAR_GATE} "
                        f"(heavy call writing — bearish OI, bullish breakout suspect)"
                    )
                    return None

        # ── MaxPain gravity guard (NIFTY/BANKNIFTY only) ────────────────────
        if oc and self.instrument != 'SENSEX':
            _mp = oc.get('max_pain')
            if _mp is not None and _mp > 0:
                _mp_dist = abs(_px - _mp) / _px
                if _mp_dist < config.PATH_B_MAX_PAIN_BUFFER:
                    self.logger.info(
                        f"  [PATH-B-SKIP] {self.instrument} {sig_type}: "
                        f"px {_px:.0f} within {_mp_dist*100:.1f}% of MaxPain {_mp:.0f} "
                        f"— gravity pin risk"
                    )
                    return None

        # ── Strength scoring ─────────────────────────────────────────────────
        strength = 0
        if _adx >= 35:
            strength += 1          # very strong trend momentum
        _break_pct = ((_px - mr_high) / mr_high if sig_type == 'CALL'
                      else (mr_low - _px) / mr_low)
        if _break_pct > 0.002:    # > 0.2% outside range → strong conviction
            strength += 1
        if not pd.isna(_vwap) and _vwap > 0:
            if abs(_px - _vwap) / _vwap >= 0.001:
                strength += 1     # clear directional distance from VWAP

        self.logger.info(
            f"  [PATH-B] {self.instrument} {sig_type} BREAKOUT ✓ | "
            f"MR=[{mr_low:.0f},{mr_high:.0f}] | "
            f"Px={_px:.0f} (+{_break_pct*100:.2f}% outside) | "
            f"ADX={_adx:.1f} | VWAP={_vwap:.0f} | Strength={strength}"
        )
        return {
            'type'    : sig_type,
            'price'   : _px,
            'adx'     : _adx,
            'strength': strength,
            'lots'    : 2 if strength >= 2 else 1,
            'path'    : 'B',
        }

    def get_path_e_signal(self, df: 'pd.DataFrame', htf: dict) -> 'dict | None':
        """Path E — HTF Trend Continuation (no fresh crossover required).

        Fires when:
          - Inside PATH_E window (12:30–13:45 by default)
          - DI+ > DI- (or DI- > DI+) for ≥ PATH_E_DI_BARS consecutive bars
          - ADX ≥ PATH_E_ADX_MIN
          - 15m SuperTrend agrees with DI direction
          - Price on correct side of VWAP
          - No Path E trade has fired today (_path_e_fired)
        """
        if not config.PATH_E_ENABLED or self._path_e_fired:
            return None

        _pf_start = dtime(*map(int, config.PATH_E_START.split(':')))
        _pf_end   = dtime(*map(int, config.PATH_E_END.split(':')))
        now_t = df.index[-1].time()
        if not (_pf_start <= now_t <= _pf_end):
            return None

        n = config.PATH_E_DI_BARS
        if len(df) < n + 1:
            return None

        row = df.iloc[-1]
        if any(pd.isna(row.get(c)) for c in ['DI_plus', 'DI_minus', 'ADX']):
            return None

        _adx = float(row['ADX'])
        if _adx < config.PATH_E_ADX_MIN:
            return None

        _dip = float(row['DI_plus'])
        _dim = float(row['DI_minus'])
        if _dip == _dim:
            return None
        if abs(_dip - _dim) < 12:   # DI spread gate: require meaningful dominance
            return None

        # DI sustained alignment for ≥ n bars
        recent = df.iloc[-n:]
        signal_type = None
        if _dip > _dim and (recent['DI_plus'] > recent['DI_minus']).all():
            signal_type = 'CALL'
        elif _dim > _dip and (recent['DI_minus'] > recent['DI_plus']).all():
            signal_type = 'PUT'

        if signal_type is None:
            return None

        # 5m SuperTrend must agree (ST_5m column, already computed in add_indicators)
        if 'ST_5m' in df.columns:
            _st5 = row.get('ST_5m', 0)
            if not pd.isna(_st5):
                if signal_type == 'CALL' and int(_st5) != 1:
                    self.logger.info(f"  [PATH-E] {self.instrument} CALL blocked: 5m ST=BEAR")
                    return None
                if signal_type == 'PUT' and int(_st5) != -1:
                    self.logger.info(f"  [PATH-E] {self.instrument} PUT blocked: 5m ST=BULL")
                    return None

        # 15m SuperTrend must agree
        st15 = htf.get('supertrend_15m')
        if signal_type == 'CALL' and st15 != 1:
            self.logger.info(
                f"  [PATH-E] {self.instrument} CALL blocked: 15m ST="
                f"{'BEAR' if st15 == -1 else 'None'} (needs BULL)"
            )
            return None
        if signal_type == 'PUT' and st15 != -1:
            self.logger.info(
                f"  [PATH-E] {self.instrument} PUT blocked: 15m ST="
                f"{'BULL' if st15 == 1 else 'None'} (needs BEAR)"
            )
            return None

        # VWAP filter
        if config.USE_VWAP_FILTER and 'VWAP' in df.columns:
            _vwap = row.get('VWAP', float('nan'))
            if not pd.isna(_vwap) and _vwap > 0:
                if signal_type == 'CALL' and row['Close'] < _vwap:
                    self.logger.info(
                        f"  [PATH-E] {self.instrument} CALL blocked: "
                        f"price {row['Close']:.0f} < VWAP {_vwap:.0f}"
                    )
                    return None
                if signal_type == 'PUT' and row['Close'] > _vwap:
                    self.logger.info(
                        f"  [PATH-E] {self.instrument} PUT blocked: "
                        f"price {row['Close']:.0f} > VWAP {_vwap:.0f}"
                    )
                    return None

        # Strength scoring (lower bar than vX — grind days are subtle)
        strength = 0
        if _adx >= 30:
            strength += 1
        if abs(_dip - _dim) >= 10:
            strength += 1
        if 'VWAP' in df.columns:
            _vwap2 = row.get('VWAP', float('nan'))
            if not pd.isna(_vwap2) and _vwap2 > 0:
                if abs(row['Close'] - _vwap2) / _vwap2 >= 0.001:
                    strength += 1

        self.logger.info(
            f"  [PATH-E] {self.instrument} {signal_type} | "
            f"DI+={_dip:.1f} DI-={_dim:.1f} ({n} bars sustained) | "
            f"ADX={_adx:.1f} | 15m-ST={'BULL' if st15 == 1 else 'BEAR'} | "
            f"strength={strength}"
        )
        self._path_e_fired = True
        return {
            'type'    : signal_type,
            'price'   : row['Close'],
            'adx'     : _adx,
            'strength': strength,
            'lots'    : 2 if strength >= 2 else 1,
            'path'    : 'E',
        }

    def _duplicate_signal(self, signal: dict) -> bool:
        return any(p['type'] == signal['type'] for p in self.positions)

    # ── Challenger: OI-guided strike selection ────────────────────────────────

    _CHALLENGER_MIN_OI = 50_000   # minimum OI for an OTM strike to be considered liquid

    def select_challenger_strike(self, signal_type: str,
                                  underlying: float, oc: dict) -> int:
        """
        OI-guided strike selection for Challenger shadow strategy.

        Evaluates up to 3 strikes in the OTM direction from ATM:
          CALL: ATM, ATM+1×gap, ATM+2×gap
          PUT : ATM, ATM-1×gap, ATM-2×gap

        Scoring per candidate (higher = better):
          iv_discount : atm_iv - strike_iv  (positive = cheaper than ATM = undervalued)
          mp_bonus    : +0.5 if strike is on the favourable side of max_pain
                        (CALL ≤ max_pain or PUT ≥ max_pain → price gravity helps)

        OTM strikes with OI < _CHALLENGER_MIN_OI are skipped (illiquid).
        ATM is always kept as fallback (score = mp_bonus only, iv_discount = 0).
        Falls back to ATM when no option-chain data is available.
        """
        atm_strike   = int(round(underlying / self.strike_gap) * self.strike_gap)
        strikes_data = oc.get('strikes', {})
        atm_iv       = oc.get('atm_iv') or 0.0
        max_pain     = oc.get('max_pain')

        if signal_type == 'CALL':
            candidates = [atm_strike + n * self.strike_gap for n in range(3)]
        else:
            candidates = [atm_strike - n * self.strike_gap for n in range(3)]

        best_strike = atm_strike
        best_score  = -999.0

        for cand in candidates:
            sdata = strikes_data.get(cand, {})
            if signal_type == 'CALL':
                oi = sdata.get('call_oi', 0)
                iv = sdata.get('call_iv', atm_iv)
            else:
                oi = sdata.get('put_oi', 0)
                iv = sdata.get('put_iv', atm_iv)

            # Skip illiquid OTM strikes; ATM always eligible
            if oi < self._CHALLENGER_MIN_OI and cand != atm_strike:
                continue

            # IV discount: lower IV than ATM = undervalued (positive = good)
            iv_discount = (atm_iv - iv) if atm_iv else 0.0

            # Max-pain alignment: price gravitates toward max_pain near expiry
            mp_bonus = 0.0
            if max_pain:
                if signal_type == 'CALL' and cand <= max_pain:
                    mp_bonus = 0.5   # CALL strike at/below max pain — gravity pulls price here
                elif signal_type == 'PUT' and cand >= max_pain:
                    mp_bonus = 0.5   # PUT strike at/above max pain — gravity helps

            score = iv_discount + mp_bonus
            if score > best_score:
                best_score  = score
                best_strike = cand

        return best_strike

    # ── OI Influence (lot sizing) ─────────────────────────────────────────────

    def _oi_adjust_lots(self, signal_type: str, base_lots: int,
                        oc: dict) -> tuple[int, str]:
        """
        OI-influenced lot adjustment — modulates size, never gates the trade.

        The core signal (EMA/ADX/VWAP) still decides IF and WHEN to enter.
        OI only adjusts HOW MUCH:

          PCR > 1.2  (heavy put writing  → bullish)  + CALL signal → +1 lot
          PCR < 0.7  (heavy call writing → bearish)  + PUT  signal → +1 lot
          PCR < 0.7  (heavy call writing → bearish)  + CALL signal → -1 lot
          PCR > 1.2  (heavy put writing  → bullish)  + PUT  signal → -1 lot

        Max-pain alignment is logged for observation but doesn't change sizing
        (requires multi-week data to calibrate; log first, tune later).

        Lots are hard-capped at [1, 2] — OI cannot exceed vX's 2-lot maximum
        or drop below 1 lot (we always take the trade the signal approves).
        """
        lots    = base_lots
        reasons = []
        pcr      = oc.get('pcr')
        max_pain = oc.get('max_pain')

        if pcr is not None:
            if signal_type == 'CALL' and pcr > 1.2:
                if lots < 2:
                    lots += 1
                    reasons.append(f"PCR={pcr:.2f}↑ (put writing→bullish) +1 lot")
            elif signal_type == 'PUT' and pcr < 0.7:
                if lots < 2:
                    lots += 1
                    reasons.append(f"PCR={pcr:.2f}↓ (call writing→bearish) +1 lot")
            elif signal_type == 'CALL' and pcr < 0.7:
                if lots > 1:
                    lots -= 1
                    reasons.append(f"PCR={pcr:.2f}↓ (call writing→bearish conflicts CALL) -1 lot")
            elif signal_type == 'PUT' and pcr > 1.2:
                if lots > 1:
                    lots -= 1
                    reasons.append(f"PCR={pcr:.2f}↑ (put writing→bullish conflicts PUT) -1 lot")

        # Max-pain note (observation only — calibrate thresholds after 4+ weeks live data)
        if max_pain is not None and oc.get('atm_iv') is not None:
            # Rough current price from underlying (from the oc context we don't have
            # underlying directly, so we skip the alignment note here — it's logged
            # in the main loop Context line via oc['max_pain'])
            pass

        return lots, ' | '.join(reasons)

    # ── Trade Entry ───────────────────────────────────────────────────────────

    def _get_risk_params(self) -> tuple:
        """Daily-cached capital-scaled (risk_cap_rs, max_lots) — v1.7.

        Reads the combined live book once per day (JSONL scan via
        capital_gate.get_risk_params) so the cap tracks capital growth and
        drawdown without re-reading files on every entry evaluation.
        """
        _today = datetime.now(IST).date()
        if getattr(self, '_risk_params_date', None) != _today:
            from capital_gate import get_risk_params
            _cap, _max_lots, _book = get_risk_params(self.logger)
            self._risk_cap_today   = _cap
            self._max_lots_today   = _max_lots
            self._risk_params_date = _today
            _book_s = f"₹{_book:,.0f}" if _book is not None else "unreadable"
            self.logger.info(
                f"[RISK-SCALE] {self.instrument}: book={_book_s} → "
                f"risk cap ₹{_cap:,.0f}/trade | max lots {_max_lots}"
            )
        return self._risk_cap_today, self._max_lots_today

    def enter_trade(self, signal: dict, hv: float, lots: int = 1) -> None:
        underlying   = signal['price']
        _atm         = int(round(underlying / self.strike_gap) * self.strike_gap)
        _otm         = signal.get('otm_strikes', 0)   # 0=ATM, 1=1-strike OTM, 2=2-strike OTM
        # OTM: CALL → higher strike (further from money), PUT → lower strike
        if signal['type'] == 'CALL':
            strike = _atm + _otm * self.strike_gap
        else:
            strike = _atm - _otm * self.strike_gap
        eff_lot_size = self.lot_size * lots   # 1× or 2× based on signal strength score

        # ── Dynamic profit target (IV-scaled) ────────────────────────────────
        # Scale BASE_TARGET up/down proportionally with ATM-IV at entry.
        # High-IV days have bigger intraday swings → option moves further per index point.
        # Low-IV days have tighter swings → exit earlier to avoid theta decay.
        _atm_iv_entry = signal.get('atm_iv')
        if getattr(config, 'TARGET_IV_DYNAMIC', False) and _atm_iv_entry:
            _iv_ref  = getattr(config, 'TARGET_IV_REF',     15.0)
            _iv_tmin = getattr(config, 'TARGET_IV_MIN_PCT',  0.20)
            _iv_tmax = getattr(config, 'TARGET_IV_MAX_PCT',  0.50)
            _dyn_tgt = max(_iv_tmin, min(_iv_tmax,
                           config.BASE_TARGET * (_atm_iv_entry / _iv_ref)))
        else:
            _dyn_tgt = config.BASE_TARGET

        # ── Phase 3 target scaling (Late Harvest 13:00–14:30) ────────────────
        # Less runway before 14:30 force-close + accelerating theta decay → take
        # profits quicker. Scale _dyn_tgt down to STRATEGY_PHASE3_TARGET_SCALE.
        _phase3_start = getattr(config, 'STRATEGY_PHASE3_START', '13:00')
        _phase3_scale = getattr(config, 'STRATEGY_PHASE3_TARGET_SCALE', 0.70)
        _now_str = datetime.now(IST).strftime('%H:%M')
        if _now_str >= _phase3_start:
            _dyn_tgt_orig = _dyn_tgt
            _dyn_tgt = round(_dyn_tgt * _phase3_scale, 4)
            self.logger.info(
                f"  [PHASE-3] Entry after {_phase3_start} → target scaled "
                f"{_dyn_tgt_orig*100:.0f}% → {_dyn_tgt*100:.0f}% "
                f"(×{_phase3_scale:.0%} — less runway to 14:30)"
            )

        # ── Capital fallback: step OTM if entry is too expensive ─────────────
        # If entry_price × lot_size > instrument_capital × CAPITAL_FALLBACK_THRESHOLD,
        # try 1 / 2 / 3 strikes further OTM (cheaper premium, smaller lot cost).
        # Ensures we can always place a trade even on high-premium days.
        _inst_capital = self.capital   # live capital — updates after each trade (not stale config)
        _max_spend    = _inst_capital * getattr(config, 'CAPITAL_FALLBACK_THRESHOLD', 0.80)
        _orig_strike  = strike
        for _fb in range(4):   # 0 = preferred strike; 1-3 = OTM fallbacks
            if _fb > 0:
                strike = (strike + self.strike_gap if signal['type'] == 'CALL'
                          else strike - self.strike_gap)
            if self.live:
                from fyers_orders import (build_option_symbol,
                                          get_next_expiry, get_ltp)
                _fb_sym   = build_option_symbol(self.instrument, strike,
                                                signal['type'],
                                                get_next_expiry(self.instrument))
                _fb_price = get_ltp(self.fyers, _fb_sym)
                if _fb_price is None:
                    break   # LTP unavailable — proceed with current strike as-is
                _fb_price = float(_fb_price)
            else:
                _fb_price = bs_price(signal['type'], underlying, strike,
                                     config.DAYS_TO_EXPIRY / 365, hv)
            if _fb_price * eff_lot_size <= _max_spend or _fb == 3:
                if _fb > 0:
                    self.logger.info(
                        f"[CAPITAL-FALLBACK] {self.instrument}: "
                        f"{signal['type']} {_orig_strike} too expensive "
                        f"(~₹{_fb_price:.0f}×{eff_lot_size}=₹{_fb_price*eff_lot_size:.0f} "
                        f"> budget ₹{_max_spend:.0f}) → using {strike}"
                    )
                break

        # Inject final strike into signal so fyers_orders uses the same value
        signal['strike'] = strike

        # Determine stop % for this path (used for exchange SL-M order)
        _path      = signal.get('path', '')
        _stop_pct  = (self.inst_cfg.get('path_a_stop', config.PATH_A_STOP)
                      if _path == 'A' else config.STOP_LOSS)

        # ── Risk-cap sizing ladder (reinstated Jul 8 — Jun 10 gate lost to deploy drift)
        # Rupee risk = premium × contracts × stop%. Multi-lot entries that bust
        # the cap first try one strike further OTM at the same lots (cheaper
        # premium), then shave to 1 lot; if even 1 lot busts, skip.
        # v1.7: cap is capital-scaled (book × 10%, floor ₹2,500) — see _get_risk_params.
        _risk_cap, _ = self._get_risk_params()
        if _fb_price:
            _risk = _fb_price * eff_lot_size * _stop_pct
            if _risk > _risk_cap and lots > 1:
                _alt_strike = (strike + self.strike_gap if signal['type'] == 'CALL'
                               else strike - self.strike_gap)
                if self.live:
                    from fyers_orders import (build_option_symbol,
                                              get_next_expiry, get_ltp)
                    _alt_prem = get_ltp(self.fyers, build_option_symbol(
                        self.instrument, _alt_strike, signal['type'],
                        get_next_expiry(self.instrument)))
                else:
                    _alt_prem = bs_price(signal['type'], underlying, _alt_strike,
                                         config.DAYS_TO_EXPIRY / 365, hv)
                if (_alt_prem
                        and _alt_prem >= config.MIN_OPTION_PRICE
                        and _alt_prem * eff_lot_size * _stop_pct <= _risk_cap):
                    self.logger.info(
                        f"  [RISK-FIT] {self.instrument}: {lots}-lot risk "
                        f"₹{_risk:,.0f} > cap ₹{_risk_cap:,.0f} → strike "
                        f"{strike}→{_alt_strike} (₹{_alt_prem:.0f}) keeps {lots} lots"
                    )
                    strike           = _alt_strike
                    _fb_price        = float(_alt_prem)
                    signal['strike'] = strike
                else:
                    self.logger.info(
                        f"  [RISK-FIT] {self.instrument}: {lots}-lot risk "
                        f"₹{_risk:,.0f} > cap ₹{_risk_cap:,.0f}, OTM shift "
                        f"unavailable → shaving to 1 lot"
                    )
                    lots         = 1
                    eff_lot_size = self.lot_size
                _risk = _fb_price * eff_lot_size * _stop_pct
            if _risk > _risk_cap:
                self.logger.info(
                    f"  [RISK-GATE] {self.instrument} {signal['type']}: 1-lot risk "
                    f"₹{_risk:,.0f} > cap ₹{_risk_cap:,.0f} — skipping trade"
                )
                return
        else:
            self.logger.warning(
                f"  [RISK-GATE] {self.instrument}: premium unavailable — cannot "
                f"pre-size risk; relying on SL-M + polling stop"
            )

        if self.live:
            # ── LIVE: place real order ────────────────────────────────────────
            from fyers_orders import enter_live_position
            pos_info = enter_live_position(
                self.fyers, self.instrument, signal,
                stop_pct=_stop_pct, lot_size=eff_lot_size
            )
            if not pos_info:
                self.logger.error("Live entry failed — skipping trade.")
                return
            entry_price   = pos_info['entry_price']
            option_symbol = pos_info['option_symbol']
            sl_order_id   = pos_info.get('sl_order_id')   # None if SL-M failed
            sl_trigger    = pos_info.get('sl_trigger', 0.0)
            if sl_order_id:
                self.logger.info(
                    f"[SL-M] Exchange stop active: trigger=₹{sl_trigger:.2f} "
                    f"({_stop_pct*100:.0f}% below entry ₹{entry_price:.2f})"
                )
        else:
            # ── PAPER: Black-Scholes simulation ──────────────────────────────
            T           = config.DAYS_TO_EXPIRY / 365
            entry_price = bs_price(signal['type'], underlying, strike, T, hv)
            if entry_price < config.MIN_OPTION_PRICE:
                self.logger.info(
                    f"Option price ₹{entry_price:.2f} < min ₹{config.MIN_OPTION_PRICE}, skipping."
                )
                return
            option_symbol = None
            sl_order_id   = None
            sl_trigger    = 0.0

        position = {
            'instrument'    : self.instrument,
            'type'             : signal['type'],
            'entry_time'       : datetime.now(IST),
            'entry_price'      : entry_price,
            'entry_underlying' : underlying,
            'strike'           : strike,
            'option_symbol'    : option_symbol,
            'lot_size'         : eff_lot_size,
            'hv_at_entry'      : hv,
            'highest_pnl_pct'  : 0.0,
            'target_pct'       : _dyn_tgt,       # IV-scaled target (or BASE_TARGET if IV unavailable)
            'path'             : _path,          # used by check_exits for per-path stop/target
            'otm_strikes'      : _otm,           # 0=ATM, 1/2=OTM degree (check_exits uses this)
            'otm_reason'       : signal.get('otm_reason', ''),  # S/R rationale for analysis
            'gap_type'         : signal.get('gap_type', self._gap_type or ''),  # for JSONL
            'dynamic_or'       : signal.get('dynamic_or', False),  # True = DYN-OR fallback
            'sl_order_id'      : sl_order_id,    # Fyers SL-M order id (None if paper/failed)
            'sl_trigger'       : sl_trigger,     # for audit logging
            # ── Learning fields (recorded in trade_log → JSONL) ───────────
            'entry_adx'        : float(signal.get('adx', 0)),
            'lots'             : lots,
            'regime'           : getattr(getattr(self, '_daily_regime', None), 'regime', 'UNKNOWN'),
            'posture'          : getattr(getattr(self, '_daily_regime', None), 'posture', 'NORMAL'),
            # v1.6: conviction scores + sizing rationale — closes the data gap
            # that made size-vs-outcome calibration impossible on the first 46 trades
            'unified_score'    : signal.get('unified_score'),
            'composite_score'  : signal.get('composite_score'),
            'size_reason'      : signal.get('size_reason', ''),
        }
        self.positions.append(position)
        self.trades_today += 1
        position['trade_no'] = self.trades_today   # stored for exit outcome lookup

        # ── Learner: record entry prediction ─────────────────────────────────
        try:
            trade_probability.compute_trade_probability(
                signal     = signal,
                position   = position,
                instrument = self.instrument,
                trade_no   = self.trades_today,
            )
        except Exception as _tp_err:
            self.logger.warning(f'[TP] entry log failed: {_tp_err}')

        mode_tag  = "[LIVE]" if self.live else "[PAPER]"
        path_tag  = f"[{signal.get('path','vX')}]"
        lots_tag  = f"[{lots}x]" if lots > 1 else ""
        str_tag   = f" | Strength:{signal.get('strength','?')}" if lots > 1 else ""
        otm_tag   = f" | OTM+{_otm} (strike {strike} vs ATM {_atm})" if _otm else ""
        dyn_tag   = " | DYN-OR" if signal.get('dynamic_or', False) else ""
        _iv_tgt_tag = (f" | IV-tgt:{_dyn_tgt*100:.0f}%"
                       f"(IV={_atm_iv_entry:.1f}%)" if _atm_iv_entry else "")
        _phase1_end = getattr(config, 'STRATEGY_PHASE1_END', '11:00')
        _phase_tag  = (f" | [PHASE-1]" if _now_str < _phase1_end
                       else f" | [PHASE-3]" if _now_str >= _phase3_start
                       else f" | [PHASE-2]")
        self.logger.info(
            f"{mode_tag}{path_tag}{lots_tag} ENTRY {signal['type']:4s} | "
            f"{self.instrument} | Strike: {strike} | "
            f"Opt: ₹{entry_price:.2f} | Idx: {underlying:.2f} | "
            f"ADX: {signal['adx']:.1f}{str_tag}{otm_tag}{dyn_tag}{_iv_tgt_tag}{_phase_tag} | Trade #{self.trades_today}"
        )
        # Phase 1 SGX correlation log — captures gap direction vs trade direction
        sgx_nifty.log_signal_alignment(
            shared_state.get_sgx_context(), signal['type'], self.logger
        )

    def enter_challenger_trade(self, signal: dict, hv: float,
                               oc: dict, lots: int = 1) -> None:
        """
        Shadow entry for Challenger — no real orders placed.

        Same signal as Champion; OI-guided strike via select_challenger_strike().
        BS pricing uses hv (same as Champion) with the selected strike, so P&L
        comparison is a clean A/B: same model, same IV, different strike K.
        """
        underlying   = signal['price']
        atm_strike   = int(round(underlying / self.strike_gap) * self.strike_gap)
        ch_strike    = self.select_challenger_strike(signal['type'], underlying, oc)
        eff_lot_size = self.lot_size * lots

        T           = config.DAYS_TO_EXPIRY / 365
        entry_price = bs_price(signal['type'], underlying, ch_strike, T, hv)

        if entry_price < config.MIN_OPTION_PRICE:
            self.logger.info(
                f"  [CHALLENGER] Option ₹{entry_price:.2f} < min "
                f"₹{config.MIN_OPTION_PRICE}. Skipping shadow entry."
            )
            return

        position = {
            'type'            : signal['type'],
            'entry_time'      : datetime.now(IST),
            'entry_price'     : entry_price,
            'entry_underlying': underlying,
            'strike'          : ch_strike,
            'lot_size'        : eff_lot_size,
            'hv_at_entry'     : hv,
            'highest_pnl_pct' : 0.0,
            'atm_strike'      : atm_strike,   # Champion's strike — for comparison log
        }
        self.challenger_positions.append(position)
        self.challenger_trades_today += 1

        delta_n  = (ch_strike - atm_strike) // self.strike_gap
        ch_label = f"OTM{delta_n:+d}" if ch_strike != atm_strike else "ATM"
        self.logger.info(
            f"  [CHALLENGER] ENTRY {signal['type']:4s} | "
            f"Strike: {ch_strike} ({ch_label} vs Champion ATM {atm_strike}) | "
            f"Opt: ₹{entry_price:.2f} | "
            f"MaxPain={oc.get('max_pain', '?')} | "
            f"Trade #{self.challenger_trades_today}"
        )

    # ── Exit Management ───────────────────────────────────────────────────────

    def check_exits(self, current_price: float, hv: float,
                    force_close: bool = False,
                    pivot_close: bool = False) -> None:
        to_close = []

        for idx, pos in enumerate(self.positions):
            elapsed_days = (
                datetime.now(IST) - pos['entry_time']
            ).total_seconds() / 86400

            # ── Check if exchange SL-M was triggered (stale-feed protection) ─
            # If Fyers executed our stop-loss market order independently (because
            # option feed was stale and polling-loop stop never fired), we detect
            # it here and close the position at the exchange fill price.
            _sl_triggered   = False
            _sl_fill_price  = 0.0
            sl_order_id     = pos.get('sl_order_id')
            if self.live and sl_order_id and not force_close and not pivot_close:
                from fyers_orders import check_sl_order_filled
                _sl_triggered, _sl_fill_price = check_sl_order_filled(
                    self.fyers, sl_order_id
                )

            if self.live and pos.get('option_symbol'):
                # Live: get actual option LTP for P&L calculation
                from fyers_orders import get_ltp
                # If exchange already stopped us out, use that fill price directly
                if _sl_triggered and _sl_fill_price > 0:
                    current_opt = _sl_fill_price
                else:
                    current_opt = get_ltp(self.fyers, pos['option_symbol'])
                if current_opt is None:
                    # Fallback to Black-Scholes if LTP fetch fails
                    T_rem = max(config.DAYS_TO_EXPIRY - elapsed_days, 0.01) / 365
                    current_opt = bs_price(
                        pos['type'], current_price, pos['strike'], T_rem, hv
                    )
            else:
                # Paper: Black-Scholes
                T_rem = max(config.DAYS_TO_EXPIRY - elapsed_days, 0.01) / 365
                current_opt = bs_price(
                    pos['type'], current_price, pos['strike'], T_rem, hv
                )

            pnl_pct = (current_opt - pos['entry_price']) / pos['entry_price']
            if pnl_pct > pos['highest_pnl_pct']:
                pos['highest_pnl_pct'] = pnl_pct

            # ── Rolling option-price history (for rapid-spike exit) ───────────
            # Append current LTP every polling cycle; keep a fixed-length window
            # so we can measure the N-bar return without unbounded memory growth.
            _spike_bars = getattr(config, 'RAPID_SPIKE_BARS', 3)
            if 'opt_price_history' not in pos:
                pos['opt_price_history'] = []
            pos['opt_price_history'].append(current_opt)
            if len(pos['opt_price_history']) > _spike_bars + 1:
                pos['opt_price_history'] = pos['opt_price_history'][-(_spike_bars + 1):]

            # ── Per-path stop/target params ──────────────────────────────────
            # Path A (ORB) uses wider stop/target/trail for early-session volatility.
            # Per-day overrides are read from PATH_A_DAY_CONFIG via _get_day_cfg().
            _path     = pos.get('path', '')
            _otm      = pos.get('otm_strikes', 0)   # 0=ATM, 1/2=OTM degree
            # Get per-day config using the day the position was opened (entry_time)
            _entry_dt = pos.get('entry_time')
            _entry_dow = (_entry_dt.strftime('%a')
                          if _entry_dt else datetime.now(IST).strftime('%a'))
            _dcfg_pos  = self._get_day_cfg(_entry_dow)
            _trail_dist = (_dcfg_pos['trail_dist'] if _path == 'A'
                           else config.TRAILING_DISTANCE)
            if _path == 'A':
                # Dynamic OR uses tighter stop — market has settled by 10:00+
                _stop = (getattr(config, 'PATH_A_DYNAMIC_OR_STOP', 0.35)
                         if pos.get('dynamic_or', False)
                         else _dcfg_pos['stop'])
                if _otm == 2:
                    _target    = getattr(config, 'PATH_A_OTM_2_TARGET',  2.50)
                    _trail_act = getattr(config, 'PATH_A_OTM_TRAIL_ACT', 0.10)
                elif _otm == 1:
                    _target    = self.inst_cfg.get('path_a_otm1_target',
                                      getattr(config, 'PATH_A_OTM_1_TARGET', 1.20))
                    _trail_act = getattr(config, 'PATH_A_OTM_TRAIL_ACT', 0.10)
                else:   # ATM — use IV-scaled target if set, else per-day config
                    _target    = pos.get('target_pct', _dcfg_pos['target'])
                    _trail_act = _dcfg_pos['trail_act']
            else:   # A_HELD — keep OTM targets so converted positions have room
                _stop = config.STOP_LOSS
                if _otm == 2:
                    _target    = getattr(config, 'PATH_A_OTM_2_TARGET',  2.50)
                    _trail_act = getattr(config, 'PATH_A_OTM_TRAIL_ACT', 0.10)
                elif _otm == 1:
                    _target    = self.inst_cfg.get('path_a_otm1_target',
                                      getattr(config, 'PATH_A_OTM_1_TARGET', 1.20))
                    _trail_act = getattr(config, 'PATH_A_OTM_TRAIL_ACT', 0.10)
                else:   # ATM — use IV-scaled target if stored, else main-session
                    _target    = pos.get('target_pct', config.BASE_TARGET)
                    # A_HELD (ORB held past checkpoint) keeps wide 18% trail — June analysis
                    # showed 12% trail fired prematurely on transient dips (Jun 1 BNF: exited
                    # at +₹1.2k vs +₹4.3k at force-close). REV/RECLAIM use TRAILING_ACTIVATION
                    # (12%) since they are shorter-lived moves where earlier protection helps.
                    _trail_act = (getattr(config, 'TRAIL_ACT_ORB_HELD',
                                          config.TRAILING_ACTIVATION)
                                  if _path == 'A_HELD'
                                  else config.TRAILING_ACTIVATION)

            exit_reason = None
            if _sl_triggered:
                # Exchange already executed the stop — book at exchange fill price
                _stop_pct_used = _stop * 100
                exit_reason = f"Stop-Loss ({_stop_pct_used:.0f}%) [Exchange SL-M]"
            elif pivot_close:
                exit_reason = 'Pivot (reversal)'
            elif force_close:
                exit_reason = f"EOD Force-Close ({config.FORCE_CLOSE_TIME})"
            elif pnl_pct <= -_stop:
                exit_reason = f"Stop-Loss ({_stop*100:.0f}%)"
            elif (getattr(config, 'RAPID_SPIKE_ENABLED', False)
                  and pnl_pct >= getattr(config, 'RAPID_SPIKE_MIN_GAIN', 0.10)):
                # ── Rapid spike exit (tier-aware) ────────────────────────
                # Spike threshold scales with unrealised P&L per lot:
                #   near the P&L ceiling (Rs8-12k/lot) a small spike = exit now;
                #   early in the trade (Rs<2.5k/lot) require a larger spike.
                # Per-lot normalisation handles 2-lot entries transparently.
                _lots         = max(pos.get('lots', 1), 1)
                _base_lot     = pos['lot_size'] / _lots
                _pnl_per_lot  = (current_opt - pos['entry_price']) * _base_lot

                _tiers = getattr(config, 'RAPID_SPIKE_TIERS',
                                 [(0, getattr(config, 'RAPID_SPIKE_PCT', 0.20))])
                _spike_pct = _tiers[-1][1]   # floor (lowest tier)
                for _floor, _pct in _tiers:
                    if _pnl_per_lot >= _floor:
                        _spike_pct = _pct
                        break

                # Tier label for logging
                _tier_labels = [
                    (12_000, 'EXCEPTIONAL'),
                    ( 8_000, 'EXCELLENT'),
                    ( 5_000, 'V.GOOD'),
                    ( 2_500, 'GOOD'),
                    (     0, 'OK'),
                ]
                _tier_lbl = next(l for f, l in _tier_labels
                                 if _pnl_per_lot >= f)

                _hist = pos.get('opt_price_history', [])
                if len(_hist) >= _spike_bars + 1:
                    _window_gain = _hist[-1] / _hist[0] - 1
                    if _window_gain >= _spike_pct:
                        exit_reason = (
                            f"Rapid Spike (+{_window_gain*100:.0f}% "
                            f"in {_spike_bars}bar)"
                        )
                        self.logger.info(
                            f"  [SPIKE] {self.instrument} {pos['type']}: "
                            f"option +{_window_gain*100:.1f}% in {_spike_bars} polls "
                            f"| tier {_tier_lbl} "
                            f"(Rs{_pnl_per_lot:,.0f}/lot, "
                            f"thresh {_spike_pct*100:.0f}%) "
                            f"| implied-peak exit"
                        )
            # Target / trail / max-hold — only if no earlier exit was chosen
            if exit_reason is None:
                if pnl_pct >= _target:
                    exit_reason = f"Target ({_target*100:.0f}%)"
                elif (config.USE_TRAILING_PROFIT
                      and pos['highest_pnl_pct'] > _trail_act
                      and pnl_pct < pos['highest_pnl_pct'] - _trail_dist):
                    exit_reason = "Trailing Stop"
                elif elapsed_days >= config.MAX_HOLDING_DAYS:
                    exit_reason = "Max Hold Period"
                elif (getattr(config, 'NEVER_PROGRESS_ENABLED', False)
                      and pnl_pct < 0
                      and pos['highest_pnl_pct']
                          < getattr(config, 'NEVER_PROGRESS_MIN_PEAK', 0.03)
                      and elapsed_days * 1440.0
                          >= getattr(config, 'NEVER_PROGRESS_MINUTES', 90)):
                    # Never-Progressed: ≥90min old, never peaked +3%, currently
                    # red → dead trade; cut before it bleeds to force-close.
                    # Winners pass trail activation (+12-18%) well inside 90min;
                    # the checkpoint loss-stop misses entries made after it.
                    exit_reason = (
                        f"Never-Progressed ({elapsed_days*1440:.0f}m, "
                        f"peak {pos['highest_pnl_pct']*100:+.1f}%)"
                    )

            if exit_reason:
                if self.live and pos.get('option_symbol'):
                    if _sl_triggered:
                        # Exchange already sold — skip the SELL order; just book P&L.
                        # SL order is already filled; no cancel needed.
                        actual_price = _sl_fill_price
                        self.logger.info(
                            f"[SL-M] Position already closed by exchange at ₹{actual_price:.2f} — "
                            f"skipping SELL order"
                        )
                        current_opt = actual_price
                    else:
                        # Normal exit: cancel SL-M first, then market sell
                        from fyers_orders import exit_live_position
                        actual_price = exit_live_position(self.fyers, pos)
                        if actual_price:
                            current_opt = actual_price  # use real fill price

                costs   = round_trip_costs(pos['entry_price'], current_opt, pos['lot_size'])
                pnl_net = (current_opt - pos['entry_price']) * pos['lot_size'] - costs

                self.capital   += pnl_net
                self.daily_pnl += pnl_net
                self.total_pnl += pnl_net

                # Update consolidated daily P&L (shared across all bot processes)
                shared_state.update_pnl(self.instrument, self._bot_id,
                                        pnl_net, self.logger)

                self.trade_log.append({
                    'instrument'       : self.instrument,
                    'entry_time'       : pos['entry_time'].isoformat(),
                    'exit_time'        : datetime.now(IST).isoformat(),
                    'type'             : pos['type'],
                    'strike'           : pos['strike'],
                    'entry_price'      : round(pos['entry_price'], 2),
                    'entry_underlying' : round(pos.get('entry_underlying', 0), 2),
                    'exit_price'       : round(current_opt, 2),
                    'pnl_pct'          : round(pnl_pct * 100, 2),
                    'max_pnl_pct'      : round(pos.get('highest_pnl_pct', 0.0) * 100, 2),
                    'costs'            : round(costs, 2),
                    'pnl_net'          : round(pnl_net, 2),
                    'exit_reason'      : exit_reason,
                    'capital'          : round(self.capital, 2),
                    'mode'             : 'live' if self.live else 'paper',
                    # ── Learning fields ────────────────────────────────────
                    'path'             : pos.get('path', ''),
                    'lots'             : pos.get('lots', 1),
                    'entry_adx'        : pos.get('entry_adx', 0),
                    'regime_at_open'   : pos.get('regime', 'UNKNOWN'),
                    'posture'          : pos.get('posture', 'NORMAL'),
                    'gap_type'         : pos.get('gap_type', ''),     # GAP_AND_GO_*/FADE/INSIDE
                    'otm_strikes'      : pos.get('otm_strikes', 0),  # 0=ATM, 1/2=OTM degree
                    'otm_reason'       : pos.get('otm_reason', ''),  # S/R logic rationale
                    'dynamic_or'       : pos.get('dynamic_or', False), # True = DYN-OR fallback
                    # ── Exit scorer fields ────────────────────────────────
                    'exit_score'       : pos.get('last_exit_score', None),  # score at last checkpoint
                    'exit_band'        : pos.get('last_exit_band',  None),  # HOLD/CAUTION/EXIT
                    # ── Conviction/sizing fields (v1.6) ───────────────────
                    'unified_score'    : pos.get('unified_score'),
                    'composite_score'  : pos.get('composite_score'),
                    'size_reason'      : pos.get('size_reason', ''),
                })
                self._save_trade_log()
                self._compute_rolling_quality()   # re-evaluate quality after each closed trade

                # ── Learner: record actual outcome ────────────────────────
                try:
                    _tp_outcome = (
                        'WIN'  if pnl_net > 0
                        else 'EOD' if any(x in exit_reason for x in ('EOD', 'Checkpoint', 'Force'))
                        else 'LOSS'
                    )
                    trade_probability.update_trade_outcome(
                        instrument  = self.instrument,
                        trade_no    = pos.get('trade_no', self.trades_today),
                        date_str    = pos['entry_time'].strftime('%Y-%m-%d'),
                        outcome     = _tp_outcome,
                        pnl_rs      = pnl_net,
                        exit_time   = datetime.now(IST),
                        exit_reason = exit_reason,
                        entry_time  = pos['entry_time'],
                    )
                except Exception as _tp_err:
                    self.logger.warning(f'[TP] exit log failed: {_tp_err}')

                icon = "✅" if pnl_net > 0 else "❌"
                self.logger.info(
                    f"{icon} EXIT  {pos['type']:4s} | {exit_reason:28s} | "
                    f"P&L: ₹{pnl_net:>8,.2f} ({pnl_pct*100:+.1f}%) | "
                    f"Capital: ₹{self.capital:,.2f}"
                )

                # ── PATH-A re-entry: open window after stop-loss ──────────
                # If this was a PATH-A stop-loss, reset _path_a_fired so the
                # OR breakout signal check can re-evaluate. One re-entry only
                # (tracked by _path_a_reentry_available), before 13:00.
                if ('Stop-Loss' in exit_reason and
                        pos.get('path') == 'A' and
                        getattr(config, 'PATH_A_REENTRY_ENABLED', False)):
                    self._path_a_fired             = False
                    self._path_a_reentry_available = True
                    self.logger.info(
                        f"[PATH-A] Stop-loss → re-entry window open "
                        f"(ADX≥{getattr(config, 'PATH_A_REENTRY_ADX_MIN', 35)} "
                        f"before {getattr(config, 'PATH_A_REENTRY_CUTOFF', '13:00')})"
                    )

                to_close.append(idx)

        for idx in reversed(to_close):
            self.positions.pop(idx)

    def check_challenger_exits(self, current_price: float, hv: float,
                               force_close: bool = False) -> None:
        """
        Evaluate stops/targets for Challenger shadow positions each cycle.
        Always uses BS pricing (no live orders). Logs [CHALLENGER] EXIT lines
        with running P&L delta vs Champion for easy side-by-side comparison.
        """
        to_close = []
        for idx, pos in enumerate(self.challenger_positions):
            elapsed_days = (
                datetime.now(IST) - pos['entry_time']
            ).total_seconds() / 86400
            T_rem       = max(config.DAYS_TO_EXPIRY - elapsed_days, 0.01) / 365
            current_opt = bs_price(pos['type'], current_price, pos['strike'], T_rem, hv)

            pnl_pct = (current_opt - pos['entry_price']) / pos['entry_price']
            if pnl_pct > pos['highest_pnl_pct']:
                pos['highest_pnl_pct'] = pnl_pct

            exit_reason = None
            if force_close:
                exit_reason = f"EOD Force-Close ({config.FORCE_CLOSE_TIME})"
            elif pnl_pct <= -config.STOP_LOSS:
                exit_reason = f"Stop-Loss ({config.STOP_LOSS*100:.0f}%)"
            elif pnl_pct >= config.BASE_TARGET:
                exit_reason = f"Target ({config.BASE_TARGET*100:.0f}%)"
            elif (config.USE_TRAILING_PROFIT
                  and pos['highest_pnl_pct'] > config.TRAILING_ACTIVATION
                  and pnl_pct < pos['highest_pnl_pct'] - config.TRAILING_DISTANCE):
                exit_reason = "Trailing Stop"
            elif elapsed_days >= config.MAX_HOLDING_DAYS:
                exit_reason = "Max Hold Period"

            if exit_reason:
                costs   = round_trip_costs(pos['entry_price'], current_opt, pos['lot_size'])
                pnl_net = (current_opt - pos['entry_price']) * pos['lot_size'] - costs

                self.challenger_daily_pnl += pnl_net
                self.challenger_total_pnl += pnl_net

                self.challenger_trade_log.append({
                    'instrument'  : self.instrument,
                    'entry_time'  : pos['entry_time'].isoformat(),
                    'exit_time'   : datetime.now(IST).isoformat(),
                    'type'        : pos['type'],
                    'strike'      : pos['strike'],
                    'atm_strike'  : pos['atm_strike'],
                    'entry_price' : round(pos['entry_price'], 2),
                    'exit_price'  : round(current_opt, 2),
                    'pnl_pct'     : round(pnl_pct * 100, 2),
                    'costs'       : round(costs, 2),
                    'pnl_net'     : round(pnl_net, 2),
                    'exit_reason' : exit_reason,
                    'mode'        : 'challenger',
                })
                self._save_challenger_trade_log()

                icon  = "✅" if pnl_net > 0 else "❌"
                delta = self.challenger_total_pnl - self.total_pnl
                self.logger.info(
                    f"  {icon} [CHALLENGER] EXIT {pos['type']:4s} | "
                    f"{exit_reason:28s} | "
                    f"P&L: ₹{pnl_net:>8,.2f} ({pnl_pct*100:+.1f}%) | "
                    f"Strike: {pos['strike']} (Champion ATM {pos['atm_strike']}) | "
                    f"Cumul vs Champion: {'+' if delta >= 0 else ''}₹{delta:,.0f}"
                )
                to_close.append(idx)

        for idx in reversed(to_close):
            self.challenger_positions.pop(idx)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _update_morning_trend(self, df: 'pd.DataFrame') -> None:
        """Track peak ADX and peak |DI+ − DI−| during the ORB window.
        Called every scan tick while _orb_window is True.
        Feeds PATH_REV direction detection.
        """
        row  = df.iloc[-1]
        _adx = float(row.get('ADX',      0.0) or 0.0)
        _dip = float(row.get('DI_plus',  0.0) or 0.0)
        _dim = float(row.get('DI_minus', 0.0) or 0.0)
        if _adx > self._morning_adx_peak:
            self._morning_adx_peak = _adx
        _spread = abs(_dip - _dim)
        if _spread > self._morning_di_peak:
            self._morning_di_peak = _spread
            self._morning_dir = 'PUT' if _dim > _dip else 'CALL'

    def get_path_rev_signal(self, df: 'pd.DataFrame', oc: dict,
                            now: 'datetime') -> 'dict | None':
        """PATH_REV: MaxPain Snap Reversal.

        Detects trend exhaustion + options-market repositioning after the ORB
        window closes.  Fires when the morning move (real trend, ADX≥30) is
        visibly losing momentum and the IVSkew + MaxPain context shifts in the
        opposite direction.

        Score system (max 6):
          DI convergence  0–2  gap narrowed ≥50% from peak (+1) or DI crossed (+2)
          IVSkew flip     0–2  drifting ≥½ threshold in reversal direction (+1) or
                               crossed reversal side (+2)
          MaxPain prox    0–1  price within PATH_REV_MAXPAIN_PROX_PCT of MaxPain
          ADX waning      0–1  current ADX < peak × PATH_REV_ADX_WANE_RATIO

        Paper-only when PATH_REV_LIVE=False (default).  Live entry uses same
        stop/target/trail as ORB entries.
        """
        if not getattr(config, 'PATH_REV_ENABLED', False):
            return None
        if self._path_rev_fired:
            return None

        # ── Time window ───────────────────────────────────────────────────────
        _start = dtime(*[int(x) for x in
                         getattr(config, 'PATH_REV_START', '12:00').split(':')])
        _end   = dtime(*[int(x) for x in
                         getattr(config, 'PATH_REV_END',   '13:30').split(':')])
        if not (_start <= now.time() <= _end):
            return None

        # ── Morning trend quality gates ───────────────────────────────────────
        _min_adx = getattr(config, 'PATH_REV_MIN_MORNING_ADX',    30)
        _min_di  = getattr(config, 'PATH_REV_MIN_DI_SPREAD_PEAK', 12)
        if (self._morning_adx_peak < _min_adx
                or self._morning_di_peak < _min_di
                or self._morning_dir is None):
            return None   # morning had no real trend to reverse from

        # Reversal direction is opposite of the morning move
        rev_dir = 'CALL' if self._morning_dir == 'PUT' else 'PUT'

        # ── Regime gate: TRENDING days are driven by directional conviction,
        # not options expiry dynamics. Waning ADX in a TRENDING day = trend
        # pause, not reversal. Evidence: Jun 4/9/10 BNF all TRENDING_BULL + PUT
        # REV = -₹5,410 combined. After v1.3 blocks CHOPPY→REV, TRENDING is
        # the remaining gap.
        if getattr(config, 'REGIME_TRENDING_REV_SKIP', False):
            _regime = getattr(self, '_regime', 'MIXED')
            if _regime == 'TRENDING':
                self.logger.info(
                    f"  [PATH-REV-SKIP] {self.instrument} {rev_dir}: "
                    f"TRENDING regime — MaxPain snap suppressed (conviction > mean-reversion)"
                )
                return None

        row  = df.iloc[-1]
        _px  = float(row['Close'])
        _adx = float(row.get('ADX',      0.0) or 0.0)
        _dip = float(row.get('DI_plus',  0.0) or 0.0)
        _dim = float(row.get('DI_minus', 0.0) or 0.0)

        score   = 0
        reasons = []

        # ── Component 1: DI convergence (0–2) ────────────────────────────────
        # Which DI was dominant in the morning?
        if self._morning_dir == 'PUT':
            _cur_spread = _dim - _dip   # positive while PUT trend still dominant
        else:
            _cur_spread = _dip - _dim   # positive while CALL trend still dominant

        if _cur_spread <= 0:
            # DI has already flipped — confirmed reversal
            score += 2
            reasons.append(f'DI flipped → {rev_dir} ✓✓')
        elif self._morning_di_peak > 0:
            _conv_pct = 1.0 - (_cur_spread / self._morning_di_peak)
            if _conv_pct >= 0.50:
                score += 1
                reasons.append(
                    f'DI converging {_conv_pct*100:.0f}% of peak closed ✓'
                )

        # ── Component 2: IVSkew flip (0–2) ───────────────────────────────────
        _cur_iv      = oc.get('iv_skew')
        _flip_thresh = getattr(config, 'PATH_REV_IVSKEW_FLIP_PCT',   4.0)
        _skew_thresh = getattr(config, 'OI_IV_SKEW_THRESHOLD',        2.0)

        if _cur_iv is not None and self._ivskew_hist:
            _now_ts  = now.timestamp()
            # 30-min-ago IVSkew: oldest snapshot within last 30 min
            _hist_30 = [v for (t, v) in self._ivskew_hist if _now_ts - t <= 1800]
            _old_iv  = _hist_30[0] if len(_hist_30) >= 2 else self._ivskew_hist[0][1]
            _iv_shift = _cur_iv - _old_iv   # positive = IV skew rising (more fear/puts)

            if rev_dir == 'CALL':
                # CALL reversal: IVSkew should turn negative (CALL bid > PUT bid)
                _full = (_cur_iv < -_skew_thresh and _iv_shift <= -_flip_thresh)
                _part = (not _full and _iv_shift <= -(_flip_thresh * 0.5))
            else:
                # PUT reversal: IVSkew should turn positive (PUT bid > CALL bid)
                _full = (_cur_iv > _skew_thresh and _iv_shift >= _flip_thresh)
                _part = (not _full and _iv_shift >= (_flip_thresh * 0.5))

            if _full:
                score += 2
                reasons.append(
                    f'IVSkew flipped {_old_iv:+.1f}%→{_cur_iv:+.1f}% '
                    f'(Δ{_iv_shift:+.1f}%) → {rev_dir} bid ✓✓'
                )
            elif _part:
                score += 1
                reasons.append(
                    f'IVSkew drifting {_iv_shift:+.1f}% toward {rev_dir} ✓'
                )

        # ── Component 3: MaxPain proximity (0–1) ─────────────────────────────
        _mp      = oc.get('max_pain')
        _mp_prox = getattr(config, 'PATH_REV_MAXPAIN_PROX_PCT', 0.005)
        if _mp and _mp > 0:
            _mp_dist = abs(_px - _mp) / _mp
            if _mp_dist <= _mp_prox:
                score += 1
                reasons.append(
                    f'MaxPain={_mp:,.0f} price {_mp_dist*100:.2f}% away (snap zone) ✓'
                )

        # ── Component 4: ADX waning (0–1) ────────────────────────────────────
        _wane = getattr(config, 'PATH_REV_ADX_WANE_RATIO', 0.85)
        if self._morning_adx_peak > 0 and _adx < self._morning_adx_peak * _wane:
            score += 1
            reasons.append(
                f'ADX waning {_adx:.1f}/{self._morning_adx_peak:.1f} '
                f'(={_adx/self._morning_adx_peak*100:.0f}% of peak) ✓'
            )

        # ── Score gate ────────────────────────────────────────────────────────
        _min_score = getattr(config, 'PATH_REV_MIN_SCORE', 3)
        _reason_str = ' | '.join(reasons) if reasons else 'no conditions met'
        self.logger.info(
            f"  [PATH-REV] {self.instrument} {rev_dir} score={score}/{_min_score} | "
            f"{_reason_str}"
        )
        if score < _min_score:
            return None

        # ── Signal ───────────────────────────────────────────────────────────
        return {
            'type'       : rev_dir,
            'price'      : _px,
            'adx'        : float(_adx),
            'strength'   : 1,      # always 1 lot — unvalidated pattern
            'lots'       : 1,
            'path'       : 'REV',
            'otm_strikes': 0,
            'otm_reason' : f'MaxPain snap | {_reason_str}',
            'gap_type'   : self._gap_type,
            'dynamic_or' : False,
        }

    def _is_monday_before_bnf_monthly_expiry(self, today) -> bool:
        """Return True on the Monday before BNF's last-Tuesday-of-month expiry.
        On that day MIN_DAYS_TO_EXPIRY=2 rolls forward to next month (~DTE 29),
        making the option far too long-dated for intraday gamma capture.
        """
        if not self.inst_cfg.get('skip_monday_before_expiry', False):
            return False
        if today.weekday() != 0:  # not Monday
            return False
        tomorrow = today + timedelta(days=1)
        if tomorrow.weekday() != 1:  # tomorrow not Tuesday (sanity check)
            return False
        # Last Tuesday of month: adding 7 days pushes into next month
        return (tomorrow + timedelta(days=7)).month != tomorrow.month

    def _reset_daily_state(self) -> None:
        today = datetime.now(IST).date()
        if self.current_date != today:
            if self.current_date is not None:
                self.logger.info(
                    f"[DAY END] {self.instrument} | "
                    f"Trades: {self.trades_today} | "
                    f"Daily P&L: ₹{self.daily_pnl:,.2f} | "
                    f"Total P&L: ₹{self.total_pnl:,.2f}"
                )
                # Challenger EOD summary — compare vs Champion for that day
                if self.challenger_trades_today > 0 or self.challenger_daily_pnl != 0.0:
                    delta_day   = self.challenger_daily_pnl - self.daily_pnl
                    delta_total = self.challenger_total_pnl - self.total_pnl
                    self.logger.info(
                        f"[DAY END CHALLENGER] {self.instrument} | "
                        f"Trades: {self.challenger_trades_today} | "
                        f"Daily P&L: ₹{self.challenger_daily_pnl:,.2f} "
                        f"({'+' if delta_day >= 0 else ''}{delta_day:,.0f} vs Champion) | "
                        f"Total P&L: ₹{self.challenger_total_pnl:,.2f} "
                        f"({'+' if delta_total >= 0 else ''}{delta_total:,.0f} vs Champion)"
                    )
            # ── Near-miss EOD summary (only on no-trade days) ─────────────────
            if self.trades_today == 0 and self._nm_best:
                nm = self._nm_best
                missing_str = (' | '.join(nm['missing']) if nm['missing']
                               else 'all filters met (signal may have been suppressed)')
                adx_gap   = nm['adx_need'] - nm['adx']
                proximity = ('🟡 VERY CLOSE' if adx_gap <= 2 else
                             '🟠 CLOSE'      if adx_gap <= 5 else
                             '🔴 FAR')
                opt_str   = (f"₹{nm['opt_price']:.2f}" if nm.get('opt_price')
                             else 'n/a')
                self.logger.info(
                    f"[NO-TRADE] {self.instrument} | "
                    f"Best near-miss @ {nm['time']} | "
                    f"Would-be: {nm['dir']} {self.instrument} "
                    f"{nm['strike']} {'CE' if nm['dir'] == 'CALL' else 'PE'} | "
                    f"Option price: {opt_str} | "
                    f"Idx={nm['price']} vs VWAP={nm['vwap']} | "
                    f"ADX={nm['adx']} (needed {nm['adx_need']}) | "
                    f"{proximity} | Missing: {missing_str}"
                )
            self.current_date            = today
            self.trades_today            = 0
            self.daily_pnl               = 0.0
            self.challenger_trades_today = 0
            self.challenger_daily_pnl    = 0.0
            self._nm_best_adx            = 0.0     # reset near-miss tracker
            self._nm_best                = {}
            self._nm_pending             = []      # discard any unresolved outcomes
            self._path_e_fired           = False   # reset Path E (HTF Grind)
            self._path_b_fired              = False   # reset Path B (MRB)
            self._or_high         = None
            self._or_low          = None
            self._or_ready        = False
            self._or_width_ok     = True
            self._gap_type        = None
            self._gap_prev_close  = None   # gap-rev supplement
            self._gap_open_price  = None
            self._gap_rev_dir     = None
            self._path_a_fired             = False
            self._path_a_reentry_available = False
            self._dynamic_or               = False  # reset fallback OR
            self._morning_dir       = None
            self._morning_adx_peak  = 0.0
            self._morning_di_peak   = 0.0
            self._path_rev_fired    = False
            self._ivskew_hist       = []

            # ── BNF Monday-before-monthly-expiry skip ─────────────────────
            self._skip_bnf_today = self._is_monday_before_bnf_monthly_expiry(today)
            if self._skip_bnf_today:
                self.logger.info(
                    f"[BNF-SKIP] {self.instrument}: Monday before monthly expiry "
                    f"— skipping today (DTE would roll to ~29d, wrong instrument)"
                )

            # ── Compute daily regime snapshot (market_regime.py) ──────────
            if _REGIME_AVAIL:
                try:
                    _ra = _RegimeAnalyzer(self.instrument, lookback=12)
                    self._daily_regime = _ra.get_snapshot(snap_lookback=5)
                    self.logger.info(
                        f"[REGIME] {self._daily_regime.brief}"
                    )
                except Exception as _re:
                    self.logger.warning(f"[REGIME] Snapshot failed: {_re}")
                    self._daily_regime = None
            else:
                self._daily_regime = None

            # Reload OI zones each morning — yesterday's EOD file is most relevant
            _fresh_zones = load_zones(self.instrument, max_age_days=3)
            if _fresh_zones:
                self._oi_zones = _fresh_zones
                self.logger.info(
                    f"[OI] Zones reloaded for {today}: "
                    f"{describe_zones(self._oi_zones)}"
                )
            else:
                self._oi_zones = None
                self.logger.warning(
                    f"[OI] No valid OI zones for {today} — "
                    f"run oi_zones_eod.py after market close. "
                    f"Proceeding without OI context."
                )
            self.logger.info(f"[NEW DAY] {today} | {self.instrument}")

            # ── Pre-market SGX/GIFT Nifty gap (NIFTY bot fetches; others read) ──
            # Phase 1: logging only — correlate gap direction vs trade outcome.
            # Phase 2 (after 30 days): use wide_gap to gate ORB, adjust lot sizing.
            if self.instrument == 'NIFTY':
                _sgx_ctx = sgx_nifty.fetch_and_analyze(self.logger)
                if _sgx_ctx:
                    shared_state.set_sgx_context(_sgx_ctx, self.logger)
            else:
                _sgx_ctx = shared_state.get_sgx_context()
                if _sgx_ctx:
                    self.logger.info(
                        f'[SGX] Pre-market gap (from NIFTY bot): '
                        f'{_sgx_ctx.get("change_pct", "?"):+.2f}% '
                        f'{_sgx_ctx.get("direction", "")} '
                        f'{"⚡ WIDE" if _sgx_ctx.get("wide_gap") else ""}'
                    )

            # Path F: reset daily P&L counter (capital and total P&L carry over)
            reversal_scout.daily_reset(self.instrument, self.logger)
            # MaxPain Trap: reset daily P&L and fired_today flag
            max_pain_trap.daily_reset(self.instrument, self.logger)

    @staticmethod
    def _trade_log_dir() -> str:
        """Return absolute path to the logs directory.

        Uses __file__ anchor (same technique as near_miss_tracker.py) so the
        path is always correct regardless of the process's current working directory.
        This fixes an intermittent bug where relative config.LOG_DIRECTORY resolved
        to the wrong directory after rapid systemd restarts.
        """
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            config.LOG_DIRECTORY)

    def _trade_log_path(self, date_str: str) -> str:
        """Return absolute path for the dated JSONL trade file."""
        return os.path.join(
            self._trade_log_dir(),
            f'{config.BOT_NAME}_{self.instrument}_trades_{date_str}.jsonl'
        )

    def _save_trade_log(self) -> None:
        """Append the latest trade to the JSONL file dated by the trade's own entry date.

        Using JSONL + append ensures trades survive mid-session bot restarts.
        File is dated: FnO_T_Bot_{instrument}_trades_{YYYY-MM-DD}.jsonl
        Uses absolute path so rapid systemd restarts cannot cause CWD mismatch.

        IMPORTANT: uses trade's entry_time date, NOT datetime.now() — prevents
        cross-day contamination when the service is stopped after midnight (e.g.
        08:45 next morning) which would otherwise write yesterday's trade into
        today's JSONL, corrupting daily_pnl and triggering a false MAX_DAILY_LOSS block.
        """
        if not self.trade_log:
            return
        record = self.trade_log[-1]   # only append the most recent trade
        # Idempotency guard: the shutdown handler (KeyboardInterrupt in run())
        # also calls this method, re-appending a trade that was already saved
        # at exit time. Every bot stop after a trade produced a duplicate JSONL
        # line (Jun 23 appeared ×4). Skip if this exact trade is already on disk.
        key = (record.get('entry_time', ''), record.get('type', ''),
               str(record.get('strike', '')))
        if key in self._persisted_trade_keys:
            return
        log_dir = self._trade_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        # Use trade's entry date (not current clock) for file naming
        entry_ts = record.get('entry_time', '')
        trade_date = entry_ts[:10] if entry_ts else datetime.now(IST).strftime('%Y-%m-%d')
        path  = self._trade_log_path(trade_date)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
        self._persisted_trade_keys.add(key)

    def _load_today_trades(self) -> None:
        """On startup, reload today's JSONL trade file to restore intraday state.

        Restores: self.trade_log, self.trades_today, self.daily_pnl, self.total_pnl.
        Silent on missing file (first run of the day).

        Dedup: multiple systemd restarts can cause the same trade to be appended
        more than once (each restart loads N lines → trade_log[-1] is re-logged on
        the next exit/force-close).  We dedup by (entry_time, type, strike) so
        only the first occurrence of each unique trade is kept.

        Cross-day filter: entries whose entry_time date ≠ today are dropped (guards
        against the shutdown-save writing to tomorrow's file via clock-at-midnight
        edge cases).
        """
        today = datetime.now(IST).strftime('%Y-%m-%d')
        path  = self._trade_log_path(today)
        if not os.path.exists(path):
            return
        loaded  = []
        seen    = set()
        skipped = 0
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    t  = json.loads(line)
                    et = t.get('entry_time', '')
                    # Cross-day filter
                    if et[:10] != today:
                        skipped += 1
                        continue
                    # Dedup by (entry_time, type, strike)
                    key = (et, t.get('type', ''), str(t.get('strike', '')))
                    if key in seen:
                        skipped += 1
                        continue
                    seen.add(key)
                    loaded.append(t)
                    # Mark as persisted so _save_trade_log never re-appends it
                    self._persisted_trade_keys.add(key)
        except (json.JSONDecodeError, OSError) as e:
            self.logger.warning(f'[TRADE-LOAD] Could not read {path}: {e}')
            return

        if not loaded:
            return

        self.trade_log    = loaded
        self.trades_today = len(loaded)
        self.daily_pnl    = sum(t.get('pnl_net', 0.0) for t in loaded)
        # total_pnl carries across days — only restore today's portion here
        self.total_pnl   += self.daily_pnl
        # Set current_date so _reset_daily_state() skips the wipe on first loop
        self.current_date = datetime.now(IST).date()
        _skip_note = f' (skipped {skipped} dups/cross-day)' if skipped else ''
        self.logger.info(
            f'[TRADE-LOAD] Restored {len(loaded)} trade(s) from {path}{_skip_note} | '
            f'daily_pnl=₹{self.daily_pnl:,.2f} | trades_today={self.trades_today}'
        )

    def _save_challenger_trade_log(self) -> None:
        os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
        path = os.path.join(config.LOG_DIRECTORY,
                            f'{config.BOT_NAME}_{self.instrument}_challenger_trades.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.challenger_trade_log, f, indent=2)

    def _log_status(self, price: float) -> None:
        mode = "LIVE" if self.live else "PAPER"
        self.logger.info(
            f"[{mode}] {self.instrument}: {price:.2f} | "
            f"Pos: {len(self.positions)}/{config.MAX_CONCURRENT_POSITIONS} | "
            f"Trades: {self.trades_today}/{config.MAX_TRADES_PER_DAY} | "
            f"Daily P&L: ₹{self.daily_pnl:,.2f} | Capital: ₹{self.capital:,.2f}"
        )
        # Challenger status line — only shown when there's data to compare
        if self.challenger_positions or self.challenger_total_pnl != 0.0:
            delta = self.challenger_total_pnl - self.total_pnl
            self.logger.info(
                f"[CHALLENGER] {self.instrument}: "
                f"Pos: {len(self.challenger_positions)} | "
                f"Daily P&L: ₹{self.challenger_daily_pnl:,.2f} | "
                f"Total P&L: ₹{self.challenger_total_pnl:,.2f} "
                f"({'+' if delta >= 0 else ''}{delta:,.0f} vs Champion)"
            )

    # ── Regime Detection ──────────────────────────────────────────────────────

    def _detect_regime(self, df: 'pd.DataFrame | None' = None) -> str:
        """Map _daily_regime posture → TRENDING/CHOPPY/MIXED.  Sets self._regime.
        Primary: reads self._daily_regime (set by market_regime.py in _reset_daily_state).
        Fallback: ADX@11:00 on last REGIME_LOOKBACK_DAYS past trading days."""
        if not getattr(config, 'REGIME_DETECTION_ENABLED', True):
            self._regime = 'MIXED'
            return self._regime
        try:
            dr = getattr(self, '_daily_regime', None)
            if dr is not None:
                if dr.posture == 'CAUTIOUS':
                    regime = 'CHOPPY'
                elif dr.posture == 'AGGRESSIVE':
                    regime = 'TRENDING'
                else:
                    regime = 'MIXED'
                self._regime = regime
                self.logger.info(
                    f"[REGIME] {self.instrument}: {regime} "
                    f"({dr.regime} conf={dr.confidence} streak={dr.streak}d "
                    f"posture={dr.posture})"
                )
                return self._regime
            # Fallback: live ADX@11:00 scan on recent past days
            if df is None:
                df_raw = self.get_index_data()
                if df_raw is None:
                    self._regime = 'MIXED'
                    return self._regime
                df = self.add_indicators(df_raw)
            lookback    = getattr(config, 'REGIME_LOOKBACK_DAYS', 3)
            choppy_max  = getattr(config, 'REGIME_CHOPPY_ADX_MAX', 25.0)
            trend_min   = getattr(config, 'REGIME_TRENDING_ADX_MIN', 28.0)
            choppy_need = getattr(config, 'REGIME_CHOPPY_DAYS_NEEDED', 2)
            today       = datetime.now(IST).date()
            _11h        = dtime(11, 0)
            past_dates  = sorted(set(
                ts.date() for ts in df.index if ts.date() < today
            ))[-lookback:]
            if len(past_dates) < 2:
                self._regime = 'MIXED'
                self.logger.info(f"[REGIME] {self.instrument}: not enough history — MIXED")
                return self._regime
            adx_at_11 = []
            for d in past_dates:
                day_bars = df[df.index.date == d]
                eligible = day_bars[day_bars.index.time <= _11h]
                if eligible.empty or 'ADX' not in eligible.columns:
                    continue
                adx_at_11.append((d, float(eligible['ADX'].iloc[-1])))
            if not adx_at_11:
                self._regime = 'MIXED'
                return self._regime
            choppy_count = sum(1 for _, a in adx_at_11 if a < choppy_max)
            trend_count  = sum(1 for _, a in adx_at_11 if a >= trend_min)
            if choppy_count >= choppy_need:
                regime = 'CHOPPY'
            elif trend_count >= choppy_need:
                regime = 'TRENDING'
            else:
                regime = 'MIXED'
            self._regime = regime
            summary = ', '.join(f"{str(d)[-5:]}={a:.1f}" for d, a in adx_at_11)
            self.logger.info(
                f"[REGIME] {self.instrument}: {regime} (ADX@11:00 fallback "
                f"[{summary}] choppy={choppy_count} trend={trend_count}/{len(adx_at_11)})"
            )
        except Exception as exc:
            self.logger.warning(f"[REGIME] Detection error: {exc} — defaulting to MIXED")
            self._regime = 'MIXED'
        return self._regime

    def _compute_rolling_quality(self) -> str:
        """Track last QUALITY_GATE_LOOKBACK live trades. Enter REDUCED if WR and
        combined loss both breach thresholds.  Reset after QUALITY_GATE_RESET_WINS
        consecutive wins.  Sets self._quality_state."""
        if not getattr(config, 'QUALITY_GATE_ENABLED', True):
            self._quality_state = 'NORMAL'
            return self._quality_state
        try:
            lookback   = getattr(config, 'QUALITY_GATE_LOOKBACK', 5)
            wr_min     = getattr(config, 'QUALITY_GATE_WR_MIN', 0.40)
            loss_thr   = getattr(config, 'QUALITY_GATE_LOSS_THRESHOLD', 5000.0)
            reset_wins = getattr(config, 'QUALITY_GATE_RESET_WINS', 2)
            today      = datetime.now(IST).date()
            trades: list = []
            for i in range(15):
                d     = today - timedelta(days=i)
                fpath = self._trade_log_path(d.strftime('%Y-%m-%d'))
                if not os.path.exists(fpath):
                    continue
                try:
                    with open(fpath) as f:
                        for raw in f:
                            raw = raw.strip()
                            if not raw:
                                continue
                            t = json.loads(raw)
                            if t.get('exit_reason') and t.get('mode') == 'live':
                                trades.append(t)
                except Exception:
                    continue
            if not trades:
                self._quality_state = 'NORMAL'
                return self._quality_state
            trades.sort(key=lambda x: x.get('exit_time', ''))
            recent   = trades[-lookback:]
            wins     = sum(1 for t in recent if t.get('pnl_net', 0) > 0)
            wr       = wins / len(recent)
            combined = sum(t.get('pnl_net', 0) for t in recent)
            consec   = 0
            for t in reversed(trades):
                if t.get('pnl_net', 0) > 0:
                    consec += 1
                else:
                    break
            self._quality_consecutive_wins = consec
            prev = self._quality_state
            if prev == 'REDUCED' and consec >= reset_wins:
                new_state = 'NORMAL'
            elif wr < wr_min and combined < -loss_thr:
                new_state = 'REDUCED'
            else:
                new_state = 'NORMAL'
            if new_state != prev:
                self.logger.info(
                    f"[QUALITY] {self.instrument}: {prev} → {new_state}"
                )
            self._quality_state = new_state
            self.logger.info(
                f"[QUALITY] {self.instrument}: {new_state} | "
                f"last {len(recent)} live WR={wr:.0%} combined=₹{combined:,.0f} "
                f"consec_wins={consec}"
            )
        except Exception as exc:
            self.logger.warning(f"[QUALITY] Gate error: {exc} — defaulting to NORMAL")
            self._quality_state = 'NORMAL'
        return self._quality_state

    # ── Main Loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        mode = "LIVE TRADING" if self.live else "PAPER TRADING"
        self.logger.info("=" * 65)
        self.logger.info(f"{config.BOT_NAME} | {self.instrument} OPTIONS | {mode}")
        self.logger.info(
            f"Strategy : EMA {config.MOMENTUM_EMA_FAST}/{config.MOMENTUM_EMA_SLOW} "
            f"+ CALL ADX>{self.call_adx_min} / PUT ADX>{self.put_adx_min} | "
            f"Stop: {config.STOP_LOSS*100:.0f}% | "
            f"Target: {config.BASE_TARGET*100:.0f}%"
        )
        self.logger.info(
            f"Capital  : ₹{self.capital:,.0f} | "
            f"Lot size : {self.lot_size} | "
            f"Max trades/day: {config.MAX_TRADES_PER_DAY} | "
            f"Entry: {self.entry_start.strftime('%H:%M')}–{self.entry_end.strftime('%H:%M')} | "
            f"Skip Tue: {self.skip_tuesday} | Skip Thu: {self.skip_thursday}"
        )
        if self.live:
            self.logger.info("!! LIVE MODE — REAL ORDERS WILL BE PLACED !!")
        else:
            self.logger.info("Paper mode — no real orders.")
        self.logger.info("=" * 65)

        # ── Dynamic DTE: override DAYS_TO_EXPIRY with actual calendar days ──────
        # config.DAYS_TO_EXPIRY=2 was calibrated for old Thu-weekly NIFTY model.
        # NIFTY monthly-Monday (~13d mid-month); BNF monthly-Wed (~14d); SENSEX weekly-Fri (~2d).
        # Each instrument runs in its own process, so mutating config here is safe.
        try:
            from fyers_orders import get_next_expiry as _gte
            _exp_date   = _gte(self.instrument)
            _actual_dte = max((_exp_date - datetime.now(IST).date()).days, 1)
            _old_dte    = config.DAYS_TO_EXPIRY
            config.DAYS_TO_EXPIRY = _actual_dte
            self.logger.info(
                f"  DTE override: {self.instrument} -> {_actual_dte}d to expiry "
                f"{_exp_date} (config was {_old_dte}d — all BS pricing updated)"
            )
        except Exception as _e:
            self.logger.warning(f"  DTE override failed: {_e} — using config {config.DAYS_TO_EXPIRY}d")

        if not self.connect():
            return

        self._recover_positions()   # re-register any open positions after mid-session restart
        self._load_today_trades()   # restore intraday trade count + P&L from JSONL
        self._compute_rolling_quality()   # assess recent signal quality at startup

        while True:
            try:
                now   = datetime.now(IST)
                today = now.date()
                self._reset_daily_state()

                # ── Holiday / weekend ─────────────────────────────────────
                if not is_market_open_today(today):
                    self.logger.info(f"Market closed ({market_status()}). Sleeping 1h.")
                    time.sleep(3600)
                    continue

                # ── Market hours ──────────────────────────────────────────
                if not is_within_market_hours(now):
                    if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                        open_dt  = now.replace(hour=9, minute=15, second=0, microsecond=0)
                        wait_sec = int((open_dt - now).total_seconds())
                        self.logger.info(f"Pre-market. Opens in {wait_sec//60}min.")
                        time.sleep(max(wait_sec, 60))
                    else:
                        self.logger.info("Market closed for the day. Sleeping 1h.")
                        time.sleep(3600)
                    continue

                # ── Path A (ORB) conditional checkpoint ──────────────────
                # Evaluate each ORB position at the per-day checkpoint time.
                # Default: PATH_A_FORCE_CLOSE ('12:00').  Per-day overrides are
                # set in PATH_A_DAY_CONFIG[day]['checkpoint'] — e.g. Fri could
                # run to '12:30' if live data shows longer holding improves WR.
                # HOLD condition (all three must be true):
                #   1. profitable ≥ PATH_A_MIN_PROFIT_TO_HOLD (15%)
                #   2. ADX ≥ PATH_A_HOLD_ADX_MIN (30) — trend still has fuel
                #   3. EMA 9/21 still aligned with trade direction
                # HOLD → convert path to 'A_HELD'; check_exits uses main-session
                #   params (SL 25%, trail act 18%, trail dist 10%) → closes at 14:30.
                # CLOSE → hard force-close (capital protection on noise days).
                _path_a_fc_time   = self._get_day_cfg(now.strftime('%a'))['checkpoint']
                _chk_t = dtime(*map(int, _path_a_fc_time.split(':')))
                _path_a_positions = [p for p in self.positions
                                     if p.get('path') == 'A' and
                                     not p.get('path_a_checkpoint_done', False) and
                                     p.get('entry_time') and
                                     p['entry_time'].time() < _chk_t]
                if (_path_a_positions and
                        now.strftime('%H:%M') >= _path_a_fc_time):
                    df_fc = self.get_index_data()
                    if df_fc is not None:
                        df_fc    = self.add_indicators(df_fc)
                        _fc_px   = float(df_fc['Close'].iloc[-1])
                        _fc_hv   = float(df_fc['HV'].iloc[-1])
                        _fc_adx  = float(df_fc['ADX'].iloc[-1]) if 'ADX'      in df_fc.columns else 0.0
                        _fc_emaf = float(df_fc['EMA_fast'].iloc[-1]) if 'EMA_fast' in df_fc.columns else 0.0
                        _fc_emas = float(df_fc['EMA_slow'].iloc[-1]) if 'EMA_slow' in df_fc.columns else 0.0

                        _use_cond = getattr(config, 'PATH_A_CONDITIONAL_EXIT', False)
                        _hold_adx = getattr(config, 'PATH_A_HOLD_ADX_MIN', 30)

                        pos = _path_a_positions[0]   # max_concurrent=1, only one
                        pos['path_a_checkpoint_done'] = True

                        # Hold threshold scales with OTM degree:
                        # OTM positions need to show more gain at 11:30 — they
                        # may not have crossed ATM yet at 15% gain.
                        _pos_otm = pos.get('otm_strikes', 0)
                        if _pos_otm == 2:
                            _min_profit = getattr(config, 'PATH_A_OTM_2_MIN_PROFIT_HOLD', 0.30)
                        elif _pos_otm == 1:
                            _min_profit = getattr(config, 'PATH_A_OTM_1_MIN_PROFIT_HOLD', 0.20)
                        else:
                            _min_profit = getattr(config, 'PATH_A_MIN_PROFIT_TO_HOLD', 0.15)

                        # Compute current P&L % for this position
                        if self.live and pos.get('option_symbol'):
                            from fyers_orders import get_ltp
                            _opt_ltp = get_ltp(self.fyers, pos['option_symbol'])
                            _cur_opt = _opt_ltp if _opt_ltp else pos['entry_price']
                        else:
                            import math
                            _elapsed  = (datetime.now(IST) - pos['entry_time']).total_seconds() / 86400
                            _T_rem    = max(config.DAYS_TO_EXPIRY - _elapsed, 0.01) / 365
                            _cur_opt  = bs_price(pos['type'], _fc_px, pos['strike'], _T_rem, _fc_hv)
                        _pnl_pct = (_cur_opt - pos['entry_price']) / pos['entry_price']
                        _otm_lbl = f' OTM+{_pos_otm}' if _pos_otm else ' ATM'

                        # ── Exceptional profit close ──────────────────────────
                        # On gap-and-go momentum days the option can be up 50–150%
                        # by checkpoint.  Trailing post-checkpoint often gives it back
                        # in afternoon consolidation.  Lock in immediately if gain is
                        # exceptional — don't bother with the ADX/EMA hold evaluation.
                        _exc_thr = getattr(config,
                                           'PATH_A_EXCEPTIONAL_PROFIT_CLOSE', 0.50)
                        if _pnl_pct >= _exc_thr:
                            self.logger.info(
                                f"[PATH-A{_otm_lbl}] CHECKPOINT EXCEPTIONAL 💰 | "
                                f"{pos['type']} P&L={_pnl_pct*100:+.1f}% "
                                f">= {_exc_thr*100:.0f}% threshold "
                                f"→ locking in gain at {_path_a_fc_time}"
                            )
                            self.check_exits(_fc_px, _fc_hv, force_close=True)
                            continue   # skip hold evaluation — already closed

                        # ── 12PM hard loss stop ───────────────────────────────
                        # If the position is at a loss at the checkpoint → close
                        # immediately, unconditionally, regardless of ADX / EMA.
                        # Configured by PATH_A_LOSS_STOP_AT_CHECKPOINT (default True).
                        if (getattr(config, 'PATH_A_LOSS_STOP_AT_CHECKPOINT', True)
                                and _pnl_pct < 0):
                            self.logger.info(
                                f"[PATH-A{_otm_lbl}] CHECKPOINT LOSS-STOP ✂️ | "
                                f"{pos['type']} P&L={_pnl_pct*100:+.1f}% "
                                f"→ hard close at {_path_a_fc_time} (position at a loss)"
                            )
                            self.check_exits(_fc_px, _fc_hv, force_close=True)
                            continue   # skip conditional hold evaluation

                        # ── Exit score checkpoint (replaces fixed profit/ADX/EMA check) ──
                        # Score 0-85 across 6 components: directional integrity,
                        # structure (EMA/ST), price vs VWAP/OR_L, option health,
                        # time decay, reversal warnings.
                        # HOLD (≥55): switch to A_HELD, continue trailing
                        # CAUTION (35-54): switch to A_HELD, log warning
                        # EXIT (<35): force-close (thesis broken or decaying)
                        _oi_ctx  = getattr(self, '_oi_context_cache', None)
                        _esc_row = df_fc.iloc[-1]   # latest 5-min bar
                        _esc = _exit_score(
                            trade_type  = pos['type'],
                            bar         = _esc_row,
                            or_high     = self._or_high or 0.0,
                            or_low      = self._or_low  or 0.0,
                            entry_price = pos['entry_price'],
                            current_opt = _cur_opt,
                            entry_time  = pos['entry_time'],
                            now         = datetime.now(IST),
                            oi_context  = _oi_ctx,
                            rev_score   = 0.0,
                            dte         = float(config.DAYS_TO_EXPIRY),
                        )
                        _esc_score = _esc['score']
                        _esc_band  = _esc['band']
                        # Store on position for trade-log exit tagging
                        pos['last_exit_score'] = _esc_score
                        pos['last_exit_band']  = _esc_band

                        _hold = _use_cond and (_esc_band in ('HOLD', 'CAUTION'))

                        if _hold:
                            # Switch to A_HELD; check_exits retains OTM targets
                            pos['path'] = 'A_HELD'
                            _tgt_held = (
                                getattr(config, f'PATH_A_OTM_{_pos_otm}_TARGET', config.PATH_A_TARGET)
                                if _pos_otm else config.BASE_TARGET
                            )
                            _band_icon = '✅' if _esc_band == 'HOLD' else '⚠️'
                            self.logger.info(
                                f"[CHECKPOINT{_otm_lbl}] {_esc_band} {_band_icon} | "
                                f"{pos['type']} P&L={_pnl_pct*100:+.1f}% "
                                f"score={_esc_score}/85 ADX={_fc_adx:.1f} "
                                f"→ hold | target {_tgt_held*100:.0f}% | "
                                f"reasons: {'; '.join(_esc['reasons'][:3])}"
                            )
                        else:
                            self.logger.info(
                                f"[CHECKPOINT{_otm_lbl}] EXIT ❌ | {pos['type']} "
                                f"P&L={_pnl_pct*100:+.1f}% score={_esc_score}/85 "
                                f"band={_esc_band} "
                                f"→ force-closing | "
                                f"reasons: {'; '.join(_esc['reasons'][:3])}"
                            )
                            self.check_exits(_fc_px, _fc_hv, force_close=True)

                # ── EOD force-close ───────────────────────────────────────
                if (config.INTRADAY_FORCE_CLOSE and
                        now.strftime('%H:%M') >= config.FORCE_CLOSE_TIME and
                        self.positions):
                    self.logger.info(
                        f"EOD force-close triggered at {config.FORCE_CLOSE_TIME}."
                    )
                    df = self.get_index_data()
                    if df is not None:
                        df           = self.add_indicators(df)
                        current_price = float(df['Close'].iloc[-1])
                        hv            = float(df['HV'].iloc[-1])
                        self.check_exits(current_price, hv, force_close=True)
                        self.check_challenger_exits(current_price, hv, force_close=True)

                # ── Consolidated daily loss circuit-breaker ────────────────
                # Checks grand total across all instruments + bots (shared file).
                if shared_state.is_consolidated_loss_exceeded(logger=self.logger):
                    self.logger.info(shared_state.snapshot())
                    time.sleep(config.BOT_CHECK_INTERVAL)
                    continue

                # ── Fetch data ────────────────────────────────────────────
                df = self.get_index_data()
                if df is None:
                    # get_index_data already logged the specific error
                    time.sleep(30)
                    continue
                _min_bars = config.MOMENTUM_EMA_SLOW + 10
                if len(df) < _min_bars:
                    self.logger.error(
                        f"[DATA-FAIL] {self.instrument}: only {len(df)} bars returned, "
                        f"need {_min_bars}. Symbol: {self.inst_cfg['index_symbol']}. "
                        f"Retrying in 30s."
                    )
                    time.sleep(30)
                    continue
                # Warn if very few TODAY bars (could happen at session open)
                _n_today = (df.index.date == now.date()).sum()
                if _n_today < 3:
                    self.logger.warning(
                        f"[DATA-WARN] {self.instrument}: only {_n_today} bars for today so far."
                    )

                df            = self.add_indicators(df)
                # ── Regime detection: once per trading day after first data ─
                if self._regime_date != today:
                    self._detect_regime(df)
                    self._regime_date = today
                current_price = float(df['Close'].iloc[-1])
                hv            = float(df['HV'].iloc[-1])

                # ── Check exits ───────────────────────────────────────────
                self.check_exits(current_price, hv)
                self.check_challenger_exits(current_price, hv)

                # ── Multi-timeframe context + option chain ────────────────
                htf = self.get_htf_context()
                self._st15m = htf.get('supertrend_15m')  # cache for get_path_a_signal late gate
                oc  = self.get_option_chain_context(current_price)
                st_label = {1: 'BULL', -1: 'BEAR'}.get(
                    htf.get('supertrend_15m'), '?')
                _iv_skew_str = (f"{oc['iv_skew']:+.1f}%" if oc.get('iv_skew') is not None else '?')
                self.logger.info(
                    f"  Context: 15m-ST={st_label} | "
                    f"PCR={oc.get('pcr', '?')} ({oc.get('oi_bias', '?')}) | "
                    f"MaxPain={oc.get('max_pain', '?')} | "
                    f"ATM-IV={oc.get('atm_iv', '?')}% | "
                    f"IVSkew={_iv_skew_str}"
                )

                # ── New entry ─────────────────────────────────────────────
                _t = now.time()
                # ORB window now extends to PATH_A_LATE_END (12:00) — late
                # breakouts use tighter gates inside get_path_a_signal().
                _orb_late_t  = dtime(*[int(x) for x in
                                       getattr(config, 'PATH_A_LATE_END', '12:00').split(':')])
                _orb_window  = (dtime(9, 30) <= _t < _orb_late_t)
                _main_window = (self.entry_start <= _t <= self.entry_end)
                _in_window   = _orb_window or _main_window
                _path_f_in_window = self.entry_start <= _t <= _PATH_F_END

                # ── Compute Opening Range once per day ─────────────────────
                if _orb_window and not self._or_ready:
                    self._compute_or(df, now)

                # ── PATH_REV continuous tracking ──────────────────────────────
                # IVSkew history: always updated (needed for 30-min drift in Path REV).
                # Morning DI/ADX peak: only during ORB window (9:30–PATH_A_LATE_END).
                if getattr(config, 'PATH_REV_ENABLED', False):
                    _iv_snap = oc.get('iv_skew')
                    if _iv_snap is not None:
                        self._ivskew_hist.append((now.timestamp(), float(_iv_snap)))
                        self._ivskew_hist = self._ivskew_hist[-20:]
                    if _orb_window:
                        self._update_morning_trend(df)

                can_enter = (
                    _in_window
                    and len(self.positions) < config.MAX_CONCURRENT_POSITIONS
                    and self.trades_today < config.MAX_TRADES_PER_DAY
                )
                if can_enter and self.skip_thursday and now.weekday() == 3:
                    can_enter = False   # Thursday block (SENSEX: all Thursday trades net negative)
                if can_enter and self._skip_bnf_today:
                    can_enter = False   # BNF Monday-before-monthly-expiry: DTE rolls ~29d

                # ── VIX Goldilocks Gate (Gap 1) ───────────────────────────────
                # Only trade when India VIX is in the "goldilocks" 11–22 zone.
                # VIX > VIX_MAX: normally blocks — options overpriced, moves uncertain.
                # Exception — directional conviction override: when BOTH 15m-ST and
                # 15m-EMA agree on direction AND ADX >= VIX_CONVICTION_ADX_MIN, the
                # market IS trending strongly. High VIX just means the move will be
                # large, which is what we need. Block only when VIX is elevated AND
                # the setup is ambiguous (HTF mixed or ADX weak/choppy).
                # VIX < VIX_MIN: too quiet (moves too small for 130% target intraday).
                # Gate is dormant (no block) when VIX data is unavailable — never hard-fails.
                if can_enter and config.USE_VIX_FILTER and 'VIX' in df.columns:
                    _vix_now = float(df.iloc[-1].get('VIX', float('nan')))
                    if not pd.isna(_vix_now) and _vix_now > 0:
                        if _vix_now > config.VIX_MAX:
                            # Directional conviction override: if BOTH 15m-ST and
                            # 15m-EMA agree on direction, the trend is unambiguous.
                            # High VIX on a confirmed trending day means a large move
                            # IS coming — that is exactly when we want to be positioned.
                            # Block only when HTF is mixed (ST and EMA disagree) because
                            # that is genuine uncertainty + overpriced options = bad combo.
                            # Note: ADX is already required by Mode A (≥30 CALL / ≥25 PUT)
                            # and Mode B (≥35) — no need to gate it here a second time.
                            _htf_bull  = (htf.get('supertrend_15m') == 1)
                            _htf_bear  = (htf.get('supertrend_15m') == -1)
                            _adx_now   = float(df.iloc[-1].get('ADX', 0) or 0)
                            _iv_skew   = (oc or {}).get('iv_skew')
                            _htf_clear = _htf_bull or _htf_bear
                            if _htf_clear:
                                _dir    = 'BULL' if _htf_bull else 'BEAR'
                                _sk_str = (f" | IVSkew={_iv_skew:+.1f}%"
                                           if _iv_skew is not None else "")
                                self.logger.info(
                                    f"  [VIX-GATE] {self.instrument}: VIX={_vix_now:.1f} "
                                    f"> {config.VIX_MAX} but HTF conviction override — "
                                    f"15m ST={_dir} | ADX={_adx_now:.1f}{_sk_str} "
                                    f"— entry allowed."
                                )
                                # can_enter remains True — signal check proceeds normally
                            else:
                                _st_val = htf.get('supertrend_15m')
                                _st_str = ('+1(BULL)' if _st_val == 1
                                           else '-1(BEAR)' if _st_val == -1 else '?')
                                self.logger.info(
                                    f"  [VIX-GATE] {self.instrument}: VIX={_vix_now:.1f} "
                                    f"> {config.VIX_MAX} + HTF ambiguous "
                                    f"(15m-ST={_st_str}) — holding entry."
                                )
                                can_enter = False
                        elif _vix_now < config.VIX_MIN:
                            self.logger.info(
                                f"  [VIX-GATE] {self.instrument}: VIX={_vix_now:.1f} "
                                f"< {config.VIX_MIN} (too quiet — 130% target unlikely). "
                                f"Holding entry."
                            )
                            can_enter = False

                # ── Path F: Reversal Scout — independent of B/C/D/E trading limits ──
                # PATH-F has its own capital (₹10k) and trade counter; it must run
                # even when Path B/C/D trades_today limit is reached or a concurrent
                # position is held.  reversal_scout has its own per-bar dedup.
                # Window: same START as vX but capped at 13:00 (PATH_F_ENTRY_END) so
                # OTM options (2 strikes out) have ≥90 min runway to force-close.
                if _path_f_in_window:
                    try:
                        reversal_scout.evaluate_bar(
                            instrument     = self.instrument,
                            df             = df,
                            htf            = htf,
                            oc             = oc,
                            oi_zones       = self._oi_zones,
                            inst_cfg       = self.inst_cfg,
                            hv             = hv,
                            logger         = self.logger,
                            now            = now,
                            in_window      = True,
                            days_to_expiry = config.DAYS_TO_EXPIRY,
                        )
                    except Exception as _pd_exc:
                        self.logger.warning(
                            f"  [PATH-F] Scout error: {_pd_exc}"
                        )

                # ── Path G: OI Breakout Scout — independent of B/C/D/E/F ──────────
                # Fires when price breaks through a MAJOR/WALL OI level with
                # momentum (ADX + VWAP + HTF) — no EMA cross required.
                # Window: 10:00–13:30 (PATH_G_ENTRY_END); wall breaks need 60+ min
                # follow-through before close. Own capital pool (₹10k), own dedup.
                try:
                    breakout_scout.evaluate_bar(
                        instrument     = self.instrument,
                        df             = df,
                        htf            = htf,
                        oi_zones       = self._oi_zones,
                        inst_cfg       = self.inst_cfg,
                        hv             = hv,
                        logger         = self.logger,
                        now            = now,
                        in_window      = _in_window,
                        days_to_expiry = config.DAYS_TO_EXPIRY,
                    )
                except Exception as _pe_exc:
                    self.logger.warning(
                        f"  [PATH-G] Breakout scout error: {_pe_exc}"
                    )

                # ── MaxPain Trap (Variant A) — independent paper strategy ─────────
                # Fires 09:15–10:00 on expiry days when spot is displaced ≥0.5%
                # from MaxPain. Own ₹10k paper capital pool, not Fyers orders.
                if config.MP_TRAP_ENABLED:
                    try:
                        max_pain_trap.evaluate_bar(
                            instrument = self.instrument,
                            df         = df,
                            oc         = oc,
                            dte        = config.DAYS_TO_EXPIRY,
                            now        = now,
                            lot_size   = int(self.inst_cfg.get('lot_size', 1)),
                            strike_gap = int(self.inst_cfg.get('strike_gap', 50)),
                            logger     = self.logger,
                        )
                    except Exception as _mp_exc:
                        self.logger.warning(
                            f"  [MP-TRAP] evaluate_bar error: {_mp_exc}"
                        )

                # Log exactly why entry is blocked (once per new bar to avoid spam)
                if not can_enter:
                    _bar_ts_gate = df.index[-1]
                    if _bar_ts_gate != self._last_scan_ts:
                        if not _in_window:
                            self.logger.info(
                                f"  [GATE] {self.instrument}: outside entry window "
                                f"({self.entry_start.strftime('%H:%M')}–"
                                f"{self.entry_end.strftime('%H:%M')}), "
                                f"current={_t.strftime('%H:%M')}"
                            )
                        elif self.trades_today >= config.MAX_TRADES_PER_DAY:
                            self.logger.info(
                                f"  [GATE] {self.instrument}: max trades/day reached "
                                f"({self.trades_today}/{config.MAX_TRADES_PER_DAY})"
                            )
                        elif len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
                            self.logger.info(
                                f"  [GATE] {self.instrument}: max concurrent positions "
                                f"({len(self.positions)}/{config.MAX_CONCURRENT_POSITIONS})"
                            )
                        elif self.skip_thursday and now.weekday() == 3:
                            self.logger.info(
                                f"  [GATE] {self.instrument}: Thursday blocked "
                                f"(historical WR negative)"
                            )
                        # Advance timestamp so GATE logs once per bar, not every poll
                        self._last_scan_ts = _bar_ts_gate
                if can_enter:
                    # ── Per-bar scan log (once per 5-min bar, not every poll) ──
                    _bar_ts = df.index[-1]
                    if _bar_ts != self._last_scan_ts:
                        self._last_scan_ts = _bar_ts
                        _r    = df.iloc[-1]
                        _adx  = _r.get('ADX',      float('nan'))
                        _dip  = _r.get('DI_plus',  float('nan'))
                        _dim  = _r.get('DI_minus',  float('nan'))
                        _vwap = _r.get('VWAP',      float('nan'))
                        _px   = _r.get('Close',     float('nan'))
                        _adx_need = min(self.call_adx_min, self.put_adx_min)
                        _di_dir   = 'BULL' if _dip > _dim else 'BEAR'
                        _di_ok    = '✓' if _dip != _dim else '–'
                        _adx_ok   = '✓' if _adx > _adx_need else '✗'
                        _vwap_str = (f"VWAP={_vwap:,.0f}"
                                     f"({'✓' if _px > _vwap else '✗'})"
                                     if not pd.isna(_vwap) else "VWAP=n/a")
                        _tue_note = (
                            f' [Tue: CALL gate ADX≥{self.tuesday_call_adx_min}'
                            f' DI+≥{self.tuesday_call_di_spread}'
                            f' | PUT gate ADX≥{self.tuesday_put_adx_min}'
                            f' DI-≥{self.tuesday_put_di_spread}]'
                            if (self.skip_tuesday and now.weekday() == 1) else ''
                        )
                        _st5_raw  = _r.get('ST_5m', float('nan'))
                        _st5_str  = (f"ST5={'BULL' if _st5_raw == 1 else 'BEAR'}"
                                     if not pd.isna(_st5_raw) else "ST5=?")
                        self.logger.info(
                            f"  [SCAN] {self.instrument} "
                            f"Px={_px:,.0f} | "
                            f"DI+={_dip:.1f} DI-={_dim:.1f} ({_di_dir}{_di_ok}) | "
                            f"ADX={_adx:.1f}(need>{_adx_need}){_adx_ok} | "
                            f"{_vwap_str} | {_st5_str}{_tue_note}"
                        )

                        # ── Near-miss: track closest-to-signal bar today ──────
                        if not pd.isna(_adx) and _adx > self._nm_best_adx:
                            self._nm_best_adx = _adx
                            # Determine likely direction from DI alignment
                            _nm_dir = 'CALL' if _dip > _dim else 'PUT'
                            _nm_adx_need = (self.call_adx_min if _nm_dir == 'CALL'
                                            else self.put_adx_min)
                            # ATM strike at this bar
                            _nm_strike = int(round(_px / self.strike_gap) * self.strike_gap)
                            # Option price — live: fetch LTP from Fyers; paper: Black-Scholes
                            _nm_opt_price = None
                            try:
                                if self.live and self.fyers:
                                    from fyers_orders import (build_option_symbol,
                                                              get_next_expiry, get_ltp)
                                    _nm_expiry = get_next_expiry(self.instrument)
                                    _nm_sym    = build_option_symbol(
                                        self.instrument, _nm_strike,
                                        _nm_dir, _nm_expiry)
                                    _nm_opt_price = get_ltp(self.fyers, _nm_sym)
                                else:
                                    _nm_hv        = _r.get('HV', 0.18)
                                    _nm_T         = config.DAYS_TO_EXPIRY / 365
                                    _nm_opt_price = bs_price(
                                        _nm_dir, _px, _nm_strike, _nm_T, _nm_hv)
                            except Exception:
                                pass   # price fetch failure never crashes near-miss
                            # What's missing to fire?
                            _missing = []
                            if _adx <= _nm_adx_need:
                                _missing.append(
                                    f"ADX {_adx:.1f} < {_nm_adx_need} "
                                    f"({_nm_adx_need - _adx:.1f} pts short)"
                                )
                            if not pd.isna(_vwap):
                                _vwap_ok = (_px > _vwap if _nm_dir == 'CALL'
                                            else _px < _vwap)
                                if not _vwap_ok:
                                    _nm_side = 'above' if _nm_dir == 'CALL' else 'below'
                                    _missing.append(f"price not {_nm_side} VWAP")
                            self._nm_best = {
                                'time'     : now.strftime('%H:%M'),
                                'dir'      : _nm_dir,
                                'adx'      : round(_adx, 1),
                                'adx_need' : _nm_adx_need,
                                'price'    : round(_px, 2),
                                'strike'   : _nm_strike,
                                'opt_price': round(_nm_opt_price, 2) if _nm_opt_price else None,
                                'vwap'     : round(_vwap, 2) if not pd.isna(_vwap) else None,
                                'missing'  : _missing,
                            }

                        # ── Near-miss JSONL recorder ──────────────────────────
                        # Records "close-call" bars to logs/near_miss_{inst}_{date}.jsonl
                        # for periodic statistical analysis (not daily tweaks).
                        # Two categories:
                        #   ADX_LOW    — ADX within 5pts of threshold, DI aligned
                        #   STALE_CROSS — ADX ok but last DI cross was 4-10 bars ago
                        _nm_direction  = 'CALL' if _dip > _dim else 'PUT'
                        _nm_adx_thr    = (self.call_adx_min if _nm_direction == 'CALL'
                                          else self.put_adx_min)
                        _nm_adx_gap    = _nm_adx_thr - _adx   # positive = ADX is short

                        # Find bars since last DI+/DI- cross (scan back up to 15 bars)
                        _cross_bars_ago = None
                        _dip_arr = df['DI_plus'].values
                        _dim_arr = df['DI_minus'].values
                        for _cb in range(1, min(15, len(_dip_arr))):
                            _was_bull = _dip_arr[-_cb - 1] > _dim_arr[-_cb - 1]
                            _is_bull  = _dip_arr[-_cb]     > _dim_arr[-_cb]
                            if _was_bull != _is_bull:
                                _cross_bars_ago = _cb
                                break

                        _vwap_ok_nm = (
                            not pd.isna(_vwap) and (
                                (_nm_direction == 'CALL' and _px > _vwap) or
                                (_nm_direction == 'PUT'  and _px < _vwap)
                            )
                        )
                        _di_max = max(abs(_dip), abs(_dim), 1.0)
                        _di_spread_pct  = abs(_dip - _dim) / _di_max
                        _htf_bull = htf.get('supertrend_15m') == 1
                        _htf_bear = htf.get('supertrend_15m') == -1
                        _today_str = now.strftime('%Y-%m-%d')
                        _time_str  = now.strftime('%H:%M')
                        _vwap_val  = None if pd.isna(_vwap) else _vwap

                        # ADX_LOW: DI pointing right, ADX within 5pts of needed
                        if 0 < _nm_adx_gap < 5:
                            near_miss_tracker.record(
                                date=_today_str, time=_time_str,
                                instrument=self.instrument,
                                reason='ADX_LOW',
                                adx_actual=_adx,
                                adx_threshold=_nm_adx_thr,
                                adx_gap=_nm_adx_gap,
                                direction=_nm_direction,
                                px=_px, di_plus=_dip, di_minus=_dim,
                                di_spread_pct=_di_spread_pct,
                                vwap=_vwap_val, vwap_ok=_vwap_ok_nm,
                                htf_bull=_htf_bull, htf_bear=_htf_bear,
                                cross_bars_ago=_cross_bars_ago,
                            )
                            self._nm_pending.append({
                                'bar_ts'  : _bar_ts,      'date': _today_str,
                                'time'    : _time_str,    'reason': 'ADX_LOW',
                                'direction': _nm_direction, 'px': _px,
                                'done_30m': False, 'done_60m': False, 'done_90m': False,
                            })

                        # STALE_CROSS: ADX sufficient but DI cross is 4-10 bars old
                        if (_nm_adx_gap <= 0 and _cross_bars_ago is not None
                                and 4 <= _cross_bars_ago <= 10):
                            near_miss_tracker.record(
                                date=_today_str, time=_time_str,
                                instrument=self.instrument,
                                reason='STALE_CROSS',
                                adx_actual=_adx,
                                adx_threshold=_nm_adx_thr,
                                adx_gap=_nm_adx_gap,
                                direction=_nm_direction,
                                px=_px, di_plus=_dip, di_minus=_dim,
                                di_spread_pct=_di_spread_pct,
                                vwap=_vwap_val, vwap_ok=_vwap_ok_nm,
                                htf_bull=_htf_bull, htf_bear=_htf_bear,
                                cross_bars_ago=_cross_bars_ago,
                            )
                            self._nm_pending.append({
                                'bar_ts'  : _bar_ts,      'date': _today_str,
                                'time'    : _time_str,    'reason': 'STALE_CROSS',
                                'direction': _nm_direction, 'px': _px,
                                'done_30m': False, 'done_60m': False, 'done_90m': False,
                            })

                        # ── Resolve pending near-miss outcomes ────────────────────────
                        # On each new bar check if any earlier near-miss has reached
                        # its +30 / +60 / +90 min mark and record what price did.
                        # Silent on all errors — never affects the trading loop.
                        _nm_done = []
                        for _pnm in self._nm_pending:
                            try:
                                _bars_el = int(
                                    (_bar_ts - _pnm['bar_ts']).total_seconds() / 300)
                            except Exception:
                                _bars_el = 0
                            if not _pnm['done_30m'] and _bars_el >= 6:
                                near_miss_tracker.record_outcome(
                                    date=_pnm['date'], time=_pnm['time'],
                                    instrument=self.instrument,
                                    reason=_pnm['reason'], direction=_pnm['direction'],
                                    px=_pnm['px'], outcome_at='30m', px_now=_px,
                                )
                                _pnm['done_30m'] = True
                            if not _pnm['done_60m'] and _bars_el >= 12:
                                near_miss_tracker.record_outcome(
                                    date=_pnm['date'], time=_pnm['time'],
                                    instrument=self.instrument,
                                    reason=_pnm['reason'], direction=_pnm['direction'],
                                    px=_pnm['px'], outcome_at='60m', px_now=_px,
                                )
                                _pnm['done_60m'] = True
                            if not _pnm['done_90m'] and _bars_el >= 18:
                                near_miss_tracker.record_outcome(
                                    date=_pnm['date'], time=_pnm['time'],
                                    instrument=self.instrument,
                                    reason=_pnm['reason'], direction=_pnm['direction'],
                                    px=_pnm['px'], outcome_at='90m', px_now=_px,
                                )
                                _pnm['done_90m'] = True
                                _nm_done.append(_pnm)
                        for _pnm in _nm_done:
                            self._nm_pending.remove(_pnm)

                    # ── Pivot check: runs whenever a position is open ──────────
                    # Checks if open position is losing + reversal signal → close + pivot.
                    # Must run before the normal signal chain so pivot_signal can bypass
                    # the can_enter trade-count gate (it's repositioning, not a new trade).
                    _pivot_signal = None
                    if self.positions:
                        _pivot_signal = self._check_pivot(
                            df, htf, oc, now, current_price, hv
                        )
                        if _pivot_signal:
                            # Force-close the losing position with pivot reason
                            self.check_exits(current_price, hv, pivot_close=True)
                            # If consolidated loss not exceeded, enter the pivot trade
                            if not shared_state.is_consolidated_loss_exceeded(
                                    logger=self.logger):
                                _lots, _oi_reason = self._oi_adjust_lots(
                                    _pivot_signal['type'], _pivot_signal.get('lots', 1), oc
                                )
                                _pivot_signal['atm_iv'] = oc.get('atm_iv')   # IV-scaled target
                                self.enter_trade(_pivot_signal, hv, lots=_lots)
                                # Note: enter_trade already increments self.trades_today

                    # ── Path A: Opening Range Breakout (09:30–11:00) ───────────
                    signal = None
                    if _orb_window and not self.positions and not self._path_a_fired:
                        _can_orb = (
                            self.trades_today < config.MAX_TRADES_PER_DAY and
                            self.daily_pnl > -config.MAX_DAILY_LOSS and
                            not shared_state.is_consolidated_loss_exceeded() and
                            not (self.skip_thursday and now.weekday() == 3) and
                            not self._skip_bnf_today
                        )
                        if _can_orb:
                            signal = self.get_path_a_signal(df, df.iloc[-1], now)
                            if signal:
                                self._path_a_fired = True

                    # ── Path A Re-entry (after stop-loss hit) ─────────────────
                    # One re-entry allowed if the stop-loss fired AND trend re-establishes.
                    # Higher ADX bar (≥35 vs per-day min 20-30) confirms re-break conviction.
                    # Time cap: before PATH_A_REENTRY_CUTOFF (13:00) for 90+ min runway.
                    # No _orb_window requirement — re-entry can fire into main session.
                    if (signal is None and not self.positions and
                            self._path_a_reentry_available and
                            getattr(config, 'PATH_A_REENTRY_ENABLED', False) and
                            now.strftime('%H:%M') < getattr(config, 'PATH_A_REENTRY_CUTOFF', '13:00')):
                        _can_reentry = (
                            self.trades_today < config.MAX_TRADES_PER_DAY and
                            self.daily_pnl > -config.MAX_DAILY_LOSS and
                            not shared_state.is_consolidated_loss_exceeded() and
                            not (self.skip_thursday and now.weekday() == 3) and
                            not self._skip_bnf_today
                        )
                        if _can_reentry:
                            _reentry_sig = self.get_path_a_signal(df, df.iloc[-1], now)
                            if _reentry_sig:
                                _reentry_adx_min = getattr(config, 'PATH_A_REENTRY_ADX_MIN', 35)
                                if _reentry_sig.get('adx', 0) >= _reentry_adx_min:
                                    signal = _reentry_sig
                                    self._path_a_reentry_available = False   # one re-entry only
                                    self._path_a_fired             = True
                                    self.logger.info(
                                        f"[PATH-A] RE-ENTRY ✅ | {_reentry_sig['type']} "
                                        f"ADX={_reentry_sig['adx']:.1f} ≥ {_reentry_adx_min}"
                                    )
                                else:
                                    self.logger.info(
                                        f"[PATH-A] RE-ENTRY SKIP | ADX="
                                        f"{_reentry_sig.get('adx', 0):.1f} < {_reentry_adx_min}"
                                    )

                    # ── Path B: Morning Range Breakout ─────────────────────────
                    # PATH_B_ENABLED = True keeps EMA crossover archived (old vX).
                    # PATH_B_LIVE    = True since Jul 8 2026 (v1.6): the disabling
                    #                  backtest was BS-premium-priced and predates the
                    #                  live gate stack; Jul 7/8 afternoon breakdowns
                    #                  had no live path that could fire 11:00-14:00.
                    #                  Once/day, full downstream gates apply.
                    if signal is None and _main_window:
                        if config.PATH_B_ENABLED and config.PATH_B_LIVE and not self._path_b_fired:
                            signal = self.get_path_b_signal(df, htf, oc)
                            if signal:
                                self._path_b_fired = True

                    # ── Path C / Path D fallback (EMA crossover archived in get_signal)
                    # Path C (CONT): EMA spread widening 3 bars + ADX ≥ 35 + 12:00+
                    # Path D (ST_FLIP): 5m SuperTrend direction flip + ADX + VWAP
                    # Both independent of Path B — still valid fallback signals.
                    if signal is None and _main_window:
                        signal = self.get_signal(df)

                    # ── Path E: HTF Trend Continuation (last resort) ─────────────
                    # Catches slow-grind days where ADX builds gradually and neither
                    # Path B/C/D fires. Window: 12:30–13:45.
                    if signal is None and _main_window:
                        signal = self.get_path_e_signal(df, htf)

                    # ── PATH_REV: MaxPain Snap Reversal ──────────────────────────
                    # Fires after ORB window when morning trend exhausts + options
                    # reposition toward MaxPain.  Paper-only until PATH_REV_LIVE=True.
                    # Runs independently of _main_window (has its own time gate).
                    _rev_choppy_skip = (
                        self._regime == 'CHOPPY'
                        and getattr(config, 'REGIME_CHOPPY_REV_SKIP', True)
                        and getattr(config, 'REGIME_DETECTION_ENABLED', True)
                    )
                    if (signal is None and _rev_choppy_skip
                            and not self._path_rev_fired and not self.positions
                            and now.strftime('%H:%M') >= getattr(config, 'PATH_REV_START', '12:00')):
                        self.logger.debug(
                            f"  [REGIME] {self.instrument}: CHOPPY — REV suppressed"
                        )
                    if (signal is None
                            and not self._path_rev_fired
                            and not self.positions
                            and not _rev_choppy_skip):
                        _rev_sig = self.get_path_rev_signal(df, oc, now)
                        if _rev_sig:
                            if getattr(config, 'PATH_REV_LIVE', False):
                                signal = _rev_sig
                                self._path_rev_fired = True
                            else:
                                # Paper-only: log + estimate option price but don't enter
                                _rv_px = _rev_sig['price']
                                _rv_tp = _rev_sig['type']
                                _rv_str = int(_rv_px // self.strike_gap) * self.strike_gap
                                if _rv_tp == 'CALL':
                                    _rv_str += self.strike_gap
                                _T_rem = max(config.DAYS_TO_EXPIRY - 0, 0.01) / 365
                                elapsed_d = 0.0
                                _rv_hv = float(df['HV'].iloc[-1]) if 'HV' in df.columns else 0.18
                                _rv_opt = bs_price(_rv_tp, _rv_px, _rv_str, _T_rem, _rv_hv)
                                self.logger.info(
                                    f"  [PATH-REV PAPER] {self.instrument} {_rv_tp} "
                                    f"| Strike={_rv_str:,} | Est opt ₹{_rv_opt:.2f} "
                                    f"| (set PATH_REV_LIVE=True to trade live)"
                                )
                                self._path_rev_fired = True   # log once per day

                    # ── Tuesday CALL filter: elevated ADX + DI-spread gate ───────
                    # Backtest: overall Tue CALL WR = 31.2% (danger zone) — but this is
                    # the aggregate including weak setups. A strong unambiguous bull trend
                    # (high ADX + clear DI+ dominance) is worth taking.
                    # Gate: ADX ≥ tuesday_call_adx_min AND DI+ dominance ≥ tuesday_call_di_spread.
                    # PATH_REV exempt: reversal signals fire when ADX is waning + DI just
                    # crossing — applying continuation-CALL criteria would always block them.
                    if (signal
                            and self.skip_tuesday
                            and now.weekday() == 1
                            and signal['type'] == 'CALL'
                            and signal.get('path') != 'REV'):
                        _sig_adx = signal.get('adx', 0)
                        _dip_val = df['DI_plus'].iloc[-1]  if 'DI_plus'  in df.columns else float('nan')
                        _dim_val = df['DI_minus'].iloc[-1] if 'DI_minus' in df.columns else float('nan')
                        _di_spread_val = (_dip_val - _dim_val) if not (pd.isna(_dip_val) or pd.isna(_dim_val)) else 999
                        path = signal.get('path', 'vX')
                        if _sig_adx < self.tuesday_call_adx_min:
                            self.logger.info(
                                f"  [SKIP] Tuesday CALL: ADX {_sig_adx:.1f} < "
                                f"{self.tuesday_call_adx_min} (Tue elevated threshold, path={path})")
                            signal = None
                        elif _di_spread_val < self.tuesday_call_di_spread:
                            self.logger.info(
                                f"  [SKIP] Tuesday CALL: DI+ spread {_di_spread_val:.1f} < "
                                f"{self.tuesday_call_di_spread} (insufficient bull dominance, path={path})")
                            signal = None
                        else:
                            self.logger.info(
                                f"  [OK] Tuesday CALL passed elevated gate: ADX={_sig_adx:.1f} "
                                f"(≥{self.tuesday_call_adx_min}), DI+-spread={_di_spread_val:.1f} "
                                f"(≥{self.tuesday_call_di_spread}), path={path}")

                    # ── Tuesday PUT filter: raised ADX + DI-spread gate ───────────
                    # Tuesday PUTs allowed — but only under strong trending conditions.
                    # Rationale:
                    #   - NIFTY: gap-fill risk on Tuesdays; weak PUT setups often recover.
                    #   - BANKNIFTY: Wed expiry means next-week (8-day) options are used
                    #     (MIN_DAYS_TO_EXPIRY=2 skips the 1-day Wed expiry), giving low
                    #     gamma. The 130% target needs a very strong downtrend to hit.
                    # Gate: ADX ≥ tuesday_put_adx_min AND DI- dominance ≥ tuesday_put_di_spread.
                    # PATH_REV exempt: reversal PUT (morning CALL trend exhausted) fires when
                    # ADX is waning — continuation-PUT criteria would block valid reversals.
                    if (signal
                            and self.skip_tuesday
                            and now.weekday() == 1
                            and signal['type'] == 'PUT'
                            and signal.get('path') != 'REV'):
                        _sig_adx = signal.get('adx', 0)
                        # DI spread from latest bar
                        _dip_val = df['DI_plus'].iloc[-1]  if 'DI_plus'  in df.columns else float('nan')
                        _dim_val = df['DI_minus'].iloc[-1] if 'DI_minus' in df.columns else float('nan')
                        _di_spread_val = (_dim_val - _dip_val) if not (pd.isna(_dip_val) or pd.isna(_dim_val)) else 999
                        path = signal.get('path', 'vX')
                        if _sig_adx < self.tuesday_put_adx_min:
                            self.logger.info(
                                f"  [SKIP] Tuesday PUT: ADX {_sig_adx:.1f} < "
                                f"{self.tuesday_put_adx_min} (Tue elevated threshold, path={path})")
                            signal = None
                        elif _di_spread_val < self.tuesday_put_di_spread:
                            self.logger.info(
                                f"  [SKIP] Tuesday PUT: DI spread {_di_spread_val:.1f} < "
                                f"{self.tuesday_put_di_spread} (insufficient bear dominance, path={path})")
                            signal = None
                        else:
                            self.logger.info(
                                f"  [OK] Tuesday PUT passed elevated gate: ADX={_sig_adx:.1f} "
                                f"(≥{self.tuesday_put_adx_min}), DI-spread={_di_spread_val:.1f} "
                                f"(≥{self.tuesday_put_di_spread}), path={path}")

                    if signal and not self._duplicate_signal(signal):
                        # ── Session handover gate ─────────────────────────────
                        # Block entry if early_bot still holds an open position
                        # on this instrument (e.g. trailing through 11:00 handover).
                        if shared_state.has_open_position(
                                self.instrument, exclude_bot=self._bot_id):
                            _eb_pos = shared_state.get_open_positions(
                                self.instrument)
                            self.logger.info(
                                f"  [GATE] {self.instrument}: early_bot has open "
                                f"position — holding entry until it closes. "
                                f"Positions: {_eb_pos}"
                            )
                            signal = None

                    if signal and not self._duplicate_signal(signal):
                        # Lot sizing: signal.get('lots') = 1 or 2 from signal_strength score.
                        # (strength>=2 → 2 lots; backtest: 80% WR on 2-lot trades)
                        # OI adjustment can add/remove 1 lot based on PCR extremes.
                        _lots, _oi_reason = self._oi_adjust_lots(
                            signal['type'], signal.get('lots', 1), oc
                        )
                        if _oi_reason:
                            self.logger.info(
                                f"  [OI] {self.instrument}: {_oi_reason}"
                            )

                        # ── Regime + Quality gate ────────────────────────────────
                        if (signal and
                                getattr(config, 'REGIME_DETECTION_ENABLED', True)
                                and self._regime == 'CHOPPY'):
                            _lots = min(_lots, getattr(config, 'REGIME_CHOPPY_LOTS_CAP', 1))
                            if signal.get('path') == 'REV':
                                self.logger.info(
                                    f"  [REGIME] {self.instrument}: CHOPPY — REV blocked"
                                )
                                signal = None
                            else:
                                self.logger.info(
                                    f"  [REGIME] {self.instrument}: CHOPPY — "
                                    f"lots capped to {_lots}"
                                )
                        if (signal and
                                getattr(config, 'QUALITY_GATE_ENABLED', True)
                                and self._quality_state == 'REDUCED'):
                            _lots = min(_lots, 1)
                            if signal.get('path') == 'REV':
                                self.logger.info(
                                    f"  [QUALITY] {self.instrument}: REDUCED — REV blocked"
                                )
                                signal = None
                            else:
                                self.logger.info(
                                    f"  [QUALITY] {self.instrument}: REDUCED — "
                                    f"lots capped to 1"
                                )

                        # ── Reversal Guard (trend exhaustion filter) ────────────
                        # Scores 0-100: RSI extreme, RSI divergence, VWAP stretch,
                        # ADX declining, consecutive candles (+ VIX spike if data present).
                        # Phase 2 (active): MODERATE (>=30) caps lots; HIGH (>=50) hard-skip.
                        try:
                            _rev = compute_reversal_risk(df, len(df) - 1,
                                                         signal['type'])
                            _rev_score = _rev['score']
                            _rev_level = _rev['risk_level']
                            self.logger.info(
                                f"  [REV-GUARD] {self.instrument} "
                                f"{signal['type']} score={_rev_score}/100 "
                                f"({_rev_level}) — {_rev['reason']}"
                            )
                            if _rev['skip'] or _rev['reduce_lots']:
                                _level = 'HIGH' if _rev['skip'] else 'MODERATE'
                                if _lots > 1:
                                    _lots = 1
                                self.logger.info(
                                    f"  [REV-GUARD] {self.instrument}: {_level} "
                                    f"exhaustion risk ({_rev_score}pts) → "
                                    f"capping to 1 lot | {_rev['reason']}"
                                )
                        except Exception as _rev_exc:
                            self.logger.warning(
                                f"  [REV-GUARD] Error computing risk: {_rev_exc}"
                                f" — proceeding without guard"
                            )

                        # ── OI Zone Gate (supply/demand context) ────────────────
                        # Uses EOD-saved option chain OI zones (from oi_zones_eod.py)
                        # to assess whether the current price is in a favourable
                        # position relative to OI walls before entering.
                        #
                        # Decision ladder:
                        #   BOOST  → price just broke through a major OI wall
                        #            (gamma squeeze likely) → add 1 lot (max 3)
                        #   TAKE   → price in clear OI space → standard sizing
                        #   REDUCE → approaching a significant wall that may cap
                        #            the move → cap to 1 lot
                        #   SKIP   → price hugging an adverse wall, tight box, or
                        #            max-pain strongly opposing → block entry
                        #
                        # Phase 1 (active): BOOST, TAKE, REDUCE all execute.
                        # Phase 2 (after 30 days paper): evaluate SKIP rate, then
                        # uncomment the hard-skip block if SKIP trades underperform.
                        _oz_result: dict = {}   # captured for signal_scorer below
                        if signal:
                            try:
                                _dte = config.DAYS_TO_EXPIRY
                                _oz  = get_zone_signal(
                                    signal['price'], signal['type'],
                                    self._oi_zones, dte=_dte
                                )
                                _oz_result = _oz   # expose to scorer
                                _oz_action = _oz['action']
                                _oz_reason = _oz['reason']

                                self.logger.info(
                                    f"  [OI-ZONE] {self.instrument} "
                                    f"{signal['type']} → {_oz_action} "
                                    f"(score={_oz['score']}) | {_oz_reason}"
                                )

                                if _oz_action == 'BOOST' and _lots < 3:
                                    _lots += 1
                                    self.logger.info(
                                        f"  [OI-ZONE] {self.instrument}: BOOST "
                                        f"→ raising to {_lots} lots "
                                        f"(broke OI wall with momentum)"
                                    )
                                elif _oz_action == 'REDUCE' and _lots > 1:
                                    _lots = 1
                                    self.logger.info(
                                        f"  [OI-ZONE] {self.instrument}: REDUCE "
                                        f"→ capping to 1 lot "
                                        f"(approaching OI wall)"
                                    )
                                # OI-ZONE never blocks an ORB trade.
                                # Worst outcome is REDUCE (1 lot). Stop-loss handles risk.

                            except Exception as _oz_exc:
                                self.logger.warning(
                                    f"  [OI-ZONE] Error computing zone signal: "
                                    f"{_oz_exc} — proceeding without OI context"
                                )

                        # ── Live OI Direction Bias ────────────────────────────
                        # Uses live PCR + 30-min PCR drift + MaxPain gravity +
                        # IV skew to decide if the options market CONFIRMS,
                        # is NEUTRAL about, or REJECTS the signal direction.
                        # This is the dynamic replacement for static no_call /
                        # no_put suppression — direction is earned, not assumed.
                        #
                        # Gate levels (from config):
                        #   REJECT  → hard block (OI_DIRECTION_BIAS_REJECT=True)
                        #   NEUTRAL → allowed normally; blocked on days where
                        #             oi_confirm_required=True (Tue, Wed)
                        #   CONFIRM → cleanest entry; no restriction
                        if signal and getattr(config, 'OI_DIRECTION_BIAS_ENABLED', True):
                            try:
                                _oi_bias, _oi_reason = self._get_oi_direction_bias(
                                    signal['type'], signal['price'], oc
                                )
                                _oi_emoji    = {'CONFIRM': '✅', 'NEUTRAL': '⚠️',
                                                'REJECT': '🚫'}.get(_oi_bias, '')
                                _day_cfg_oi  = self._get_day_cfg(now.strftime('%a'))
                                _oi_conf_req = _day_cfg_oi.get('oi_confirm_required', False)
                                _oi_reject   = getattr(config, 'OI_DIRECTION_BIAS_REJECT', True)

                                self.logger.info(
                                    f"  [OI-BIAS] {self.instrument} "
                                    f"{signal['type']}: {_oi_emoji}{_oi_bias} "
                                    f"| {_oi_reason}"
                                )
                                if _oi_bias == 'REJECT' and _oi_reject:
                                    self.logger.info(
                                        f"  [OI-BIAS] → BLOCKING entry "
                                        f"(score ≤ −2, OI contradicts {signal['type']})"
                                    )
                                    signal = None
                                # NEUTRAL: OI has no strong view — ORB direction
                                # from price action is the primary signal.
                                # Only REJECT (score ≤-2) hard-blocks an entry.

                            except Exception as _oi_exc:
                                self.logger.warning(
                                    f"  [OI-BIAS] Error: {_oi_exc} — "
                                    f"proceeding without live OI bias"
                                )

                        # ── Unified Scorer Gate (time-band weighted) ──────────
                        # Scores signal quality 0-100 across 11 components with
                        # weights that shift across 5 time bands.  Blocks entry
                        # when overall quality is below UNIFIED_SCORE_THRESHOLD.
                        # Key benefit: blocks PATH-REV trades when HTF (15m ST)
                        # directly opposes direction — today's learned lesson.
                        if signal and getattr(config, 'UNIFIED_SCORER_ENABLED', False):
                            try:
                                try:
                                    _us_oi = _oi_bias
                                except NameError:
                                    _us_oi = 'NEUTRAL'
                                _ub = unified_scorer.compute_score(
                                    direction        = signal['type'],
                                    df               = df,
                                    or_hi            = self._or_high,
                                    or_lo            = self._or_low,
                                    st15_val         = htf.get('supertrend_15m'),
                                    oi_bias          = _us_oi,
                                    now_str          = now.strftime('%H:%M'),
                                    morning_adx_peak = self._morning_adx_peak,
                                    morning_dir      = self._morning_dir,
                                )
                                _ub_thr  = getattr(config, 'UNIFIED_SCORE_THRESHOLD', 55)
                                # Per-band offset: 11:00-12:00 lunchtime entries ran
                                # 0/4 (-₹7,420) live — demand extra quality there.
                                _ub_thr += getattr(
                                    config, 'UNIFIED_BAND_THRESHOLD_OFFSET', {}
                                ).get(_ub.get('band', ''), 0)
                                _ub_pass = _ub['score'] >= _ub_thr
                                signal['unified_score']      = _ub['score']
                                signal['unified_band']       = _ub['band']
                                signal['unified_components'] = {
                                    k: v[2] for k, v in _ub['components'].items()
                                }
                                _ub_cpts = '  '.join(
                                    f"{k}:{v[2]}/{v[0]}"
                                    for k, v in _ub['components'].items()
                                    if v[0] > 0
                                )
                                self.logger.info(
                                    f"  [UNIFIED] {self.instrument} "
                                    f"{signal['type']} path={signal.get('path', '')} "
                                    f"score={_ub['score']}/100 band={_ub['band']} "
                                    f"{'PASS' if _ub_pass else 'BLOCK'} "
                                    f"(thr={_ub_thr}) | {_ub_cpts}"
                                )
                                if not _ub_pass:
                                    self.logger.info(
                                        f"  [UNIFIED-BLOCK] {self.instrument} "
                                        f"{signal['type']} score={_ub['score']} "
                                        f"< {_ub_thr} — entry suppressed"
                                    )
                                    signal = None
                            except Exception as _ub_exc:
                                self.logger.warning(
                                    f"  [UNIFIED] Error: {_ub_exc} — "
                                    f"proceeding without unified gate"
                                )

                        if signal:
                            # ── Composite Signal Scorer (Phase 2: gate active) ────
                            # Logs a weighted 0-100 breakdown of signal quality
                            # across 6 components.
                            # Phase 2: gate='REDUCE' (score<40) → cap to 1 lot.
                            # Phase 3 (future): gate='SKIP' (score<30) → block entry.
                            try:
                                _sc = signal_scorer.score(
                                    signal_type   = signal['type'],
                                    df            = df,
                                    htf           = htf,
                                    oc            = oc,
                                    oz            = _oz_result,
                                    lookback_used = config.EMA_CROSSOVER_LOOKBACK,
                                    path          = signal.get('path', 'vX'),
                                )
                                self.logger.info(signal_scorer.format_score(_sc))
                                signal['composite_score'] = _sc['total']
                                # Phase 2: cap lots on low-quality signals; upgrade on high conviction
                                if _sc['gate'] == 'REDUCE' and _lots > 1:
                                    _lots = 1
                                    self.logger.info(
                                        f"  [SCORER] {self.instrument}: WEAK signal "
                                        f"({_sc['total']}/100 < 40) → capping to 1 lot"
                                    )
                                elif (_sc.get('total', 0) >= getattr(
                                        config, 'HIGH_CONVICTION_SCORER_THRESHOLD', 65)
                                      and _lots < 2):
                                    _lots = 2
                                    self.logger.info(
                                        f"  [SCORER] {self.instrument}: HIGH CONVICTION "
                                        f"({_sc['total']}/100 ≥ "
                                        f"{getattr(config, 'HIGH_CONVICTION_SCORER_THRESHOLD', 65)}"
                                        f") → upgrading to 2 lots"
                                    )
                            except Exception as _sc_exc:
                                self.logger.warning(
                                    f"  [SCORER] Error computing composite score: "
                                    f"{_sc_exc}"
                                )

                            # ── Post-11 Scorer (Path A ORB entries from 11:00 onward) ──
                            # By 11am the EMA cross used by signal_scorer is 8–16+ bars
                            # old — its 'freshness' score is always 0.  Post-11 scorer
                            # replaces that with live indicators: OR extension, DI spread
                            # (momentum now), trend-bar consistency, and theta-aware time
                            # quality.
                            #
                            # Gate:
                            #   SKIP   (<35)  → block entry (theta risk > edge)
                            #   REDUCE (35-49)→ cap to 1 lot, proceed with warning
                            #   TRADE  (≥50)  → proceed with existing lot count
                            #
                            # Only fires for Path A ORB signals at or after 11:00.
                            # Re-entry signals are also evaluated (they fire post-11 by
                            # definition — PATH_A_REENTRY_CUTOFF=13:00).
                            if signal and signal.get('path') == 'A' and now.time() >= dtime(11, 0):
                                try:
                                    _p11 = post11_scorer.score(
                                        signal_type = signal['type'],
                                        entry_time  = now.time(),
                                        df          = df,
                                        or_high     = self._or_high,
                                        or_low      = self._or_low,
                                        htf         = htf,
                                        oc          = oc,
                                        oz          = _oz_result,  # OI wall proximity
                                        atm_iv      = oc.get('atm_iv'),
                                        dte         = config.DAYS_TO_EXPIRY,
                                    )
                                    self.logger.info(post11_scorer.format_score(_p11))
                                    _p11_gate = _p11['gate']

                                    if _p11_gate == 'SKIP':
                                        # Aggregate too weak — combination of factors bad.
                                        # No single component blocked; the total score did.
                                        self.logger.info(
                                            f"  [POST11] {self.instrument}: SKIP "
                                            f"({_p11['total']}/100 < "
                                            f"{getattr(config, 'POST11_SCORE_SKIP_MIN', 40)}) "
                                            f"— overall quality too low, skipping entry"
                                        )
                                        signal = None

                                    elif _p11_gate == 'STRONG':
                                        # High-confidence signal: 2 lots (if guards allow)
                                        # + OTM boost: buy one strike further OTM for
                                        # better payout if breakout continues to move the
                                        # option from OTM → near-ATM / ITM.
                                        _lots = max(1, _p11['lot_suggestion'])  # STRONG: upgrade (not cap)
                                        if getattr(config, 'POST11_OTM_BOOST', False) and getattr(config, 'PATH_A_OTM_ENABLED', False) and signal:
                                            _old_otm = signal.get('otm_strikes', 0)
                                            _new_otm = min(_old_otm + 1, 2)
                                            if _new_otm != _old_otm:
                                                signal['otm_strikes'] = _new_otm
                                                self.logger.info(
                                                    f"  [POST11] {self.instrument}: STRONG "
                                                    f"({_p11['total']}/100) — OTM boost "
                                                    f"OTM+{_old_otm}→OTM+{_new_otm} "
                                                    f"(OTM→ITM leverage on continuation) | "
                                                    f"2-lot quality ✓"
                                                )
                                            else:
                                                self.logger.info(
                                                    f"  [POST11] {self.instrument}: STRONG "
                                                    f"({_p11['total']}/100) — 2-lot quality ✓ "
                                                    f"| already at OTM+{_old_otm} max"
                                                )
                                        else:
                                            self.logger.info(
                                                f"  [POST11] {self.instrument}: STRONG "
                                                f"({_p11['total']}/100) — 2-lot quality ✓"
                                            )

                                    elif _p11_gate == 'TRADE':
                                        _lots = min(_lots, _p11['lot_suggestion'])

                                    else:   # MARGINAL
                                        _lots = min(_lots, 1)
                                        self.logger.info(
                                            f"  [POST11] {self.instrument}: MARGINAL "
                                            f"({_p11['total']}/100) — 1 lot, caution"
                                        )
                                except Exception as _p11_exc:
                                    self.logger.warning(
                                        f"  [POST11] Error computing score: {_p11_exc} "
                                        f"— proceeding without post-11 scorer"
                                    )

                            if signal:
                                # ── Final sizing clamp (v1.6) ─────────────────
                                # The composite-scorer (≥65 → 2 lots) and post-11
                                # STRONG upgrades run AFTER the regime/quality
                                # caps, so they could silently override them.
                                # Re-assert caps as the last word. Live data:
                                # CHOPPY 32% WR / avg -₹453 per trade vs
                                # TRENDING 50% WR / avg +₹989 — multi-lot
                                # conviction belongs in trending tape only.
                                _lots_pre_clamp = _lots
                                # v1.7: lot ceiling scales with book capital
                                # (DYN_MAX_LOTS_LADDER: 2 now, 3 at ₹75k, 4 at ₹1L)
                                _, _max_lots_cap = self._get_risk_params()
                                _lots = min(_lots, _max_lots_cap)
                                if (getattr(config, 'REGIME_DETECTION_ENABLED', True)
                                        and self._regime == 'CHOPPY'):
                                    _lots = min(_lots, getattr(
                                        config, 'REGIME_CHOPPY_LOTS_CAP', 1))
                                if (getattr(config, 'QUALITY_GATE_ENABLED', True)
                                        and self._quality_state == 'REDUCED'):
                                    _lots = min(_lots, 1)
                                if _lots != _lots_pre_clamp:
                                    self.logger.info(
                                        f"  [DYN-SIZE] {self.instrument}: final clamp "
                                        f"{_lots_pre_clamp}→{_lots} lots "
                                        f"(regime={self._regime}, "
                                        f"quality={self._quality_state})"
                                    )
                                signal['size_reason'] = (
                                    f"sig={signal.get('lots', 1)} "
                                    f"pre_clamp={_lots_pre_clamp} final={_lots} "
                                    f"regime={self._regime} "
                                    f"quality={self._quality_state}"
                                )
                                signal['atm_iv'] = oc.get('atm_iv')   # IV-scaled target
                                self.enter_trade(signal, hv, lots=_lots)
                                self.enter_challenger_trade(signal, hv, oc, lots=_lots)

                # ── Path F: update any open sim position outside entry window ─
                # (e.g. force-close at 14:30 even if can_enter is False)
                if not _in_window:
                    try:
                        reversal_scout.evaluate_bar(
                            instrument     = self.instrument,
                            df             = df,
                            htf            = {},
                            oc             = {},
                            oi_zones       = None,
                            inst_cfg       = self.inst_cfg,
                            hv             = hv,
                            logger         = self.logger,
                            now            = now,
                            in_window      = False,   # update-only, no new signals
                            days_to_expiry = config.DAYS_TO_EXPIRY,
                        )
                    except Exception:
                        pass

                self._log_status(current_price)

                # ── Adaptive sleep: quick exit polls when a position is open ──
                # Full index-data cycle runs every BOT_CHECK_INTERVAL (60s).
                # When positions are live, option LTP is re-fetched and exits
                # re-evaluated every EXIT_POLL_INTERVAL (20s) inside that window.
                # This means spike/stop exits fire within ~20s of the move rather
                # than up to 60s later — critical for 2-DTE option spikes.
                # Paper positions are BS-priced from index data, so quick polls
                # add nothing there; live positions get the full benefit.
                _t0       = time.time()
                _full_ivl = config.BOT_CHECK_INTERVAL
                _exit_ivl = getattr(config, 'EXIT_POLL_INTERVAL', 20)

                if self.positions and self.live and _exit_ivl < _full_ivl:
                    while True:
                        _elapsed = time.time() - _t0
                        _remain  = _full_ivl - _elapsed
                        if _remain <= 0:
                            break
                        # Sleep until the next quick-poll tick or end of cycle,
                        # whichever comes first.
                        _to_next_tick = _exit_ivl - (_elapsed % _exit_ivl)
                        time.sleep(min(_to_next_tick, _remain))

                        _elapsed = time.time() - _t0
                        if _elapsed >= _full_ivl or not self.positions:
                            break

                        # Quick exit check: get fresh option LTP, run exit logic.
                        # current_price / hv are from the last full cycle — fine
                        # because live check_exits() fetches option LTP directly
                        # from Fyers and only falls back to BS when LTP is None.
                        self.check_exits(current_price, hv)
                else:
                    time.sleep(_full_ivl)

            except KeyboardInterrupt:
                self.logger.info("Bot stopped (Ctrl+C).")
                self.logger.info(
                    f"Capital: ₹{self.capital:,.2f} | "
                    f"Total P&L: ₹{self.total_pnl:,.2f} | "
                    f"Trades: {len(self.trade_log)}"
                )
                self._save_trade_log()
                break

            except Exception as exc:
                self.logger.error(f"Loop error: {exc}", exc_info=True)
                time.sleep(30)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    instrument = sys.argv[1].upper() if len(sys.argv) > 1 else 'NIFTY'
    bot = TradingBot(instrument)
    bot.run()
