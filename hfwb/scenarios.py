"""Price-move scenarios and liquidation calculations (step 3 of price-drop feature)."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from hfwb.positions import PositionBreakdown

_STABLE_SYMBOLS = {
    "USDC",
    "USDT",
    "USDT0",
    "DAI",
    "USDS",
    "GHO",
    "USDE",
    "SUSDE",
    "EUSDE",
    "PYUSD",
    "RLUSD",
    "USDG",
    "FRAX",
    "FRXUSD",
    "LUSD",
    "CRVUSD",
    "FDUSD",
    "SDAI",
    "SUSD",
    "MAI",
    "MIMATIC",
    "AUSD",
    "MUSD",
    "USDBC",
    "USDTB",
    "STCUSD",
    "SYRUPUSDC",
    "SYRUPUSDT",
    "WXDAI",
    "USDM",
    "SRUSDE",
}

_OTHER_SYMBOLS = {
    "EURC",
    "EURE",
    "EURS",
    "EURA",
    "JEUR",
    "EURM",
    "XAUT",
    "XAUT0",
}


def asset_class(symbol: str) -> str:
    """Classify an asset symbol into 'stable', 'other', or 'volatile'."""
    norm = symbol.replace("₮", "T").upper().removeprefix("M.").removesuffix(".E")
    if norm in _STABLE_SYMBOLS or (norm.startswith("PT-") and "USD" in norm):
        return "stable"
    if norm in _OTHER_SYMBOLS:
        return "other"
    return "volatile"


@dataclass(frozen=True)
class LiquidationMove:
    """Hypothetical price move required for health factor to drop to 1."""

    label: str
    direction: str
    factor: Decimal | None
    price_now: Decimal | None
    hf_now: Decimal

    @property
    def change(self) -> Decimal | None:
        """Fractional price change to liquidation (e.g. 0.30 for 30% drop or rise)."""
        if self.factor is None:
            return None
        if self.direction == "down":
            return Decimal(1) - self.factor
        return self.factor - Decimal(1)

    @property
    def price_at_liquidation(self) -> Decimal | None:
        """Asset price at which liquidation occurs (None if factor or price_now is None)."""
        if self.price_now is None or self.factor is None:
            return None
        return self.price_now * self.factor

    @property
    def already_liquidatable(self) -> bool:
        """True if the position is already at or below liquidation threshold (HF <= 1)."""
        return self.hf_now <= Decimal(1)


def _sort_key(m: LiquidationMove) -> tuple[int, Decimal]:
    if m.change is None:
        return (1, Decimal(0))
    return (0, m.change)


def single_asset_moves(breakdown: PositionBreakdown) -> list[LiquidationMove]:
    """Calculate single-asset liquidation price moves for each active asset.

    Returns moves sorted by change ascending, with None (never liquidatable) last.
    """
    if breakdown.hf is None:
        return []

    moves: list[LiquidationMove] = []
    for a in breakdown.assets:
        w_s = Decimal(0)
        if a.used_as_collateral and a.collateral > Decimal(0) and a.liq_threshold > Decimal(0):
            w_s = a.collateral * a.price_usd * a.liq_threshold
        d_s = a.debt * a.price_usd

        if w_s == d_s:
            continue

        w_fix = breakdown.weighted_collateral_usd - w_s
        d_fix = breakdown.debt_usd - d_s
        f_star = (d_fix - w_fix) / (w_s - d_s)

        if w_s > d_s:
            direction = "down"
            factor = None if f_star <= Decimal(0) else f_star
        else:
            direction = "up"
            factor = f_star

        moves.append(
            LiquidationMove(
                label=a.symbol,
                direction=direction,
                factor=factor,
                price_now=a.price_usd,
                hf_now=breakdown.hf,
            )
        )

    moves.sort(key=_sort_key)
    return moves


def market_move(breakdown: PositionBreakdown) -> LiquidationMove | None:
    """Calculate market-wide liquidation move assuming all volatile assets move together."""
    if breakdown.hf is None:
        return None

    volatile_assets = [a for a in breakdown.assets if asset_class(a.symbol) == "volatile"]
    if not volatile_assets:
        return None

    w_s = Decimal(0)
    d_s = Decimal(0)
    for a in volatile_assets:
        if a.used_as_collateral and a.collateral > Decimal(0) and a.liq_threshold > Decimal(0):
            w_s += a.collateral * a.price_usd * a.liq_threshold
        d_s += a.debt * a.price_usd

    if w_s == d_s:
        return None

    w_fix = breakdown.weighted_collateral_usd - w_s
    d_fix = breakdown.debt_usd - d_s
    f_star = (d_fix - w_fix) / (w_s - d_s)

    if w_s > d_s:
        direction = "down"
        factor = None if f_star <= Decimal(0) else f_star
    else:
        direction = "up"
        factor = f_star

    return LiquidationMove(
        label="market",
        direction=direction,
        factor=factor,
        price_now=None,
        hf_now=breakdown.hf,
    )


def hf_after(breakdown: PositionBreakdown, multipliers: Mapping[str, Decimal]) -> Decimal | None:
    """Calculate health factor after applying asset price multipliers.

    Multipliers are keyed by asset address (lowercase). Missing assets default to 1.
    Raises ValueError if any multiplier is negative or not finite.
    Returns None if debt is 0 after the change.
    """
    for mult in multipliers.values():
        if not mult.is_finite() or mult < Decimal(0):
            raise ValueError(f"Multiplier must be finite and non-negative: {mult}")

    lowered_mults = {k.lower(): v for k, v in multipliers.items()}
    w_total = Decimal(0)
    d_total = Decimal(0)

    for a in breakdown.assets:
        mult = lowered_mults.get(a.asset.lower(), Decimal(1))
        new_price = a.price_usd * mult
        if a.used_as_collateral and a.collateral > Decimal(0) and a.liq_threshold > Decimal(0):
            w_total += a.collateral * new_price * a.liq_threshold
        d_total += a.debt * new_price

    if d_total == Decimal(0):
        return None

    return w_total / d_total
