import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from hfwb.state import DEFAULT_LEVELS
from hfwb.store import (
    DEFAULT_LEGACY_MARKET_KEY,
    PRO_FEATURES,
    LimitError,
    TrackedAddress,
    WatchRecord,
    add_depeg_subs,
    add_tracked_address,
    add_watch,
    clear_failure,
    clear_pro_interest,
    delete_chat,
    distinct_addresses,
    distinct_pairs,
    get_levels,
    get_pro_interest,
    get_stats,
    get_table_columns,
    init_db,
    list_depeg_subs,
    list_tracked_addresses,
    list_watches,
    list_watches_by_address,
    pro_interest_counts,
    record_failure,
    remove_address,
    remove_depeg_subs,
    remove_watch,
    reset_levels,
    set_levels,
    set_pro_interest,
    subscribers_by_symbol,
    update_depeg_state,
    update_last_scan_ts,
    update_state,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test_bot.db"
    init_db(p)
    return p


def test_no_usernames_or_messages_in_schema(db_path: Path):
    columns = get_table_columns(db_path, "watches")
    forbidden = {"username", "user_name", "first_name", "last_name", "message", "text"}
    for col in columns:
        assert col.lower() not in forbidden, f"Forbidden column found: {col}"
    expected_cols = {"chat_id", "address", "market_key", "state", "last_alert_ts", "fail_count"}
    assert expected_cols.issubset(set(columns))

    tracked_cols = get_table_columns(db_path, "tracked_addresses")
    for col in tracked_cols:
        assert col.lower() not in forbidden
    assert {"chat_id", "address", "last_scan_ts"}.issubset(set(tracked_cols))


def test_migration_from_old_schema(tmp_path: Path):
    old_db = tmp_path / "old_schema.db"
    conn = sqlite3.connect(str(old_db))
    conn.execute(
        """
        CREATE TABLE watches (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'ok',
            last_alert_ts REAL,
            fail_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, address)
        );
        """
    )
    conn.execute(
        "INSERT INTO watches (chat_id, address, state, last_alert_ts, fail_count) VALUES (?, ?, ?, ?, ?)",
        (1001, "0x1111111111111111111111111111111111111111", "L1", 12345.0, 2),
    )
    conn.commit()
    conn.close()

    # Run init_db which should migrate the old DB
    init_db(old_db)

    # Verify migration results
    conn = sqlite3.connect(str(old_db))
    cur = conn.cursor()
    cur.execute("PRAGMA user_version;")
    assert cur.fetchone()[0] == 4

    cur.execute("SELECT chat_id, address, market_key, state, last_alert_ts, fail_count FROM watches;")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1001
    assert rows[0][1] == "0x1111111111111111111111111111111111111111"
    assert rows[0][2] == DEFAULT_LEGACY_MARKET_KEY
    assert rows[0][3] == "L1"
    assert rows[0][4] == 12345.0
    assert rows[0][5] == 2

    cur.execute("SELECT chat_id, address, last_scan_ts FROM tracked_addresses;")
    tracked_rows = cur.fetchall()
    assert len(tracked_rows) == 1
    assert tracked_rows[0][0] == 1001
    assert tracked_rows[0][1] == "0x1111111111111111111111111111111111111111"
    conn.close()


def test_add_and_list_watches(db_path: Path):
    chat_id = 12345
    add_watch(db_path, chat_id, "0x1111111111111111111111111111111111111111", "m_key_1")
    add_watch(db_path, chat_id, "0x2222222222222222222222222222222222222222", "m_key_2")

    watches = list_watches(db_path, chat_id)
    assert len(watches) == 2
    assert all(isinstance(w, WatchRecord) for w in watches)
    addrs = [w.address for w in watches]
    assert "0x1111111111111111111111111111111111111111" in addrs
    assert "0x2222222222222222222222222222222222222222" in addrs


def test_watch_limits_per_chat(db_path: Path):
    chat_id = 100
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"
    addr3 = "0x3333333333333333333333333333333333333333"
    addr4 = "0x4444444444444444444444444444444444444444"

    add_tracked_address(db_path, chat_id, addr1)
    add_tracked_address(db_path, chat_id, addr2)
    add_tracked_address(db_path, chat_id, addr3)

    # 4th address raises LimitError
    with pytest.raises(LimitError, match="Limit"):
        add_tracked_address(db_path, chat_id, addr4)

    # 30 watches limit per chat
    for i in range(25):
        add_watch(db_path, chat_id, addr1, f"m_key_{i}")
    for i in range(5):
        add_watch(db_path, chat_id, addr2, f"m_key_{i}")

    # Total watches is now 30
    assert len(list_watches(db_path, chat_id)) == 30

    # 31st watch raises LimitError
    with pytest.raises(LimitError, match="Limit"):
        add_watch(db_path, chat_id, addr3, "m_key_overflow")


def test_remove_watch_and_remove_address(db_path: Path):
    chat_id = 200
    addr = "0x1111111111111111111111111111111111111111"
    add_tracked_address(db_path, chat_id, addr)
    add_watch(db_path, chat_id, addr, "m_key_1")
    add_watch(db_path, chat_id, addr, "m_key_2")

    # Remove single market watch
    assert remove_watch(db_path, chat_id, addr, "m_key_1") is True
    assert len(list_watches(db_path, chat_id, addr)) == 1

    # Remove address completely
    assert remove_address(db_path, chat_id, addr) is True
    assert len(list_watches(db_path, chat_id, addr)) == 0
    assert len(list_tracked_addresses(db_path, chat_id)) == 0


def test_distinct_pairs(db_path: Path):
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"
    m1 = "m_key_1"
    m2 = "m_key_2"

    add_watch(db_path, 1, addr1, m1)
    add_watch(db_path, 1, addr1, m2)
    add_watch(db_path, 2, addr1, m1)  # shared (m1, addr1)
    add_watch(db_path, 2, addr2, m2)

    pairs = distinct_pairs(db_path)
    assert set(pairs) == {(m1, addr1), (m2, addr1), (m2, addr2)}
    assert len(pairs) == 3


def test_distinct_addresses(db_path: Path):
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"
    add_tracked_address(db_path, 1, addr1)
    add_tracked_address(db_path, 1, addr2)
    add_tracked_address(db_path, 2, addr1)

    distinct = distinct_addresses(db_path)
    assert set(distinct) == {addr1, addr2}


def test_list_watches_by_address(db_path: Path):
    addr = "0x1111111111111111111111111111111111111111"
    add_watch(db_path, 1, addr, "m_key_1")
    add_watch(db_path, 2, addr, "m_key_1")
    add_watch(db_path, 1, addr, "m_key_2")

    watches_m1 = list_watches_by_address(db_path, addr, "m_key_1")
    assert len(watches_m1) == 2
    chat_ids = {w.chat_id for w in watches_m1}
    assert chat_ids == {1, 2}


def test_update_state(db_path: Path):
    chat_id = 300
    addr = "0x1111111111111111111111111111111111111111"
    m_key = "m_key_1"
    add_watch(db_path, chat_id, addr, m_key)

    update_state(db_path, chat_id, addr, m_key, "warn", last_alert_ts=1700000000.0)
    watches = list_watches(db_path, chat_id, addr)
    assert len(watches) == 1
    assert watches[0].state == "warn"
    assert watches[0].last_alert_ts == 1700000000.0


def test_delete_chat_wipes_all_tables(db_path: Path):
    chat_id = 400
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"
    add_tracked_address(db_path, chat_id, addr1)
    add_tracked_address(db_path, chat_id, addr2)
    add_watch(db_path, chat_id, addr1, "m1")
    add_watch(db_path, chat_id, addr2, "m2")

    add_tracked_address(db_path, 999, addr1)
    add_watch(db_path, 999, addr1, "m1")

    deleted = delete_chat(db_path, chat_id)
    assert deleted >= 2
    assert list_watches(db_path, chat_id) == []
    assert list_tracked_addresses(db_path, chat_id) == []

    # Chat 999 remains intact
    assert len(list_watches(db_path, 999)) == 1
    assert len(list_tracked_addresses(db_path, 999)) == 1


def test_record_failure_and_clear_failure_isolated(db_path: Path):
    addr = "0x1111111111111111111111111111111111111111"
    m1 = "m_key_1"
    m2 = "m_key_2"
    add_watch(db_path, 500, addr, m1)
    add_watch(db_path, 500, addr, m2)

    count1 = record_failure(db_path, m1, addr)
    assert count1 == 1

    # m2 failure count should still be 0
    w_m2 = next(w for w in list_watches(db_path, 500) if w.market_key == m2)
    assert w_m2.fail_count == 0

    clear_failure(db_path, m1, addr)
    w_m1 = next(w for w in list_watches(db_path, 500) if w.market_key == m1)
    assert w_m1.fail_count == 0


def test_tracked_address_rescan_ts(db_path: Path):
    addr = "0x1111111111111111111111111111111111111111"
    add_tracked_address(db_path, 600, addr, last_scan_ts=100.0)
    tracked = list_tracked_addresses(db_path, 600)
    assert len(tracked) == 1
    assert tracked[0] == TrackedAddress(chat_id=600, address=addr, last_scan_ts=100.0)

    update_last_scan_ts(db_path, 600, addr, 250.0)
    tracked = list_tracked_addresses(db_path, 600)
    assert tracked[0].last_scan_ts == 250.0


def test_schema_user_version_4_and_chat_settings_table(db_path: Path):
    with sqlite3.connect(str(db_path)) as conn:
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 4

    cols = get_table_columns(db_path, "chat_settings")
    forbidden = {"username", "user_name", "first_name", "last_name", "message", "text"}
    for col in cols:
        assert col.lower() not in forbidden
    assert {"chat_id", "levels"}.issubset(set(cols))


def test_migration_from_v1_schema(tmp_path: Path):
    v1_db = tmp_path / "v1_schema.db"
    conn = sqlite3.connect(str(v1_db))
    conn.execute(
        """
        CREATE TABLE watches (
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
        CREATE TABLE tracked_addresses (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_scan_ts REAL,
            PRIMARY KEY (chat_id, address)
        );
        """
    )
    conn.execute("PRAGMA user_version = 1;")
    conn.execute(
        "INSERT INTO watches (chat_id, address, market_key, state, last_alert_ts, fail_count) VALUES (?, ?, ?, ?, ?, ?)",
        (1001, "0x1111111111111111111111111111111111111111", "m_key_1", "L2", 54321.0, 1),
    )
    conn.execute(
        "INSERT INTO tracked_addresses (chat_id, address, last_scan_ts) VALUES (?, ?, ?)",
        (1001, "0x1111111111111111111111111111111111111111", 12345.0),
    )
    conn.commit()
    conn.close()

    # Upgrade via init_db
    init_db(v1_db)

    # Check upgraded version and preserved data
    conn = sqlite3.connect(str(v1_db))
    assert conn.execute("PRAGMA user_version;").fetchone()[0] == 4
    row = conn.execute("SELECT chat_id, address, market_key, state, last_alert_ts, fail_count FROM watches;").fetchone()
    assert row == (1001, "0x1111111111111111111111111111111111111111", "m_key_1", "L2", 54321.0, 1)
    t_row = conn.execute("SELECT chat_id, address, last_scan_ts FROM tracked_addresses;").fetchone()
    assert t_row == (1001, "0x1111111111111111111111111111111111111111", 12345.0)
    conn.close()


def test_get_levels_default_when_no_row(db_path: Path):
    levels = get_levels(db_path, chat_id=9999)
    assert levels == DEFAULT_LEVELS


def test_set_levels_and_resets_watch_states(db_path: Path):
    chat_id = 100
    other_chat = 200
    addr = "0x1111111111111111111111111111111111111111"

    add_watch(db_path, chat_id, addr, "m1", state="L2")
    update_state(db_path, chat_id, addr, "m1", "L2", last_alert_ts=12345.0)

    add_watch(db_path, other_chat, addr, "m1", state="L3")
    update_state(db_path, other_chat, addr, "m1", "L3", last_alert_ts=99999.0)

    custom_levels = (Decimal("1.5"), Decimal("1.3"), Decimal("1.1"))
    set_levels(db_path, chat_id, custom_levels)

    # Levels saved
    assert get_levels(db_path, chat_id) == custom_levels

    # Watches for chat_id reset to state ok and last_alert_ts None
    w = list_watches(db_path, chat_id, addr)[0]
    assert w.state == "ok"
    assert w.last_alert_ts is None

    # Other chat unchanged
    w_other = list_watches(db_path, other_chat, addr)[0]
    assert w_other.state == "L3"
    assert w_other.last_alert_ts == 99999.0


def test_reset_levels(db_path: Path):
    chat_id = 300
    addr = "0x1111111111111111111111111111111111111111"
    custom_levels = (Decimal("1.6"), Decimal("1.3"))

    set_levels(db_path, chat_id, custom_levels)
    assert get_levels(db_path, chat_id) == custom_levels

    add_watch(db_path, chat_id, addr, "m1", state="L1")
    update_state(db_path, chat_id, addr, "m1", "L1", last_alert_ts=12345.0)

    reset_levels(db_path, chat_id)

    # Levels back to default
    assert get_levels(db_path, chat_id) == DEFAULT_LEVELS

    # Watches reset to ok and last_alert_ts None
    w = list_watches(db_path, chat_id, addr)[0]
    assert w.state == "ok"
    assert w.last_alert_ts is None


def test_delete_chat_removes_settings(db_path: Path):
    chat_id = 400
    custom_levels = (Decimal("1.6"), Decimal("1.3"))
    set_levels(db_path, chat_id, custom_levels)
    assert get_levels(db_path, chat_id) == custom_levels

    delete_chat(db_path, chat_id)
    assert get_levels(db_path, chat_id) == DEFAULT_LEVELS


def test_corrupt_stored_levels_fallback(db_path: Path, caplog):
    chat_id = 500
    # Manually insert invalid levels string
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO chat_settings (chat_id, levels) VALUES (?, ?)",
            (chat_id, "corrupted,data,abc"),
        )
        conn.commit()

    import logging
    with caplog.at_level(logging.WARNING):
        levels = get_levels(db_path, chat_id)

    assert levels == DEFAULT_LEVELS
    assert any("corrupt" in r.getMessage().lower() or "falling back" in r.getMessage().lower() for r in caplog.records)


