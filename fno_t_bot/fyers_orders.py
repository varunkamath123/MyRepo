"""
Fyers Live Order Execution
Handles option symbol construction and order placement for NIFTY / BANKNIFTY.

Symbol format (Fyers v3):  NSE:NIFTY27FEB2625000CE
                            NSE:BANKNIFTY25FEB2651000CE
  ─ DDMMMYY  = expiry date   (27FEB26)
  ─ STRIKE   = strike price  (25000)
  ─ CE / PE  = option type

!! Verify symbol format with Fyers support before going live !!
   Quick check: fyers.quotes({"symbols": "NSE:NIFTY27FEB2625000CE"})
"""

from __future__ import annotations

import time
import logging
from datetime import date, datetime, timedelta

import pytz
import config

IST    = pytz.timezone('Asia/Kolkata')
logger = logging.getLogger('fyers_orders')

# ─── Symbol Construction ──────────────────────────────────────────────────────

def get_next_expiry(instrument: str) -> date:
    """
    Return the nearest valid expiry date with at least MIN_DAYS_TO_EXPIRY remaining.

    Two modes controlled by 'monthly_expiry_only' in INSTRUMENTS config:
      False (default) → weekly: walk forward to nearest target weekday (NIFTY, SENSEX)
      True            → monthly: last occurrence of target weekday in the month
                        (BANKNIFTY — SEBI Nov 2023 removed weekly BANKNIFTY options;
                         NSE kept only NIFTY as a weekly-expiry index)

    Weekday mapping: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4
    """
    import calendar as _cal
    inst        = config.INSTRUMENTS[instrument]
    target_wday = inst['expiry_weekday']
    min_days    = config.MIN_DAYS_TO_EXPIRY
    today       = datetime.now(IST).date()

    if inst.get('monthly_expiry_only', False):
        # Monthly mode: find last target_wday of current month; if too close, use next month.
        for month_offset in range(3):
            # Compute year/month for this offset
            raw_month = today.month + month_offset
            yr  = today.year + (raw_month - 1) // 12
            mon = ((raw_month - 1) % 12) + 1
            # Start from last day of month and walk back to target weekday
            last_day = date(yr, mon, _cal.monthrange(yr, mon)[1])
            expiry   = last_day - timedelta(days=(last_day.weekday() - target_wday) % 7)
            if (expiry - today).days >= min_days:
                return expiry
        raise RuntimeError(f"Could not find valid monthly expiry for {instrument}")

    # Weekly mode: walk forward up to 2 weeks
    check = today
    for _ in range(14):
        if check.weekday() == target_wday:
            if (check - today).days >= min_days:
                return check
        check += timedelta(days=1)

    raise RuntimeError(f"Could not find valid expiry for {instrument}")


def build_option_symbol(instrument: str, strike: int,
                        option_type: str, expiry: date) -> str:
    """
    Build the Fyers option symbol string.
    Monthly instruments (monthly_expiry_only=True) use YYMMM format: NSE:NIFTY26APR24000CE
    Weekly instruments use compact format: NSE:SENSEX26APR1577000CE

    option_type accepts: 'CALL', 'PUT', 'CE', or 'PE' (case-insensitive).
    """
    prefix   = config.INSTRUMENTS[instrument]['option_prefix']
    opt_type = 'CE' if option_type.upper() in ('CALL', 'CE') else 'PE'

    # Determine if this is a monthly expiry (last expiry of the month).
    # Heuristic: if no same-weekday falls later in the same month, it's monthly.
    from datetime import timedelta
    next_week = expiry + timedelta(days=7)
    is_monthly = (next_week.month != expiry.month)

    if is_monthly:
        # Monthly format: YYMMM (e.g. 26APR)
        exp_str = expiry.strftime('%y%b').upper()   # e.g. 26APR
    else:
        # Weekly compact format: YY + single-char month + DD
        _MONTH_CHAR = {10: 'A', 11: 'B', 12: 'C'}
        m_char  = _MONTH_CHAR.get(expiry.month, str(expiry.month))
        exp_str = expiry.strftime('%y') + m_char + expiry.strftime('%d')  # e.g. 26413

    return f"{prefix}{exp_str}{strike}{opt_type}"


