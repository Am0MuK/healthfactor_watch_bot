import logging
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from hfwb.aave import AccountData, ReadError
from hfwb.markets import Market
from hfwb.runner import PollScheduler, poll_due, poll_once, rescan_due_addresses, run_poll_loop
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


def test_poll_due_far_position_read_every_300s_and_not_before(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1001, addr, M_ARB.key, state="ok")

    read_calls = 0

    def far_reader(market, address):
        nonlocal read_calls
        read_calls += 1
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    # Initial tick at 1000.0: new pair is due immediately
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=far_reader, clock=clock)
    assert read_calls == 1
    assert scheduler.next_due[(M_ARB.key, addr)] == 1300.0

    # Advance clock to 1100 (not due yet: 1100 < 1300)
    clock.advance(100.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=far_reader, clock=clock)
    assert read_calls == 1

    # Advance clock to 1299 (not due yet)
    clock.advance(199.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=far_reader, clock=clock)
    assert read_calls == 1

    # Advance clock to 1300 (due now!)
    clock.advance(1.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=far_reader, clock=clock)
    assert read_calls == 2
    assert scheduler.next_due[(M_ARB.key, addr)] == 1600.0


def test_poll_due_near_position_hf_1_12_read_every_60s(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1002, addr, M_ARB.key, state="ok")

    read_calls = 0

    def near_reader(market, address):
        nonlocal read_calls
        read_calls += 1
        return AccountData(Decimal(10000), Decimal(8900), Decimal("1.12"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    # Initial tick: due immediately, HF=1.12 -> interval 60s
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=near_reader, clock=clock)
    assert read_calls == 1
    assert scheduler.next_due[(M_ARB.key, addr)] == 1060.0

    # Advance to 1059: not due
    clock.advance(59.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=near_reader, clock=clock)
    assert read_calls == 1

    # Advance to 1060: due!
    clock.advance(1.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=near_reader, clock=clock)
    assert read_calls == 2
    assert scheduler.next_due[(M_ARB.key, addr)] == 1120.0


def test_poll_due_moving_from_far_to_near_shortens_next_interval(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1003, addr, M_ARB.key, state="ok")

    current_hf = Decimal("2.0")
    read_calls = 0

    def dynamic_reader(market, address):
        nonlocal read_calls
        read_calls += 1
        return AccountData(Decimal(10000), Decimal(5000), current_hf, False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    # 1st read at 1000: far position (HF 2.0) -> next due 1300
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=dynamic_reader, clock=clock)
    assert read_calls == 1
    assert scheduler.next_due[(M_ARB.key, addr)] == 1300.0

    # Advance clock to 1300; position drops to near (HF 1.12)
    clock.advance(300.0)
    current_hf = Decimal("1.12")
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=dynamic_reader, clock=clock)
    assert read_calls == 2
    # Interval shortened to 60s -> next due 1360
    assert scheduler.next_due[(M_ARB.key, addr)] == 1360.0

    # Advance clock to 1360: due!
    clock.advance(60.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=dynamic_reader, clock=clock)
    assert read_calls == 3