def test_depeg_subs_schema(db_path: Path):
    cols = get_table_columns(db_path, "depeg_subs")
    forbidden = {"username", "user_name", "first_name", "last_name", "message", "text"}
    for col in cols:
        assert col.lower() not in forbidden
    assert {"chat_id", "symbol", "state", "last_alert_ts"}.issubset(set(cols))


def test_migration_v2_to_v3(tmp_path: Path):
    v2_db = tmp_path / "v2.db"
    conn = sqlite3.connect(str(v2_db))
    conn.execute(
        """
        CREATE TABLE watches (
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
        CREATE TABLE tracked_addresses (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_scan_ts REAL,
            PRIMARY KEY (chat_id, address)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE chat_settings (
            chat_id INTEGER PRIMARY KEY,
            levels TEXT NOT NULL
        );
        """
    )
    conn.execute("PRAGMA user_version = 2;")
    conn.execute(
        "INSERT INTO watches (chat_id, address, market_key, state) VALUES (?, ?, ?, ?)",
        (101, "0x1111111111111111111111111111111111111111", "m1", "L1"),
    )
    conn.execute(
        "INSERT INTO tracked_addresses (chat_id, address, last_scan_ts) VALUES (?, ?, ?)",
        (101, "0x1111111111111111111111111111111111111111", 500.0),
    )
    conn.execute(
        "INSERT INTO chat_settings (chat_id, levels) VALUES (?, ?)",
        (101, "1.5,1.2"),
    )
    conn.commit()
    conn.close()

    # Migrate by running init_db
    init_db(v2_db)

    conn = sqlite3.connect(str(v2_db))
    cur = conn.cursor()
    cur.execute("PRAGMA user_version;")
    assert cur.fetchone()[0] == 4

    # Check preserved rows
    cur.execute("SELECT chat_id, state FROM watches;")
    assert cur.fetchall() == [(101, "L1")]
    cur.execute("SELECT chat_id, last_scan_ts FROM tracked_addresses;")
    assert cur.fetchall() == [(101, 500.0)]
    cur.execute("SELECT chat_id, levels FROM chat_settings;")
    assert cur.fetchall() == [(101, "1.5,1.2")]

    # Check depeg_subs table exists
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='depeg_subs';")
    assert cur.fetchone() is not None
    conn.close()