def atm_strike(instrument: str, underlying_price: float) -> int:
    """Round underlying price to nearest ATM strike for the instrument."""
    gap = config.INSTRUMENTS[instrument]['strike_gap']
    return int(round(underlying_price / gap) * gap)


# ─── Market Data ─────────────────────────────────────────────────────────────

def get_ltp(fyers, symbol: str, retries: int = 2) -> float | None:
    """Fetch Last Traded Price for a symbol.

    Retries transient rate-limit errors (Fyers 429 'request limit reached'):
    all three bots share one API key, and simultaneous evaluations at window
    boundaries (09:40) can burst past the per-second quota. A one-shot failure
    killed the Jul 13 BNF entry. 0.4s backoff clears a per-second window.
    Per-symbol errors (invalid symbol → errmsg in quote body) are NOT retried.
    """
    import time as _time
    for attempt in range(retries + 1):
        try:
            resp = fyers.quotes({"symbols": symbol})
            if resp.get('s') == 'ok':
                v = resp['d'][0]['v']
                # BSE options use 'ltp' instead of 'lp' (NSE/NFO standard)
                for key in ('lp', 'ltp', 'last_price'):
                    if v.get(key) is not None:
                        return float(v[key])
                # Valid transport, no price key → per-symbol error (e.g. bad
                # contract). Retrying cannot help.
                logger.warning(f"No LTP key in {symbol} quote. Available: {list(v.keys())}")
                return None
            # Transport-level error: retry only rate-limit style failures
            _msg = str(resp.get('message', '')).lower()
            if attempt < retries and (resp.get('code') == 429 or 'limit' in _msg):
                _time.sleep(0.4)
                continue
            logger.warning(f"LTP fetch failed for {symbol}: {resp}")
            return None
        except Exception as e:
            if attempt < retries:
                _time.sleep(0.4)
                continue
            logger.error(f"get_ltp({symbol}): {e}")
            return None
    return None


# ─── Order Placement ─────────────────────────────────────────────────────────

def _round_to_tick(price: float, tick: float = 0.05) -> float:
    """Round price to nearest option tick (₹0.05 on NSE/BSE)."""
    return round(round(price / tick) * tick, 2)


def place_sl_m_order(fyers, symbol: str, qty: int,
                     trigger_price: float, tag: str = 'botsl') -> str | None:
    """
    Place a Sell SL-M (Stop-Loss Market, type=4) order at the exchange level.

    The exchange will execute this SELL order the moment the option's LTP
    touches or falls below trigger_price — independently of the bot's polling
    loop.  This is the primary stop-loss defence against a stale Fyers option
    feed (which prevents the polling-loop stop from firing on time).

    trigger_price is rounded to the nearest ₹0.05 tick.
    If placement fails the trade proceeds normally — the polling-loop stop acts
    as fallback.

    Fyers order types: 1=Limit, 2=Market, 3=SL-Limit, 4=SL-M
    """
    tick      = 0.05
    trigger   = max(_round_to_tick(trigger_price, tick), tick)

    order_data = {
        "symbol"       : symbol,
        "qty"          : qty,
        "type"         : 4,           # SL-M
        "side"         : -1,          # SELL
        "productType"  : "INTRADAY",
        "limitPrice"   : tick,        # Fyers v3 rejects limitPrice=0 even for SL-M ("Must be >= 0.0025")
                                      # tick (₹0.05) satisfies validation; exchange ignores it for SL-M
        "stopPrice"    : trigger,
        "validity"     : "DAY",
        "disclosedQty" : 0,
        "offlineOrder" : False,
        "orderTag"     : tag,
    }
    try:
        resp = fyers.place_order(order_data)
        if resp.get('s') == 'ok':
            order_id = resp['id']
            logger.info(
                f"[SL-M] Exchange stop placed: {symbol} "
                f"trigger=₹{trigger:.2f} qty={qty} id={order_id}"
            )
            return order_id
        else:
            logger.error(f"[SL-M] Rejected: {symbol} trigger={trigger} → {resp}")
    except Exception as e:
        logger.error(f"place_sl_m_order({symbol}): {e}")
    return None


