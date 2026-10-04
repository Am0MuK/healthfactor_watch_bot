import html
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from hfwb.state import LEVELS

FOOTER = "<i>Informational only, not financial advice.</i>"

LEVEL_THRESHOLDS: dict[str, str] = {
    f"L{i + 1}": str(thresh) for i, thresh in enumerate(LEVELS)
}

STATE_EMOJIS: dict[str, str] = {
    "ok": "🟢",
    "L1": "🟡",
    "L2": "🟠",
    "L3": "🔴",
    "L4": "🚨",
}


def hf_emoji(hf: Decimal | None) -> str:
    """Return health factor emoji.

    None (no debt) or HF >= 1.4 -> 🟢; < 1.4 -> 🟡; < 1.2 -> 🟠; < 1.1 -> 🔴; < 1.05 -> 🚨.
    """
    if hf is None:
        return "🟢"
    val = Decimal(str(hf))
    if val >= LEVELS[0]:
        return "🟢"
    if val < LEVELS[3]:
        return "🚨"
    if val < LEVELS[2]:
        return "🔴"
    if val < LEVELS[1]:
        return "🟠"
    return "🟡"


def state_emoji(state: str) -> str:
    """Map state string to emoji."""
    return STATE_EMOJIS.get(state, "🟢")


def hf_gauge(hf: Decimal | None) -> str:
    """10-cell bar from HF 1.0 (empty) to 2.0 (full), clamped, using ▰ and ▱."""
    if hf is None:
        return "▰" * 10
    val = Decimal(str(hf))
    if val <= Decimal("1.0"):
        return "▱" * 10
    if val >= Decimal("2.0"):
        return "▰" * 10
    fraction = (val - Decimal("1.0")) * Decimal(10)
    filled = int(fraction.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    filled = max(0, min(10, filled))
    return "▰" * filled + "▱" * (10 - filled)


def drop_to_liquidation_pct(hf: Decimal | None) -> float:
    """(1 - 1/hf) * 100 rounded to 1 decimal; hf <= 1 -> 0.0."""
    if hf is None:
        return 0.0
    val = Decimal(str(hf))
    if val <= Decimal("1.0"):
        return 0.0
    pct = (Decimal(1) - Decimal(1) / val) * Decimal(100)
    rounded = pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return float(rounded)


def profile_link(address: str) -> str:
    """<a href="https://debank.com/profile/{address}">0x1234…abcd</a>"""
    escaped_address = html.escape(address)
    if len(address) >= 10:
        short = f"{html.escape(address[:6])}…{html.escape(address[-4:])}"
    else:
        short = escaped_address
    return f'<a href="https://debank.com/profile/{escaped_address}">{short}</a>'


def format_short_address(address: str) -> str:
    """Shorten address to 0x1234...abcd format."""
    if len(address) >= 10:
        return f"{address[:6]}...{address[-4:]}"
    return address


def format_currency(amount: Decimal) -> str:
    """Format decimal amount as USD currency string."""
    if amount == amount.to_integral():
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


def format_hf(hf: Decimal | None) -> str:
    """Format health factor to 2 decimals or 'No debt'."""
    if hf is None:
        return "No debt"
    return f"{hf:.2f}"


def format_market_name(market: Any) -> str:
    """Format market as a display name: e.g. 'Aave V3 · Base' or 'Aave V4 · Ethereum · Main Spoke'."""
    if hasattr(market, "protocol") and hasattr(market, "chain") and hasattr(market, "name"):
        if market.protocol == "aave_v3":
            if market.name.lower() in ("main market", "main"):
                return f"Aave V3 · {market.chain}"
            return f"Aave V3 · {market.chain} · {market.name}"
        else:  # aave_v4
            return f"Aave V4 · {market.chain} · {market.name}"
    return str(market)


def format_alert(
    alert_type: str,
    address: str,
    hf: Decimal | None,
    collateral_usd: Decimal | None = None,
    debt_usd: Decimal | None = None,
    market: Any | None = None,
) -> str:
    """Format an alert notification naming the level reached and the market in HTML."""
    emoji = hf_emoji(hf)
    if alert_type == "recovery":
        emoji = "🟢"

    m_name = html.escape(format_market_name(market)) if market is not None else "Position"
    lines = [f"{emoji} <b>{m_name}</b>"]

    hf_str = format_hf(hf)
    gauge = hf_gauge(hf)
    lines.append(f"Health factor <b>{hf_str}</b>  {gauge}")

    is_critical = False
    if alert_type in LEVEL_THRESHOLDS:
        thresh = LEVEL_THRESHOLDS[alert_type]
        lines.append(f"Fell below <b>{thresh}</b>")
        if alert_type in ("L3", "L4"):
            is_critical = True
    elif alert_type.startswith("repeat_"):
        lvl = alert_type.removeprefix("repeat_")
        thresh = LEVEL_THRESHOLDS.get(lvl, "")
        lines.append(f"Still below <b>{thresh}</b>")
        lines.append("Next reminder in 30 min")
        if lvl in ("L3", "L4"):
            is_critical = True
    elif alert_type == "recovery":
        lines.append(f"Back above <b>{LEVELS[0]}</b>")
    else:
        lines.append(f"Update: <b>{html.escape(alert_type)}</b>")

    if is_critical:
        lines.append("<b>Close to liquidation (1.00)</b>")

    if hf is not None:
        drop_pct = drop_to_liquidation_pct(hf)
        lines.append(
            f"About <b>{drop_pct:.1f}%</b> of collateral value can fall before liquidation at 1.00 (if debt value stays the same)."
        )

    if collateral_usd is not None and debt_usd is not None:
        c_str = format_currency(collateral_usd)
        d_str = format_currency(debt_usd)
        lines.append(f"Collateral {c_str} · Debt {d_str}")

    lines.append(f"Account: {profile_link(address)}")
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_start() -> str:
    return (
        "Health Factor Watch Bot\n\n"
        "Monitors Aave V3 (20 EVM chains) and Aave V4 spokes (Ethereum, Arc, Avalanche, Base).\n"
        "Read-only: no private keys, no signing, no transactions.\n\n"
        "Commands:\n"
        "/watch &lt;address&gt; - Scan all markets and watch active positions (max 3 addresses per chat)\n"
        "/rescan &lt;address&gt; - Rescan all markets for an address (max once per 5 min)\n"
        "/list - List monitored addresses and positions\n"
        "/remove &lt;address&gt; - Stop watching an address\n"
        "/privacy - View privacy information\n"
        "/delete - Remove all data for this chat\n"
        "/help - Show available commands"
    )


def format_help() -> str:
    return format_start()


def format_privacy() -> str:
    return (
        "Privacy Notice:\n\n"
        "This bot stores chat_id, wallet address, market list, and alert state only.\n"
        "No names, usernames, or message text are stored.\n"
        "Wallet addresses are public on chain but personal data under GDPR. "
        "Retention is until /remove or /delete, plus auto-removal if the bot is blocked.\n"
        "/delete removes all rows for this chat at any time."
    )


def format_scan_response(
    address: str,
    found: Sequence[tuple[Any, Any]],
    failed_count: int = 0,
    is_rescan: bool = False,
) -> str:
    link = profile_link(address)
    action = "Rescan completed" if is_rescan else "Now watching"

    if found:
        lines = [f"{action} for {link}. Found {len(found)} position(s):", ""]
        for market, data in found:
            emoji = hf_emoji(data.hf)
            m_name = html.escape(format_market_name(market))
            hf_val = format_hf(data.hf)
            gauge = hf_gauge(data.hf)
            lines.append(f"{emoji} <b>{m_name}</b>")
            lines.append(f"Health factor <b>{hf_val}</b>  {gauge}")
            if data.collateral_usd is not None and data.debt_usd is not None:
                lines.append(
                    f"Collateral {format_currency(data.collateral_usd)} · Debt {format_currency(data.debt_usd)}"
                )
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
    else:
        lines = [
            f"No open positions found for {link} across active markets.",
            "A daily rescan will watch for new positions.",
        ]

    if failed_count > 0:
        lines.append(
            f"{failed_count} market(s) could not be checked right now and will be retried in the daily rescan."
        )

    lines.extend(["", FOOTER])
    return "\n".join(lines)


def format_new_position(
    address: str,
    market: Any,
    hf: Decimal | None,
    collateral_usd: Decimal | None = None,
    debt_usd: Decimal | None = None,
) -> str:
    link = profile_link(address)
    emoji = hf_emoji(hf)
    m_name = html.escape(format_market_name(market))
    hf_val = format_hf(hf)
    gauge = hf_gauge(hf)
    lines = [
        f"Found a new position for {link}:",
        "",
        f"{emoji} <b>{m_name}</b>",
        f"Health factor <b>{hf_val}</b>  {gauge}",
    ]
    if collateral_usd is not None and debt_usd is not None:
        lines.append(
            f"Collateral {format_currency(collateral_usd)} · Debt {format_currency(debt_usd)}"
        )
    lines.extend(["", FOOTER])
    return "\n".join(lines)


def format_watch_success(
    address: str,
    hf: Decimal | None,
    collateral_usd: Decimal | None,
    debt_usd: Decimal | None,
    state: str,
    market: Any | None = None,
) -> str:
    short = format_short_address(address)
    hf_str = format_hf(hf)
    lines = [f"Now watching {short}."]
    if market is not None:
        lines.append(f"Market: {format_market_name(market)}")
    lines.extend([
        f"State: {state}",
        f"Health factor: {hf_str}",
    ])
    if collateral_usd is not None:
        lines.append(f"Collateral: {format_currency(collateral_usd)}")
    if debt_usd is not None:
        lines.append(f"Debt: {format_currency(debt_usd)}")
    lines.extend(["", FOOTER])
    return "\n".join(lines)


def format_watch_failure(address: str) -> str:
    short = format_short_address(address)
    return f"Saved watch for {short}. Could not read right now, I will keep trying."


def format_watch_limit() -> str:
    return "Limit reached: maximum 3 addresses per chat. Use /remove &lt;address&gt; or /delete."


def format_positions_limit() -> str:
    return "Limit reached: maximum 30 market positions per chat, so some positions are not being watched. Use /remove &lt;address&gt; to free space."


def format_invalid_address(msg: str = "") -> str:
    detail = f": {html.escape(msg)}" if msg else ""
    return f"Invalid Ethereum address{detail}. Must be a 0x-prefixed 40-hex address."


def format_list(watches: Sequence[Any]) -> str:
    if not watches:
        return "No addresses currently monitored. Use /watch &lt;address&gt; to add one."

    # Check if grouped: list of (address, list_of_positions)
    first = watches[0]
    if isinstance(first, tuple) and len(first) == 2 and isinstance(first[1], list):
        lines = ["Monitored addresses:"]
        for addr, positions in watches:
            link = profile_link(addr)
            lines.append(f"\n{link}:")
            if not positions:
                lines.append("  (no active market positions; watching in daily rescan)")
            else:
                for item in positions:
                    if isinstance(item, tuple):
                        if len(item) == 3:
                            m_name, state_or_hf, extra = item
                            emoji = state_emoji(str(state_or_hf))
                            lines.append(f"{emoji} {m_name}  HF {format_hf(extra)}")
                        elif len(item) == 2:
                            m_name, val = item
                            if isinstance(val, (int, float, Decimal)):
                                emoji = hf_emoji(val)
                                lines.append(f"{emoji} {m_name}  HF {format_hf(val)}")
                            else:
                                emoji = state_emoji(str(val))
                                lines.append(f"{emoji} {m_name}")
                        else:
                            lines.append(f"- {item[0]}")
                    else:
                        lines.append(f"- {item}")
        return "\n".join(lines)

    # Legacy flat format: list of (address, state)
    lines = ["Monitored addresses:"]
    for item in watches:
        if isinstance(item, tuple) and len(item) == 2:
            addr, state = item
            lines.append(f"{state_emoji(str(state))} {profile_link(addr)}")
    return "\n".join(lines)


def format_remove(address: str, removed: bool) -> str:
    short = html.escape(format_short_address(address))
    if removed:
        return f"Removed {short} from watch list."
    return f"Address {short} was not in your watch list."


def format_delete(count: int) -> str:
    return f"Deleted all data for this chat ({count} address{'es' if count != 1 else ''} removed)."


def format_rate_limit() -> str:
    return "Rate limit exceeded (10 commands per minute). Please wait a moment."


def format_rescan_rate_limit() -> str:
    return "Rescan rate limit reached. Please wait before rescanning again (max once per 5 minutes)."


def format_unknown() -> str:
    return "Unknown command. Use /help to see available commands."
