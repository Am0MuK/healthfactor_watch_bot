import html
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hfwb.aave import AccountData, normalize_address, read_market
from hfwb.depeg import STABLES
from hfwb.format import (
    format_delete,
    format_depeg_error,
    format_depeg_off,
    format_depeg_on,
    format_depeg_status,
    format_donate,
    format_help,
    format_invalid_address,
    format_levels,
    format_levels_error,
    format_levels_reset,
    format_levels_saved,
    format_list,
    format_market_name,
    format_positions_limit,
    format_privacy,
    format_rate_limit,
    format_remove,
    format_rescan_rate_limit,
    format_scan_response,
    format_start,
    format_unknown,
    format_watch_limit,
)
from hfwb.markets import Market, get_market, load_markets
from hfwb.pricedrop import liquidation_lines, parse_whatif, run_whatif
from hfwb.scan import ScanResult, scan_address
from hfwb.state import DEFAULT_LEVELS, step, validate_levels
from hfwb.store import (
    LimitError,
    add_depeg_subs,
    add_tracked_address,
    add_watch,
    delete_chat,
    get_levels,
    list_depeg_subs,
    list_tracked_addresses,
    list_watches,
    remove_address,
    remove_depeg_subs,
    reset_levels,
    set_levels,
    update_last_scan_ts,
)

RATE_LIMIT_COMMANDS = 10
RATE_LIMIT_WINDOW = 60.0  # seconds
RESCAN_RATE_LIMIT_WINDOW = 300.0  # 5 minutes in seconds
WHATIF_COOLDOWN = 10.0  # seconds


