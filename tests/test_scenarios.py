"""Price-move scenarios (step 3 of the price-drop feature). All numbers are synthetic."""
from decimal import Decimal

import pytest

from hfwb.positions import AssetPosition, PositionBreakdown, recompute
from hfwb.scenarios import (
    LiquidationMove,
    asset_class,
    hf_after,
    market_move,
    single_asset_moves,
)

D = Decimal


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
        assets=tuple(assets),
        emode_category=0,
        collateral_usd=coll,
        weighted_collateral_usd=weighted,
        debt_usd=debt,
        hf=hf,
    )


def by_label(moves: list[LiquidationMove]) -> dict[str, LiquidationMove]:
    return {m.label: m for m in moves}


def close(a: Decimal, b: Decimal, eps: str = "0.000001") -> bool:
    return abs(a - b) <= D(eps)


# ---------------------------------------------------------------- asset_class


@pytest.mark.parametrize(
    "sym",
    ["USDC", "USDC.e", "USD₮0", "USDT0", "USDt", "DAI.e", "m.USDC", "sDAI", "sUSDe", "GHO", "USDS",
     "crvUSD", "PT-sUSDE-22OCT2026", "PT-USDG-29OCT2026", "syrupUSDC", "WXDAI", "USDbC", "RLUSD"],
)
def test_asset_class_usd_stable(sym):
    assert asset_class(sym) == "stable"


@pytest.mark.parametrize("sym", ["EURC", "EURe", "jEUR", "XAUt", "XAUt0"])
def test_asset_class_other_fiat_or_gold_is_not_crypto(sym):
    assert asset_class(sym) == "other"


@pytest.mark.parametrize(
    "sym", ["WETH", "WETH.e", "wstETH", "weETH", "WBTC", "cbBTC", "BTC.b", "ARB", "LINK", "AAVE.e", "0xabcd"]
)
def test_asset_class_volatile(sym):
    assert asset_class(sym) == "volatile"


# ---------------------------------------------------------------- single asset


def test_single_collateral_asset_drop():
    # 0.1 WBTC @ 60,000, LT 0.78 -> weighted 4,680; debt 2,000 USDC
    b = bd(pos("WBTC", "0.1", 0, 60000, "0.78"), pos("USDC", 0, 2000, 1, "0.78", used=False))
    m = by_label(single_asset_moves(b))["WBTC"]
    assert m.direction == "down"
    assert close(m.factor, D(2000) / D(4680))
    assert close(m.change, 1 - D(2000) / D(4680))  # about 57.3 %
    assert m.price_now == D(60000)
    assert close(m.price_at_liquidation, D(60000) * D(2000) / D(4680), "0.0001")
    assert m.already_liquidatable is False


def test_single_asset_moves_two_collaterals():
    # WETH 2 @ 2,000 LT .825 (weighted 3,300) + WBTC 0.1 @ 60,000 LT .78 (4,680); debt 5,000 USDC
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),
        pos("WBTC", "0.1", 0, 60000, "0.78"),
        pos("USDC", 0, 5000, 1, "0.78", used=False),
    )
    moves = by_label(single_asset_moves(b))
    assert close(moves["WETH"].factor, D(5000 - 4680) / D(3300))  # WETH must fall about 90.3 %
    assert close(moves["WBTC"].factor, D(5000 - 3300) / D(4680))  # WBTC about 63.7 %
    # the debt asset (a stablecoin) is listed too: liquidation if USDC rose
    assert moves["USDC"].direction == "up"
    assert close(moves["USDC"].factor, D(7980) / D(5000))


def test_single_debt_asset_rise():
    # 10,000 USDC collateral LT .78 (weighted 7,800); debt 2 WETH @ 2,000 = 4,000
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("WETH", 0, 2, 2000, "0.825", used=False))
    m = by_label(single_asset_moves(b))["WETH"]
    assert m.direction == "up"
    assert close(m.factor, D("1.95"))
    assert close(m.change, D("0.95"))
    assert close(m.price_at_liquidation, D(3900), "0.0001")


def test_single_asset_safe_even_at_zero():
    # WETH alone covers the debt; WBTC can go to zero
    b = bd(
        pos("WETH", 10, 0, 2000, "0.825"),
        pos("WBTC", "0.01", 0, 60000, "0.78"),
        pos("USDC", 0, 1000, 1, "0.78", used=False),
    )
    m = by_label(single_asset_moves(b))["WBTC"]
    assert m.direction == "down"
    assert m.factor is None
    assert m.change is None
    assert m.price_at_liquidation is None
    assert m.already_liquidatable is False


def test_single_asset_supplied_and_borrowed_uses_net_exposure():
    # WETH 2 collateral (LT .825 -> 3,300) and 1 WETH debt (2,000); plus 500 USDC debt
    b = bd(pos("WETH", 2, 1, 2000, "0.825"), pos("USDC", 0, 500, 1, "0.78", used=False))
    m = by_label(single_asset_moves(b))["WETH"]
    assert m.direction == "down"
    assert close(m.factor, D(500) / D(1300))


def test_single_asset_with_no_net_effect_is_omitted():
    # weighted collateral equals debt for the same asset: its price does not move HF
    b = bd(pos("WETH", 1, "0.8", 2000, "0.8"), pos("WBTC", "0.1", 0, 60000, "0.78"),
           pos("USDC", 0, 1000, 1, "0.78", used=False))
    assert "WETH" not in by_label(single_asset_moves(b))


