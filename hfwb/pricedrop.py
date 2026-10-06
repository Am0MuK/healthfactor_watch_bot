"""Price-drop presentation: liquidation lines, /whatif parsing, matching and replies."""

import html
import logging
import re
import time
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from hfwb.aave import AccountData
from hfwb.format import (
    FOOTER,
    format_market_name,
    format_short_address,
    hf_emoji,
    hf_gauge,
    profile_link,
)
from hfwb.markets import Market
from hfwb.positions import AssetPosition, PositionBreakdown, tie_out
from hfwb.scenarios import (
    asset_class,
    hf_after,
    market_move,
    single_asset_moves,
)
from hfwb.state import DEFAULT_LEVELS

logger = logging.getLogger(__name__)

MIN_EXPOSURE_USD = Decimal(1)
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,16}$")
_PCT_RE = re.compile(r"^[+-]?\d+(\.\d+)?%?$")

_ETH_FAMILY = {
    "ETH",
    "WETH",
    "WSTETH",
    "WEETH",
    "RETH",
    "CBETH",
    "OSETH",
    "EZETH",
    "RSETH",
    "WRSETH",
    "ETHX",
    "TETH",
}

_BTC_FAMILY = {
    "WBTC",
    "CBBTC",
    "TBTC",
    "LBTC",
    "EBTC",
    "FBTC",
    "BTC.B",
    "BTCB",
    "XBTC",
}


def normalize_symbol(sym: str) -> str:
    """Normalize symbol similarly to scenarios.asset_class."""
    return sym.replace("₮", "T").upper().removeprefix("M.").removesuffix(".E")


def format_price(value: Decimal) -> str:
    """Format price: >= 1000 -> $48,336; >= 1 -> $194.20; < 1 -> 4 sig digits; 0 -> $0."""
    if value <= Decimal(0):
        return "$0"
    if value >= Decimal(1000):
        q = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"${int(q):,}"
    if value >= Decimal(1):
        q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${q:,.2f}"

    decimals = -value.adjusted() + 3
    q = value.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP)
    if q.adjusted() > value.adjusted():
        decimals = -q.adjusted() + 3
        q = value.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP)
    if q >= Decimal(1):
        return f"${q:,.2f}"
    return f"${q:f}"


