import logging
import signal
import threading
import time
import urllib.parse
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from typing import Any

from hfwb.aave import AccountData, ReadError, read_market
from hfwb.bot import BotHandler
from hfwb.format import format_alert, format_new_position
from hfwb.markets import Market, get_market, load_markets
from hfwb.scan import scan_address
from hfwb.schedule import INTERVAL_FAR, backoff_for, combine, poll_interval_for
from hfwb.state import step
from hfwb.store import (
    LimitError,
    add_watch,
    clear_failure,
    delete_chat,
    distinct_pairs,
    get_due_rescans,
    get_levels,
    list_watches,
    list_watches_by_address,
    record_failure,
    update_last_scan_ts,
    update_state,
)
from hfwb.telegram import Blocked, TelegramClient, TelegramError

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY_CAP = 5
DEFAULT_PER_HOST_CAP = 3
DEFAULT_POLL_INTERVAL = 300.0  # seconds (deprecated, kept for backwards compatibility)
DEFAULT_TICK_SECONDS = 15.0  # seconds
DEFAULT_RESCAN_INTERVAL = 86400.0  # 24 hours


class PollScheduler:
    """In-memory scheduler tracking next due timestamps and failure streaks per pair."""

    def __init__(self) -> None:
        self.next_due: dict[tuple[str, str], float] = {}
        self.fail_streak: dict[tuple[str, str], int] = {}

    def is_due(self, pair: tuple[str, str], now: float) -> bool:
        """A pair that is not known yet is due immediately; at startup everything is due."""
        if pair not in self.next_due:
            return True
        return now >= self.next_due[pair]

    def record_success(self, pair: tuple[str, str], next_poll_ts: float) -> None:
        """Reset fail streak and schedule next poll timestamp."""
        self.fail_streak.pop(pair, None)
        self.next_due[pair] = next_poll_ts

    def record_failure(self, pair: tuple[str, str], now: float) -> int:
        """Increment fail streak and calculate next due timestamp using backoff. Returns new streak."""
        streak = self.fail_streak.get(pair, 0) + 1
        self.fail_streak[pair] = streak
        self.next_due[pair] = now + backoff_for(streak)
        return streak

    def get_fail_streak(self, pair: tuple[str, str]) -> int:
        """Return the current consecutive failure streak for a pair."""
        return self.fail_streak.get(pair, 0)

    def prune(self, active_pairs: set[tuple[str, str]]) -> None:
        """Forget pairs that have disappeared from distinct_pairs()."""
        for p in list(self.next_due.keys()):
            if p not in active_pairs:
                del self.next_due[p]
        for p in list(self.fail_streak.keys()):
            if p not in active_pairs:
                del self.fail_streak[p]


def _extract_host(market: Market | None) -> str:
    if market is None or not market.rpcs:
        return "unknown"
    parsed = urllib.parse.urlparse(market.rpcs[0])
    return parsed.netloc or parsed.path or "unknown"


