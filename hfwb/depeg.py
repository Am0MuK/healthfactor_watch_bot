import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

STABLES: dict[str, str] = {
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "USDS": "0xdC035D45d973E3EC169d2276DDab16f1e407384F",
    "GHO": "0x40D16FC0246aD3160Ccc09B8D0D3A2cD28aE6C2f",
    "USDe": "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3",
    "PYUSD": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
    "RLUSD": "0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD",
    "USDG": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
    "frxUSD": "0xCAcd6fd266aF91b8AeD52aCCc382b4e165586E29",
    "crvUSD": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
    "FDUSD": "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409",
}

DEPEG_LEVELS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
RECOVER_BELOW = Decimal("0.003")
# DefiLlama refreshes prices in steps of about 5 minutes and several tokens are normally 11 to 15 minutes old,
# so anything fresher than 20 minutes is a usable data point.
MAX_PRICE_AGE_S = 1200.0
ORACLE_NOT_INDEPENDENT = {"GHO", "USDe"}
AAVE_ORACLE_ADDRESS = "0x54586bE62E3c3580375aE3723C145253060Ca0C2"


class PriceError(Exception):
    """Raised when fetching prices fails or returns an invalid body."""


@dataclass(frozen=True)
class PriceReading:
    symbol: str
    price: Decimal
    confidence: float
    timestamp: int


