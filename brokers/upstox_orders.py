"""
Upstox order execution for index futures.
"""
from __future__ import annotations
import os
import logging
import requests

from brokers.upstox_auth import get_access_token

log = logging.getLogger(__name__)
BASE_URL = "https://api.upstox.com/v2"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def place_order(
    instrument_key: str,
    transaction_type: str,   # "BUY" | "SELL"
    quantity: int,
    order_type: str = "MARKET",
    price: float = 0.0,
    product: str = "D",      # D=delivery(NRML for futures)
    paper: bool = False,
) -> dict:
    if paper:
        log.info("[ORDER:PAPER] %s %s qty=%d", transaction_type, instrument_key, quantity)
        return {"status": "paper", "instrument": instrument_key, "qty": quantity}

    payload = {
        "quantity": quantity,
        "product": product,
        "validity": "DAY",
        "price": price,
        "instrument_token": instrument_key,
        "order_type": order_type,
        "transaction_type": transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    resp = requests.post(f"{BASE_URL}/order/place", headers=_headers(), json=payload)
    resp.raise_for_status()
    result = resp.json()
    log.info("[ORDER] Placed: %s %s qty=%d → order_id=%s", transaction_type, instrument_key, quantity, result.get("data", {}).get("order_id"))
    return result


def get_ltp(instrument_key: str) -> float:
    """Fetch last traded price for a futures instrument."""
    resp = requests.get(
        f"{BASE_URL}/market-quote/ltp",
        headers=_headers(),
        params={"instrument_key": instrument_key},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    key = list(data.keys())[0]
    return float(data[key]["last_price"])


def get_positions() -> list[dict]:
    resp = requests.get(f"{BASE_URL}/portfolio/short-term-positions", headers=_headers())
    resp.raise_for_status()
    return resp.json().get("data", [])