def test_add_and_list_depeg_subs(db_path: Path):
    chat_id = 999
    add_depeg_subs(db_path, chat_id, ["USDC", "USDT"])
    subs = list_depeg_subs(db_path, chat_id)
    assert len(subs) == 2
    syms = {s.symbol: (s.state, s.last_alert_ts) for s in subs}
    assert syms == {"USDC": ("ok", None), "USDT": ("ok", None)}

    # Idempotent re-add
    add_depeg_subs(db_path, chat_id, ["USDC"])
    subs2 = list_depeg_subs(db_path, chat_id)
    assert len(subs2) == 2


def test_depeg_subs_limit(db_path: Path):
    chat_id = 888
    twelve = [f"S{i}" for i in range(12)]
    add_depeg_subs(db_path, chat_id, twelve)
    assert len(list_depeg_subs(db_path, chat_id)) == 12

    # Adding a 13th symbol raises LimitError
    with pytest.raises(LimitError, match="Limit of 12"):
        add_depeg_subs(db_path, chat_id, ["S13"])


def test_remove_depeg_subs(db_path: Path):
    chat_id = 777
    add_depeg_subs(db_path, chat_id, ["USDC", "USDT", "DAI"])
    assert len(list_depeg_subs(db_path, chat_id)) == 3

    # Remove single symbol
    remove_depeg_subs(db_path, chat_id, ["USDC"])
    subs = list_depeg_subs(db_path, chat_id)
    assert len(subs) == 2
    assert {s.symbol for s in subs} == {"USDT", "DAI"}

    # Remove all (symbols=None)
    remove_depeg_subs(db_path, chat_id, None)
    assert list_depeg_subs(db_path, chat_id) == []