def format_liquidation_lines(breakdown: PositionBreakdown) -> list[str]:
    """Format liquidation lines for a PositionBreakdown.

    Returns [] if breakdown.hf is None or <= 1, or if all moves are for stablecoins.
    """
    if breakdown.hf is None or breakdown.hf <= Decimal(1):
        return []

    moves = single_asset_moves(breakdown)
    # dust positions (under MIN_EXPOSURE_USD of collateral plus debt) would only add noise
    exposure: dict[str, Decimal] = {}
    for a in breakdown.assets:
        value = (a.collateral if a.used_as_collateral else Decimal(0)) * a.price_usd + a.debt * a.price_usd
        exposure[a.symbol] = exposure.get(a.symbol, Decimal(0)) + value
    non_stable_moves = [
        m
        for m in moves
        if asset_class(m.label) != "stable" and exposure.get(m.label, Decimal(0)) >= MIN_EXPOSURE_USD
    ]

    asset_lines: list[str] = []
    for m in non_stable_moves[:3]:
        sym = html.escape(m.label)
        if m.direction == "down":
            if m.factor is None:
                asset_lines.append(f"• {sym} safe even at $0")
            else:
                pct = (m.change * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                p_str = format_price(m.price_at_liquidation)
                asset_lines.append(f"• {sym} −{pct:.1f}% (to {p_str})")
        else:
            pct = (m.change * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            p_str = format_price(m.price_at_liquidation)
            asset_lines.append(f"• {sym} +{pct:.1f}% (to {p_str})")

    # Market line only when at least 2 volatile assets have non-zero exposure
    volatile_with_exposure = [
        a
        for a in breakdown.assets
        if asset_class(a.symbol) == "volatile"
        and exposure.get(a.symbol, Decimal(0)) >= MIN_EXPOSURE_USD
        and (
            (a.used_as_collateral and a.collateral > 0 and a.liq_threshold > 0)
            or a.debt > 0
        )
    ]

    market_line: str | None = None
    if len(volatile_with_exposure) >= 2:
        mm = market_move(breakdown)
        if mm is not None:
            if mm.factor is None:
                market_line = "All crypto together: no liquidation"
            else:
                pct = (mm.change * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                if mm.direction == "down":
                    market_line = f"All crypto together −{pct:.1f}%"
                else:
                    market_line = f"All crypto together +{pct:.1f}%"

    if not asset_lines and not market_line:
        return []

    lines = ["Liquidation if, with other prices unchanged:"]
    lines.extend(asset_lines)
    if market_line:
        lines.append(market_line)
    return lines


def liquidation_lines(
    market: Market,
    address: str,
    account: AccountData,
    positions_reader: Callable[..., PositionBreakdown],
) -> list[str]:
    """Read positions and return formatted liquidation lines, or [] on error.

    Never raises.
    """
    if market.protocol != "aave_v3" or account.no_debt or account.hf is None:
        return []

    try:
        breakdown = positions_reader(market, address)
        if not tie_out(breakdown, account):
            return []
        return format_liquidation_lines(breakdown)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to compute liquidation lines for market %s: %s", market.name, exc)
        return []


def parse_whatif(args: list[str]) -> list[tuple[str, Decimal]]:
    """Parse /whatif command argument tokens into list of (SYMBOL, pct) pairs."""
    if not args or len(args) % 2 != 0:
        raise ValueError("Must provide pairs of SYMBOL and PERCENTAGE")

    num_pairs = len(args) // 2
    if num_pairs < 1 or num_pairs > 5:
        raise ValueError("Must provide between 1 and 5 pairs")

    pairs: list[tuple[str, Decimal]] = []
    seen_symbols: set[str] = set()

    for i in range(0, len(args), 2):
        sym = args[i].replace("₮", "T").upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError(f"Invalid symbol: {args[i]!r}")
        if sym in seen_symbols:
            raise ValueError(f"Duplicate symbol: {sym}")
        seen_symbols.add(sym)

        pct_raw = args[i + 1]
        if not _PCT_RE.match(pct_raw):
            raise ValueError(f"Invalid percentage: {pct_raw!r}")

        pct_clean = pct_raw.rstrip("%")
        try:
            pct_val = Decimal(pct_clean)
        except ArithmeticError:
            raise ValueError(f"Invalid percentage number: {pct_clean!r}") from None

        if not (Decimal(-100) < pct_val <= Decimal(1000)):
            raise ValueError(f"Percentage must be > -100 and <= 1000: {pct_val}")

        pairs.append((sym, pct_val))

    return pairs


def match_assets(token: str, breakdown: PositionBreakdown) -> list[AssetPosition]:
    """Match assets in a breakdown against a user-specified token or family."""
    tok_norm = normalize_symbol(token)
    if tok_norm == "MARKET":
        return [a for a in breakdown.assets if asset_class(a.symbol) == "volatile"]
    if tok_norm == "ETH":
        return [a for a in breakdown.assets if normalize_symbol(a.symbol) in _ETH_FAMILY]
    if tok_norm == "BTC":
        return [a for a in breakdown.assets if normalize_symbol(a.symbol) in _BTC_FAMILY]
    return [a for a in breakdown.assets if normalize_symbol(a.symbol) == tok_norm]


def run_whatif(
    changes: list[tuple[str, Decimal]],
    positions: Sequence[tuple[Market, str]],
    account_reader: Callable[..., AccountData],
    positions_reader: Callable[..., PositionBreakdown],
    levels: Sequence[Decimal] = DEFAULT_LEVELS,
    time_budget_s: float = 25.0,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Execute /whatif simulation across watched positions and return HTML formatted reply."""
    if not positions:
        return (
            "No watched positions found. Use /watch &lt;address&gt; to track a position first.\n\n"
            + FOOTER
        )

    # Format title
    change_parts: list[str] = []
    for tok, pct in changes:
        tok_name = "all crypto" if tok == "MARKET" else tok
        abs_pct = abs(pct)
        pct_str = f"{abs_pct:f}"
        if "." in pct_str:
            pct_str = pct_str.rstrip("0").rstrip(".")
        sign = "−" if pct < 0 else "+"
        change_parts.append(f"{tok_name} {sign}{pct_str}%")

    title = f"🔮 <b>What if {', '.join(change_parts)}?</b>"

    status_notes: list[str] = []
    no_exposure_markets: list[str] = []
    blocks: list[str] = []

    started = clock()
    for market, address in positions:
        m_name = html.escape(format_market_name(market))
        short_addr = format_short_address(address)

        if clock() - started > time_budget_s:
            status_notes.append(f"{m_name} {short_addr}: could not be read right now (time limit)")
            continue

        if market.protocol != "aave_v3":
            status_notes.append(f"{m_name}: not supported yet")
            continue

        try:
            account = account_reader(market, address)
        except Exception:  # noqa: BLE001
            status_notes.append(f"{m_name} {short_addr}: could not be read right now")
            continue

        if account.no_debt or account.hf is None:
            status_notes.append(f"{m_name} {short_addr}: no debt, prices cannot liquidate it")
            continue

        try:
            breakdown = positions_reader(market, address)
        except Exception:  # noqa: BLE001
            status_notes.append(f"{m_name} {short_addr}: could not be read right now")
            continue

        if not tie_out(breakdown, account):
            status_notes.append(
                f"{m_name} {short_addr}: skipped, the per-asset data did not match the contract right now"
            )
            continue

        # Match assets and build multipliers
        multipliers: dict[str, Decimal] = {}
        matched_symbols: set[str] = set()

        for tok, pct in changes:
            matched = match_assets(tok, breakdown)
            mult = Decimal(1) + (pct / Decimal(100))
            for a in matched:
                multipliers[a.asset.lower()] = mult
                matched_symbols.add(a.symbol)

        if not multipliers:
            no_exposure_markets.append(m_name)
            continue

        after_hf = hf_after(breakdown, multipliers)
        if after_hf is None:
            status_notes.append(f"{m_name} {short_addr}: no debt, prices cannot liquidate it")
            continue

        # Build position block
        link = profile_link(address)
        emoji = hf_emoji(after_hf, levels=levels)
        gauge = hf_gauge(after_hf)
        symbols_str = ", ".join(sorted(matched_symbols, key=str.lower))

        now_val = account.hf
        now_str = f"{now_val:.2f}" if now_val is not None else "N/A"
        after_str = f"{after_hf:.2f}"

        block_lines = [
            f"{emoji} <b>{m_name}</b> {link}",
            f"Health factor <b>{now_str} → {after_str}</b>  {gauge}",
            f"Moved: {symbols_str}",
        ]
        if after_hf <= Decimal(1):
            block_lines.append("⚠️ HF would be below 1.00: the position could be liquidated")

        blocks.append("\n".join(block_lines))

    # Assemble reply
    lines: list[str] = [title]

    if status_notes:
        lines.append("")
        lines.extend(status_notes)

    if no_exposure_markets:
        tokens_label = ", ".join(tok if tok != "MARKET" else "crypto" for tok, _ in changes)
        lines.append("")
        lines.append(f"No {tokens_label} exposure: {', '.join(no_exposure_markets)}")

    if blocks:
        for b in blocks:
            lines.append("")
            lines.append(b)

    lines.extend([
        "",
        "<i>Hypothetical: only the listed prices move; debt amounts and other prices stay the same.</i>",
        "",
        FOOTER,
    ])

    return "\n".join(lines)
