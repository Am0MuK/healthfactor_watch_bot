from decimal import Decimal
from pathlib import Path

import pytest

from hfwb.aave import AccountData
from hfwb.bot import BotHandler
from hfwb.markets import Market
from hfwb.scan import ScanResult
from hfwb.store import get_levels, init_db, list_tracked_addresses, list_watches

M_ARB = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("http://rpc.arb",),
)

M_ETH = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("http://rpc.eth",),
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "bot_test.db"
    init_db(p)
    return p


def test_start_command(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB, M_ETH])
    update = {"message": {"chat": {"id": 123}, "text": "/start"}}
    replies = handler.handle(update)
    assert len(replies) == 1
    chat_id, text = replies[0]
    assert chat_id == 123
    assert "Health Factor Watch Bot" in text
    assert "/levels" in text


def test_help_command(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB, M_ETH])
    update = {"message": {"chat": {"id": 123}, "text": "/help@healthfactor_watch_bot"}}
    replies = handler.handle(update)
    assert len(replies) == 1
    chat_id, text = replies[0]
    assert chat_id == 123
    assert "/watch" in text
    assert "/rescan" in text
    assert "/levels" in text


def test_privacy_command(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB, M_ETH])
    update = {"message": {"chat": {"id": 123}, "text": "/privacy"}}
    replies = handler.handle(update)
    assert len(replies) == 1
    _, text = replies[0]
    assert "chat_id, wallet address, market list, and alert state only" in text
    assert "/delete" in text