def test_subscribers_by_symbol(db_path: Path):
    add_depeg_subs(db_path, 1, ["USDC", "USDT"])
    add_depeg_subs(db_path, 2, ["USDC", "DAI"])

    subs_map = subscribers_by_symbol(db_path)
    assert set(subs_map.keys()) == {"USDC", "USDT", "DAI"}
    assert subs_map["USDC"] == [(1, "ok", None), (2, "ok", None)]
    assert subs_map["USDT"] == [(1, "ok", None)]
    assert subs_map["DAI"] == [(2, "ok", None)]


def test_update_depeg_state(db_path: Path):
    chat_id = 666
    add_depeg_subs(db_path, chat_id, ["USDC"])

    update_depeg_state(db_path, chat_id, "USDC", "D1")
    sub = list_depeg_subs(db_path, chat_id)[0]
    assert sub.state == "D1"
    assert sub.last_alert_ts is None

    update_depeg_state(db_path, chat_id, "USDC", "D2", last_alert_ts=12345.0)
    sub = list_depeg_subs(db_path, chat_id)[0]
    assert sub.state == "D2"
    assert sub.last_alert_ts == 12345.0


def test_delete_chat_wipes_depeg_subs(db_path: Path):
    chat_id = 555
    add_depeg_subs(db_path, chat_id, ["USDC", "USDT"])
    assert len(list_depeg_subs(db_path, chat_id)) == 2

    delete_chat(db_path, chat_id)
    assert list_depeg_subs(db_path, chat_id) == []