def cancel_order(fyers, order_id: str) -> bool:
    """
    Cancel a pending order (e.g. SL-M after target/trail exit).
    Returns True if cancelled or already done (filled/cancelled).
    """
    try:
        resp = fyers.cancel_order({"id": order_id})
        if resp.get('s') == 'ok':
            logger.info(f"[CANCEL] Order {order_id} cancelled successfully")
            return True
        code = resp.get('code', 0)
        # 1601/1602 = already executed or cancelled — treat as success
        if code in (1601, 1602, 400, -99):
            logger.info(f"[CANCEL] Order {order_id} already done/cancelled (code={code})")
            return True
        logger.warning(f"[CANCEL] Failed for {order_id}: {resp}")
    except Exception as e:
        logger.error(f"cancel_order({order_id}): {e}")
    return False


def check_sl_order_filled(fyers, sl_order_id: str) -> tuple:
    """
    Check whether a SL-M order was triggered and filled by the exchange.

    Returns:
        (True,  fill_price)  — SL was executed by exchange
        (False, 0.0)         — still pending, cancelled, or unknown
    """
    try:
        resp = fyers.orderbook()
        if resp.get('s') == 'ok':
            for order in resp.get('orderBook', []):
                if order.get('id') == sl_order_id:
                    status = order.get('status')
                    if status == 2:                          # Filled
                        fill_px = float(order.get('tradedPrice', 0))
                        logger.info(
                            f"[SL-M] Exchange-triggered stop confirmed: "
                            f"id={sl_order_id} fill=₹{fill_px:.2f}"
                        )
                        return True, fill_px
                    elif status in (5, 6):                   # Cancelled/Rejected
                        return False, 0.0
    except Exception as e:
        logger.error(f"check_sl_order_filled({sl_order_id}): {e}")
    return False, 0.0


def _place_order(fyers, symbol: str, qty: int, side: int,
                 tag: str = 'bot') -> str | None:
    """
    Place a market order.
      side: 1 = BUY, -1 = SELL
    Returns Fyers order ID or None on failure.

    qty = number of shares (1 lot = lot_size shares).
    e.g. NIFTY 1 lot = qty 25, BANKNIFTY 1 lot = qty 15.
    !! Verify with Fyers support that qty is in shares not lots !!
    """
    order_data = {
        "symbol"       : symbol,
        "qty"          : qty,
        "type"         : 2,           # 2 = Market order
        "side"         : side,
        "productType"  : "INTRADAY",   # NFO (options) segment only supports INTRADAY, not CNC.
                                      # CNC is equity-only — using CNC on NFO → ORA:-99 rejection.
                                      # INTRADAY margin for BUYING options = premium only (not SPAN).
                                      # Yesterday's margin shortfall was a separate account-balance issue.
        "limitPrice"   : 0,
        "stopPrice"    : 0,
        "validity"     : "DAY",
        "disclosedQty" : 0,
        "offlineOrder" : False,
        "orderTag"     : tag,
    }
    try:
        resp = fyers.place_order(order_data)
        if resp.get('s') == 'ok':
            order_id = resp['id']
            logger.info(f"Order placed: {symbol} side={side} qty={qty} id={order_id}")
            return order_id
        else:
            logger.error(f"Order rejected: {symbol} → {resp}")
    except Exception as e:
        logger.error(f"place_order({symbol}): {e}")
    return None


def place_buy_order(fyers, symbol: str, lot_size: int) -> str | None:
    """Buy 1 lot of an option. Returns order ID."""
    return _place_order(fyers, symbol, lot_size, side=1, tag='botentry')


def place_sell_order(fyers, symbol: str, lot_size: int) -> str | None:
    """Sell 1 lot of an option. Returns order ID."""
    return _place_order(fyers, symbol, lot_size, side=-1, tag='botexit')


def get_order_fill_price(fyers, order_id: str,
                         retries: int = 6, wait: float = 2.0) -> float | None:
    """
    Poll order status until filled and return average fill price.
    Retries every `wait` seconds up to `retries` times.
    """
    for attempt in range(retries):
        try:
            resp = fyers.orderbook()
            if resp.get('s') != 'ok':
                time.sleep(wait)
                continue
            for order in resp.get('orderBook', []):
                if order.get('id') == order_id:
                    status = order.get('status')
                    # Fyers status 2 = Filled
                    if status == 2:
                        fill_price = float(order.get('tradedPrice', 0))
                        if fill_price > 0:
                            logger.info(f"Order {order_id} filled at ₹{fill_price:.2f}")
                            return fill_price
                    elif status in (5, 6):   # 5=Cancelled, 6=Rejected
                        logger.error(f"Order {order_id} not filled: status={status}")
                        return None
        except Exception as e:
            logger.error(f"get_order_fill_price({order_id}): {e}")
        time.sleep(wait)

    logger.warning(f"Order {order_id}: fill price not confirmed after {retries} retries")
    return None


