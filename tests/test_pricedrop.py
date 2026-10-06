"""Price-drop presentation: liquidation lines, /whatif parsing, matching and replies. Synthetic numbers."""
from decimal import Decimal

import pytest

from hfwb.aave import AccountData, ReadError
from hfwb.markets import Market
from hfwb.positions import AssetPosition, PositionBreakdown, recompute
from hfwb.pricedrop import (
    format_liquidation_lines,
    format_price,
    liquidation_lines,
    match_assets,
    parse_whatif,
    run_whatif,
)

D = Decimal
ADDR = "0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
ADDR2 = "0x000000000000000000000000000000000000beef"

V3 = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("http://rpc.arb",),
)
V3_BASE = Market(
    protocol="aave_v3",
    chain="Base",
    chain_id=8453,
    name="Main market",
    address="0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
    rpcs=("http://rpc.base",),
)
V4 = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("http://rpc.eth",),
)


def pos(sym, coll, debt, price, lt="0.8", used=True) -> AssetPosition:
    return AssetPosition(
        symbol=sym,
        asset="0x" + sym.lower().encode().hex().ljust(40, "0")[:40],
        decimals=18,
        collateral=D(coll),
        debt=D(debt),
        price_usd=D(price),
        liq_threshold=D(lt),
        used_as_collateral=used,
    )


def bd(*assets: AssetPosition) -> PositionBreakdown:
    coll, weighted, debt, hf = recompute(assets)
    return PositionBreakdown(
        assets=tuple(assets), emode_category=0, collateral_usd=coll,
        weighted_collateral_usd=weighted, debt_usd=debt, hf=hf,
    )


def acct_for(b: PositionBreakdown) -> AccountData:
    return AccountData(
        collateral_usd=b.collateral_usd, debt_usd=b.debt_usd, hf=b.hf,
        no_debt=b.hf is None, has_position=True,
    )


# owner-like: 0.1 WBTC @ 60,000 (LT .78) vs 2,000 USDC debt -> WBTC -57.3 % to $25,641
WBTC_ONLY = bd(pos("WBTC", "0.1", 0, 60000, "0.78"), pos("USDC", 0, 2000, 1, "0.78", used=False))
# two volatile collaterals: WETH -90.3 %, WBTC -63.7 %, all crypto together -37.3 %
TWO = bd(
    pos("WETH", 2, 0, 2000, "0.825"),
    pos("WBTC", "0.1", 0, 60000, "0.78"),
    pos("USDC", 0, 5000, 1, "0.78", used=False),
)


# ---------------------------------------------------------------- format_price


@pytest.mark.parametrize(
    "value,expected",
    [("48336.18", "$48,336"), ("1000", "$1,000"), ("194.2", "$194.20"), ("1.7712", "$1.77"),
     ("0.123456", "$0.1235"), ("0.0000123456", "$0.00001235")],
)
def test_format_price(value, expected):
    assert format_price(D(value)) == expected


# ---------------------------------------------------------------- format_liquidation_lines


def test_lines_single_volatile_collateral():
    lines = format_liquidation_lines(WBTC_ONLY)
    text = "\n".join(lines)
    assert "WBTC −57.3% (to $25,641)" in text
    assert "USDC" not in text  # stablecoin moves are noise
    assert "All crypto" not in text  # only one volatile asset: same as the WBTC line


def test_lines_two_collaterals_and_market_line():
    text = "\n".join(format_liquidation_lines(TWO))
    assert "WBTC −63.7%" in text
    assert "WETH −90.3%" in text
    assert text.index("WBTC") < text.index("WETH")  # smallest move first
    assert "All crypto together −37.3%" in text


def test_lines_volatile_debt_rise():
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("WETH", 0, 2, 2000, "0.825", used=False))
    text = "\n".join(format_liquidation_lines(b))
    assert "WETH +95.0% (to $3,900)" in text


def test_lines_safe_even_at_zero():
    b = bd(
        pos("WETH", 10, 0, 2000, "0.825"),
        pos("WBTC", "0.01", 0, 60000, "0.78"),
        pos("USDC", 0, 1000, 1, "0.78", used=False),
    )
    text = "\n".join(format_liquidation_lines(b))
    assert "WBTC safe even at $0" in text