def test_pro_interest_schema(db_path: Path):
    cols = get_table_columns(db_path, "pro_interest")
    forbidden = {"username", "user_name", "first_name", "last_name", "message", "text"}
    for col in cols:
        assert col.lower() not in forbidden
    assert {"chat_id", "features", "created_ts"}.issubset(set(cols))


def test_migration_v3_to_v4(tmp_path: Path):
    v3_db = tmp_path / "v3.db"
    conn = sqlite3.connect(str(v3_db))
    conn.execute(
        """
        CREATE TABLE watches (
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
        CREATE TABLE tracked_addresses (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_scan_ts REAL,
            PRIMARY KEY (chat_id, address)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE chat_settings (
            chat_id INTEGER PRIMARY KEY,
            levels TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE depeg_subs (
            chat_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'ok',
            last_alert_ts REAL,
            PRIMARY KEY (chat_id, symbol)
        );
        """
    )
    conn.execute("PRAGMA user_version = 3;")
    conn.execute(
        "INSERT INTO watches (chat_id, address, market_key, state) VALUES (?, ?, ?, ?)",
        (101, "0x1111111111111111111111111111111111111111", "m1", "L1"),
    )
    conn.execute(
        "INSERT INTO depeg_subs (chat_id, symbol) VALUES (?, ?)",
        (101, "USDC"),
    )
    conn.commit()
    conn.close()

    # Migrate via init_db
    init_db(v3_db)

    conn = sqlite3.connect(str(v3_db))
    cur = conn.cursor()
    cur.execute("PRAGMA user_version;")
    assert cur.fetchone()[0] == 4

    # Existing data preserved
    cur.execute("SELECT chat_id, state FROM watches;")
    assert cur.fetchall() == [(101, "L1")]
    cur.execute("SELECT chat_id, symbol FROM depeg_subs;")
    assert cur.fetchall() == [(101, "USDC")]

    # pro_interest table exists and is empty
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pro_interest';")
    assert cur.fetchone() is not None
    cur.execute("SELECT COUNT(*) FROM pro_interest;")
    assert cur.fetchone()[0] == 0
    conn.close()