# ─── Convenience wrapper ─────────────────────────────────────────────────────

def get_available_funds(fyers) -> float | None:
    """Return available trading balance from Fyers funds API, or None on failure.

    Reinstated Jul 8 2026 (v1.6) — the original Jun 10 funds check lived only
    on EC2 and was destroyed by deploy drift. Pre-checking funds avoids broker
    margin rejections (7 occurred before the original gate, including a missed
    Jun 5 BNF +₹13k winner whose retry window had closed).
    """
    try:
        resp = fyers.funds()
        if resp.get('s') != 'ok':
            logger.warning(f"[FUNDS] API response not ok: {resp}")
            return None
        for row in resp.get('fund_limit', []):
            title = str(row.get('title', '')).lower()
            if 'available balance' in title:
                return float(row.get('equityAmount', 0.0))
        logger.warning("[FUNDS] 'Available Balance' row not found in fund_limit")
    except Exception as e:
        logger.error(f"get_available_funds: {e}")
    return None


def enter_live_position(fyers, instrument: str,
                        signal: dict,
                        stop_pct: float = 0.25,
                        lot_size: int = None) -> dict | None:
    """
    Full entry flow for a live trade:
      1. Find nearest expiry
      2. Build option symbol
      3. Get LTP
      4. Place BUY order (lot_size shares — pass eff_lot_size for 2-lot trades)
      5. Confirm fill price
      6. Place SL-M exchange stop at fill_price × (1 - stop_pct)

    stop_pct: fractional stop distance (e.g. 0.25 = 25%, 0.50 = 50% for Path A).
              Caller passes config.PATH_A_STOP or config.STOP_LOSS.
    lot_size: number of shares to order. If None, defaults to instrument base lot
              (e.g. 30 for BANKNIFTY). Pass eff_lot_size from enter_trade() for
              strength≥2 double-lot entries (60 for BANKNIFTY).

    Returns position dict with sl_order_id (None if SL-M placement failed —
    the bot's polling-loop stop acts as fallback in that case).
    """
    inst = config.INSTRUMENTS[instrument]
    if lot_size is None:
        lot_size = inst['lot_size']   # fallback to base lot (1× = 30 for BNF, 65 for NF)
    # Use pre-computed strike if options_bot already applied an OTM offset;
    # otherwise fall back to ATM (backward-compatible with callers that omit it).
    strike    = signal.get('strike') or atm_strike(instrument, signal['price'])
    expiry    = get_next_expiry(instrument)
    symbol    = build_option_symbol(instrument, strike, signal['type'], expiry)

    # Single quotes call captures LTP + best bid/ask (entry-quality logging);
    # falls back to the retrying get_ltp if depth keys are absent.
    ltp = None
    entry_bid = entry_ask = None
    try:
        _resp = fyers.quotes({"symbols": symbol})
        if _resp.get('s') == 'ok':
            _v = _resp['d'][0]['v']
            for _k in ('lp', 'ltp', 'last_price'):
                if _v.get(_k) is not None:
                    ltp = float(_v[_k])
                    break
            def _fnum(x):
                try:
                    _f = float(x)
                    return _f if _f > 0 else None
                except (TypeError, ValueError):
                    return None
            entry_bid = _fnum(_v.get('bid'))
            entry_ask = _fnum(_v.get('ask'))
    except Exception as _e:
        logger.warning(f"quote-with-depth failed for {symbol}: {_e}")
    if ltp is None:
        ltp = get_ltp(fyers, symbol)   # retrying fallback (handles 429s)
    if ltp is None:
        logger.error(f"Cannot get LTP for {symbol}, skipping entry")
        return None
    spread_pct = (round((entry_ask - entry_bid) / ltp * 100, 3)
                  if (entry_bid and entry_ask and entry_ask >= entry_bid) else None)

    # ── Pre-order funds check (reinstated v1.6) ──────────────────────────────
    # Skip when order cost exceeds 98% of the live available balance — a broker
    # margin rejection burns the entry moment; better to know before placing.
    # Fail-open: if the funds API errors, proceed (broker rejects if truly short).
    _cost  = ltp * lot_size
    _avail = get_available_funds(fyers)
    if _avail is not None and _cost > _avail * 0.98:
        logger.error(
            f"[FUNDS] {symbol}: cost ₹{_cost:,.0f} > available "
            f"₹{_avail:,.0f} ×0.98 — skipping entry (insufficient funds)"
        )
        return None

    logger.info(f"Entering {instrument} {signal['type']} | {symbol} | LTP=₹{ltp:.2f}")
    order_id = place_buy_order(fyers, symbol, lot_size)
    if not order_id:
        return None

    fill_price = get_order_fill_price(fyers, order_id) or ltp  # fallback to LTP

    # ── Exchange SL-M stop (protects against stale option feed) ──────────────
    sl_trigger   = fill_price * (1.0 - stop_pct)
    sl_order_id  = place_sl_m_order(fyers, symbol, lot_size, sl_trigger)
    if sl_order_id is None:
        logger.warning(
            f"[SL-M] Placement failed for {symbol} — "
            f"polling-loop stop ({stop_pct*100:.0f}%) is the only protection."
        )

    return {
        'instrument'    : instrument,
        'type'          : signal['type'],
        'option_symbol' : symbol,
        'strike'        : strike,
        'expiry'        : expiry,
        'lot_size'      : lot_size,
        'entry_price'   : fill_price,
        'entry_order_id': order_id,
        'sl_order_id'   : sl_order_id,    # None if SL-M placement failed
        'sl_trigger'    : sl_trigger,     # for logging/audit
        # v1.7.3 entry-quality: quote snapshot at order time
        'entry_ltp'     : ltp,            # LTP the order was sent against
        'spread_pct'    : spread_pct,     # bid-ask spread % of premium (None if depth absent)
    }


