import logging
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from hfwb.aave import AccountData, ReadError
from hfwb.markets import Market
from hfwb.runner import poll_once, rescan_due_addresses
from hfwb.scan import ScanResult
from hfwb.store import (
    add_tracked_address,
    add_watch,
    distinct_pairs,
    init_db,
    list_tracked_addresses,
    list_watches,
    set_levels,
)
from hfwb.telegram import Blocked

M_ARB = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("https://arb.rpc.test",),
)

M_ETH = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("https://eth.rpc.test",),
)


class FakeTelegramClient:
    def __init__(self, blocked_chats: set[int] | None = None) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.blocked_chats = blocked_chats or set()

    def send_message(self, chat_id: int, text: str) -> dict:
        if chat_id in self.blocked_chats:
            raise Blocked("User blocked bot")
        self.sent_messages.append((chat_id, text))
        return {"ok": True}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, s: float) -> None:
        self.current += s


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "runner_test.db"
    init_db(p)
    return p


def test_poll_once_single_call_for_shared_pair(db_path: Path, tmp_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    # 2 different chats watching the same (market, address) pair
    add_watch(db_path, 101, addr, M_ARB.key, state="ok")
    add_watch(db_path, 102, addr, M_ARB.key, state="ok")

    read_calls: list[tuple[str, str]] = []

    def mock_reader(market, address):
        read_calls.append((market.key, address))
        return AccountData(
            collateral_usd=Decimal(10000),
            debt_usd=Decimal(7000),
            hf=Decimal("1.35"),  # critical (< 1.4)
            no_debt=False,
            has_position=True,
        )

    tg = FakeTelegramClient()
    heartbeat = tmp_path / "heartbeat.txt"
    clock = FakeClock(2000.0)

    poll_once(
        db_path,
        markets=[M_ARB, M_ETH],
        tg_client=tg,
        reader_fn=mock_reader,
        clock=clock,
        heartbeat_file=heartbeat,
    )

    # Distinct pair: reader called exactly ONCE
    assert len(read_calls) == 1
    assert read_calls[0] == (M_ARB.key, addr)

    # Both chats received L1 alerts with market name
    assert len(tg.sent_messages) == 2
    chat_ids = {m[0] for m in tg.sent_messages}
    assert chat_ids == {101, 102}
    for _, text in tg.sent_messages:
        assert "below <b>1.4</b>" in text
        assert "1.35" in text
        assert "Aave V3 · Arbitrum" in text

    # State updated in db for both
    w101 = list_watches(db_path, 101)[0]
    w102 = list_watches(db_path, 102)[0]
    assert w101.state == "L1"
    assert w102.state == "L1"
    assert w101.last_alert_ts == 2000.0
    assert w102.last_alert_ts == 2000.0

    # Heartbeat file written
    assert heartbeat.read_text().strip() == "2000"


def test_poll_once_read_error_handling_and_logging(db_path: Path, caplog):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 201, addr, M_ARB.key, state="ok")

    def failing_reader(market, address):
        raise ReadError("RPC Timeout")

    tg = FakeTelegramClient()
    clock = FakeClock(1000.0)

    # 1st failure: fail_count=1, no error log, no messages sent
    with caplog.at_level(logging.ERROR):
        poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=failing_reader, clock=clock)
    assert len(tg.sent_messages) == 0
    assert list_watches(db_path, 201)[0].state == "ok"
    assert list_watches(db_path, 201)[0].fail_count == 1
    assert not any("consecutive" in r.getMessage().lower() for r in caplog.records)

    # 2nd failure: fail_count=2, no error log
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=failing_reader, clock=clock)
    assert list_watches(db_path, 201)[0].fail_count == 2
    assert not any("consecutive" in r.getMessage().lower() for r in caplog.records)

    # 3rd failure: fail_count=3, logs ERROR!
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=failing_reader, clock=clock)
    assert list_watches(db_path, 201)[0].fail_count == 3
    assert any("consecutive" in r.getMessage().lower() or "failure" in r.getMessage().lower() for r in caplog.records)
    # Still no user spam
    assert len(tg.sent_messages) == 0


def test_poll_once_recovers_from_failures(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 301, addr, M_ARB.key, state="ok")

    # Fail twice
    def failing_reader(market, address):
        raise ReadError("500 Error")

    tg = FakeTelegramClient()
    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=failing_reader)
    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=failing_reader)
    assert list_watches(db_path, 301)[0].fail_count == 2

    # Succeed on 3rd
    def ok_reader(market, address):
        return AccountData(Decimal(1000), Decimal(0), None, True, True)

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=ok_reader)
    # Fail count reset to 0
    assert list_watches(db_path, 301)[0].fail_count == 0