def test_watch_command_with_positions(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[
                (M_ARB, AccountData(Decimal(10000), Decimal(7000), Decimal("1.35"), False, True)),
                (M_ETH, AccountData(None, None, Decimal("1.25"), False, True)),
            ],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    update = {"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}}
    replies = handler.handle(update)

    assert len(replies) == 1
    _, text = replies[0]
    assert "0x794a…14ad" in text
    assert "Aave V3 · Arbitrum" in text
    assert "1.35" in text
    assert "Aave V4 · Ethereum · Main Spoke" in text
    assert "1.25" in text

    # Verify watches were saved in DB
    watches = list_watches(db_path, 123)
    assert len(watches) == 2
    keys = {w.market_key for w in watches}
    assert M_ARB.key in keys
    assert M_ETH.key in keys

    # Verify tracked address was saved
    tracked = list_tracked_addresses(db_path, 123)
    assert len(tracked) == 1
    assert tracked[0].address == addr


def test_watch_command_no_position_anywhere(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    def fake_scanner(address, markets, reader=None):
        return ScanResult(found=[], failed=[M_ETH])

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    update = {"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}}
    replies = handler.handle(update)

    assert len(replies) == 1
    _, text = replies[0]
    assert "No open positions" in text
    assert "daily rescan" in text
    assert "1 market(s) could not be checked" in text

    # Address is tracked even if no active position
    tracked = list_tracked_addresses(db_path, 123)
    assert len(tracked) == 1
    assert tracked[0].address == addr
    # No watches saved yet
    assert len(list_watches(db_path, 123)) == 0


def test_watch_command_invalid_address(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB, M_ETH])
    update = {"message": {"chat": {"id": 123}, "text": "/watch not_an_address"}}
    replies = handler.handle(update)

    assert len(replies) == 1
    _, text = replies[0]
    assert "Invalid Ethereum address" in text
    assert len(list_watches(db_path, 123)) == 0


def test_watch_command_address_limit(db_path: Path):
    def fake_scanner(address, markets, reader=None):
        return ScanResult(found=[], failed=[])

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    addrs = [
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ]
    for a in addrs:
        res = handler.handle({"message": {"chat": {"id": 123}, "text": f"/watch {a}"}})
        assert "No open positions" in res[0][1] or "watching" in res[0][1].lower()

    assert len(list_tracked_addresses(db_path, 123)) == 3

    # 4th watch should fail with limit message
    res = handler.handle(
        {"message": {"chat": {"id": 123}, "text": "/watch 0x4444444444444444444444444444444444444444"}}
    )
    assert "limit" in res[0][1].lower()
    assert len(list_tracked_addresses(db_path, 123)) == 3


def test_rescan_command_and_rate_limit(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    clock = FakeClock(1000.0)

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[(M_ARB, AccountData(Decimal(1000), Decimal(500), Decimal("1.50"), False, True))],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], clock=clock, scanner_fn=fake_scanner)
    # First watch
    handler.handle({"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}})

    # Rescan immediately
    res1 = handler.handle({"message": {"chat": {"id": 123}, "text": f"/rescan {addr}"}})
    assert "Rescan completed" in res1[0][1]

    # Second rescan within 5 minutes (300s) is rate-limited
    res2 = handler.handle({"message": {"chat": {"id": 123}, "text": f"/rescan {addr}"}})
    assert "Rescan rate limit" in res2[0][1]

    # After 301 seconds, rescan succeeds again
    clock.advance(301.0)
    res3 = handler.handle({"message": {"chat": {"id": 123}, "text": f"/rescan {addr}"}})
    assert "Rescan completed" in res3[0][1]


def test_list_and_remove_commands(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[(M_ARB, AccountData(Decimal(1000), Decimal(500), Decimal("1.50"), False, True))],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    # Empty list
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/list"}})
    assert "No addresses" in res[0][1]

    # Add watch
    handler.handle({"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}})

    # Non-empty list grouped by address then market
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/list"}})
    assert "0x794a…14ad" in res[0][1]
    assert "Aave V3 · Arbitrum" in res[0][1]

    # Remove address
    res = handler.handle({"message": {"chat": {"id": 123}, "text": f"/remove {addr}"}})
    assert "Removed" in res[0][1]
    assert len(list_watches(db_path, 123)) == 0
    assert len(list_tracked_addresses(db_path, 123)) == 0

    # Remove again
    res = handler.handle({"message": {"chat": {"id": 123}, "text": f"/remove {addr}"}})
    assert "was not in your watch list" in res[0][1]


def test_delete_command(db_path: Path):
    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[(M_ARB, AccountData(Decimal(1000), Decimal(500), Decimal("1.50"), False, True))],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    handler.handle(
        {
            "message": {
                "chat": {"id": 123},
                "text": "/watch 0x1111111111111111111111111111111111111111",
            }
        }
    )
    assert len(list_tracked_addresses(db_path, 123)) == 1

    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/delete"}})
    assert "Deleted all data" in res[0][1]
    assert len(list_watches(db_path, 123)) == 0
    assert len(list_tracked_addresses(db_path, 123)) == 0


def test_rate_limiting_10_per_min(db_path: Path):
    clock = FakeClock(1000.0)
    handler = BotHandler(db_path, markets=[M_ARB], clock=clock)
    chat_id = 456

    # 10 commands in 60s should succeed
    for _ in range(10):
        res = handler.handle({"message": {"chat": {"id": chat_id}, "text": "/help"}})
        assert "Commands" in res[0][1]

    # 11th command within the 60s window should be rate-limited
    res = handler.handle({"message": {"chat": {"id": chat_id}, "text": "/help"}})
    assert "Rate limit exceeded" in res[0][1]

    # Advance clock by 61s
    clock.advance(61.0)
    res = handler.handle({"message": {"chat": {"id": chat_id}, "text": "/help"}})
    assert "Commands" in res[0][1]


def test_unknown_command(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/foobar"}})
    assert "Unknown command" in res[0][1]


def test_watch_over_position_limit_still_replies_and_caps_at_30(db_path: Path):
    from hfwb.store import MAX_WATCHES_PER_CHAT

    many = [
        Market("aave_v3", f"Chain{i}", 1000 + i, "Main market", f"0x{i:040x}", ("http://rpc",))
        for i in range(MAX_WATCHES_PER_CHAT + 1)
    ]

    def fake_scanner(address, markets, reader=None):
        found = [(m, AccountData(Decimal(1000), Decimal(500), Decimal("1.5"), False, True)) for m in many]
        return ScanResult(found=found, failed=[])

    handler = BotHandler(db_path, markets=many, scanner_fn=fake_scanner)
    res = handler.handle(
        {"message": {"chat": {"id": 55}, "text": "/watch 0x1111111111111111111111111111111111111111"}}
    )
    assert res and "limit" in res[0][1].lower()
    assert len(list_watches(db_path, 55)) == MAX_WATCHES_PER_CHAT


def test_watch_with_failed_markets_is_not_marked_scanned(db_path: Path):
    def fake_scanner(address, markets, reader=None):
        return ScanResult(found=[], failed=[M_ETH])

    handler = BotHandler(db_path, markets=[M_ARB, M_ETH], scanner_fn=fake_scanner)
    handler.handle({"message": {"chat": {"id": 56}, "text": "/watch 0x1111111111111111111111111111111111111111"}})
    assert list_tracked_addresses(db_path, 56)[0].last_scan_ts is None  # stays due for the next rescan


def test_levels_command_no_args(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels"}})
    assert len(res) == 1
    assert "1.4 / 1.2 / 1.1 / 1.05" in res[0][1]
    assert "(default)" in res[0][1]


def test_levels_command_set_space_separated(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.5 1.3 1.15 1.05"}})
    assert len(res) == 1
    assert "1.5 / 1.3 / 1.15 / 1.05" in res[0][1]
    assert "re-armed" in res[0][1]
    assert get_levels(db_path, 123) == (Decimal("1.5"), Decimal("1.3"), Decimal("1.15"), Decimal("1.05"))

    # Viewing levels now shows updated without (default)
    res2 = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels"}})
    assert "1.5 / 1.3 / 1.15 / 1.05" in res2[0][1]
    assert "(default)" not in res2[0][1]


def test_levels_command_set_comma_separated(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.6,1.4,1.2"}})
    assert len(res) == 1
    assert "1.6 / 1.4 / 1.2" in res[0][1]
    assert get_levels(db_path, 123) == (Decimal("1.6"), Decimal("1.4"), Decimal("1.2"))


def test_levels_command_set_mixed_whitespace_and_commas(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.6,  1.4 , 1.2"}})
    assert len(res) == 1
    assert "1.6 / 1.4 / 1.2" in res[0][1]
    assert get_levels(db_path, 123) == (Decimal("1.6"), Decimal("1.4"), Decimal("1.2"))


def test_levels_command_reset(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.6,1.4,1.2"}})
    assert get_levels(db_path, 123) == (Decimal("1.6"), Decimal("1.4"), Decimal("1.2"))

    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels reset"}})
    assert len(res) == 1
    assert "reset to default" in res[0][1]
    assert get_levels(db_path, 123) == (Decimal("1.4"), Decimal("1.2"), Decimal("1.1"), Decimal("1.05"))


def test_levels_command_invalid_input(db_path: Path):
    from hfwb.state import DEFAULT_LEVELS

    handler = BotHandler(db_path, markets=[M_ARB])

    # Not numbers
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels not_numbers"}})
    assert "Invalid alert levels" in res[0][1]
    assert get_levels(db_path, 123) == DEFAULT_LEVELS

    # Ascending
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.2 1.4"}})
    assert "Invalid alert levels" in res[0][1]
    assert get_levels(db_path, 123) == DEFAULT_LEVELS

    # Out of range (< 1.01)
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.4 1.00"}})
    assert "Invalid alert levels" in res[0][1]
    assert get_levels(db_path, 123) == DEFAULT_LEVELS

    # Too many decimals
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.455 1.20"}})
    assert "Invalid alert levels" in res[0][1]
    assert get_levels(db_path, 123) == DEFAULT_LEVELS


def test_levels_command_rate_limit(db_path: Path):
    clock = FakeClock(1000.0)
    handler = BotHandler(db_path, markets=[M_ARB], clock=clock)

    for _ in range(10):
        res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels"}})
        assert "Alert levels" in res[0][1]

    # 11th is rate limited
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/levels"}})
    assert "Rate limit exceeded" in res[0][1]


def test_watch_and_scan_uses_chat_levels(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[
                (M_ARB, AccountData(Decimal(10000), Decimal(7000), Decimal("1.70"), False, True)),
            ],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB], scanner_fn=fake_scanner)
    # Set custom levels: 1.8, 1.4 (2 levels). HF 1.70 is < 1.80, so L1
    handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.8 1.4"}})

    res = handler.handle({"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}})
    assert len(res) == 1
    # Emoji in scan response: level_emoji(1, 2) is 🔴 (r = 2 - 1 = 1)
    assert "🔴 <b>Aave V3 · Arbitrum</b>" in res[0][1]

    # Check watch state saved: initial state is L1 (not ok!)
    watches = list_watches(db_path, 123)
    assert len(watches) == 1
    assert watches[0].state == "L1"


def test_list_uses_chat_levels(db_path: Path):
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    def fake_scanner(address, markets, reader=None):
        return ScanResult(
            found=[(M_ARB, AccountData(Decimal(1000), Decimal(500), Decimal("1.50"), False, True))],
            failed=[],
        )

    handler = BotHandler(db_path, markets=[M_ARB], scanner_fn=fake_scanner)
    handler.handle({"message": {"chat": {"id": 123}, "text": "/levels 1.8 1.4"}})
    handler.handle({"message": {"chat": {"id": 123}, "text": f"/watch {addr}"}})

    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/list"}})
    assert len(res) == 1
    assert res[0][1].strip().endswith("Levels: 1.8 / 1.4")


def test_depeg_status_command(db_path: Path):
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg"}})
    assert len(res) == 1
    assert "off" in res[0][1].lower()


def test_depeg_on_all_and_status(db_path: Path):
    from hfwb.store import list_depeg_subs
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg on"}})
    assert len(res) == 1
    assert "Subscribed to stablecoin depeg alerts" in res[0][1]

    subs = list_depeg_subs(db_path, 123)
    assert len(subs) == 12

    # /depeg now shows status with active symbols
    status_res = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg"}})
    assert "USDC" in status_res[0][1]
    assert "USDT" in status_res[0][1]


def test_depeg_on_specific_symbols(db_path: Path):
    from hfwb.store import list_depeg_subs
    handler = BotHandler(db_path, markets=[M_ARB])
    # Case-insensitive with comma and spaces
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg on usdc,  usdt"}})
    assert len(res) == 1
    assert "USDC, USDT" in res[0][1]

    subs = list_depeg_subs(db_path, 123)
    assert len(subs) == 2
    assert {s.symbol for s in subs} == {"USDC", "USDT"}


def test_depeg_on_unknown_symbol(db_path: Path):
    from hfwb.store import list_depeg_subs
    handler = BotHandler(db_path, markets=[M_ARB])
    res = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg on USDC FOO"}})
    assert len(res) == 1
    assert "Invalid stablecoin symbol" in res[0][1]
    assert "FOO" in res[0][1]
    assert "USDC" in res[0][1]  # Supported symbols listed

    # Nothing saved
    subs = list_depeg_subs(db_path, 123)
    assert len(subs) == 0


def test_depeg_off_commands(db_path: Path):
    from hfwb.store import list_depeg_subs
    handler = BotHandler(db_path, markets=[M_ARB])
    handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg on USDC USDT DAI"}})
    assert len(list_depeg_subs(db_path, 123)) == 3

    # Remove one
    res_off_one = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg off usdc"}})
    assert "Removed stablecoin depeg alerts for <b>USDC</b>" in res_off_one[0][1]
    subs = list_depeg_subs(db_path, 123)
    assert len(subs) == 2
    assert {s.symbol for s in subs} == {"USDT", "DAI"}

    # Remove all
    res_off_all = handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg off"}})
    assert "turned <b>off</b> for all tokens" in res_off_all[0][1]
    assert len(list_depeg_subs(db_path, 123)) == 0


def test_depeg_delete_wipes_subs(db_path: Path):
    from hfwb.store import list_depeg_subs
    handler = BotHandler(db_path, markets=[M_ARB])
    handler.handle({"message": {"chat": {"id": 123}, "text": "/depeg on USDC"}})
    assert len(list_depeg_subs(db_path, 123)) == 1

    handler.handle({"message": {"chat": {"id": 123}, "text": "/delete"}})
    assert len(list_depeg_subs(db_path, 123)) == 0

