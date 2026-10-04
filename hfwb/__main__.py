import argparse
import logging
import sys
from pathlib import Path

import httpx

from hfwb.aave import read_market
from hfwb.config import ConfigError, get_db_path, load_config
from hfwb.markets import load_markets
from hfwb.runner import run_all
from hfwb.store import get_stats, init_db
from hfwb.telegram import TelegramClient, TelegramError

logger = logging.getLogger("hfwb")

DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def get_http_client() -> httpx.Client | None:
    """Factory for HTTP client; can be replaced in tests."""
    return None


def verify_markets() -> int:
    """Verify live connectivity for all configured markets.

    Returns 0 if all succeed, 1 if any fail.
    Never prints RPC URLs with secret keys.
    """
    markets = load_markets()
    http_client = get_http_client()
    failed_count = 0

    for market in markets:
        try:
            read_market(market, DEAD_ADDRESS, client=http_client)
            print(f"ok   {market.chain} - {market.name}")
        except Exception:  # noqa: BLE001 - never print RPC URLs or keys on failure
            print(f"FAIL {market.chain} - {market.name}")
            failed_count += 1

    return 1 if failed_count > 0 else 0


def print_stats(db_path: Path | str) -> int:
    """Print anonymous usage statistics from the database only."""
    stats = get_stats(db_path)
    print(f"Chats with tracked addresses: {stats['chats_with_tracked_addresses']}")
    print(f"Watches: {stats['watches']}")
    print(f"Depeg alerts: {stats['depeg_subs']}")
    print(f"Custom levels chats: {stats['custom_levels_chats']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entrypoint for healthfactor_watch_bot."""
    parser = argparse.ArgumentParser(description="DeFi Health Alert Telegram Watch Bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and reach getMe, printing only bot username",
    )
    parser.add_argument(
        "--verify-markets",
        action="store_true",
        help="Live test eth_call on dead address for all markets; exit 1 if any fails",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print database statistics and exit",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.stats:
        db_path = get_db_path()
        try:
            return print_stats(db_path)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.verify_markets:
        return verify_markets()

    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        sys.exit(2)

    http_client = get_http_client()
    tg_client = TelegramClient(token=config.telegram_bot_token, client=http_client)

    if args.check:
        try:
            me = tg_client.get_me()
            username = me.get("username", "<unknown>")
            print(f"Bot username: @{username}")
            if config.donate_address:
                print("Donations: configured")
            else:
                print("Donations: not configured")
            return 0
        except (TelegramError, httpx.HTTPError, OSError) as exc:
            sys.stderr.write(f"Connection check failed: {exc}\n")
            return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Initializing database at %s", config.db_path)
    init_db(config.db_path)

    all_markets = load_markets()
    logger.info("Starting bot runner with %d market(s)", len(all_markets))
    run_all(
        db_path=config.db_path,
        tg_client=tg_client,
        markets=all_markets,
        rpc_urls=config.rpc_urls,
        heartbeat_file=config.heartbeat_file,
        donate_address=config.donate_address,
        donate_note=config.donate_note,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