def exit_live_position(fyers, pos: dict) -> float | None:
    """
    Full exit flow for a live trade.

    Returns:
      float  — actual fill price (exit succeeded)
      None   — exit order failed; caller should retry next bar

    Pre-exit verification: queries Fyers positions before placing the sell
    order. If netQty == 0 the position is already closed (e.g. auto-squareoff
    or closed by another process). In that case we return the LTP as a proxy
    exit price so the caller can book P&L and clear the position cleanly,
    without attempting a sell that Fyers would treat as a new naked short
    (which triggers a margin-shortfall rejection).
    """
    symbol   = pos['option_symbol']
    lot_size = pos['lot_size']
    inst     = pos.get('instrument', '?')

    # ── Pre-exit: verify position still open on Fyers ────────────────────────
    try:
        resp = fyers.positions()
        net_qty = 0
        ltp_fyers = None
        for p in (resp.get('netPositions') or []):
            if p.get('symbol') == symbol:
                net_qty   = int(p.get('netQty', 0))
                ltp_fyers = p.get('ltp')
                break
        if net_qty == 0:
            proxy = float(ltp_fyers) if ltp_fyers else 0.0
            logger.warning(
                f"[EXIT-SKIP] {inst} {pos.get('type','?')} {symbol} — "
                f"netQty=0 on Fyers (already closed). "
                f"Using LTP ₹{proxy:.2f} as proxy exit price."
            )
            return proxy if proxy > 0 else None
    except Exception as e:
        logger.warning(f"[EXIT-WARN] Pre-exit position check failed: {e} — proceeding anyway")

    # ── Cancel pending SL-M before placing market sell ────────────────────────
    # Without cancelling, a target/trail exit would sell the position, then the
    # SL-M would trigger a second SELL — creating an unintended naked short.
    sl_order_id = pos.get('sl_order_id')
    if sl_order_id:
        cancel_order(fyers, sl_order_id)

    logger.info(f"Exiting {inst} {pos.get('type','?')} | {symbol}")
    order_id = place_sell_order(fyers, symbol, lot_size)
    if not order_id:
        return None

    return get_order_fill_price(fyers, order_id)
