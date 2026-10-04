import threading
import time
from decimal import Decimal

from hfwb.aave import AccountData, ReadError
from hfwb.markets import Market
from hfwb.scan import ScanResult, scan_address

M1 = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("https://arb1.test/rpc",),
    best_effort=False,
)

M2 = Market(
    protocol="aave_v3",
    chain="Optimism",
    chain_id=10,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("https://opt1.test/rpc",),
    best_effort=False,
)

M3 = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("https://eth1.test/rpc",),
    best_effort=True,
)


def test_scan_finds_positions():
    def mock_reader(market: Market, address: str) -> AccountData:
        if market.chain == "Arbitrum":
            return AccountData(
                collateral_usd=Decimal(1000),
                debt_usd=Decimal(500),
                hf=Decimal("1.5"),
                no_debt=False,
                has_position=True,
            )
        elif market.chain == "Optimism":
            # No position
            return AccountData(
                collateral_usd=Decimal(0),
                debt_usd=Decimal(0),
                hf=None,
                no_debt=True,
                has_position=False,
            )
        else:  # Ethereum
            return AccountData(
                collateral_usd=None,
                debt_usd=None,
                hf=Decimal("1.2"),
                no_debt=False,
                has_position=True,
            )

    result = scan_address("0x000000000000000000000000000000000000dead", [M1, M2, M3], reader=mock_reader)
    assert isinstance(result, ScanResult)
    assert len(result.found) == 2
    assert result.found[0][0] == M1
    assert result.found[1][0] == M3
    assert len(result.failed) == 0


def test_scan_isolated_failure():
    def mock_reader(market: Market, address: str) -> AccountData:
        if market.chain == "Arbitrum":
            return AccountData(
                collateral_usd=Decimal(1000),
                debt_usd=Decimal(500),
                hf=Decimal("1.5"),
                no_debt=False,
                has_position=True,
            )
        elif market.chain == "Optimism":
            raise ReadError("RPC connection timeout")
        else:
            return AccountData(
                collateral_usd=None,
                debt_usd=None,
                hf=None,
                no_debt=True,
                has_position=False,
            )

    result = scan_address("0x000000000000000000000000000000000000dead", [M1, M2, M3], reader=mock_reader)
    assert len(result.found) == 1
    assert result.found[0][0] == M1
    assert len(result.failed) == 1
    assert result.failed[0] == M2


def test_scan_per_host_and_executor_cap():
    # 6 markets with same host, 2 markets with different host
    markets = []
    for i in range(6):
        markets.append(
            Market(
                protocol="aave_v3",
                chain=f"ChainShared{i}",
                chain_id=100 + i,
                name="Market",
                address=f"0x{'0'*39}{i}",
                rpcs=("https://shared-host.test/rpc",),
            )
        )
    for i in range(2):
        markets.append(
            Market(
                protocol="aave_v3",
                chain=f"ChainOther{i}",
                chain_id=200 + i,
                name="Market",
                address=f"0x{'1'*39}{i}",
                rpcs=(f"https://other-host-{i}.test/rpc",),
            )
        )

    lock = threading.Lock()
    current_shared_host = 0
    max_shared_host = 0
    current_total = 0
    max_total = 0

    def reader(market: Market, address: str) -> AccountData:
        nonlocal current_shared_host, max_shared_host, current_total, max_total
        with lock:
            current_total += 1
            max_total = max(max_total, current_total)
            if "shared-host.test" in market.rpcs[0]:
                current_shared_host += 1
                max_shared_host = max(max_shared_host, current_shared_host)

        time.sleep(0.02)

        with lock:
            current_total -= 1
            if "shared-host.test" in market.rpcs[0]:
                current_shared_host -= 1

        return AccountData(None, None, None, True, False)

    result = scan_address(
        "0x000000000000000000000000000000000000dead",
        markets,
        reader=reader,
        executor_cap=8,
        per_host_cap=3,
    )
    assert len(result.failed) == 0
    assert max_total <= 8
    assert max_shared_host <= 3
