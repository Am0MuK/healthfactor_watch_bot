import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from hfwb.state import DEFAULT_LEVELS, validate_levels

logger = logging.getLogger(__name__)

DEFAULT_LEGACY_MARKET_KEY = "aave_v3:42161:0x794a61358d6845594f94dc1db02a252b5b4814ad"
MAX_ADDRESSES_PER_CHAT = 3
MAX_WATCHES_PER_CHAT = 30
MAX_DEPEG_SUBS_PER_CHAT = 12


class LimitError(Exception):
    """Raised when a chat attempts to watch more than the allowed number of addresses or positions."""


@dataclass(frozen=True)
class WatchRecord:
    chat_id: int
    address: str
    market_key: str
    state: str
    last_alert_ts: float | None
    fail_count: int


@dataclass(frozen=True)
class TrackedAddress:
    chat_id: int
    address: str
    last_scan_ts: float | None


@dataclass(frozen=True)
class DepegSubRecord:
    chat_id: int
    symbol: str
    state: str
    last_alert_ts: float | None


@contextmanager
def get_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    """Initialize database schema with WAL mode, running migrations if needed."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version;")
        version_row = cur.fetchone()
        user_version = version_row[0] if version_row else 0

        # Check existing tables and schema
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watches';")
        has_watches = cur.fetchone() is not None

        if has_watches:
            cur.execute("PRAGMA table_info(watches);")
            cols = [r[1] for r in cur.fetchall()]
            if "market_key" not in cols:
                # Migrate existing table
                conn.execute(
                    """
                    CREATE TABLE watches_new (
                        chat_id INTEGER NOT NULL,
                        address TEXT NOT NULL,
                        market_key TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'ok',
                        last_alert_ts REAL,
                        fail_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (chat_id, address, market_key)
                    );
                    """
                )
                conn.execute(
                    f"""
                    INSERT INTO watches_new (chat_id, address, market_key, state, last_alert_ts, fail_count)
                    SELECT chat_id, address, '{DEFAULT_LEGACY_MARKET_KEY}', state, last_alert_ts, fail_count
                    FROM watches;
                    """
                )
                conn.execute("DROP TABLE watches;")
                conn.execute("ALTER TABLE watches_new RENAME TO watches;")

        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    chat_id INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ok',
                    last_alert_ts REAL,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, address, market_key)
                );
                """
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_addresses (
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                last_scan_ts REAL,
                PRIMARY KEY (chat_id, address)
            );
            """
        )

        if has_watches and user_version < 1:
            conn.execute(
                """
                INSERT OR IGNORE INTO tracked_addresses (chat_id, address, last_scan_ts)
                SELECT DISTINCT chat_id, address, NULL FROM watches;
                """
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                levels TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS depeg_subs (
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ok',
                last_alert_ts REAL,
                PRIMARY KEY (chat_id, symbol)
            );
            """
        )

        conn.execute("PRAGMA user_version = 3;")


def add_tracked_address(
    db_path: str | Path,
    chat_id: int,
    address: str,
    last_scan_ts: float | None = None,
) -> None:
    """Track an address for a chat. Raises LimitError if 3-address limit reached."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM tracked_addresses WHERE chat_id = ? AND address != ?",
            (chat_id, address),
        )
        count = cur.fetchone()[0]
        if count >= MAX_ADDRESSES_PER_CHAT:
            raise LimitError(f"Limit of {MAX_ADDRESSES_PER_CHAT} addresses per chat reached")

        cur.execute(
            """
            INSERT INTO tracked_addresses (chat_id, address, last_scan_ts)
            VALUES (?, ?, ?)
            ON CONFLICT (chat_id, address) DO UPDATE SET
                last_scan_ts = COALESCE(excluded.last_scan_ts, tracked_addresses.last_scan_ts)
            """,
            (chat_id, address, last_scan_ts),
        )


def update_last_scan_ts(
    db_path: str | Path,
    chat_id: int,
    address: str,
    last_scan_ts: float,
) -> None:
    """Update last_scan_ts for a tracked address."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tracked_addresses
            SET last_scan_ts = ?
            WHERE chat_id = ? AND address = ?
            """,
            (last_scan_ts, chat_id, address),
        )


def list_tracked_addresses(
    db_path: str | Path,
    chat_id: int | None = None,
) -> list[TrackedAddress]:
    """Return tracked addresses for a chat or all chats."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if chat_id is not None:
            cur.execute(
                """
                SELECT chat_id, address, last_scan_ts
                FROM tracked_addresses
                WHERE chat_id = ?
                ORDER BY address ASC
                """,
                (chat_id,),
            )
        else:
            cur.execute(
                """
                SELECT chat_id, address, last_scan_ts
                FROM tracked_addresses
                ORDER BY chat_id ASC, address ASC
                """
            )
        return [TrackedAddress(chat_id=r[0], address=r[1], last_scan_ts=r[2]) for r in cur.fetchall()]


