import html
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from hfwb.depeg import ORACLE_NOT_INDEPENDENT
from hfwb.state import DEFAULT_LEVELS, LEVELS

FOOTER = "<i>Informational only, not financial advice.</i>"
DEPEG_FOOTER = "<i>Aggregated market price; it can differ between exchanges and chains. Informational only, not financial advice.</i>"

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


def level_emoji(k: int, n: int) -> str:
    """Map 1-indexed level k out of n total levels to an emoji based on distance from the bottom.

    r = n - k: r=0 -> 🚨, r=1 -> 🔴, r=2 -> 🟠, r>=3 -> 🟡.
    """
    r = n - k
    if r <= 0:
        return "🚨"
    if r == 1:
        return "🔴"
    if r == 2:
        return "🟠"
    return "🟡"


def hf_emoji(hf: Decimal | None, levels: Sequence[Decimal] = DEFAULT_LEVELS) -> str:
    """Return health factor emoji.

    None (no debt) or hf >= levels[0] -> 🟢, otherwise the emoji of the deepest level the hf is below.
    """
    if hf is None:
        return "🟢"
    val = Decimal(str(hf))
    if val >= levels[0]:
        return "🟢"
    deepest = 1
    for i, thresh in enumerate(levels, start=1):
        if val < thresh:
            deepest = i
        else:
            break
    return level_emoji(deepest, len(levels))


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
    levels: Sequence[Decimal] = DEFAULT_LEVELS,
) -> str:
    """Format an alert notification naming the level reached and the market in HTML."""
    emoji = hf_emoji(hf, levels=levels)
    if alert_type == "recovery":
        emoji = "🟢"

    m_name = html.escape(format_market_name(market)) if market is not None else "Position"
    lines = [f"{emoji} <b>{m_name}</b>"]

    hf_str = format_hf(hf)
    gauge = hf_gauge(hf)
    lines.append(f"Health factor <b>{hf_str}</b>  {gauge}")

    n = len(levels)
    is_critical = False
    if alert_type.startswith("L") and alert_type[1:].isdigit():
        k = int(alert_type[1:])
        thresh = levels[k - 1] if 1 <= k <= n else levels[-1]
        lines.append(f"Fell below <b>{thresh}</b>")
        if k >= (n - 1):
            is_critical = True
    elif alert_type.startswith("repeat_"):
        lvl = alert_type.removeprefix("repeat_")
        if lvl.startswith("L") and lvl[1:].isdigit():
            k = int(lvl[1:])
            thresh = levels[k - 1] if 1 <= k <= n else levels[-1]
            lines.append(f"Still below <b>{thresh}</b>")
            lines.append("Next reminder in 30 min")
            if k >= (n - 1):
                is_critical = True
        else:
            lines.append(f"Update: <b>{html.escape(alert_type)}</b>")
    elif alert_type == "recovery":
        lines.append(f"Back above <b>{levels[0]}</b>")
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
        "/levels [levels|reset] - View or set custom alert levels per chat\n"
        "/depeg [on|off] - Configure stablecoin depeg alerts (default off)\n"
        "/donate - Support the bot with a voluntary donation\n"
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
        "This bot stores chat_id, wallet address, market list, and alert state only "
        "(plus subscribed token symbols for stablecoin depeg alerts).\n"
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
    levels: Sequence[Decimal] = DEFAULT_LEVELS,
) -> str:
    link = profile_link(address)
    action = "Rescan completed" if is_rescan else "Now watching"

    if found:
        lines = [f"{action} for {link}. Found {len(found)} position(s):", ""]
        for market, data in found:
            emoji = hf_emoji(data.hf, levels=levels)
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
    levels: Sequence[Decimal] = DEFAULT_LEVELS,
) -> str:
    link = profile_link(address)
    emoji = hf_emoji(hf, levels=levels)
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


