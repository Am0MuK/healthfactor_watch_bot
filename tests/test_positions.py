"""Per-asset position read + tie-out guard (step 2 of the price-drop feature).

All numbers here are synthetic. The mock RPC routes eth_call by (to, selector, args).
"""
import json
from decimal import Decimal

import httpx
import pytest

from hfwb.aave import AccountData, ReadError, _keccak_256
from hfwb.markets import Market
from hfwb.positions import (
    AssetPosition,
    PositionBreakdown,
    read_positions,
    recompute,
    tie_out,
)

POOL = "0x1111111111111111111111111111111111111111"
PROVIDER = "0x2222222222222222222222222222222222222222"
DATA_PROVIDER = "0x3333333333333333333333333333333333333333"
ORACLE = "0x4444444444444444444444444444444444444444"
WETH = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
USDC = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WBTC = "0xcccccccccccccccccccccccccccccccccccccccc"
LINK = "0xdddddddddddddddddddddddddddddddddddddddd"
USER = "0x000000000000000000000000000000000000beef"

V3 = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address=POOL,
    rpcs=("http://rpc-a.test", "http://rpc-b.test"),
)
V4 = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address=POOL,
    rpcs=("http://rpc-a.test",),
    best_effort=True,
)


def sel(sig: str) -> str:
    return "0x" + _keccak_256(sig.encode()).hex()[:8]


def word(v: int) -> str:
    return format(v, "064x")


def addr_word(a: str) -> str:
    return a.lower()[2:].rjust(64, "0")