def get_due_rescans(
    db_path: str | Path,
    now_ts: float,
    interval_seconds: float = 86400.0,
) -> list[TrackedAddress]:
    """Return tracked addresses where last_scan_ts is older than interval_seconds or None."""
    cutoff = now_ts - interval_seconds
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT chat_id, address, last_scan_ts
            FROM tracked_addresses
            WHERE last_scan_ts IS NULL OR last_scan_ts <= ?
            ORDER BY last_scan_ts ASC NULLS FIRST
            """,
            (cutoff,),
        )
        return [TrackedAddress(chat_id=r[0], address=r[1], last_scan_ts=r[2]) for r in cur.fetchall()]


def add_watch(
    db_path: str | Path,
    chat_id: int,
    address: str,
    market_key: str = DEFAULT_LEGACY_MARKET_KEY,
    state: str = "ok",
) -> None:
    """Add a watch for a chat, address, and market_key.

    Enforces limits: max 3 addresses per chat, max 30 watches per chat.
    """
    add_tracked_address(db_path, chat_id, address)

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM watches WHERE chat_id = ? AND NOT (address = ? AND market_key = ?)",
            (chat_id, address, market_key),
        )
        watch_count = cur.fetchone()[0]
        if watch_count >= MAX_WATCHES_PER_CHAT:
            raise LimitError(f"Limit of {MAX_WATCHES_PER_CHAT} market positions per chat reached")

        cur.execute(
            """
            INSERT INTO watches (chat_id, address, market_key, state)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (chat_id, address, market_key) DO NOTHING
            """,
            (chat_id, address, market_key, state),
        )


def remove_watch(
    db_path: str | Path,
    chat_id: int,
    address: str,
    market_key: str | None = None,
) -> bool:
    """Remove a watch for a market, or all watches for an address if market_key is None."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if market_key is not None:
            cur.execute(
                "DELETE FROM watches WHERE chat_id = ? AND address = ? AND market_key = ?",
                (chat_id, address, market_key),
            )
            return cur.rowcount > 0
        else:
            return remove_address(db_path, chat_id, address)


def remove_address(db_path: str | Path, chat_id: int, address: str) -> bool:
    """Remove an address and all its watches for a chat."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM watches WHERE chat_id = ? AND address = ?",
            (chat_id, address),
        )
        w_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM tracked_addresses WHERE chat_id = ? AND address = ?",
            (chat_id, address),
        )
        t_deleted = cur.rowcount
        return (w_deleted > 0) or (t_deleted > 0)


def list_watches(
    db_path: str | Path,
    chat_id: int,
    address: str | None = None,
) -> list[WatchRecord]:
    """Return all watches for a chat, optionally filtered by address."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if address is not None:
            cur.execute(
                """
                SELECT chat_id, address, market_key, state, last_alert_ts, fail_count
                FROM watches
                WHERE chat_id = ? AND address = ?
                ORDER BY market_key ASC
                """,
                (chat_id, address),
            )
        else:
            cur.execute(
                """
                SELECT chat_id, address, market_key, state, last_alert_ts, fail_count
                FROM watches
                WHERE chat_id = ?
                ORDER BY address ASC, market_key ASC
                """,
                (chat_id,),
            )
        rows = cur.fetchall()
        return [
            WatchRecord(
                chat_id=row[0],
                address=row[1],
                market_key=row[2],
                state=row[3],
                last_alert_ts=row[4],
                fail_count=row[5],
            )
            for row in rows
        ]


def list_watches_by_address(
    db_path: str | Path,
    address: str,
    market_key: str | None = None,
) -> list[WatchRecord]:
    """Return all watches monitoring a specific address, optionally filtered by market_key."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if market_key is not None:
            cur.execute(
                """
                SELECT chat_id, address, market_key, state, last_alert_ts, fail_count
                FROM watches
                WHERE address = ? AND market_key = ?
                ORDER BY chat_id ASC
                """,
                (address, market_key),
            )
        else:
            cur.execute(
                """
                SELECT chat_id, address, market_key, state, last_alert_ts, fail_count
                FROM watches
                WHERE address = ?
                ORDER BY chat_id ASC, market_key ASC
                """,
                (address,),
            )
        rows = cur.fetchall()
        return [
            WatchRecord(
                chat_id=row[0],
                address=row[1],
                market_key=row[2],
                state=row[3],
                last_alert_ts=row[4],
                fail_count=row[5],
            )
            for row in rows
        ]


def distinct_pairs(db_path: str | Path) -> list[tuple[str, str]]:
    """Return distinct (market_key, address) pairs across all chats for polling."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT market_key, address FROM watches ORDER BY market_key ASC, address ASC")
        return [(row[0], row[1]) for row in cur.fetchall()]