def test_lines_loop_market_never():
    b = bd(
        pos("wstETH", 10, 0, 2400, "0.95"),
        pos("WBTC", "0.01", 0, 60000, "0.78"),
        pos("WETH", 0, 8, 2000, "0.95", used=False),
    )
    text = "\n".join(format_liquidation_lines(b))
    assert "All crypto together: no liquidation" in text


def test_lines_at_most_three_assets():
    b = bd(
        pos("WETH", 1, 0, 2000, "0.8"), pos("WBTC", "0.05", 0, 60000, "0.78"),
        pos("LINK", 100, 0, 10, "0.7"), pos("AAVE", 5, 0, 200, "0.7"), pos("ARB", 1000, 0, "0.5", "0.6"),
        pos("USDC", 0, 3000, 1, "0.78", used=False),
    )
    asset_lines = [ln for ln in format_liquidation_lines(b) if ln.startswith("• ")]
    assert len(asset_lines) == 3


def test_lines_escape_html_in_symbols():
    b = bd(pos("<b>X", 1, 0, 2000, "0.8"), pos("USDC", 0, 1000, 1, "0.78", used=False))
    text = "\n".join(format_liquidation_lines(b))
    assert "<b>X" not in text
    assert "&lt;b&gt;X" in text


def test_lines_empty_when_no_debt_or_already_liquidatable():
    assert format_liquidation_lines(bd(pos("WETH", 1, 0, 2000, "0.8"))) == []
    under = bd(pos("WETH", 1, 0, 2000, "0.8"), pos("USDC", 0, 1700, 1, "0.78", used=False))
    assert format_liquidation_lines(under) == []


def test_lines_empty_when_only_stablecoins():
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("USDT", 0, 5000, 1, "0.75", used=False))
    assert format_liquidation_lines(b) == []


def test_lines_have_header():
    lines = format_liquidation_lines(WBTC_ONLY)
    assert lines[0] == "Liquidation if, with other prices unchanged:"


# ---------------------------------------------------------------- liquidation_lines (guarded)


def test_liquidation_lines_ok():
    calls = []

    def reader(market, address):
        calls.append((market.key, address))
        return WBTC_ONLY

    lines = liquidation_lines(V3, ADDR, acct_for(WBTC_ONLY), positions_reader=reader)
    assert any("WBTC −57.3%" in ln for ln in lines)
    assert calls == [(V3.key, ADDR)]


def test_liquidation_lines_skip_v4_and_no_debt_without_reading():
    def reader(market, address):
        raise AssertionError("must not read")

    assert liquidation_lines(V4, ADDR, acct_for(WBTC_ONLY), positions_reader=reader) == []
    no_debt = AccountData(collateral_usd=D(100), debt_usd=D(0), hf=None, no_debt=True)
    assert liquidation_lines(V3, ADDR, no_debt, positions_reader=reader) == []


@pytest.mark.parametrize("exc", [ReadError("rpc down"), ValueError("bad"), RuntimeError("bug")])
def test_liquidation_lines_never_raise(exc):
    def reader(market, address):
        raise exc

    assert liquidation_lines(V3, ADDR, acct_for(WBTC_ONLY), positions_reader=reader) == []


def test_liquidation_lines_hidden_when_tie_out_fails():
    wrong = AccountData(collateral_usd=D(6000), debt_usd=D(2000), hf=D("2.5"), no_debt=False)
    assert liquidation_lines(V3, ADDR, wrong, positions_reader=lambda m, a: WBTC_ONLY) == []


# ---------------------------------------------------------------- parse_whatif