def test_poll_due_two_chats_different_levels_use_shorter_interval(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    # Chat 1004 has custom levels: (2.0, 1.5, 1.25)
    # At HF = 1.70: d = 1.70 - 1.50 = 0.20 < 0.30 -> interval 120s
    set_levels(db_path, 1004, (Decimal("2.0"), Decimal("1.5"), Decimal("1.25")))
    add_watch(db_path, 1004, addr, M_ARB.key, state="ok")

    # Chat 1005 has default levels: (1.4, 1.2, 1.1, 1.05)
    # At HF = 1.70: d = 1.70 - 1.40 = 0.30 >= 0.30, hf >= 1.4 -> interval 300s
    add_watch(db_path, 1005, addr, M_ARB.key, state="ok")

    read_calls = 0

    def reader(market, address):
        nonlocal read_calls
        read_calls += 1
        return AccountData(Decimal(10000), Decimal(5800), Decimal("1.70"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    assert read_calls == 1
    # Shorter interval: min(120, 300) = 120s -> next due 1120.0
    assert scheduler.next_due[(M_ARB.key, addr)] == 1120.0

    # Advance to 1119: not due
    clock.advance(119.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    assert read_calls == 1

    # Advance to 1120: due!
    clock.advance(1.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    assert read_calls == 2


def test_poll_due_failure_backoff_and_reset_on_success(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1006, addr, M_ARB.key, state="ok")

    should_fail = True

    def flake_reader(market, address):
        if should_fail:
            raise ReadError("RPC Timeout")
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    # 1st failure: streak 1 -> backoff 60 -> next_due 1060
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 1060.0

    # 2nd failure at 1060: streak 2 -> backoff 120 -> next_due 1180
    clock.advance(60.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 1180.0

    # 3rd failure at 1180: streak 3 -> backoff 240 -> next_due 1420
    clock.advance(120.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 1420.0

    # 4th failure at 1420: streak 4 -> backoff 300 -> next_due 1720
    clock.advance(240.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 1720.0

    # 5th failure at 1720: streak 5 -> backoff 300 -> next_due 2020
    clock.advance(300.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 2020.0

    # Success at 2020: streak reset, safe position -> next_due = 2020 + 300 = 2320
    clock.advance(300.0)
    should_fail = False
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 2320.0
    assert scheduler.get_fail_streak((M_ARB.key, addr)) == 0

    # Failure again at 2320: streak starts fresh at 1 -> backoff 60 -> next_due = 2380
    clock.advance(300.0)
    should_fail = True
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=flake_reader, clock=clock)
    assert scheduler.next_due[(M_ARB.key, addr)] == 2380.0
    assert scheduler.get_fail_streak((M_ARB.key, addr)) == 1


def test_poll_due_pair_removed_from_db_is_forgotten(db_path: Path):
    from hfwb.store import delete_chat

    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1007, addr, M_ARB.key, state="ok")

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    def reader(market, address):
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    pair = (M_ARB.key, addr)
    assert pair in scheduler.next_due

    # Remove watch by deleting chat
    delete_chat(db_path, 1007)
    assert pair not in distinct_pairs(db_path)

    # Next cycle prunes forgotten pair
    clock.advance(15.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    assert pair not in scheduler.next_due
    assert pair not in scheduler.fail_streak


def test_poll_due_heartbeat_written_on_tick_with_nothing_due(db_path: Path, tmp_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1008, addr, M_ARB.key, state="ok")

    read_calls = 0

    def reader(market, address):
        nonlocal read_calls
        read_calls += 1
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()
    hb = tmp_path / "heartbeat.txt"

    # Initial poll at 1000: read once, next due 1300, hb = 1000
    poll_due(
        db_path,
        scheduler=scheduler,
        markets=[M_ARB],
        tg_client=tg,
        reader_fn=reader,
        clock=clock,
        heartbeat_file=hb,
    )
    assert read_calls == 1
    assert hb.read_text().strip() == "1000"

    # Tick at 1015: nothing is due
    clock.advance(15.0)
    poll_due(
        db_path,
        scheduler=scheduler,
        markets=[M_ARB],
        tg_client=tg,
        reader_fn=reader,
        clock=clock,
        heartbeat_file=hb,
    )
    # Reader was NOT called
    assert read_calls == 1
    # But heartbeat file was updated
    assert hb.read_text().strip() == "1015"


def test_poll_due_reader_called_once_per_due_pair_per_tick(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    # 3 chats watching the exact same (market, address) pair
    add_watch(db_path, 1009, addr, M_ARB.key, state="ok")
    add_watch(db_path, 1010, addr, M_ARB.key, state="ok")
    add_watch(db_path, 1011, addr, M_ARB.key, state="ok")

    read_calls: list[tuple[str, str]] = []

    def reader(market, address):
        read_calls.append((market.key, address))
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = FakeTelegramClient()

    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)
    assert len(read_calls) == 1
    assert read_calls[0] == (M_ARB.key, addr)


def test_run_poll_loop_tick_seconds_and_heartbeat(db_path: Path, tmp_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    add_watch(db_path, 1012, addr, M_ARB.key, state="ok")

    hb = tmp_path / "hb_loop.txt"
    clock = FakeClock(5000.0)
    scheduler = PollScheduler()
    stop_event = threading.Event()

    calls = 0

    def counting_reader(market, address):
        nonlocal calls
        calls += 1
        return AccountData(Decimal(10000), Decimal(4000), Decimal("2.5"), False, True)

    # Pre-set stop_event so run_poll_loop executes exactly one iteration if checked, or we use a thread
    t = threading.Thread(
        target=run_poll_loop,
        kwargs={
            "stop_event": stop_event,
            "db_path": db_path,
            "markets": [M_ARB],
            "reader_fn": counting_reader,
            "clock": clock,
            "heartbeat_file": hb,
            "tick_seconds": 0.05,
            "scheduler": scheduler,
        },
    )
    t.start()
    # Give it a short moment to run tick
    stop_event.wait(0.08)
    stop_event.set()
    t.join(timeout=2.0)

    assert calls >= 1
    assert hb.is_file()
    assert hb.read_text().strip() == "5000"




def test_poll_due_unexpected_error_while_processing_backs_off_and_isolates(db_path: Path):
    addr_bad = "0x1111111111111111111111111111111111111111"
    addr_ok = "0x2222222222222222222222222222222222222222"
    add_watch(db_path, 3001, addr_bad, M_ARB.key, state="ok")
    add_watch(db_path, 3002, addr_ok, M_ARB.key, state="ok")

    class ExplodingTelegram:
        """Unexpected (non-Telegram) error while sending the alert of the first chat only."""

        def __init__(self) -> None:
            self.sent: list[int] = []

        def send_message(self, chat_id: int, text: str) -> dict:
            if chat_id == 3001:
                raise RuntimeError("unexpected bug")
            self.sent.append(chat_id)
            return {"ok": True}

    def reader(market, address):  # both positions are in danger, so both try to alert
        return AccountData(Decimal(10000), Decimal(9000), Decimal("1.1"), False, True)

    clock = FakeClock(1000.0)
    scheduler = PollScheduler()
    tg = ExplodingTelegram()
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=reader, clock=clock)

    assert tg.sent == [3002]  # the healthy pair was still processed
    bad = (M_ARB.key, addr_bad)
    assert scheduler.next_due[bad] == 1060.0  # backed off (60 s), not re-read on every tick
    assert scheduler.get_fail_streak(bad) == 1
    # one tick later (15 s) the bad pair is NOT read again
    reads: list[str] = []

    def counting_reader(market, address):
        reads.append(address)
        return reader(market, address)

    clock.advance(15.0)
    poll_due(db_path, scheduler=scheduler, markets=[M_ARB], tg_client=tg, reader_fn=counting_reader, clock=clock)
    assert addr_bad not in reads