def encode_reserve_tokens(items: list[tuple[str, str]]) -> str:
    """ABI-encode (string symbol, address token)[] as returned by getAllReservesTokens()."""
    n = len(items)
    heads, tails = [], []
    offset = 32 * n
    for sym, a in items:
        heads.append(word(offset))
        raw = sym.encode()
        padded = raw.hex().ljust(((len(raw) + 31) // 32) * 64 or 64, "0") if raw else ""
        elem = word(64) + addr_word(a) + word(len(raw)) + padded
        tails.append(elem)
        offset += len(elem) // 2
    return "0x" + word(32) + word(n) + "".join(heads) + "".join(tails)


def user_reserve_words(a_token: int, stable: int, variable: int, collateral: bool) -> str:
    # currentATokenBalance, currentStableDebt, currentVariableDebt, principalStableDebt,
    # scaledVariableDebt, stableBorrowRate, liquidityRate, stableRateLastUpdated,
    # usageAsCollateralEnabled
    return "0x" + "".join(
        word(x) for x in (a_token, stable, variable, 0, variable, 0, 0, 0, int(collateral))
    )


def reserve_config_words(decimals: int, ltv: int, lt: int) -> str:
    # decimals, ltv, liquidationThreshold, liquidationBonus, reserveFactor,
    # usageAsCollateralEnabled, borrowingEnabled, stableBorrowRateEnabled, isActive, isFrozen
    return "0x" + "".join(
        word(x) for x in (decimals, ltv, lt, 10500, 1000, int(lt > 0), 1, 0, 1, 0)
    )


def user_config(entries: dict[int, tuple[bool, bool]]) -> int:
    """{reserve_id: (borrowing, collateral)} -> Aave UserConfigurationMap bitmap."""
    bitmap = 0
    for rid, (borrowing, collateral) in entries.items():
        if borrowing:
            bitmap |= 1 << (2 * rid)
        if collateral:
            bitmap |= 1 << (2 * rid + 1)
    return bitmap


class Chain:
    """Synthetic Aave V3 market state with a mock JSON-RPC endpoint."""

    def __init__(self):
        self.reserves = [WETH, USDC, WBTC, LINK]  # ids 0..3
        self.symbols = {WETH: "WETH", USDC: "USDC", WBTC: "WBTC", LINK: "LINK"}
        self.decimals = {WETH: 18, USDC: 6, WBTC: 8, LINK: 18}
        self.lt = {WETH: 8250, USDC: 7800, WBTC: 7800, LINK: 7100}
        self.prices = {WETH: 2000_00000000, USDC: 1_00000000, WBTC: 60000_00000000, LINK: 10_00000000}
        # user balances in raw token units: (aToken, stableDebt, variableDebt, collateral flag)
        self.user = {
            WETH: (2 * 10**18, 0, 0, True),  # 4,000 USD collateral
            USDC: (0, 0, 2000 * 10**6, False),  # 2,000 USD debt
        }
        self.emode = 0
        self.emode_lt: dict[int, int] = {}  # category -> eMode liquidation threshold (bps)
        self.emode_bitmap: dict[int, int] = {}  # category -> collateral bitmap by reserve id
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict[tuple[str, str], dict] = {}

    def config_bitmap(self) -> int:
        entries = {}
        for rid, a in enumerate(self.reserves):
            if a in self.user:
                at, st, var, col = self.user[a]
                entries[rid] = (st + var > 0, col and at > 0)
        return user_config(entries)

    def result(self, to: str, data: str) -> str:
        s, args = data[:10], data[10:]
        if to == POOL and s == sel("getUserConfiguration(address)"):
            return "0x" + word(self.config_bitmap())
        if to == POOL and s == sel("getUserEMode(address)"):
            return "0x" + word(self.emode)
        if to == POOL and s == sel("getEModeCategoryCollateralConfig(uint8)"):
            lt = self.emode_lt.get(int(args, 16), 0)
            return "0x" + word(max(0, lt - 200)) + word(lt) + word(10100 if lt else 0)
        if to == POOL and s == sel("getEModeCategoryCollateralBitmap(uint8)"):
            return "0x" + word(self.emode_bitmap.get(int(args, 16), 0))
        if to == POOL and s == sel("ADDRESSES_PROVIDER()"):
            return "0x" + addr_word(PROVIDER)
        if to == POOL and s == sel("getReserveAddressById(uint16)"):
            return "0x" + addr_word(self.reserves[int(args, 16)])
        if to == PROVIDER and s == sel("getPoolDataProvider()"):
            return "0x" + addr_word(DATA_PROVIDER)
        if to == PROVIDER and s == sel("getPriceOracle()"):
            return "0x" + addr_word(ORACLE)
        if to == DATA_PROVIDER and s == sel("getAllReservesTokens()"):
            return encode_reserve_tokens([(self.symbols[a], a) for a in self.reserves])
        if to == DATA_PROVIDER and s == sel("getUserReserveData(address,address)"):
            asset = "0x" + args[24:64]
            at, st, var, col = self.user.get(asset, (0, 0, 0, False))
            return user_reserve_words(at, st, var, col)
        if to == DATA_PROVIDER and s == sel("getReserveConfigurationData(address)"):
            asset = "0x" + args[24:64]
            return reserve_config_words(self.decimals[asset], max(0, self.lt[asset] - 500), self.lt[asset])
        if to == ORACLE and s == sel("getAssetPrice(address)"):
            asset = "0x" + args[24:64]
            return "0x" + word(self.prices[asset])
        raise AssertionError(f"unexpected call to={to} data={data[:10]}")

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["method"] == "eth_call"
            to = body["params"][0]["to"].lower()
            data = body["params"][0]["data"]
            self.calls.append((str(request.url), data[:10]))
            key = (str(request.url).rstrip("/"), data[:10])
            if key in self.overrides:
                return httpx.Response(200, json=self.overrides[key])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": self.result(to, data)})

        return httpx.Client(transport=httpx.MockTransport(handler))


def account(coll: str, debt: str, hf: str | None) -> AccountData:
    return AccountData(
        collateral_usd=Decimal(coll),
        debt_usd=Decimal(debt),
        hf=Decimal(hf) if hf is not None else None,
        no_debt=hf is None,
        has_position=True,
    )


# ---------------------------------------------------------------- read_positions


def test_read_positions_basic_breakdown():
    chain = Chain()
    b = read_positions(V3, USER, client=chain.client())
    assert isinstance(b, PositionBreakdown)
    assert b.emode_category == 0
    by_sym = {a.symbol: a for a in b.assets}
    assert set(by_sym) == {"WETH", "USDC"}
    weth = by_sym["WETH"]
    assert weth.asset == WETH
    assert weth.decimals == 18
    assert weth.collateral == Decimal(2)
    assert weth.debt == Decimal(0)
    assert weth.price_usd == Decimal(2000)
    assert weth.liq_threshold == Decimal("0.825")
    assert weth.used_as_collateral is True
    usdc = by_sym["USDC"]
    assert usdc.debt == Decimal(2000)
    assert usdc.used_as_collateral is False
    assert b.collateral_usd == Decimal(4000)
    assert b.weighted_collateral_usd == Decimal(3300)
    assert b.debt_usd == Decimal(2000)
    assert b.hf == Decimal("1.65")


def test_read_positions_only_reads_reserves_in_user_bitmap():
    chain = Chain()
    read_positions(V3, USER, client=chain.client())
    user_reads = [d for _, d in chain.calls if d == sel("getUserReserveData(address,address)")]
    assert len(user_reads) == 2  # WETH and USDC, not WBTC or LINK


def test_read_positions_stable_and_variable_debt_are_summed():
    chain = Chain()
    chain.user[USDC] = (0, 500 * 10**6, 1500 * 10**6, False)
    b = read_positions(V3, USER, client=chain.client())
    assert b.debt_usd == Decimal(2000)


def test_read_positions_collateral_disabled_is_not_counted():
    chain = Chain()
    chain.user[WBTC] = (10**8, 0, 0, False)  # supplied, but not used as collateral
    b = read_positions(V3, USER, client=chain.client())
    assert b.collateral_usd == Decimal(4000)
    assert "WBTC" not in {a.symbol for a in b.assets if a.used_as_collateral}


def test_read_positions_zero_liquidation_threshold_not_counted():
    # Aave skips collateral whose liquidation threshold is 0 in its HF calculation
    chain = Chain()
    chain.user[LINK] = (100 * 10**18, 0, 0, True)
    chain.lt[LINK] = 0
    b = read_positions(V3, USER, client=chain.client())
    assert b.weighted_collateral_usd == Decimal(3300)
    assert b.hf == Decimal("1.65")


def test_read_positions_same_asset_supplied_and_borrowed():
    chain = Chain()
    chain.user[WETH] = (2 * 10**18, 0, 5 * 10**17, True)
    b = read_positions(V3, USER, client=chain.client())
    weth = next(a for a in b.assets if a.symbol == "WETH")
    assert weth.collateral == Decimal(2)
    assert weth.debt == Decimal("0.5")
    assert b.debt_usd == Decimal(3000)


def test_read_positions_reports_emode_category():
    chain = Chain()
    chain.emode = 1
    b = read_positions(V3, USER, client=chain.client())
    assert b.emode_category == 1


def test_read_positions_no_position_returns_empty():
    chain = Chain()
    chain.user = {}
    b = read_positions(V3, USER, client=chain.client())
    assert b.assets == ()
    assert b.debt_usd == Decimal(0)
    assert b.hf is None


def test_read_positions_unknown_symbol_falls_back_to_short_address():
    chain = Chain()
    del chain.symbols[USDC]
    chain_reserves_tokens = [a for a in chain.reserves if a != USDC]

    orig = chain.result

    def result(to, data):
        if to == DATA_PROVIDER and data[:10] == sel("getAllReservesTokens()"):
            return encode_reserve_tokens([(chain.symbols[a], a) for a in chain_reserves_tokens])
        return orig(to, data)

    chain.result = result
    b = read_positions(V3, USER, client=chain.client())
    syms = {a.symbol for a in b.assets}
    assert "WETH" in syms
    assert any(s.startswith("0xbbbb") for s in syms)


def test_read_positions_falls_back_to_second_rpc():
    chain = Chain()
    chain.overrides[("http://rpc-a.test", sel("getUserConfiguration(address)"))] = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "boom"},
    }
    b = read_positions(V3, USER, client=chain.client())
    assert b.hf == Decimal("1.65")