def fetch_prices(
    client: httpx.Client | None = None,
    now: Callable[[], float] | float = time.time,
) -> dict[str, PriceReading]:
    """Fetch current prices for supported stablecoins from DefiLlama.

    Raises PriceError on HTTP != 200, non-JSON, non-object body, missing/non-dict coins,
    or an entirely empty coins dict. Silently drops individual invalid, low confidence,
    or stale entries.
    """
    now_ts = now() if callable(now) else float(now)
    query_tokens = [f"ethereum:{addr}" for addr in STABLES.values()]
    url = f"https://coins.llama.fi/prices/current/{','.join(query_tokens)}"

    own_client = client is None
    active_client = client if client is not None else httpx.Client(timeout=10.0)

    try:
        resp = active_client.get(url)
    except httpx.HTTPError as exc:
        raise PriceError(f"Network error fetching prices: {exc}") from exc
    finally:
        if own_client:
            active_client.close()

    if resp.status_code != 200:
        raise PriceError(f"DefiLlama returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except Exception as exc:
        raise PriceError(f"Invalid JSON response from DefiLlama: {exc}") from exc

    if not isinstance(body, dict):
        raise PriceError("Response body is not a JSON object")

    if "coins" not in body or not isinstance(body["coins"], dict):
        raise PriceError("Response body missing 'coins' object")

    coins_data: dict[str, Any] = body["coins"]
    if not coins_data:
        raise PriceError("DefiLlama returned empty coins dictionary")

    readings: dict[str, PriceReading] = {}
    for symbol, addr in STABLES.items():
        key = f"ethereum:{addr}"
        entry = coins_data.get(key)
        if not isinstance(entry, dict):
            continue

        price_raw = entry.get("price")
        if price_raw is None or isinstance(price_raw, bool):
            continue
        try:
            price_dec = Decimal(str(price_raw))
        except (InvalidOperation, TypeError, ValueError):
            continue

        if not price_dec.is_finite() or price_dec <= 0:
            continue

        conf_raw = entry.get("confidence")
        try:
            conf_val = float(conf_raw)
        except (TypeError, ValueError):
            continue

        if not math.isfinite(conf_val) or conf_val < 0.9 or conf_val > 1.0:
            continue

        ts_raw = entry.get("timestamp")
        try:
            ts_val = int(ts_raw)
        except (TypeError, ValueError, OverflowError):
            continue

        age = now_ts - ts_val
        if age > MAX_PRICE_AGE_S or age < -300.0:  # stale, or too far in the future to trust
            continue

        readings[symbol] = PriceReading(
            symbol=symbol,
            price=price_dec,
            confidence=conf_val,
            timestamp=ts_val,
        )

    return readings


def deviation(price: Decimal) -> Decimal:
    """Calculate absolute deviation from 1.00 USD."""
    return abs(price - Decimal(1))


def level_for(dev: Decimal) -> int:
    """Depeg level index for a deviation: 0 = within the first threshold, 1..4 = D1..D4."""
    level = 0
    for i, threshold in enumerate(DEPEG_LEVELS, start=1):
        if dev >= threshold:
            level = i
    return level


def depeg_step(
    prev_state: str,
    dev: Decimal,
    now_ts: float,
    last_alert_ts: float | None,
    confirmed: bool,
    escalate_dev: Decimal | None = None,
) -> tuple[str, str | None]:
    """Advance the stablecoin depeg state machine.

    States: ok, D1, D2, D3, D4.
    Alerts: D1..D4, repeat_D3, repeat_D4, recovery.
    """
    # Immediate recovery to ok if dev < RECOVER_BELOW (0.3%)
    if dev < RECOVER_BELOW:
        if prev_state != "ok":
            return "ok", "recovery"
        return "ok", None

    # Escalation uses the confirmed deviation (the lower of the last two readings); recovery and
    # step-down use the current one.
    cand = level_for(dev if escalate_dev is None else escalate_dev)

    prev_level = 0 if prev_state == "ok" else int(prev_state[1:])

    # Escalation: candidate higher than previous level
    if cand > prev_level and confirmed:
        new_state = f"D{cand}"
        return new_state, new_state
    # Not confirmed yet: stays in prev_state. Falls through to check repeats.

    # Calculate effective level considering hysteresis for step-down
    if prev_level > 0:
        if prev_level == 4:
            if dev >= DEPEG_LEVELS[3] * Decimal("0.8"):  # >= 0.040
                effective_level = 4
            elif dev >= DEPEG_LEVELS[2] * Decimal("0.8"):  # >= 0.016
                effective_level = 3
            elif dev >= DEPEG_LEVELS[1] * Decimal("0.8"):  # >= 0.008
                effective_level = 2
            elif dev >= RECOVER_BELOW:  # >= 0.003
                effective_level = 1
            else:
                return "ok", "recovery"
        elif prev_level == 3:
            if dev >= DEPEG_LEVELS[2] * Decimal("0.8"):  # >= 0.016
                effective_level = 3
            elif dev >= DEPEG_LEVELS[1] * Decimal("0.8"):  # >= 0.008
                effective_level = 2
            elif dev >= RECOVER_BELOW:  # >= 0.003
                effective_level = 1
            else:
                return "ok", "recovery"
        elif prev_level == 2:
            if dev >= DEPEG_LEVELS[1] * Decimal("0.8"):  # >= 0.008
                effective_level = 2
            elif dev >= RECOVER_BELOW:  # >= 0.003
                effective_level = 1
            else:
                return "ok", "recovery"
        elif prev_level == 1:
            if dev >= RECOVER_BELOW:  # >= 0.003
                effective_level = 1
            else:
                return "ok", "recovery"
        else:
            effective_level = 0
    else:
        effective_level = 0

    new_state = "ok" if effective_level == 0 else f"D{effective_level}"

    # Step-down is immediate and sends no alert
    if effective_level < prev_level:
        return new_state, None

    # Repeat reminder every 60 min while in D3 or D4
    if effective_level in (3, 4) and (last_alert_ts is None or (now_ts - last_alert_ts) >= 3600.0):
        return new_state, f"repeat_{new_state}"

    return new_state, None


class DepegTracker:
    """In-memory tracker of the previous data point per symbol.

    The price source updates each token about every 5 minutes, so polling every minute returns the same
    data point several times. Only a NEW data point (a different `timestamp`) counts as another observation.
    An escalation is trusted only when the previous data point was bad too: the confirmed deviation is the
    LOWER of the last two data points. A fast collapse (1% -> 3% -> 7% -> 10%) therefore alerts one data
    point behind the price instead of waiting for it to settle on one level, and a single glitchy data
    point never alerts.
    """

    def __init__(self) -> None:
        # symbol -> (timestamp of the latest data point, its deviation, previous deviation or None, last answer)
        self._seen: dict[str, tuple[int, Decimal, Decimal | None, tuple[Decimal, bool]]] = {}

    def observe(self, symbol: str, dev: Decimal, timestamp: int) -> tuple[Decimal, bool]:
        """Record a reading. Returns (escalation_dev, confirmed); confirmed is False until a second data point."""
        seen = self._seen.get(symbol)
        if seen is not None and seen[0] == timestamp:
            return seen[3]  # same data point polled again: same answer, history unchanged
        prev_dev = seen[1] if seen is not None else None
        answer = (dev, False) if prev_dev is None else (min(dev, prev_dev), True)
        self._seen[symbol] = (timestamp, dev, prev_dev, answer)
        return answer

    def forget(self, symbol: str) -> None:
        """Forget a symbol (its reading was unavailable): the next reading counts as a first one."""
        self._seen.pop(symbol, None)

    def reset(self) -> None:
        """Forget everything (price source outage)."""
        self._seen.clear()


def oracle_price(
    symbol: str,
    rpc_urls: Sequence[str],
    client: httpx.Client | None = None,
) -> Decimal | None:
    """Fetch best-effort 8-decimal asset price from Ethereum Aave Oracle.

    Never raises; returns None on any error, revert, short result, or non-hex string.
    """
    if symbol not in STABLES or not rpc_urls:
        return None

    token_addr = STABLES[symbol]
    padded_addr = token_addr.lower().removeprefix("0x").rjust(64, "0")
    call_data = f"0xb3596f07{padded_addr}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": AAVE_ORACLE_ADDRESS,
                "data": call_data,
            },
            "latest",
        ],
    }

    own_client = client is None
    active_client = client if client is not None else httpx.Client(timeout=10.0)

    try:
        for rpc in rpc_urls:
            try:
                resp = active_client.post(rpc, json=payload)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if "result" not in data:
                    continue
                result_hex = data["result"]
                if not isinstance(result_hex, str) or not result_hex.startswith("0x"):
                    continue
                hex_str = result_hex.removeprefix("0x")
                if len(hex_str) != 64:
                    continue
                try:
                    int_val = int(hex_str, 16)
                except ValueError:
                    continue
                return Decimal(int_val) / Decimal(100000000)
            except Exception:  # noqa: BLE001, S112
                continue
        return None
    finally:
        if own_client:
            active_client.close()
