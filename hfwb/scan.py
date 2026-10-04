import threading
import urllib.parse
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from hfwb.aave import AccountData, normalize_address, read_market
from hfwb.markets import Market


@dataclass(frozen=True)
class ScanResult:
    found: list[tuple[Market, AccountData]]
    failed: list[Market]


def _extract_host(market: Market) -> str:
    """Extract primary RPC host from market for rate limiting."""
    if not market.rpcs:
        return "unknown"
    parsed = urllib.parse.urlparse(market.rpcs[0])
    return parsed.netloc or parsed.path or "unknown"


def scan_address(
    address: str,
    markets: Sequence[Market],
    reader: Callable[..., AccountData] = read_market,
    executor_cap: int = 8,
    per_host_cap: int = 3,
) -> ScanResult:
    """Scan all markets concurrently for an address.

    Limits total concurrency to executor_cap and per-host concurrency to per_host_cap.
    One market failure does not affect other markets.
    """
    if not markets:
        return ScanResult(found=[], failed=[])

    norm_addr = normalize_address(address)

    # Semaphores per RPC host
    host_semaphores: dict[str, threading.Semaphore] = defaultdict(
        lambda: threading.Semaphore(per_host_cap)
    )

    def _check_market(m: Market) -> tuple[Market, AccountData | Exception]:
        host = _extract_host(m)
        with host_semaphores[host]:
            try:
                data = reader(m, norm_addr)
                return (m, data)
            except Exception as exc:  # noqa: BLE001 - one failing market must not affect others
                return (m, exc)

    results: dict[Market, AccountData | Exception] = {}
    max_workers = min(executor_cap, len(markets)) or 1

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_market = {executor.submit(_check_market, m): m for m in markets}
        for future in as_completed(future_to_market):
            m, outcome = future.result()
            results[m] = outcome

    found: list[tuple[Market, AccountData]] = []
    failed: list[Market] = []

    # Preserve original market ordering
    for m in markets:
        outcome = results.get(m)
        if isinstance(outcome, Exception):
            failed.append(m)
        elif isinstance(outcome, AccountData) and outcome.has_position:
            found.append((m, outcome))

    return ScanResult(found=found, failed=failed)