@pytest.mark.parametrize(
    "bad",
    [
        {"jsonrpc": "2.0", "id": 1, "result": "0x"},
        {"jsonrpc": "2.0", "id": 1, "error": "string error"},
        {"jsonrpc": "2.0", "id": 1, "result": None},
        ["not", "an", "object"],
    ],
)
def test_read_positions_raises_when_all_rpcs_bad(bad):
    chain = Chain()
    for url in V3.rpcs:
        chain.overrides[(url, sel("getAssetPrice(address)"))] = bad
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_zero_price_raises():
    # a zero oracle price would silently turn collateral into 0 USD
    chain = Chain()
    chain.prices[WETH] = 0
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_rejects_v4():
    with pytest.raises(ReadError):
        read_positions(V4, USER, client=Chain().client())


def test_read_positions_rejects_bad_address():
    with pytest.raises(ValueError):
        read_positions(V3, "0x123", client=Chain().client())


# ---------------------------------------------------------------- recompute


def pos(sym, coll, debt, price, lt, used=True) -> AssetPosition:
    return AssetPosition(
        symbol=sym,
        asset="0x" + sym.lower().ljust(40, "0")[:40],
        decimals=18,
        collateral=Decimal(coll),
        debt=Decimal(debt),
        price_usd=Decimal(price),
        liq_threshold=Decimal(lt),
        used_as_collateral=used,
    )