def test_set_and_get_pro_interest(db_path: Path):
    assert get_pro_interest(db_path, 123) is None

    now = 1700000000.0
    set_pro_interest(db_path, 123, ["addresses", "faster"], now)
    interest = get_pro_interest(db_path, 123)
    assert interest == ("addresses", "faster")


def test_set_pro_interest_empty_list_stores_any(db_path: Path):
    now = 1700000000.0
    set_pro_interest(db_path, 123, [], now)
    interest = get_pro_interest(db_path, 123)
    assert interest == ("any",)

    # Check raw DB content is 'any'
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT features, created_ts FROM pro_interest WHERE chat_id = 123;").fetchone()
        assert row[0] == "any"
        assert row[1] == now


def test_set_pro_interest_upsert(db_path: Path):
    t1 = 1000.0
    t2 = 2000.0
    set_pro_interest(db_path, 123, ["addresses"], t1)
    assert get_pro_interest(db_path, 123) == ("addresses",)

    set_pro_interest(db_path, 123, ["faster", "email"], t2)
    assert get_pro_interest(db_path, 123) == ("faster", "email")

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT features, created_ts FROM pro_interest WHERE chat_id = 123;").fetchone()
        assert row[0] == "faster,email"
        assert row[1] == t2


def test_clear_pro_interest(db_path: Path):
    set_pro_interest(db_path, 123, ["addresses"], 1000.0)
    assert get_pro_interest(db_path, 123) == ("addresses",)

    clear_pro_interest(db_path, 123)
    assert get_pro_interest(db_path, 123) is None


