"""Wiring of the price-drop lines into alerts, /watch, /list, /help and the /whatif command."""
from decimal import Decimal
from pathlib import Path

import pytest

from hfwb.aave import AccountData, ReadError
from hfwb.bot import BotHandler
from hfwb.format import format_alert, format_help, format_scan_response
from hfwb.markets import Market
from hfwb.positions import AssetPosition, PositionBreakdown, recompute
from hfwb.runner import poll_once
from hfwb.scan import ScanResult
from hfwb.store import add_tracked_address, add_watch, init_db

D = Decimal
ADDR = "0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"

M_ARB = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("https://arb.rpc.test",),
)
M_V4 = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("https://eth.rpc.test",),
)


def pos(sym, coll, debt, price, lt="0.8", used=True) -> AssetPosition:
    return AssetPosition(
        symbol=sym,
        asset="0x" + sym.lower().encode().hex().ljust(40, "0")[:40],
        decimals=18,
        collateral=D(coll),
        debt=D(debt),
        price_usd=D(price),
        liq_threshold=D(lt),
        used_as_collateral=used,
    )


def bd(*assets) -> PositionBreakdown:
    coll, weighted, debt, hf = recompute(assets)
    return PositionBreakdown(
        assets=tuple(assets), emode_category=0, collateral_usd=coll,
        weighted_collateral_usd=weighted, debt_usd=debt, hf=hf,
    )


def acct(b: PositionBreakdown) -> AccountData:
    return AccountData(collateral_usd=b.collateral_usd, debt_usd=b.debt_usd, hf=b.hf, no_debt=False)


# HF 1.17: 0.05 WBTC @ 60,000 (LT .78 -> 2,340) vs 2,000 USDC
LOW = bd(pos("WBTC", "0.05", 0, 60000, "0.78"), pos("USDC", 0, 2000, 1, "0.78", used=False))
DETAIL = "Liquidation if, with other prices unchanged:"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"ok": True}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "wiring.db"
    init_db(p)
    return p


# ---------------------------------------------------------------- format functions


def test_format_alert_details_replace_generic_drop_line():
    generic = format_alert("L3", ADDR, D("1.17"), D(3000), D(2000), market=M_ARB)
    assert "of collateral value can fall" in generic
    msg = format_alert("L3", ADDR, D("1.17"), D(3000), D(2000), market=M_ARB, details=[DETAIL, "• WBTC −14.5% (to $51,282)"])
    assert DETAIL in msg
    assert "• WBTC −14.5% (to $51,282)" in msg
    assert "of collateral value can fall" not in msg
    assert msg.index(DETAIL) < msg.index("not financial advice")


def test_format_alert_empty_details_keeps_generic_line():
    msg = format_alert("L3", ADDR, D("1.17"), D(3000), D(2000), market=M_ARB, details=[])
    assert "of collateral value can fall" in msg


def test_format_scan_response_details_per_market():
    found = [(M_ARB, acct(LOW)), (M_V4, AccountData(None, None, D("1.5"), False))]
    msg = format_scan_response(ADDR, found, details={M_ARB.key: [DETAIL, "• WBTC −14.5% (to $51,282)"]})
    arb_part = msg.split("Aave V4")[0]
    assert DETAIL in arb_part
    assert "• WBTC −14.5%" in arb_part
    assert msg.count(DETAIL) == 1


def test_help_mentions_whatif():
    assert "/whatif" in format_help()


# ---------------------------------------------------------------- alerts in the poll loop


def test_alert_includes_liquidation_lines(db_path: Path):
    add_watch(db_path, 7, ADDR, M_ARB.key, state="ok")
    tg = FakeTelegram()
    reads = []

    def positions_reader(market, address):
        reads.append(address)
        return LOW

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=lambda m, a: acct(LOW),
              positions_reader=positions_reader, clock=Clock())
    assert len(tg.sent) == 1
    assert DETAIL in tg.sent[0][1]
    assert "WBTC −14.5%" in tg.sent[0][1]
    assert reads == [ADDR]


def test_no_positions_read_when_no_alert(db_path: Path):
    healthy = bd(pos("WBTC", "1", 0, 60000, "0.78"), pos("USDC", 0, 2000, 1, "0.78", used=False))
    add_watch(db_path, 7, ADDR, M_ARB.key, state="ok")
    tg = FakeTelegram()

    def positions_reader(market, address):
        raise AssertionError("must not read without an alert")

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=lambda m, a: acct(healthy),
              positions_reader=positions_reader, clock=Clock())
    assert tg.sent == []


