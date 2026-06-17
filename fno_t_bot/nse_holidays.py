"""
NSE Market Holiday Calendar and Market Hours Utilities

IMPORTANT: Verify 2026 dates at https://www.nseindia.com/resources/exchange-communication-holidays
"""

from datetime import date, datetime, timedelta
import pytz

IST = pytz.timezone('Asia/Kolkata')

# ─── NSE Trading Holidays 2025 (Official) ─────────────────────────────────────
NSE_HOLIDAYS_2025 = [
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Maha Shivratri
    date(2025, 3, 14),   # Holi
    date(2025, 4, 10),   # Shri Ram Navami
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 20),  # Diwali - Laxmi Puja (Muhurat Trading only)
    date(2025, 10, 21),  # Diwali - Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25),  # Christmas Day
]

# ─── NSE Trading Holidays 2026 (Verify before use!) ───────────────────────────
# Source: NSE circular. Update this list from nseindia.com each year.
# !! Dates marked (approx) must be verified at nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS_2026 = [
    date(2026, 1, 26),   # Republic Day (Monday) ✓
    date(2026, 3,  3),   # Holi ✓ (confirmed — market was closed)
    date(2026, 4,  3),   # Good Friday ✓ (Easter Sunday = Apr 5, 2026)
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti ✓
    date(2026, 5,  1),   # Maharashtra Day ✓
    date(2026, 10, 2),   # Gandhi Jayanti ✓
    date(2026, 10, 9),   # Dussehra (approx — verify at NSE)
    date(2026, 11, 9),   # Diwali (approx — verify at NSE)
    date(2026, 12, 25),  # Christmas Day ✓
]

ALL_HOLIDAYS = set(NSE_HOLIDAYS_2025 + NSE_HOLIDAYS_2026)


def is_nse_holiday(check_date=None):
    """Returns True if the given date is an NSE trading holiday."""
    if check_date is None:
        check_date = datetime.now(IST).date()
    return check_date in ALL_HOLIDAYS


def is_weekend(check_date=None):
    """Returns True if Saturday or Sunday."""
    if check_date is None:
        check_date = datetime.now(IST).date()
    return check_date.weekday() >= 5  # 5=Sat, 6=Sun


def is_market_open_today(check_date=None):
    """Returns True if NSE is open for trading today."""
    if check_date is None:
        check_date = datetime.now(IST).date()
    return not is_weekend(check_date) and not is_nse_holiday(check_date)


def is_within_market_hours(dt=None):
    """Returns True if current time is within NSE market hours (9:15–15:30 IST)."""
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = IST.localize(dt)
    market_open  = dt.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= dt <= market_close


def is_valid_entry_window(dt=None):
    """
    Returns True if within the valid entry window:
      - After first 15 min (skip 9:15–9:30 opening volatility)
      - Before last 30 min (skip 15:00–15:30 EOD volatility)
    """
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = IST.localize(dt)
    entry_start = dt.replace(hour=9,  minute=30, second=0, microsecond=0)
    entry_end   = dt.replace(hour=15, minute=0,  second=0, microsecond=0)
    return entry_start <= dt <= entry_end


def get_next_trading_day(from_date=None):
    """Returns the next NSE trading day after from_date."""
    if from_date is None:
        from_date = datetime.now(IST).date()
    check = from_date + timedelta(days=1)
    while not is_market_open_today(check):
        check += timedelta(days=1)
    return check


def market_status():
    """Returns a human-readable string describing current market status."""
    now   = datetime.now(IST)
    today = now.date()

    if is_weekend(today):
        next_open = get_next_trading_day(today)
        return f"CLOSED — Weekend ({today.strftime('%A')}). Next open: {next_open}"

    if is_nse_holiday(today):
        next_open = get_next_trading_day(today)
        return f"CLOSED — NSE Holiday. Next open: {next_open}"

    if not is_within_market_hours(now):
        if now.hour < 9 or (now.hour == 9 and now.minute < 15):
            return "PRE-MARKET — Opens at 09:15 IST"
        return "CLOSED — Market closed at 15:30 IST"

    if not is_valid_entry_window(now):
        if now.hour == 9 and now.minute < 30:
            return "OPEN — Skipping opening 15 min (9:15–9:30)"
        return "OPEN — Skipping last 30 min (15:00–15:30)"

    return "OPEN — Valid entry window"


if __name__ == "__main__":
    print(f"Today: {datetime.now(IST).date()}")
    print(f"Status: {market_status()}")
    print(f"Market open today: {is_market_open_today()}")
    print(f"Within market hours: {is_within_market_hours()}")
    print(f"Valid entry window: {is_valid_entry_window()}")
    print(f"Next trading day: {get_next_trading_day()}")