def test_collateral_not_used_or_zero_lt_has_no_drop():
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),
        pos("LINK", 100, 0, 10, "0"),  # LT 0: not counted by Aave
        pos("WBTC", 1, 0, 60000, "0.78", used=False),  # supplied, not collateral
        pos("USDC", 0, 1000, 1, "0.78", used=False),
    )
    labels = by_label(single_asset_moves(b))
    assert "LINK" not in labels
    assert "WBTC" not in labels


def test_already_liquidatable():
    b = bd(pos("WETH", 1, 0, 2000, "0.8"), pos("USDC", 0, 1700, 1, "0.78", used=False))
    assert b.hf < 1
    m = by_label(single_asset_moves(b))["WETH"]
    assert m.already_liquidatable is True


def test_no_debt_gives_no_moves():
    b = bd(pos("WETH", 1, 0, 2000, "0.8"))
    assert single_asset_moves(b) == []
    assert market_move(b) is None


def test_moves_sorted_by_smallest_change_first():
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),
        pos("WBTC", "0.1", 0, 60000, "0.78"),
        pos("USDC", 0, 5000, 1, "0.78", used=False),
    )
    changes = [m.change for m in single_asset_moves(b) if m.change is not None]
    assert changes == sorted(changes)


# ---------------------------------------------------------------- market move


def test_market_move_all_volatile_collateral_together():
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),
        pos("WBTC", "0.1", 0, 60000, "0.78"),
        pos("USDC", 0, 5000, 1, "0.78", used=False),
    )
    m = market_move(b)
    assert m.label == "market"
    assert m.direction == "down"
    assert close(m.factor, D(5000) / D(7980))  # about 37.3 %
    assert m.price_now is None
    assert m.price_at_liquidation is None


def test_market_move_keeps_stablecoin_collateral_fixed():
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),  # 3,300 weighted, moves
        pos("USDC", 1000, 0, 1, "0.78"),  # 780 weighted, fixed
        pos("GHO", 0, 3000, 1, "0.78", used=False),
    )
    m = market_move(b)
    assert close(m.factor, D(3000 - 780) / D(3300))


def test_market_move_loop_is_never_liquidated_by_a_joint_drop():
    # wstETH collateral, WETH debt: both fall together, HF does not change
    b = bd(pos("wstETH", 10, 0, 2400, "0.81"), pos("WETH", 0, 8, 2000, "0.825", used=False))
    m = market_move(b)
    assert m.direction == "down"
    assert m.factor is None


def test_market_move_volatile_debt_against_stable_collateral_is_a_rise():
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("WETH", 0, 2, 2000, "0.825", used=False))
    m = market_move(b)
    assert m.direction == "up"
    assert close(m.factor, D("1.95"))


def test_market_move_none_when_only_stablecoins():
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), pos("USDT", 0, 5000, 1, "0.75", used=False))
    assert market_move(b) is None


def test_market_move_ignores_gold_and_euro():
    b = bd(
        pos("XAUt", 1, 0, 4000, "0.7"),  # 2,800 weighted, fixed
        pos("WETH", 1, 0, 2000, "0.8"),  # 1,600, moves
        pos("USDC", 0, 3000, 1, "0.78", used=False),
    )
    m = market_move(b)
    assert close(m.factor, D(3000 - 2800) / D(1600))


# ---------------------------------------------------------------- hf_after


def test_hf_after_single_change():
    weth = pos("WETH", 2, 0, 2000, "0.825")
    b = bd(weth, pos("USDC", 0, 2000, 1, "0.78", used=False))
    assert hf_after(b, {weth.asset: D("0.8")}) == D("2640") / D("2000")


def test_hf_after_no_changes_equals_current():
    b = bd(pos("WETH", 2, 0, 2000, "0.825"), pos("USDC", 0, 2000, 1, "0.78", used=False))
    assert hf_after(b, {}) == b.hf


def test_hf_after_debt_asset_change():
    weth = pos("WETH", 0, 2, 2000, "0.825", used=False)
    b = bd(pos("USDC", 10000, 0, 1, "0.78"), weth)
    assert hf_after(b, {weth.asset: D("1.5")}) == D(7800) / D(6000)


def test_hf_after_no_debt_is_none():
    weth = pos("WETH", 2, 0, 2000, "0.825")
    assert hf_after(bd(weth), {weth.asset: D("0.5")}) is None


def test_hf_after_rejects_negative_multiplier():
    weth = pos("WETH", 2, 0, 2000, "0.825")
    b = bd(weth, pos("USDC", 0, 2000, 1, "0.78", used=False))
    with pytest.raises(ValueError):
        hf_after(b, {weth.asset: D("-0.1")})


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_hf_after_rejects_non_finite_multiplier(bad):
    weth = pos("WETH", 2, 0, 2000, "0.825")
    b = bd(weth, pos("USDC", 0, 2000, 1, "0.78", used=False))
    with pytest.raises(ValueError):
        hf_after(b, {weth.asset: D(bad)})


def test_factor_applied_reaches_hf_one():
    # cross-check: applying the computed factor really gives HF = 1
    b = bd(
        pos("WETH", 2, 0, 2000, "0.825"),
        pos("WBTC", "0.1", 0, 60000, "0.78"),
        pos("USDC", 0, 5000, 1, "0.78", used=False),
    )
    for m in single_asset_moves(b):
        if m.factor is None:
            continue
        asset = next(a.asset for a in b.assets if a.symbol == m.label)
        assert close(hf_after(b, {asset: m.factor}), D(1))
    mm = market_move(b)
    vol = {a.asset: mm.factor for a in b.assets if asset_class(a.symbol) == "volatile"}
    assert close(hf_after(b, vol), D(1))