def format_list(watches: Sequence[Any], levels: Sequence[Decimal] = DEFAULT_LEVELS) -> str:
    lvl_line = f"Levels: {' / '.join(str(lvl) for lvl in levels)}"
    if tuple(levels) == DEFAULT_LEVELS:
        lvl_line += " (default)"

    if not watches:
        return f"No addresses currently monitored. Use /watch &lt;address&gt; to add one.\n\n{lvl_line}"

    def _state_or_hf_emoji(val: Any) -> str:
        if isinstance(val, (int, float, Decimal)):
            return hf_emoji(val, levels=levels)
        st = str(val)
        if st == "ok":
            return "🟢"
        if st.startswith("L") and st[1:].isdigit():
            return level_emoji(int(st[1:]), len(levels))
        return state_emoji(st)

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
                            emoji = _state_or_hf_emoji(state_or_hf)
                            lines.append(f"{emoji} {m_name}  HF {format_hf(extra)}")
                        elif len(item) == 2:
                            m_name, val = item
                            emoji = _state_or_hf_emoji(val)
                            if isinstance(val, (int, float, Decimal)):
                                lines.append(f"{emoji} {m_name}  HF {format_hf(val)}")
                            else:
                                lines.append(f"{emoji} {m_name}")
                        else:
                            lines.append(f"- {item[0]}")
                    else:
                        lines.append(f"- {item}")
        lines.extend(["", lvl_line])
        return "\n".join(lines)

    # Legacy flat format: list of (address, state)
    lines = ["Monitored addresses:"]
    for item in watches:
        if isinstance(item, tuple) and len(item) == 2:
            addr, state = item
            lines.append(f"{_state_or_hf_emoji(state)} {profile_link(addr)}")
    lines.extend(["", lvl_line])
    return "\n".join(lines)


def format_levels(levels: Sequence[Decimal], is_default: bool) -> str:
    """Format current alert levels display."""
    lvl_str = " / ".join(str(lvl) for lvl in levels)
    suffix = " (default)" if is_default else ""
    return (
        f"Alert levels: <b>{lvl_str}</b>{suffix}\n\n"
        "Use <code>/levels &lt;l1&gt; &lt;l2&gt; ...</code> to change (2 to 5 levels, descending, min gap 0.04).\n"
        "Use <code>/levels reset</code> to restore default levels."
    )


def format_levels_error(msg: str) -> str:
    """Format error message for invalid alert levels input."""
    return f"Invalid alert levels: {html.escape(msg)}\nUse /levels to view requirements."


def format_levels_saved(levels: Sequence[Decimal]) -> str:
    """Format confirmation message after saving custom alert levels."""
    lvl_str = " / ".join(str(lvl) for lvl in levels)
    return (
        f"Alert levels updated to <b>{lvl_str}</b>.\n\n"
        "Watch states were re-armed. An alert may arrive immediately if a position is currently below a level."
    )


def format_levels_reset() -> str:
    """Format confirmation message after resetting alert levels to default."""
    lvl_str = " / ".join(str(lvl) for lvl in DEFAULT_LEVELS)
    return (
        f"Alert levels reset to default (<b>{lvl_str}</b>).\n\n"
        "Watch states were re-armed. An alert may arrive immediately if a position is currently below a level."
    )


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