def test_poll_once_deletes_chat_when_blocked(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 401, addr, M_ARB.key, state="ok")
    add_watch(db_path, 402, addr, M_ARB.key, state="ok")

    # Chat 401 blocked the bot, 402 didn't
    tg = FakeTelegramClient(blocked_chats={401})

    def l1_reader(market, address):
        return AccountData(Decimal(10000), Decimal(7000), Decimal("1.35"), False, True)

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=l1_reader)

    # 401 was deleted because blocked
    assert len(list_watches(db_path, 401)) == 0
    # 402 is intact and updated
    assert len(list_watches(db_path, 402)) == 1
    assert list_watches(db_path, 402)[0].state == "L1"


def test_concurrency_cap_5_and_per_host(db_path: Path):
    markets = []
    for i in range(10):
        m = Market(
            protocol="aave_v3",
            chain=f"Chain{i}",
            chain_id=1000 + i,
            name="Market",
            address=f"0x{i:040x}",
            rpcs=(f"https://host-{i % 2}.test/rpc",),
        )
        markets.append(m)
        add_watch(db_path, 500 + i, "0x794a61358d6845594f94dc1db02a252b5b4814ad", m.key, state="ok")

    active_count = 0
    max_active = 0
    lock = threading.Lock()

    def concurrent_reader(market, address):
        nonlocal active_count, max_active
        with lock:
            active_count += 1
            max_active = max(max_active, active_count)
        threading.Event().wait(0.02)
        with lock:
            active_count -= 1
        return AccountData(Decimal(1000), Decimal(0), None, True, True)

    tg = FakeTelegramClient()
    poll_once(
        db_path,
        markets=markets,
        tg_client=tg,
        reader_fn=concurrent_reader,
        concurrency_cap=5,
        per_host_cap=3,
    )

    assert max_active <= 5
    assert len(distinct_pairs(db_path)) == 10


def test_failed_alert_send_keeps_state_so_next_poll_retries(db_path: Path):
    from hfwb.telegram import TelegramError

    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 201, addr, M_ARB.key, state="ok")

    class FlakyClient:
        def __init__(self) -> None:
            self.fail = True
            self.sent: list[tuple[int, str]] = []

        def send_message(self, chat_id: int, text: str) -> dict:
            if self.fail:
                raise TelegramError("network down")
            self.sent.append((chat_id, text))
            return {"ok": True}

    def reader(market, address):
        return AccountData(Decimal(10000), Decimal(9000), Decimal("1.15"), False, True)

    client = FlakyClient()
    clock = FakeClock()
    poll_once(db_path, markets=[M_ARB], tg_client=client, reader_fn=reader, clock=clock)
    assert [w.state for w in list_watches(db_path, 201)] == ["ok"]  # not advanced
    client.fail = False
    clock.advance(300)
    poll_once(db_path, markets=[M_ARB], tg_client=client, reader_fn=reader, clock=clock)
    assert len(client.sent) == 1  # alert finally delivered
    assert [w.state for w in list_watches(db_path, 201)] == ["L2"]


def test_daily_rescan_discovers_new_position(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    clock = FakeClock(100000.0)
    # Tracked address with last_scan_ts 25h ago (90000s > 86400s)
    add_tracked_address(db_path, 701, addr, last_scan_ts=clock() - 90000.0)

    # Initial watch on Arbitrum only
    add_watch(db_path, 701, addr, M_ARB.key, state="ok")

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[
                (M_ARB, AccountData(Decimal(1000), Decimal(500), Decimal("1.5"), False, True)),
                (M_ETH, AccountData(None, None, Decimal("1.3"), False, True)),  # NEW POSITION!
            ],
            failed=[],
        )

    tg = FakeTelegramClient()
    rescan_due_addresses(
        db_path=db_path,
        tg_client=tg,
        markets=[M_ARB, M_ETH],
        scanner_fn=fake_scanner,
        clock=clock,
        interval_seconds=86400.0,
    )

    # Chat 701 received notification about the new position on M_ETH
    assert len(tg.sent_messages) == 1
    chat_id, text = tg.sent_messages[0]
    assert chat_id == 701
    assert "Found a new position" in text
    assert "Aave V4 · Ethereum · Main Spoke" in text
    assert "1.30" in text

    # DB now has watches for both M_ARB and M_ETH
    watches = list_watches(db_path, 701)
    assert len(watches) == 2
    keys = {w.market_key for w in watches}
    assert M_ARB.key in keys
    assert M_ETH.key in keys

    # last_scan_ts was updated
    tracked = list_tracked_addresses(db_path, 701)[0]
    assert tracked.last_scan_ts == clock()