@pytest.mark.parametrize(
    "args,expected",
    [
        (["ETH", "-20"], [("ETH", D("-20"))]),
        (["eth", "-20%"], [("ETH", D("-20"))]),
        (["BTC", "+15"], [("BTC", D("15"))]),
        (["wstETH", "-12.5", "BTC", "-10"], [("WSTETH", D("-12.5")), ("BTC", D("-10"))]),
        (["market", "-30"], [("MARKET", D("-30"))]),
        (["USD₮0", "-5"], [("USDT0", D("-5"))]),
    ],
)
def test_parse_whatif_ok(args, expected):
    assert parse_whatif(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["ETH"],
        ["ETH", "abc"],
        ["ETH", "-100"],  # price 0 is not a scenario
        ["ETH", "-150"],
        ["ETH", "1001"],
        ["ETH", "NaN"],
        ["ETH", "inf"],
        ["ETH", "-1e3"],
        ["ETH", "-20", "BTC"],
        ["E" * 17, "-20"],
        ["ET<H", "-20"],
        ["A", "-1", "B", "-1", "C", "-1", "D", "-1", "E", "-1", "F", "-1"],  # more than 5 pairs
        ["ETH", "-20", "ETH", "-10"],  # duplicate
    ],
)
def test_parse_whatif_rejects(args):
    with pytest.raises(ValueError):
        parse_whatif(args)


# ---------------------------------------------------------------- match_assets


LSTS = bd(
    pos("wstETH", 1, 0, 2400, "0.8"), pos("weETH", 1, 0, 2100, "0.8"), pos("WETH", 1, 0, 2000, "0.8"),
    pos("cbBTC", "0.01", 0, 60000, "0.78"), pos("WBTC.e", "0.01", 0, 60000, "0.78"),
    pos("USDC", 0, 3000, 1, "0.78", used=False),
)


def syms(assets):
    return sorted(a.symbol for a in assets)


def test_match_eth_family():
    assert syms(match_assets("ETH", LSTS)) == ["WETH", "weETH", "wstETH"]


def test_match_btc_family():
    assert syms(match_assets("BTC", LSTS)) == ["WBTC.e", "cbBTC"]


def test_match_exact_symbol_only():
    assert syms(match_assets("WETH", LSTS)) == ["WETH"]
    assert syms(match_assets("WSTETH", LSTS)) == ["wstETH"]
    assert syms(match_assets("WBTC", LSTS)) == ["WBTC.e"]  # trailing .e is normalized away


def test_match_market_is_all_volatile():
    assert syms(match_assets("MARKET", LSTS)) == ["WBTC.e", "WETH", "cbBTC", "weETH", "wstETH"]


def test_match_nothing():
    assert match_assets("LINK", LSTS) == []


# ---------------------------------------------------------------- run_whatif


def reader_from(table):
    """table: {(market.key, address): PositionBreakdown | Exception}"""

    def account_reader(market, address):
        b = table[(market.key, address)]
        if isinstance(b, Exception):
            raise b
        return acct_for(b)

    def positions_reader(market, address):
        b = table[(market.key, address)]
        if isinstance(b, Exception):
            raise b
        return b

    return account_reader, positions_reader