def distinct_addresses(db_path: str | Path) -> list[str]:
    """Return distinct monitored addresses across all chats."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT address FROM tracked_addresses
            UNION
            SELECT DISTINCT address FROM watches
            ORDER BY address ASC
            """
        )
        return [row[0] for row in cur.fetchall()]


def update_state(
    db_path: str | Path,
    chat_id: int,
    address: str,
    market_key_or_state: str,
    state: str | None = None,
    last_alert_ts: float | None = None,
) -> None:
    """Update watch state and optionally last alert timestamp.

    Supports both:
    - update_state(db_path, chat_id, address, market_key, state, last_alert_ts)
    - update_state(db_path, chat_id, address, state, last_alert_ts) (legacy)
    """
    if state is None:
        actual_state = market_key_or_state
        market_key = None
    else:
        market_key = market_key_or_state
        actual_state = state

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        query_parts = ["UPDATE watches SET state = ?"]
        params: list[object] = [actual_state]

        if last_alert_ts is not None:
            query_parts.append(", last_alert_ts = ?")
            params.append(last_alert_ts)

        query_parts.append(" WHERE chat_id = ? AND address = ?")
        params.extend([chat_id, address])

        if market_key is not None:
            query_parts.append(" AND market_key = ?")
            params.append(market_key)

        cur.execute("".join(query_parts), tuple(params))


def delete_chat(db_path: str | Path, chat_id: int) -> int:
    """Delete all watches, tracked addresses, settings, and depeg subscriptions for a chat."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM watches WHERE chat_id = ?", (chat_id,))
        w_count = cur.rowcount
        cur.execute("DELETE FROM tracked_addresses WHERE chat_id = ?", (chat_id,))
        t_count = cur.rowcount
        cur.execute("DELETE FROM chat_settings WHERE chat_id = ?", (chat_id,))
        cur.execute("DELETE FROM depeg_subs WHERE chat_id = ?", (chat_id,))
        d_count = cur.rowcount
        return max(w_count, t_count, d_count)


def get_levels(db_path: str | Path, chat_id: int) -> tuple[Decimal, ...]:
    """Return configured alert levels for chat_id, or DEFAULT_LEVELS."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT levels FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return DEFAULT_LEVELS
        raw = row[0]
        try:
            tokens = [t.strip() for t in raw.split(",") if t.strip()]
            return validate_levels(tokens)
        except Exception as exc:  # noqa: BLE001 - fallback on corrupt stored values
            logger.warning(
                "Corrupt alert levels stored for chat %s ('%s'): %s. Falling back to default levels.",
                chat_id,
                raw,
                exc,
            )
            return DEFAULT_LEVELS


def set_levels(
    db_path: str | Path,
    chat_id: int,
    levels: Sequence[Decimal],
) -> None:
    """Set alert levels for chat_id and reset all watches for that chat to ok in one transaction."""
    levels_str = ",".join(str(lvl) for lvl in levels)
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_settings (chat_id, levels)
            VALUES (?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET levels = excluded.levels
            """,
            (chat_id, levels_str),
        )
        cur.execute(
            "UPDATE watches SET state = 'ok', last_alert_ts = NULL WHERE chat_id = ?",
            (chat_id,),
        )


def reset_levels(db_path: str | Path, chat_id: int) -> None:
    """Reset alert levels for chat_id to default and reset all watches for that chat to ok."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_settings WHERE chat_id = ?", (chat_id,))
        cur.execute(
            "UPDATE watches SET state = 'ok', last_alert_ts = NULL WHERE chat_id = ?",
            (chat_id,),
        )