def test_rescan_with_failed_markets_stays_due(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    clock = FakeClock(100000.0)
    old = clock() - 90000.0
    add_tracked_address(db_path, 801, addr, last_scan_ts=old)

    def partial_scanner(address, markets, reader=None):
        return ScanResult(found=[], failed=[M_ETH])

    rescan_due_addresses(
        db_path=db_path, tg_client=FakeTelegramClient(), markets=[M_ARB, M_ETH],
        scanner_fn=partial_scanner, clock=clock, interval_seconds=86400.0,
    )
    assert list_tracked_addresses(db_path, 801)[0].last_scan_ts == old  # not advanced


def test_rescan_limit_for_one_chat_does_not_block_other_chats(db_path: Path):
    from hfwb.store import MAX_WATCHES_PER_CHAT

    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    clock = FakeClock(100000.0)
    add_tracked_address(db_path, 901, addr, last_scan_ts=clock() - 90000.0)
    add_tracked_address(db_path, 902, addr, last_scan_ts=clock() - 90000.0)
    # chat 901 is already full on other addresses' watches
    for i in range(MAX_WATCHES_PER_CHAT):
        add_watch(db_path, 901, "0x2222222222222222222222222222222222222222", f"aave_v3:{i}:0x{i:040x}", state="ok")

    def scanner(address, markets, reader=None):
        return ScanResult(found=[(M_ETH, AccountData(None, None, Decimal("1.3"), False, True))], failed=[])

    tg = FakeTelegramClient()
    rescan_due_addresses(
        db_path=db_path, tg_client=tg, markets=[M_ARB, M_ETH], scanner_fn=scanner,
        clock=clock, interval_seconds=86400.0,
    )
    assert [w.market_key for w in list_watches(db_path, 902)] == [M_ETH.key]
    assert any(cid == 902 for cid, _ in tg.sent_messages)


def test_poll_once_uses_chat_levels_and_caches_per_cycle(db_path: Path, monkeypatch):
    import hfwb.runner
    import hfwb.store

    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    # Chat 101 has custom levels: 1.8, 1.4
    set_levels(db_path, 101, (Decimal("1.8"), Decimal("1.4")))
    # Chat 102 has default levels (1.4, 1.2, 1.1, 1.05)
    add_watch(db_path, 101, addr, M_ARB.key, state="ok")
    add_watch(db_path, 102, addr, M_ARB.key, state="ok")
    # Additional watch for Chat 101 on M_ETH to verify caching
    add_watch(db_path, 101, addr, M_ETH.key, state="ok")

    def reader(market, address):
        return AccountData(
            collateral_usd=Decimal(10000),
            debt_usd=Decimal(6000),
            hf=Decimal("1.65"),
            no_debt=False,
            has_position=True,
        )

    tg = FakeTelegramClient()
    clock = FakeClock(2000.0)

    orig_get_levels = hfwb.store.get_levels
    call_counts = {101: 0, 102: 0}

    def counting_get_levels(db, chat_id):
        if chat_id in call_counts:
            call_counts[chat_id] += 1
        return orig_get_levels(db, chat_id)

    monkeypatch.setattr(hfwb.runner, "get_levels", counting_get_levels, raising=False)
    monkeypatch.setattr(hfwb.store, "get_levels", counting_get_levels)

    poll_once(
        db_path,
        markets=[M_ARB, M_ETH],
        tg_client=tg,
        reader_fn=reader,
        clock=clock,
    )

    # Chat 101 received alerts with its custom level (1.8) and emoji 🔴
    sent_101 = [m for m in tg.sent_messages if m[0] == 101]
    assert len(sent_101) == 2
    for _, text in sent_101:
        assert "below <b>1.8</b>" in text
        assert "🔴" in text

    # Chat 102 received NO alert (1.65 >= 1.4)
    sent_102 = [m for m in tg.sent_messages if m[0] == 102]
    assert len(sent_102) == 0

    # States in DB
    w_101 = list_watches(db_path, 101)
    assert all(w.state == "L1" for w in w_101)
    w_102 = list_watches(db_path, 102)
    assert all(w.state == "ok" for w in w_102)

    # Verify caching: get_levels called exactly ONCE per chat_id during the cycle
    assert call_counts[101] == 1
    assert call_counts[102] == 1


def test_rescan_due_addresses_uses_chat_levels(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    clock = FakeClock(100000.0)
    add_tracked_address(db_path, 701, addr, last_scan_ts=clock() - 90000.0)
    # Chat 701 has custom levels: 1.8, 1.4
    set_levels(db_path, 701, (Decimal("1.8"), Decimal("1.4")))

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[(M_ETH, AccountData(None, None, Decimal("1.65"), False, True))],
            failed=[],
        )

    tg = FakeTelegramClient()
    rescan_due_addresses(
        db_path=db_path,
        tg_client=tg,
        markets=[M_ETH],
        scanner_fn=fake_scanner,
        clock=clock,
        interval_seconds=86400.0,
    )

    # Initial state should be L1 (not ok) because 1.65 < 1.8
    watches = list_watches(db_path, 701)
    assert len(watches) == 1
    assert watches[0].state == "L1"

    # Emoji in notification should be 🔴 (level_emoji(1, 2))
    assert len(tg.sent_messages) == 1
    assert "🔴 <b>Aave V4 · Ethereum · Main Spoke</b>" in tg.sent_messages[0][1]