def test_run_whatif_single_position():
    ar, pr = reader_from({(V3.key, ADDR): WBTC_ONLY})
    text = run_whatif([("BTC", D("-20"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "What if BTC −20%?" in text
    assert "Aave V3 · Arbitrum" in text
    # HF 2.34 -> 2.34 * 0.8 = 1.87
    assert "2.34 → 1.87" in text
    assert "Moved: WBTC" in text
    assert "not financial advice" in text


def test_run_whatif_reports_liquidation():
    ar, pr = reader_from({(V3.key, ADDR): WBTC_ONLY})
    text = run_whatif([("BTC", D("-60"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "0.94" in text
    assert "below 1.00" in text


def test_run_whatif_rise():
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("WETH", 0, 2, 2000, "0.825", used=False))
    ar, pr = reader_from({(V3.key, ADDR): b})
    text = run_whatif([("ETH", D("50"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "What if ETH +50%?" in text
    assert "1.95 → 1.30" in text


def test_run_whatif_no_exposure_and_unsupported_and_mismatch():
    mismatch = bd(pos("WETH", 1, 0, 2000, "0.8"), pos("USDC", 0, 1000, 1, "0.78", used=False))

    def account_reader(market, address):
        if market.key == V3_BASE.key:
            return AccountData(collateral_usd=D(5000), debt_usd=D(1000), hf=D("4"), no_debt=False)
        return acct_for(WBTC_ONLY)

    def positions_reader(market, address):
        if market.key == V3_BASE.key:
            return mismatch
        return WBTC_ONLY

    text = run_whatif(
        [("LINK", D("-20"))],
        [(V3, ADDR), (V3_BASE, ADDR), (V4, ADDR)],
        account_reader=account_reader,
        positions_reader=positions_reader,
    )
    assert "No LINK" in text
    assert "Aave V4 · Ethereum · Main Spoke" in text and "not supported yet" in text
    assert "Aave V3 · Base" in text and "did not match" in text


def test_run_whatif_skips_positions_without_debt():
    ar, pr = reader_from({(V3.key, ADDR): bd(pos("WETH", 1, 0, 2000, "0.8"))})
    text = run_whatif([("ETH", D("-20"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "no debt" in text.lower()


def test_run_whatif_read_error_is_reported_not_raised():
    ar, pr = reader_from({(V3.key, ADDR): ReadError("rpc down")})
    text = run_whatif([("ETH", D("-20"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "could not be read" in text


def test_run_whatif_no_positions():
    ar, pr = reader_from({})
    text = run_whatif([("ETH", D("-20"))], [], account_reader=ar, positions_reader=pr)
    assert "/watch" in text


def test_run_whatif_multiple_changes():
    ar, pr = reader_from({(V3.key, ADDR): TWO})
    text = run_whatif(
        [("ETH", D("-10")), ("BTC", D("-10"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr
    )
    assert "What if ETH −10%, BTC −10%?" in text
    # HF 1.596 -> 1.596 * 0.9 = 1.4364
    assert "1.60 → 1.44" in text
    assert "Moved: WBTC, WETH" in text


def test_run_whatif_market_keyword():
    ar, pr = reader_from({(V3.key, ADDR): TWO})
    text = run_whatif([("MARKET", D("-10"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    assert "What if all crypto −10%?" in text
    assert "1.60 → 1.44" in text


def test_run_whatif_two_addresses_same_market():
    ar, pr = reader_from({(V3.key, ADDR): WBTC_ONLY, (V3.key, ADDR2): TWO})
    text = run_whatif([("BTC", D("-20"))], [(V3, ADDR), (V3, ADDR2)], account_reader=ar, positions_reader=pr)
    assert "0x5a5a" in text and "0x0000" in text


def test_run_whatif_uses_chat_levels_for_emoji():
    ar, pr = reader_from({(V3.key, ADDR): WBTC_ONLY})
    # after = 1.87: green with default levels (top 1.4), not green with a top level of 2.0
    default = run_whatif([("BTC", D("-20"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr)
    custom = run_whatif([("BTC", D("-20"))], [(V3, ADDR)], account_reader=ar, positions_reader=pr,
                        levels=(D("2.0"), D("1.5"), D("1.2"), D("1.1")))
    assert "🟢" in default
    assert "🟢" not in custom


def test_run_whatif_time_budget_skips_remaining_positions():
    ar, pr = reader_from({(V3.key, ADDR): WBTC_ONLY, (V3.key, ADDR2): TWO})
    ticks = iter([0.0, 0.0, 30.0])
    text = run_whatif([("BTC", D("-20"))], [(V3, ADDR), (V3, ADDR2)], account_reader=ar,
                      positions_reader=pr, time_budget_s=25.0, clock=lambda: next(ticks))
    assert "2.34 → 1.87" in text
    assert "time limit" in text


def test_lines_skip_dust_assets():
    # a same-asset WETH loop plus a weETH dust balance (seen live on Arbitrum)
    b = bd(
        pos("WETH", "0.6232", "0.5411", 2688, "0.95"),
        pos("weETH", "0.000000000159", 0, 2970, "0.95"),
    )
    text = "\n".join(format_liquidation_lines(b))
    assert "weETH" not in text
    assert "WETH safe even at $0" in text
    assert "All crypto" not in text  # only one volatile asset with real exposure