def test_alert_still_sent_when_positions_read_fails(db_path: Path):
    add_watch(db_path, 7, ADDR, M_ARB.key, state="ok")
    tg = FakeTelegram()

    def positions_reader(market, address):
        raise ReadError("rpc down")

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=lambda m, a: acct(LOW),
              positions_reader=positions_reader, clock=Clock())
    assert len(tg.sent) == 1
    assert "of collateral value can fall" in tg.sent[0][1]


def test_one_positions_read_for_several_chats(db_path: Path):
    add_watch(db_path, 7, ADDR, M_ARB.key, state="ok")
    add_watch(db_path, 8, ADDR, M_ARB.key, state="ok")
    tg = FakeTelegram()
    reads = []

    def positions_reader(market, address):
        reads.append(address)
        return LOW

    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=lambda m, a: acct(LOW),
              positions_reader=positions_reader, clock=Clock())
    assert len(tg.sent) == 2
    assert all(DETAIL in text for _, text in tg.sent)
    assert reads == [ADDR]


def test_poll_without_positions_reader_uses_generic_line(db_path: Path):
    add_watch(db_path, 7, ADDR, M_ARB.key, state="ok")
    tg = FakeTelegram()
    poll_once(db_path, markets=[M_ARB], tg_client=tg, reader_fn=lambda m, a: acct(LOW), clock=Clock())
    assert "of collateral value can fall" in tg.sent[0][1]


# ---------------------------------------------------------------- bot: /watch, /list, /whatif


def handler(db_path, positions_reader=None, account=None, clock=None):
    account = account or acct(LOW)
    return BotHandler(
        db_path,
        markets=[M_ARB, M_V4],
        clock=clock or Clock(),
        aave_reader=lambda m, a: account,
        scanner_fn=lambda address, markets, reader=None: ScanResult(found=[(M_ARB, account)], failed=[]),
        positions_reader=positions_reader,
    )


def msg(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def test_watch_shows_liquidation_lines(db_path: Path):
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, f"/watch {ADDR}"))
    assert DETAIL in text
    assert "WBTC −14.5%" in text


def test_watch_without_positions_reader_has_no_details(db_path: Path):
    h = handler(db_path, positions_reader=None)
    [(_, text)] = h.handle(msg(5, f"/watch {ADDR}"))
    assert DETAIL not in text


def test_list_mentions_whatif_when_watching(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    h = handler(db_path)
    [(_, text)] = h.handle(msg(5, "/list"))
    assert "/whatif" in text


def test_whatif_uses_chat_watches(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, "/whatif BTC -10"))
    assert "What if BTC −10%?" in text
    # 1.17 -> 1.053
    assert "1.17 → 1.05" in text


def test_whatif_usage_on_bad_input(db_path: Path):
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, "/whatif"))
    assert "/whatif ETH -20" in text
    [(_, text)] = h.handle(msg(5, "/whatif ETH NaN"))
    assert "/whatif ETH -20" in text


def test_whatif_bad_input_is_escaped(db_path: Path):
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, "/whatif <b>x -20"))
    assert "<b>x" not in text


def test_whatif_cooldown(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    clock = Clock()
    h = handler(db_path, positions_reader=lambda m, a: LOW, clock=clock)
    h.handle(msg(5, "/whatif BTC -10"))
    clock.advance(3)
    [(_, text)] = h.handle(msg(5, "/whatif BTC -20"))
    assert "wait" in text.lower()
    clock.advance(10)
    [(_, text)] = h.handle(msg(5, "/whatif BTC -20"))
    assert "What if BTC −20%?" in text


def test_whatif_bad_input_does_not_start_cooldown(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    h.handle(msg(5, "/whatif BTC"))
    [(_, text)] = h.handle(msg(5, "/whatif BTC -20"))
    assert "What if BTC −20%?" in text


def test_whatif_without_watches(db_path: Path):
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, "/whatif ETH -20"))
    assert "/watch" in text


def test_whatif_disabled_without_positions_reader(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    h = handler(db_path, positions_reader=None)
    [(_, text)] = h.handle(msg(5, "/whatif ETH -20"))
    assert "not available" in text.lower()


def test_whatif_command_with_bot_suffix(db_path: Path):
    add_tracked_address(db_path, 5, ADDR)
    add_watch(db_path, 5, ADDR, M_ARB.key, state="ok")
    h = handler(db_path, positions_reader=lambda m, a: LOW)
    [(_, text)] = h.handle(msg(5, "/whatif@healthfactor_watch_bot BTC -10"))
    assert "What if BTC −10%?" in text