def test_pro_interest_counts(db_path: Path):
    # Empty DB
    counts = pro_interest_counts(db_path)
    expected_empty = {"total": 0, "addresses": 0, "faster": 0, "levels": 0, "email": 0, "discord": 0}
    assert counts == expected_empty

    # Chat 1: "any" -> counts in total only
    set_pro_interest(db_path, 1, [], 1000.0)
    counts1 = pro_interest_counts(db_path)
    assert counts1["total"] == 1
    for f in PRO_FEATURES:
        assert counts1[f] == 0

    # Chat 2: "addresses", "faster"
    set_pro_interest(db_path, 2, ["addresses", "faster"], 1001.0)
    # Chat 3: "addresses", "email", "discord"
    set_pro_interest(db_path, 3, ["addresses", "email", "discord"], 1002.0)

    counts2 = pro_interest_counts(db_path)
    assert counts2["total"] == 3
    assert counts2["addresses"] == 2
    assert counts2["faster"] == 1
    assert counts2["levels"] == 0
    assert counts2["email"] == 1
    assert counts2["discord"] == 1


def test_delete_chat_wipes_pro_interest(db_path: Path):
    set_pro_interest(db_path, 100, ["faster"], 1000.0)
    set_pro_interest(db_path, 200, ["levels"], 1000.0)

    delete_chat(db_path, 100)
    assert get_pro_interest(db_path, 100) is None
    assert get_pro_interest(db_path, 200) == ("levels",)


def test_corrupt_stored_pro_interest_fallback(db_path: Path, caplog):
    import logging
    # Manually insert invalid features string
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO pro_interest (chat_id, features, created_ts) VALUES (?, ?, ?)",
            (999, "corrupted_feature,not_valid", 1000.0),
        )
        conn.commit()

    with caplog.at_level(logging.WARNING):
        interest = get_pro_interest(db_path, 999)
    assert interest == ("any",)

    # pro_interest_counts counts corrupt as "any" (in total only, no crash)
    counts = pro_interest_counts(db_path)
    assert counts["total"] == 1
    for f in PRO_FEATURES:
        assert counts[f] == 0


def test_get_stats(db_path: Path):
    # Empty DB stats
    empty_stats = get_stats(db_path)
    assert empty_stats["chats_with_tracked_addresses"] == 0
    assert empty_stats["watches"] == 0
    assert empty_stats["depeg_subs"] == 0
    assert empty_stats["custom_levels_chats"] == 0
    assert empty_stats["pro_interest"] == {"total": 0, "addresses": 0, "faster": 0, "levels": 0, "email": 0, "discord": 0}

    # Seed DB
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"
    add_tracked_address(db_path, 1, addr1)
    add_tracked_address(db_path, 1, addr2)
    add_tracked_address(db_path, 2, addr1)
    add_watch(db_path, 1, addr1, "m1")
    add_watch(db_path, 1, addr2, "m2")
    add_watch(db_path, 2, addr1, "m1")
    add_depeg_subs(db_path, 1, ["USDC", "USDT"])
    set_levels(db_path, 1, (Decimal("1.6"), Decimal("1.2")))
    set_pro_interest(db_path, 1, ["addresses", "faster"], 1000.0)
    set_pro_interest(db_path, 2, [], 1000.0)

    stats = get_stats(db_path)
    assert stats["chats_with_tracked_addresses"] == 2
    assert stats["watches"] == 3
    assert stats["depeg_subs"] == 2
    assert stats["custom_levels_chats"] == 1
    assert stats["pro_interest"]["total"] == 2
    assert stats["pro_interest"]["addresses"] == 1
    assert stats["pro_interest"]["faster"] == 1
    assert stats["pro_interest"]["levels"] == 0