def test_recompute_matches_hand_calculation():
    coll, weighted, debt, hf = recompute(
        [pos("WETH", 2, 0, 2000, "0.825"), pos("WBTC", "0.1", 0, 60000, "0.78"), pos("USDC", 0, 3000, 1, "0.78", used=False)]
    )
    assert coll == Decimal(10000)
    assert weighted == Decimal(3300) + Decimal(4680)
    assert debt == Decimal(3000)
    assert hf == (Decimal(7980) / Decimal(3000))


def test_recompute_no_debt_gives_none_hf():
    _, _, debt, hf = recompute([pos("WETH", 1, 0, 2000, "0.8")])
    assert debt == Decimal(0)
    assert hf is None


# ---------------------------------------------------------------- tie_out


def breakdown(coll, weighted, debt, hf) -> PositionBreakdown:
    return PositionBreakdown(
        assets=(pos("WETH", 1, 0, 1, "0.8"),),
        emode_category=0,
        collateral_usd=Decimal(coll),
        weighted_collateral_usd=Decimal(weighted),
        debt_usd=Decimal(debt),
        hf=Decimal(hf) if hf is not None else None,
    )


def test_tie_out_exact_match():
    assert tie_out(breakdown(4000, 3300, 2000, "1.65"), account("4000", "2000", "1.65")) is True


def test_tie_out_within_half_percent():
    assert tie_out(breakdown(4000, 3300, 2000, "1.65"), account("4019", "2009", "1.658")) is True


@pytest.mark.parametrize(
    "acc",
    [
        account("4000", "2000", "1.66"),  # HF off by 0.6 %
        account("4030", "2000", "1.65"),  # collateral off by 0.75 %
        account("4000", "2015", "1.65"),  # debt off by 0.75 %
    ],
)
def test_tie_out_rejects_outside_tolerance(acc):
    assert tie_out(breakdown(4000, 3300, 2000, "1.65"), acc) is False


def test_tie_out_rejects_no_debt():
    assert tie_out(breakdown(4000, 3300, 0, None), account("4000", "0", None)) is False


def test_tie_out_rejects_when_contract_has_debt_but_breakdown_has_none():
    assert tie_out(breakdown(4000, 3300, 0, None), account("4000", "2000", "1.65")) is False


def test_tie_out_rejects_missing_account_values():
    v4_like = AccountData(collateral_usd=None, debt_usd=None, hf=Decimal("1.65"), no_debt=False)
    assert tie_out(breakdown(4000, 3300, 2000, "1.65"), v4_like) is False


def test_tie_out_rejects_zero_contract_collateral():
    assert tie_out(breakdown(4000, 3300, 2000, "1.65"), account("0", "2000", "1.65")) is False


def test_tie_out_custom_tolerance():
    b = breakdown(4000, 3300, 2000, "1.65")
    assert tie_out(b, account("4000", "2000", "1.66"), tolerance=Decimal("0.01")) is True