class BotHandler:
    """Processes incoming Telegram updates and executes bot commands."""

    def __init__(
        self,
        db_path: str | Path,
        rpc_urls: Sequence[str] = (),
        markets: Sequence[Market] | None = None,
        clock: Callable[[], float] = time.time,
        aave_reader: Callable[..., AccountData] = read_market,
        scanner_fn: Callable[..., ScanResult] = scan_address,
        donate_address: str | None = None,
        donate_note: str | None = None,
        positions_reader: Callable[..., Any] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.rpc_urls = rpc_urls
        self.markets = list(markets) if markets is not None else load_markets()
        self.clock = clock
        self.aave_reader = aave_reader
        self.scanner_fn = scanner_fn
        self.donate_address = donate_address
        self.donate_note = donate_note
        self.positions_reader = positions_reader
        self._history: dict[int, list[float]] = defaultdict(list)
        self._rescan_history: dict[int, float] = {}
        self._whatif_history: dict[int, float] = {}

    def _is_rate_limited(self, chat_id: int) -> bool:
        now = self.clock()
        window_start = now - RATE_LIMIT_WINDOW
        timestamps = [ts for ts in self._history[chat_id] if ts > window_start]
        self._history[chat_id] = timestamps

        if len(timestamps) >= RATE_LIMIT_COMMANDS:
            return True

        self._history[chat_id].append(now)
        return False

    def handle(self, update: dict[str, Any]) -> list[tuple[int, str]]:
        """Handle a single Telegram update dict and return list of (chat_id, text) replies."""
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return []

        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            return []
        chat_id = chat["id"]

        text = message.get("text", "")
        if not isinstance(text, str):
            return []
        text = text.strip()
        if not text:
            return []

        if self._is_rate_limited(chat_id):
            return [(chat_id, format_rate_limit())]

        tokens = text.split()
        cmd = tokens[0].split("@")[0].lower()

        if cmd == "/start":
            return [(chat_id, format_start())]
        if cmd == "/help":
            return [(chat_id, format_help())]
        if cmd == "/privacy":
            return [(chat_id, format_privacy())]
        if cmd == "/delete":
            count = delete_chat(self.db_path, chat_id)
            return [(chat_id, format_delete(count))]

        if cmd == "/levels":
            arg_str = text[len(tokens[0]):].strip()
            if not arg_str:
                current_levels = get_levels(self.db_path, chat_id)
                is_default = (current_levels == DEFAULT_LEVELS)
                return [(chat_id, format_levels(current_levels, is_default))]

            if arg_str.lower() == "reset":
                reset_levels(self.db_path, chat_id)
                return [(chat_id, format_levels_reset())]

            raw_tokens = [tok for tok in re.split(r"[\s,]+", arg_str) if tok]
            try:
                new_levels = validate_levels(raw_tokens)
            except ValueError as exc:
                return [(chat_id, format_levels_error(str(exc)))]

            set_levels(self.db_path, chat_id, new_levels)
            return [(chat_id, format_levels_saved(new_levels))]

        if cmd == "/depeg":
            arg_str = text[len(tokens[0]):].strip()
            if not arg_str:
                subs = list_depeg_subs(self.db_path, chat_id)
                return [(chat_id, format_depeg_status(subs))]

            raw_tokens = [tok for tok in re.split(r"[\s,]+", arg_str) if tok]
            sub_cmd = raw_tokens[0].lower()
            symbols_args = raw_tokens[1:]
            canonical_map = {k.lower(): k for k in STABLES}

            if sub_cmd == "on":
                if not symbols_args:
                    add_depeg_subs(self.db_path, chat_id, list(STABLES.keys()))
                    return [(chat_id, format_depeg_on(list(STABLES.keys())))]

                valid_symbols: list[str] = []
                for s in symbols_args:
                    s_clean = s.strip().lower()
                    if s_clean not in canonical_map:
                        return [(chat_id, format_depeg_error(s, list(STABLES.keys())))]
                    canon = canonical_map[s_clean]
                    if canon not in valid_symbols:
                        valid_symbols.append(canon)

                add_depeg_subs(self.db_path, chat_id, valid_symbols)
                return [(chat_id, format_depeg_on(valid_symbols))]

            if sub_cmd == "off":
                if not symbols_args:
                    remove_depeg_subs(self.db_path, chat_id, None)
                    return [(chat_id, format_depeg_off())]

                valid_symbols = []
                for s in symbols_args:
                    s_clean = s.strip().lower()
                    if s_clean not in canonical_map:
                        return [(chat_id, format_depeg_error(s, list(STABLES.keys())))]
                    canon = canonical_map[s_clean]
                    if canon not in valid_symbols:
                        valid_symbols.append(canon)

                remove_depeg_subs(self.db_path, chat_id, valid_symbols)
                return [(chat_id, format_depeg_off(valid_symbols))]

            return [(chat_id, format_depeg_error(raw_tokens[0], list(STABLES.keys())))]

        if cmd == "/donate":
            return [(chat_id, format_donate(self.donate_address, self.donate_note))]

        if cmd == "/list":
            chat_levels = get_levels(self.db_path, chat_id)
            tracked = list_tracked_addresses(self.db_path, chat_id)
            watches = list_watches(self.db_path, chat_id)
            if not tracked and not watches:
                return [(chat_id, format_list([], levels=chat_levels))]

            watches_by_addr: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for w in watches:
                m = get_market(w.market_key)
                m_name = format_market_name(m) if m else w.market_key
                watches_by_addr[w.address].append((m_name, w.state))

            grouped: list[tuple[str, list[tuple[str, str]]]] = []
            seen_addrs: set[str] = set()
            for t in tracked:
                grouped.append((t.address, watches_by_addr.get(t.address, [])))
                seen_addrs.add(t.address)

            for addr, w_list in watches_by_addr.items():
                if addr not in seen_addrs:
                    grouped.append((addr, w_list))

            return [(chat_id, format_list(grouped, levels=chat_levels))]

        if cmd == "/remove":
            if len(tokens) < 2:
                return [(chat_id, "Usage: /remove &lt;address&gt;")]
            try:
                norm_addr = normalize_address(tokens[1])
            except ValueError as exc:
                return [(chat_id, format_invalid_address(str(exc)))]
            removed = remove_address(self.db_path, chat_id, norm_addr)
            return [(chat_id, format_remove(norm_addr, removed))]

        if cmd in ("/watch", "/rescan"):
            is_rescan = cmd == "/rescan"
            if len(tokens) < 2:
                return [(chat_id, f"Usage: {cmd} &lt;address&gt;")]
            try:
                norm_addr = normalize_address(tokens[1])
            except ValueError as exc:
                return [(chat_id, format_invalid_address(str(exc)))]

            now = self.clock()

            if is_rescan:
                last_rescan = self._rescan_history.get(chat_id)
                if last_rescan is not None and (now - last_rescan) < RESCAN_RATE_LIMIT_WINDOW:
                    return [(chat_id, format_rescan_rate_limit())]
                self._rescan_history[chat_id] = now

            try:
                add_tracked_address(self.db_path, chat_id, norm_addr)
            except LimitError:
                return [(chat_id, format_watch_limit())]

            scan_res = self.scanner_fn(norm_addr, self.markets, reader=self.aave_reader)

            chat_levels = get_levels(self.db_path, chat_id)
            limit_hit = False
            for market, acct in scan_res.found:
                initial_state, _ = step("ok", acct.hf, now_ts=now, last_alert_ts=None, levels=chat_levels)
                try:
                    add_watch(self.db_path, chat_id, norm_addr, market.key, state=initial_state)
                except LimitError:
                    limit_hit = True
                    break

            # a scan with unchecked markets stays due, so the next rescan retries it
            if not scan_res.failed:
                update_last_scan_ts(self.db_path, chat_id, norm_addr, now)

            details: dict[str, list[str]] = {}
            if self.positions_reader is not None:
                for market, acct in scan_res.found:
                    lines = liquidation_lines(
                        market, norm_addr, acct, positions_reader=self.positions_reader
                    )
                    if lines:
                        details[market.key] = lines

            text = format_scan_response(
                norm_addr,
                scan_res.found,
                len(scan_res.failed),
                is_rescan=is_rescan,
                levels=chat_levels,
                details=details if details else None,
            )
            if limit_hit:
                text += "\n\n" + format_positions_limit()
            return [(chat_id, text)]

        if cmd == "/whatif":
            raw_args = tokens[1:]
            try:
                changes = parse_whatif(raw_args)
            except ValueError as exc:
                msg = (
                    "Usage: /whatif ETH -20 (asset and % change, up to 5 pairs; ETH and BTC include their wrapped and staked versions; 'market' moves all crypto)"
                    f"\n\nError: {html.escape(str(exc))}"
                )
                return [(chat_id, msg)]

            now = self.clock()
            last_whatif = self._whatif_history.get(chat_id)
            if last_whatif is not None and (now - last_whatif) < WHATIF_COOLDOWN:
                return [(chat_id, "Please wait a few seconds between /whatif commands.")]
            self._whatif_history[chat_id] = now

            if self.positions_reader is None:
                return [(chat_id, "/whatif is not available right now.")]

            watches = list_watches(self.db_path, chat_id)
            if not watches:
                return [
                    (
                        chat_id,
                        "No watched positions found. Use /watch &lt;address&gt; to track a position first.",
                    )
                ]

            market_map = {m.key: m for m in self.markets}
            positions: list[tuple[Market, str]] = []
            for w in watches:
                m = market_map.get(w.market_key) or get_market(w.market_key)
                if m is not None:
                    positions.append((m, w.address))

            reply = run_whatif(
                changes=changes,
                positions=positions,
                account_reader=self.aave_reader,
                positions_reader=self.positions_reader,
                levels=get_levels(self.db_path, chat_id),
            )
            return [(chat_id, reply)]

        return [(chat_id, format_unknown())]