def _poll_core(
    db_path: str | Path,
    scheduler: PollScheduler | None = None,
    poll_all: bool = False,
    markets: Sequence[Market] | None = None,
    rpc_urls: Sequence[str] | None = None,
    tg_client: Any = None,
    reader_fn: Callable[..., AccountData] = read_market,
    clock: Callable[[], float] = time.time,
    heartbeat_file: str | Path | None = None,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    per_host_cap: int = DEFAULT_PER_HOST_CAP,
) -> None:
    now = clock()
    pairs = distinct_pairs(db_path)
    active_pairs_set = set(pairs)

    if scheduler is not None:
        scheduler.prune(active_pairs_set)

    if poll_all or scheduler is None:
        pairs_to_read = pairs
    else:
        pairs_to_read = [p for p in pairs if scheduler.is_due(p, now)]

    all_markets = list(markets) if markets is not None else load_markets()
    market_map = {m.key: m for m in all_markets}

    host_semaphores: dict[str, threading.Semaphore] = defaultdict(
        lambda: threading.Semaphore(per_host_cap)
    )

    def _read_pair(m_key: str, addr: str) -> tuple[str, str, AccountData | Exception]:
        m = market_map.get(m_key) or get_market(m_key)
        host = _extract_host(m)
        with host_semaphores[host]:
            try:
                if m is not None:
                    data = reader_fn(m, addr)
                else:
                    raise ReadError(f"Unknown market key: {m_key}")
                return (m_key, addr, data)
            except Exception as exc:  # noqa: BLE001 - one bad pair must not abort cycle
                return (m_key, addr, exc)

    results: dict[tuple[str, str], AccountData | Exception] = {}
    if pairs_to_read:
        max_workers = min(concurrency_cap, len(pairs_to_read)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_pair = {
                executor.submit(_read_pair, m_key, addr): (m_key, addr)
                for m_key, addr in pairs_to_read
            }
            for future in as_completed(future_to_pair):
                m_key, addr, outcome = future.result()
                results[(m_key, addr)] = outcome

    chat_levels_cache: dict[int, tuple[Decimal, ...]] = {}

    def _get_chat_levels(chat_id: int) -> tuple[Decimal, ...]:
        if chat_id not in chat_levels_cache:
            chat_levels_cache[chat_id] = get_levels(db_path, chat_id)
        return chat_levels_cache[chat_id]

    def _handle_result(m_key: str, addr: str, outcome: AccountData | Exception) -> None:
        market = market_map.get(m_key) or get_market(m_key)
        pair = (m_key, addr)

        if isinstance(outcome, Exception):
            if isinstance(outcome, ReadError):
                fail_count = record_failure(db_path, m_key, addr)
                if fail_count >= 3:
                    logger.error(
                        "Market %s address %s failed %d consecutive RPC reads: %s",
                        m_key,
                        addr,
                        fail_count,
                        outcome,
                    )
            else:
                logger.error(
                    "Unexpected error reading market %s address %s: %s",
                    m_key,
                    addr,
                    outcome,
                )
            if scheduler is not None:
                scheduler.record_failure(pair, now)
            # On error, keep previous state, send nothing to users
            return

        # Successful read
        clear_failure(db_path, m_key, addr)
        watches = list_watches_by_address(db_path, addr, market_key=m_key)
        blocked_chats: set[int] = set()
        for watch in watches:
            chat_levels = _get_chat_levels(watch.chat_id)
            new_state, alert = step(
                prev_state=watch.state,
                hf=outcome.hf,
                now_ts=now,
                last_alert_ts=watch.last_alert_ts,
                levels=chat_levels,
            )

            if alert is not None:
                alert_text = format_alert(
                    alert_type=alert,
                    address=addr,
                    hf=outcome.hf,
                    collateral_usd=outcome.collateral_usd,
                    debt_usd=outcome.debt_usd,
                    market=market,
                    levels=chat_levels,
                )
                try:
                    tg_client.send_message(watch.chat_id, alert_text)
                    update_state(
                        db_path,
                        watch.chat_id,
                        addr,
                        m_key,
                        new_state,
                        last_alert_ts=now,
                    )
                except Blocked:
                    logger.info("Bot blocked by user %s; deleting all chat data", watch.chat_id)
                    delete_chat(db_path, watch.chat_id)
                    blocked_chats.add(watch.chat_id)
                except TelegramError as exc:
                    # Keep previous state so next poll retries this alert
                    logger.warning("Failed to send alert to %s: %s", watch.chat_id, exc)
            elif new_state != watch.state:
                update_state(db_path, watch.chat_id, addr, m_key, new_state)

        if scheduler is not None:
            active_watches = [w for w in watches if w.chat_id not in blocked_chats]
            if active_watches:
                intervals = [
                    poll_interval_for(outcome.hf, _get_chat_levels(w.chat_id))
                    for w in active_watches
                ]
                next_interval = combine(intervals)
            else:
                next_interval = INTERVAL_FAR
            scheduler.record_success(pair, now + next_interval)

    for (m_key, addr), outcome in results.items():
        try:
            _handle_result(m_key, addr, outcome)
        except Exception:  # top-level guard per pair: one bug must not skip the other pairs or cause a re-read every tick
            logger.exception("Unexpected error while processing market %s address %s", m_key, addr)
            if scheduler is not None:
                scheduler.record_failure((m_key, addr), now)

    if heartbeat_file is not None:
        hb_path = Path(heartbeat_file)
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(f"{int(now)}\n", encoding="utf-8")


def poll_due(
    db_path: str | Path,
    scheduler: PollScheduler | None = None,
    markets: Sequence[Market] | None = None,
    rpc_urls: Sequence[str] | None = None,
    tg_client: Any = None,
    reader_fn: Callable[..., AccountData] = read_market,
    clock: Callable[[], float] = time.time,
    heartbeat_file: str | Path | None = None,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    per_host_cap: int = DEFAULT_PER_HOST_CAP,
) -> None:
    """Execute adaptive polling cycle across due (market, address) pairs."""
    if scheduler is not None and not isinstance(scheduler, PollScheduler):
        markets = scheduler  # type: ignore[assignment]
        scheduler = PollScheduler()
    elif scheduler is None:
        scheduler = PollScheduler()

    return _poll_core(
        db_path=db_path,
        scheduler=scheduler,
        poll_all=False,
        markets=markets,
        rpc_urls=rpc_urls,
        tg_client=tg_client,
        reader_fn=reader_fn,
        clock=clock,
        heartbeat_file=heartbeat_file,
        concurrency_cap=concurrency_cap,
        per_host_cap=per_host_cap,
    )


def poll_once(
    db_path: str | Path,
    markets: Sequence[Market] | None = None,
    rpc_urls: Sequence[str] | None = None,
    tg_client: Any = None,
    reader_fn: Callable[..., AccountData] = read_market,
    clock: Callable[[], float] = time.time,
    heartbeat_file: str | Path | None = None,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    per_host_cap: int = DEFAULT_PER_HOST_CAP,
    scheduler: PollScheduler | None = None,
) -> None:
    """Execute a single polling cycle across all distinct (market, address) pairs."""
    return _poll_core(
        db_path=db_path,
        scheduler=scheduler,
        poll_all=True,
        markets=markets,
        rpc_urls=rpc_urls,
        tg_client=tg_client,
        reader_fn=reader_fn,
        clock=clock,
        heartbeat_file=heartbeat_file,
        concurrency_cap=concurrency_cap,
        per_host_cap=per_host_cap,
    )


def rescan_due_addresses(
    db_path: str | Path,
    tg_client: Any,
    markets: Sequence[Market] | None = None,
    scanner_fn: Callable[..., Any] = scan_address,
    reader_fn: Callable[..., AccountData] = read_market,
    clock: Callable[[], float] = time.time,
    interval_seconds: float = DEFAULT_RESCAN_INTERVAL,
) -> None:
    """Scan tracked addresses that are due for daily rescan (older than 24h)."""
    now = clock()
    due_records = get_due_rescans(db_path, now, interval_seconds=interval_seconds)
    if not due_records:
        return

    all_markets = list(markets) if markets is not None else load_markets()
    chat_levels_cache: dict[int, tuple[Decimal, ...]] = {}

    def _get_chat_levels(chat_id: int) -> tuple[Decimal, ...]:
        if chat_id not in chat_levels_cache:
            chat_levels_cache[chat_id] = get_levels(db_path, chat_id)
        return chat_levels_cache[chat_id]

    # Group due records by address
    addrs_to_chats: dict[str, list[int]] = defaultdict(list)
    for rec in due_records:
        addrs_to_chats[rec.address].append(rec.chat_id)

    for addr, chat_ids in addrs_to_chats.items():
        try:
            scan_res = scanner_fn(addr, all_markets, reader=reader_fn)
            for chat_id in chat_ids:
                chat_levels = _get_chat_levels(chat_id)
                existing_watches = list_watches(db_path, chat_id, address=addr)
                watched_keys = {w.market_key for w in existing_watches}

                for market, acct in scan_res.found:
                    if market.key not in watched_keys:
                        initial_state, _ = step("ok", acct.hf, now_ts=now, last_alert_ts=None, levels=chat_levels)
                        try:
                            add_watch(db_path, chat_id, addr, market.key, state=initial_state)
                        except LimitError:
                            logger.warning("Chat %s is at its position limit; skipping %s", chat_id, market.key)
                            continue
                        msg = format_new_position(
                            addr,
                            market,
                            acct.hf,
                            collateral_usd=acct.collateral_usd,
                            debt_usd=acct.debt_usd,
                            levels=chat_levels,
                        )
                        try:
                            tg_client.send_message(chat_id, msg)
                        except Blocked:
                            logger.info("Bot blocked by user %s during rescan; deleting chat", chat_id)
                            delete_chat(db_path, chat_id)
                        except TelegramError as exc:
                            logger.warning("Failed to send new position alert to %s: %s", chat_id, exc)

                # a scan with unchecked markets stays due, so the next rescan retries it
                if not scan_res.failed:
                    update_last_scan_ts(db_path, chat_id, addr, now)
        except Exception:
            logger.exception("Error during rescan for address %s", addr)


def run_poll_loop(
    stop_event: threading.Event,
    db_path: str | Path,
    markets: Sequence[Market] | None = None,
    rpc_urls: Sequence[str] | None = None,
    tg_client: Any = None,
    heartbeat_file: str | Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    poll_interval: float | None = None,
    clock: Callable[[], float] = time.time,
    reader_fn: Callable[..., AccountData] = read_market,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
    per_host_cap: int = DEFAULT_PER_HOST_CAP,
    scheduler: PollScheduler | None = None,
) -> None:
    """Run recurring poll loop in background ticking every tick_seconds until stop_event is set."""
    effective_tick = poll_interval if poll_interval is not None else tick_seconds
    if scheduler is None:
        scheduler = PollScheduler()

    logger.info("Starting poll loop (tick=%.1fs)", effective_tick)
    while not stop_event.is_set():
        try:
            poll_due(
                db_path=db_path,
                scheduler=scheduler,
                markets=markets,
                rpc_urls=rpc_urls,
                tg_client=tg_client,
                reader_fn=reader_fn,
                clock=clock,
                heartbeat_file=heartbeat_file,
                concurrency_cap=concurrency_cap,
                per_host_cap=per_host_cap,
            )
        except Exception:  # top-level guard: a bug must not silently stop alerting
            logger.exception("Unhandled error in poll_due")

        stop_event.wait(effective_tick)
    logger.info("Poll loop stopped")


def run_rescan_loop(
    stop_event: threading.Event,
    db_path: str | Path,
    tg_client: Any,
    markets: Sequence[Market] | None = None,
    check_interval: float = 300.0,
    scanner_fn: Callable[..., Any] = scan_address,
    reader_fn: Callable[..., AccountData] = read_market,
    clock: Callable[[], float] = time.time,
    rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
) -> None:
    """Run recurring daily rescan worker in background until stop_event is set."""
    logger.info("Starting rescan loop (check_interval=%.1fs)", check_interval)
    while not stop_event.is_set():
        try:
            rescan_due_addresses(
                db_path=db_path,
                tg_client=tg_client,
                markets=markets,
                scanner_fn=scanner_fn,
                reader_fn=reader_fn,
                clock=clock,
                interval_seconds=rescan_interval,
            )
        except Exception:
            logger.exception("Unhandled error in rescan_due_addresses")

        stop_event.wait(check_interval)
    logger.info("Rescan loop stopped")


def run_command_loop(
    stop_event: threading.Event,
    bot_handler: BotHandler,
    tg_client: TelegramClient,
    poll_timeout: int = 50,
) -> None:
    """Run long-polling command loop until stop_event is set."""
    logger.info("Starting command loop (timeout=%ds)", poll_timeout)
    offset: int | None = None
    while not stop_event.is_set():
        try:
            updates = tg_client.get_updates(offset=offset, timeout=poll_timeout)
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = update_id + 1

                replies = bot_handler.handle(update)
                for chat_id, text in replies:
                    try:
                        tg_client.send_message(chat_id, text)
                    except Blocked:
                        logger.info("Bot blocked by user %s; deleting chat", chat_id)
                        delete_chat(bot_handler.db_path, chat_id)
                    except TelegramError as exc:
                        logger.warning("Failed to send reply to %s: %s", chat_id, exc)
        except TelegramError as exc:
            if stop_event.is_set():
                break
            logger.error("Telegram error in command loop: %s", exc)
            stop_event.wait(5.0)
        except Exception:  # top-level guard: keep serving commands after a bug
            if stop_event.is_set():
                break
            logger.exception("Unexpected error in command loop")
            stop_event.wait(5.0)
    logger.info("Command loop stopped")


def run_all(
    db_path: str | Path,
    tg_client: TelegramClient,
    markets: Sequence[Market] | None = None,
    rpc_urls: Sequence[str] | None = None,
    heartbeat_file: str | Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    poll_interval: float | None = None,
    poll_timeout: int = 50,
) -> None:
    """Run poll loop, daily rescan loop, and command loop with graceful shutdown."""
    stop_event = threading.Event()

    def _sig_handler(signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating graceful shutdown...", sig_name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    all_markets = list(markets) if markets is not None else load_markets()
    bot_handler = BotHandler(db_path=db_path, markets=all_markets, rpc_urls=rpc_urls or ())

    effective_tick = poll_interval if poll_interval is not None else tick_seconds

    poll_thread = threading.Thread(
        target=run_poll_loop,
        kwargs={
            "stop_event": stop_event,
            "db_path": db_path,
            "markets": all_markets,
            "rpc_urls": rpc_urls,
            "tg_client": tg_client,
            "heartbeat_file": heartbeat_file,
            "tick_seconds": effective_tick,
        },
        daemon=True,
    )
    poll_thread.start()

    rescan_thread = threading.Thread(
        target=run_rescan_loop,
        kwargs={
            "stop_event": stop_event,
            "db_path": db_path,
            "tg_client": tg_client,
            "markets": all_markets,
        },
        daemon=True,
    )
    rescan_thread.start()

    try:
        run_command_loop(
            stop_event=stop_event,
            bot_handler=bot_handler,
            tg_client=tg_client,
            poll_timeout=poll_timeout,
        )
    finally:
        stop_event.set()
        poll_thread.join(timeout=10.0)
        rescan_thread.join(timeout=5.0)
        logger.info("Shutdown complete")