def test_end_to_end_read_then_tie_out():
    b = read_positions(V3, USER, client=Chain().client())
    assert tie_out(b, account("4000", "2000", "1.65")) is True


# ---------------------------------------------------------------- hostile RPC values


def test_read_positions_rejects_implausible_decimals():
    chain = Chain()
    chain.decimals[WETH] = 10**9  # 10**decimals would hang the bot
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_rejects_liquidation_threshold_above_100_percent():
    chain = Chain()
    chain.lt[WETH] = 10001
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_rejects_reserve_id_out_of_range():
    chain = Chain()
    orig = chain.result

    def result(to, data):
        if to == POOL and data[:10] == sel("getUserConfiguration(address)"):
            return "0x" + word(1 << 2 * 200)
        return orig(to, data)

    chain.result = result
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_cleans_symbol():
    chain = Chain()
    chain.symbols[WETH] = "W\x00ETH\n" + "X" * 40
    b = read_positions(V3, USER, client=chain.client())
    sym = next(a.symbol for a in b.assets if a.asset == WETH)
    assert sym == ("WETH" + "X" * 40)[:16]


# ---------------------------------------------------------------- eMode (Aave V3.2+ layout)


def test_emode_threshold_applies_to_collateral_in_category_bitmap():
    chain = Chain()
    chain.emode = 1
    chain.emode_lt[1] = 9500
    chain.emode_bitmap[1] = 1 << 0  # WETH (id 0)
    b = read_positions(V3, USER, client=chain.client())
    weth = next(a for a in b.assets if a.symbol == "WETH")
    assert weth.liq_threshold == Decimal("0.95")
    assert b.weighted_collateral_usd == Decimal(3800)
    assert b.hf == Decimal("1.9")


def test_emode_threshold_not_applied_outside_bitmap():
    chain = Chain()
    chain.emode = 1
    chain.emode_lt[1] = 9500
    chain.emode_bitmap[1] = 1 << 2  # only WBTC (id 2)
    b = read_positions(V3, USER, client=chain.client())
    weth = next(a for a in b.assets if a.symbol == "WETH")
    assert weth.liq_threshold == Decimal("0.825")
    assert b.hf == Decimal("1.65")


def test_emode_mixed_collateral():
    chain = Chain()
    chain.user[WBTC] = (10**7, 0, 0, True)  # 0.1 WBTC = 6,000 USD, base LT 0.78
    chain.emode = 2
    chain.emode_lt[2] = 9300
    chain.emode_bitmap[2] = 1 << 0  # WETH only
    b = read_positions(V3, USER, client=chain.client())
    # WETH 4,000 * 0.93 + WBTC 6,000 * 0.78 = 3,720 + 4,680
    assert b.weighted_collateral_usd == Decimal(8400)
    assert b.hf == Decimal("4.2")


def test_emode_zero_does_not_read_category():
    chain = Chain()
    read_positions(V3, USER, client=chain.client())
    called = {d for _, d in chain.calls}
    assert sel("getEModeCategoryCollateralConfig(uint8)") not in called
    assert sel("getEModeCategoryCollateralBitmap(uint8)") not in called


def test_emode_category_read_failure_raises():
    chain = Chain()
    chain.emode = 1
    chain.emode_lt[1] = 9500
    for url in V3.rpcs:
        chain.overrides[(url, sel("getEModeCategoryCollateralConfig(uint8)"))] = {
            "jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}
        }
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_emode_threshold_above_100_percent_raises():
    chain = Chain()
    chain.emode = 1
    chain.emode_lt[1] = 10001
    chain.emode_bitmap[1] = 1
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_emode_category_out_of_range_raises():
    chain = Chain()
    chain.emode = 256  # categories are uint8
    with pytest.raises(ReadError):
        read_positions(V3, USER, client=chain.client())


def test_read_positions_stops_after_time_budget():
    chain = Chain()
    with pytest.raises(ReadError, match="time budget"):
        read_positions(V3, USER, client=chain.client(), budget_s=-1.0)