def record_failure(
    db_path: str | Path,
    market_key_or_address: str,
    address: str | None = None,
) -> int:
    """Increment fail count for (market_key, address) or address. Returns max fail count."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if address is not None:
            market_key = market_key_or_address
            cur.execute(
                """
                UPDATE watches
                SET fail_count = fail_count + 1
                WHERE market_key = ? AND address = ?
                """,
                (market_key, address),
            )
            cur.execute(
                """
                SELECT COALESCE(MAX(fail_count), 0)
                FROM watches
                WHERE market_key = ? AND address = ?
                """,
                (market_key, address),
            )
        else:
            addr = market_key_or_address
            cur.execute(
                "UPDATE watches SET fail_count = fail_count + 1 WHERE address = ?",
                (addr,),
            )
            cur.execute(
                "SELECT COALESCE(MAX(fail_count), 0) FROM watches WHERE address = ?",
                (addr,),
            )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def clear_failure(
    db_path: str | Path,
    market_key_or_address: str,
    address: str | None = None,
) -> None:
    """Reset fail count to 0 for (market_key, address) or address."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if address is not None:
            market_key = market_key_or_address
            cur.execute(
                "UPDATE watches SET fail_count = 0 WHERE market_key = ? AND address = ?",
                (market_key, address),
            )
        else:
            addr = market_key_or_address
            cur.execute(
                "UPDATE watches SET fail_count = 0 WHERE address = ?",
                (addr,),
            )


def get_table_columns(db_path: str | Path, table_name: str) -> list[str]:
    """Helper for testing schema column names."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cur.fetchall()]


def add_depeg_subs(
    db_path: str | Path,
    chat_id: int,
    symbols: Sequence[str],
) -> list[str]:
    """Subscribe a chat to stablecoin depeg alerts for given symbols.

    Enforces max 12 subscriptions per chat.
    """
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT symbol FROM depeg_subs WHERE chat_id = ?", (chat_id,))
        existing = {r[0] for r in cur.fetchall()}
        new_symbols = [s for s in symbols if s not in existing]
        if len(existing) + len(new_symbols) > MAX_DEPEG_SUBS_PER_CHAT:
            raise LimitError(
                f"Limit of {MAX_DEPEG_SUBS_PER_CHAT} stablecoin subscriptions per chat reached"
            )

        for s in symbols:
            cur.execute(
                """
                INSERT INTO depeg_subs (chat_id, symbol, state, last_alert_ts)
                VALUES (?, ?, 'ok', NULL)
                ON CONFLICT (chat_id, symbol) DO NOTHING
                """,
                (chat_id, s),
            )
        return list(symbols)


def remove_depeg_subs(
    db_path: str | Path,
    chat_id: int,
    symbols: Sequence[str] | None = None,
) -> int:
    """Remove depeg subscriptions for a chat, or all if symbols is None."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if symbols is None:
            cur.execute("DELETE FROM depeg_subs WHERE chat_id = ?", (chat_id,))
            return cur.rowcount
        total = 0
        for s in symbols:
            cur.execute(
                "DELETE FROM depeg_subs WHERE chat_id = ? AND symbol = ?",
                (chat_id, s),
            )
            total += cur.rowcount
        return total


def list_depeg_subs(
    db_path: str | Path,
    chat_id: int,
) -> list[DepegSubRecord]:
    """Return all depeg subscriptions for a chat ordered by symbol."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT chat_id, symbol, state, last_alert_ts
            FROM depeg_subs
            WHERE chat_id = ?
            ORDER BY symbol ASC
            """,
            (chat_id,),
        )
        return [
            DepegSubRecord(
                chat_id=r[0],
                symbol=r[1],
                state=r[2],
                last_alert_ts=r[3],
            )
            for r in cur.fetchall()
        ]


def subscribers_by_symbol(
    db_path: str | Path,
) -> dict[str, list[tuple[int, str, float | None]]]:
    """Return mapping of symbol to list of (chat_id, state, last_alert_ts) tuples."""
    result: dict[str, list[tuple[int, str, float | None]]] = defaultdict(list)
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT symbol, chat_id, state, last_alert_ts
            FROM depeg_subs
            ORDER BY symbol ASC, chat_id ASC
            """
        )
        for r in cur.fetchall():
            result[r[0]].append((r[1], r[2], r[3]))
    return dict(result)


def update_depeg_state(
    db_path: str | Path,
    chat_id: int,
    symbol: str,
    state: str,
    last_alert_ts: float | None = None,
) -> None:
    """Update state and optionally last_alert_ts for a depeg subscription."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if last_alert_ts is not None:
            cur.execute(
                """
                UPDATE depeg_subs
                SET state = ?, last_alert_ts = ?
                WHERE chat_id = ? AND symbol = ?
                """,
                (state, last_alert_ts, chat_id, symbol),
            )
        else:
            cur.execute(
                """
                UPDATE depeg_subs
                SET state = ?
                WHERE chat_id = ? AND symbol = ?
                """,
                (state, chat_id, symbol),
            )