def format_depeg_alert(
    alert_type: str,
    symbol: str,
    price: Decimal,
    readings_age_s: float,
    oracle_price: Decimal | None = None,
) -> str:
    """Format stablecoin depeg alert in HTML."""
    emoji_map = {
        "D1": "🟡",
        "D2": "🟠",
        "D3": "🔴",
        "D4": "🚨",
        "repeat_D3": "🔴",
        "repeat_D4": "🚨",
        "recovery": "🟢",
    }
    emoji = emoji_map.get(alert_type, "🟡")
    esc_symbol = html.escape(symbol)

    lines = [f"{emoji} <b>{esc_symbol} price alert</b>"]

    pct = abs(price - Decimal(1)) * 100
    direction = "below" if price < Decimal(1) else "above"
    lines.append(f"Price <b>${price:.4f}</b> ({pct:.2f}% {direction} $1.00)")

    verb = "Fell" if price < Decimal(1) else "Rose"
    if alert_type == "D1":
        lines.append(f"{verb} past <b>0.5%</b> from the peg")
    elif alert_type == "D2":
        lines.append(f"{verb} past <b>1%</b> from the peg")
    elif alert_type == "D3":
        lines.append(f"{verb} past <b>2%</b> from the peg")
    elif alert_type == "D4":
        lines.append(f"{verb} past <b>5%</b> from the peg")
    elif alert_type == "repeat_D3":
        lines.append("Still more than 2% from the peg")
    elif alert_type == "repeat_D4":
        lines.append("Still more than 5% from the peg")
    elif alert_type == "recovery":
        lines.append("Back within 0.3% of $1.00")
    else:
        lines.append(f"Status: <b>{html.escape(alert_type)}</b>")

    age_m = int(readings_age_s // 60)
    if age_m <= 0:
        age_str = "just now"
    elif age_m == 1:
        age_str = "1 min ago"
    else:
        age_str = f"{age_m} min ago"
    lines.append(f"Market price (DefiLlama), updated {age_str}")

    if symbol in ORACLE_NOT_INDEPENDENT:
        lines.append("Aave oracle: not independent (fixed or proxy price)")
    elif oracle_price is not None:
        lines.append(f"Aave oracle: ${oracle_price:.4f}")

    if alert_type in ("D3", "D4", "repeat_D3", "repeat_D4"):
        lines.append("<b>Large move</b>")

    lines.append("")
    lines.append(DEPEG_FOOTER)
    return "\n".join(lines)


def format_depeg_status(
    subs: Sequence[Any],
    readings: dict[str, Any] | None = None,
) -> str:
    """Format status of stablecoin depeg alert subscriptions."""
    if not subs:
        return (
            "Stablecoin depeg alerts are currently <b>off</b>.\n\n"
            "Use <code>/depeg on</code> to subscribe to all supported stablecoins, "
            "or <code>/depeg on &lt;symbols&gt;</code> to choose specific tokens."
        )

    lines = ["<b>Stablecoin depeg alerts</b>\n\nSubscribed tokens:"]
    emoji_map = {
        "ok": "🟢",
        "D1": "🟡",
        "D2": "🟠",
        "D3": "🔴",
        "D4": "🚨",
    }
    for sub in subs:
        sym = getattr(sub, "symbol", None) or (sub[0] if isinstance(sub, (list, tuple)) else str(sub))
        st = getattr(sub, "state", None) or (sub[1] if isinstance(sub, (list, tuple)) and len(sub) > 1 else "ok")
        emoji = emoji_map.get(st, "🟢")
        esc_sym = html.escape(str(sym))
        esc_st = html.escape(str(st))
        line = f"{emoji} <b>{esc_sym}</b> ({esc_st})"
        if readings and sym in readings:
            r = readings[sym]
            price_val = getattr(r, "price", r)
            line += f"  ${price_val:.4f}"
        lines.append(line)

    lines.extend([
        "",
        "Use <code>/depeg on [symbols]</code> or <code>/depeg off [symbols]</code> to modify.",
        "",
        DEPEG_FOOTER,
    ])
    return "\n".join(lines)


def format_depeg_on(symbols: Sequence[str]) -> str:
    """Format confirmation message after enabling depeg alerts."""
    esc = ", ".join(html.escape(s) for s in symbols)
    return (
        f"Subscribed to stablecoin depeg alerts for <b>{esc}</b>.\n\n"
        "Alerts trigger when price deviates by 0.5% or more from $1.00.\n\n"
        f"{DEPEG_FOOTER}"
    )


def format_depeg_off(symbols: Sequence[str] | None = None) -> str:
    """Format confirmation message after disabling depeg alerts."""
    if not symbols:
        return "Stablecoin depeg alerts turned <b>off</b> for all tokens."
    esc = ", ".join(html.escape(s) for s in symbols)
    return f"Removed stablecoin depeg alerts for <b>{esc}</b>."


def format_depeg_error(msg: str, supported: Sequence[str]) -> str:
    """Format error message for invalid depeg command input."""
    esc_msg = html.escape(msg)
    esc_sup = ", ".join(html.escape(s) for s in supported)
    return f"Invalid stablecoin symbol: <b>{esc_msg}</b>\n\nSupported stablecoins: {esc_sup}"


def format_donate(address: str | None, note: str | None = None) -> str:
    """Format /donate response message."""
    if not address:
        return "Donations are not set up for this bot."
    lines = [
        "This bot is free and open source. If it is useful to you, you can support it with a donation. It is voluntary and gives no extra features or guarantees."
    ]
    if note:
        lines.append(f"Network and token: {html.escape(note)}")
    lines.append(f"Address (tap to copy):\n<code>{html.escape(address)}</code>")
    lines.append("Please check the network before you send.")
    return "\n\n".join(lines)

