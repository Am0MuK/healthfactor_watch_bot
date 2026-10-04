import re
from decimal import Decimal

from hfwb.aave import AccountData
from hfwb.format import (
    FOOTER,
    drop_to_liquidation_pct,
    format_alert,
    format_delete,
    format_help,
    format_levels,
    format_levels_error,
    format_levels_reset,
    format_levels_saved,
    format_list,
    format_market_name,
    format_new_position,
    format_privacy,
    format_rate_limit,
    format_remove,
    format_rescan_rate_limit,
    format_scan_response,
    format_short_address,
    format_start,
    format_unknown,
    format_watch_failure,
    format_watch_limit,
    format_watch_success,
    hf_emoji,
    hf_gauge,
    level_emoji,
    profile_link,
)
from hfwb.markets import Market
from hfwb.state import DEFAULT_LEVELS

FORBIDDEN_WORDS = [
    "buy",
    "sell",
    "deposit",
    "repay",
    "add collateral",
    "should",
    "you need to",
    "act now",
]

M_V3 = Market(
    protocol="aave_v3",
    chain="Base",
    chain_id=8453,
    name="Main market",
    address="0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
    rpcs=("https://base.rpc.test",),
)

M_V4 = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("https://eth.rpc.test",),
)


def assert_no_forbidden_words(text: str) -> None:
    text_lower = text.lower()
    for word in FORBIDDEN_WORDS:
        if " " in word:
            assert word not in text_lower, f"Forbidden phrase '{word}' found in message:\n{text}"
        else:
            pattern = rf"\b{re.escape(word)}\b"
            assert not re.search(pattern, text_lower), (
                f"Forbidden word '{word}' found in message:\n{text}"
            )


def test_short_address():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    assert format_short_address(addr) == "0x794a...14ad"


def test_hf_emoji():
    assert hf_emoji(None) == "🟢"
    assert hf_emoji(Decimal("1.50")) == "🟢"
    assert hf_emoji(Decimal("1.40")) == "🟢"
    assert hf_emoji(Decimal("1.39")) == "🟡"
    assert hf_emoji(Decimal("1.20")) == "🟡"
    assert hf_emoji(Decimal("1.19")) == "🟠"
    assert hf_emoji(Decimal("1.10")) == "🟠"
    assert hf_emoji(Decimal("1.09")) == "🔴"
    assert hf_emoji(Decimal("1.05")) == "🔴"
    assert hf_emoji(Decimal("1.04")) == "🚨"
    assert hf_emoji(Decimal("0.90")) == "🚨"


def test_hf_gauge():
    assert hf_gauge(Decimal("1.18")) == "▰▰▱▱▱▱▱▱▱▱"
    assert hf_gauge(Decimal("1.00")) == "▱▱▱▱▱▱▱▱▱▱"
    assert hf_gauge(Decimal("0.80")) == "▱▱▱▱▱▱▱▱▱▱"
    assert hf_gauge(Decimal("2.00")) == "▰▰▰▰▰▰▰▰▰▰"
    assert hf_gauge(Decimal("2.50")) == "▰▰▰▰▰▰▰▰▰▰"
    assert hf_gauge(None) == "▰▰▰▰▰▰▰▰▰▰"
    assert hf_gauge(Decimal("1.50")) == "▰▰▰▰▰▱▱▱▱▱"


def test_drop_to_liquidation_pct():
    assert drop_to_liquidation_pct(Decimal("1.18")) == 15.3
    assert drop_to_liquidation_pct(Decimal("1.00")) == 0.0
    assert drop_to_liquidation_pct(Decimal("0.85")) == 0.0
    assert drop_to_liquidation_pct(None) == 0.0
    assert drop_to_liquidation_pct(Decimal("2.00")) == 50.0


def test_profile_link():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    expected = '<a href="https://debank.com/profile/0x794a61358d6845594f94dc1db02a252b5b4814ad">0x794a…14ad</a>'
    assert profile_link(addr) == expected


def test_address_with_tags_is_escaped_defense_in_depth():
    malicious = "<b>0x1234567890abcdef1234567890abcdef12345678</b>"

    link = profile_link(malicious)
    assert "&lt;b&gt;" in link
    assert "<b>0x1234" not in link

    alert = format_alert("L1", malicious, Decimal("1.35"), market=M_V3)
    assert "&lt;b&gt;" in alert
    assert "<b>0x1234" not in alert

    scan = format_scan_response(
        malicious,
        [(M_V3, AccountData(Decimal(1000), Decimal(500), Decimal("1.5"), False, True))],
    )
    assert "&lt;b&gt;" in scan
    assert "<b>0x1234" not in scan

    new_pos = format_new_position(malicious, M_V3, Decimal("1.5"))
    assert "&lt;b&gt;" in new_pos
    assert "<b>0x1234" not in new_pos


def test_format_market_name():
    assert format_market_name(M_V3) == "Aave V3 · Base"
    assert format_market_name(M_V4) == "Aave V4 · Ethereum · Main Spoke"


def test_format_alert_v3_with_market():
    msg = format_alert(
        alert_type="L1",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.35"),
        collateral_usd=Decimal(10000),
        debt_usd=Decimal(7000),
        market=M_V3,
    )
    assert "🟡 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.35</b>" in msg
    assert "Fell below <b>1.4</b>" in msg
    assert "Collateral $10,000 · Debt $7,000" in msg
    assert profile_link("0x794a61358d6845594f94dc1db02a252b5b4814ad") in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_alert_v4_omits_usd_lines():
    msg = format_alert(
        alert_type="L2",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.18"),
        collateral_usd=None,
        debt_usd=None,
        market=M_V4,
    )
    assert "🟠 <b>Aave V4 · Ethereum · Main Spoke</b>" in msg
    assert "Health factor <b>1.18</b>  ▰▰▱▱▱▱▱▱▱▱" in msg
    assert "Fell below <b>1.2</b>" in msg
    assert "About <b>15.3%</b> of collateral value can fall before liquidation at 1.00" in msg
    assert "Collateral" not in msg
    assert "Debt" not in msg
    assert profile_link("0x794a61358d6845594f94dc1db02a252b5b4814ad") in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_alert_l3():
    msg = format_alert(
        alert_type="L3",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.08"),
        collateral_usd=Decimal(10000),
        debt_usd=Decimal(9000),
        market=M_V3,
    )
    assert "🔴 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.08</b>" in msg
    assert "Fell below <b>1.1</b>" in msg
    assert "<b>Close to liquidation (1.00)</b>" in msg
    assert "About <b>7.4%</b> of collateral value can fall before liquidation at 1.00" in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_alert_l4():
    msg = format_alert(
        alert_type="L4",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.02"),
        collateral_usd=Decimal(10000),
        debt_usd=Decimal(9500),
        market=M_V3,
    )
    assert "🚨 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.02</b>" in msg
    assert "Fell below <b>1.05</b>" in msg
    assert "<b>Close to liquidation (1.00)</b>" in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_alert_repeat_l4():
    msg = format_alert(
        alert_type="repeat_L4",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.01"),
        collateral_usd=None,
        debt_usd=None,
        market=M_V4,
    )
    assert "🚨 <b>Aave V4 · Ethereum · Main Spoke</b>" in msg
    assert "Health factor <b>1.01</b>" in msg
    assert "Still below <b>1.05</b>" in msg
    assert "Next reminder in 30 min" in msg
    assert "<b>Close to liquidation (1.00)</b>" in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_alert_recovery():
    msg = format_alert(
        alert_type="recovery",
        address="0x794a61358d6845594f94dc1db02a252b5b4814ad",
        hf=Decimal("1.50"),
        collateral_usd=Decimal(10000),
        debt_usd=Decimal(4000),
        market=M_V3,
    )
    assert "🟢 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.50</b>" in msg
    assert "Back above <b>1.4</b>" in msg
    assert "<b>Close to liquidation (1.00)</b>" not in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_scan_response_found():
    found = [
        (M_V3, AccountData(Decimal(1000), Decimal(500), Decimal("1.56"), False, True)),
        (M_V4, AccountData(None, None, Decimal("1.25"), False, True)),
    ]
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    msg = format_scan_response(
        address=addr,
        found=found,
        failed_count=2,
    )
    assert profile_link(addr) in msg
    assert "🟢 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.56</b>" in msg
    assert "Collateral $1,000 · Debt $500" in msg
    assert "🟡 <b>Aave V4 · Ethereum · Main Spoke</b>" in msg
    assert "Health factor <b>1.25</b>" in msg
    assert "2 market(s) could not be checked right now and will be retried in the daily rescan." in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_scan_response_none_found():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    msg = format_scan_response(
        address=addr,
        found=[],
        failed_count=1,
    )
    assert "No open positions found for " in msg
    assert profile_link(addr) in msg
    assert "A daily rescan will watch for new positions." in msg
    assert "1 market(s) could not be checked right now and will be retried in the daily rescan." in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_format_list_grouped():
    addr1 = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    addr2 = "0x000000000000000000000000000000000000dead"
    grouped = [
        (addr1, [("Aave V3 · Base", "ok"), ("Aave V4 · Ethereum · Main Spoke", "L1")]),
        (addr2, []),
    ]
    msg = format_list(grouped)
    assert profile_link(addr1) in msg
    assert "🟢 Aave V3 · Base" in msg
    assert "🟡 Aave V4 · Ethereum · Main Spoke" in msg
    assert profile_link(addr2) in msg
    assert "no active market positions" in msg
    assert_no_forbidden_words(msg)


def test_format_list_with_hf():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    grouped = [
        (addr, [("Aave V3 · Base", Decimal("2.31"))]),
    ]
    msg = format_list(grouped)
    assert "🟢 Aave V3 · Base  HF 2.31" in msg


def test_format_new_position():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    msg = format_new_position(
        address=addr,
        market=M_V3,
        hf=Decimal("1.45"),
        collateral_usd=Decimal(10000),
        debt_usd=Decimal(5000),
    )
    assert "Found a new position for " in msg
    assert profile_link(addr) in msg
    assert "🟢 <b>Aave V3 · Base</b>" in msg
    assert "Health factor <b>1.45</b>" in msg
    assert "Collateral $10,000 · Debt $5,000" in msg
    assert FOOTER in msg
    assert_no_forbidden_words(msg)


def test_all_messages_forbidden_words():
    samples = [
        format_start(),
        format_help(),
        format_privacy(),
        format_delete(1),
        format_delete(0),
        format_rate_limit(),
        format_rescan_rate_limit(),
        format_unknown(),
        format_watch_failure("0x794a61358d6845594f94dc1db02a252b5b4814ad"),
        format_watch_success(
            "0x794a61358d6845594f94dc1db02a252b5b4814ad",
            Decimal("1.85"),
            Decimal(1000),
            Decimal(500),
            "ok",
        ),
        format_watch_limit(),
        format_list([]),
        format_list([("0x794a61358d6845594f94dc1db02a252b5b4814ad", [("Aave V3 · Base", "ok")])]),
        format_remove("0x794a61358d6845594f94dc1db02a252b5b4814ad", removed=True),
        format_remove("0x794a61358d6845594f94dc1db02a252b5b4814ad", removed=False),
        format_scan_response("0x794a61358d6845594f94dc1db02a252b5b4814ad", [], 0),
        format_new_position("0x794a61358d6845594f94dc1db02a252b5b4814ad", M_V3, Decimal("1.5")),
        format_levels(DEFAULT_LEVELS, is_default=True),
        format_levels((Decimal("1.8"), Decimal("1.4")), is_default=False),
        format_levels_error("Invalid"),
        format_levels_saved((Decimal("1.8"), Decimal("1.4"))),
        format_levels_reset(),
    ]
    for sample in samples:
        assert_no_forbidden_words(sample)


def test_level_emoji():
    # n=4: r = 4 - k
    assert level_emoji(4, 4) == "🚨"  # r=0
    assert level_emoji(3, 4) == "🔴"  # r=1
    assert level_emoji(2, 4) == "🟠"  # r=2
    assert level_emoji(1, 4) == "🟡"  # r=3

    # n=2:
    assert level_emoji(2, 2) == "🚨"  # r=0
    assert level_emoji(1, 2) == "🔴"  # r=1

    # n=3:
    assert level_emoji(3, 3) == "🚨"  # r=0
    assert level_emoji(2, 3) == "🔴"  # r=1
    assert level_emoji(1, 3) == "🟠"  # r=2

    # n=5:
    assert level_emoji(5, 5) == "🚨"  # r=0
    assert level_emoji(4, 5) == "🔴"  # r=1
    assert level_emoji(3, 5) == "🟠"  # r=2
    assert level_emoji(2, 5) == "🟡"  # r=3
    assert level_emoji(1, 5) == "🟡"  # r=4


def test_hf_emoji_custom_levels():
    levels = (Decimal("2.0"), Decimal("1.5"))
    assert hf_emoji(None, levels=levels) == "🟢"
    assert hf_emoji(Decimal("2.5"), levels=levels) == "🟢"
    assert hf_emoji(Decimal("2.0"), levels=levels) == "🟢"
    assert hf_emoji(Decimal("1.8"), levels=levels) == "🔴"  # below 2.0 (k=1, r=1)
    assert hf_emoji(Decimal("1.4"), levels=levels) == "🚨"  # below 1.5 (k=2, r=0)


def test_format_alert_custom_levels():
    levels = (Decimal("1.8"), Decimal("1.4"), Decimal("1.2"))
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"

    # L1 alert: Fell below 1.8; emoji is level_emoji(1, 3) = 🟠; NOT in lowest two levels
    msg_l1 = format_alert("L1", addr, Decimal("1.7"), market=M_V3, levels=levels)
    assert "🟠 <b>Aave V3 · Base</b>" in msg_l1
    assert "Fell below <b>1.8</b>" in msg_l1
    assert "Close to liquidation (1.00)" not in msg_l1

    # L2 alert: Fell below 1.4; emoji is level_emoji(2, 3) = 🔴; IS in lowest two levels
    msg_l2 = format_alert("L2", addr, Decimal("1.3"), market=M_V3, levels=levels)
    assert "🔴 <b>Aave V3 · Base</b>" in msg_l2
    assert "Fell below <b>1.4</b>" in msg_l2
    assert "Close to liquidation (1.00)" in msg_l2

    # repeat_L2 alert: Still below 1.4
    msg_rep = format_alert("repeat_L2", addr, Decimal("1.3"), market=M_V3, levels=levels)
    assert "Still below <b>1.4</b>" in msg_rep
    assert "Next reminder in 30 min" in msg_rep
    assert "Close to liquidation (1.00)" in msg_rep

    # Recovery alert: Back above 1.8 (names levels[0])
    msg_rec = format_alert("recovery", addr, Decimal("1.9"), market=M_V3, levels=levels)
    assert "🟢 <b>Aave V3 · Base</b>" in msg_rec
    assert "Back above <b>1.8</b>" in msg_rec


def test_format_list_levels_footer():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    grouped = [(addr, [("Aave V3 · Base", "ok")])]

    # With default levels
    msg_default = format_list(grouped, levels=DEFAULT_LEVELS)
    assert msg_default.strip().endswith("Levels: 1.4 / 1.2 / 1.1 / 1.05 (default)")

    # With custom levels
    custom = (Decimal("1.8"), Decimal("1.4"), Decimal("1.2"))
    msg_custom = format_list(grouped, levels=custom)
    assert msg_custom.strip().endswith("Levels: 1.8 / 1.4 / 1.2")
    assert "(default)" not in msg_custom


def test_format_levels_functions():
    msg_curr = format_levels(DEFAULT_LEVELS, is_default=True)
    assert "1.4 / 1.2 / 1.1 / 1.05" in msg_curr
    assert "(default)" in msg_curr
    assert_no_forbidden_words(msg_curr)

    custom = (Decimal("1.8"), Decimal("1.4"), Decimal("1.2"))
    msg_custom = format_levels(custom, is_default=False)
    assert "1.8 / 1.4 / 1.2" in msg_custom
    assert "(default)" not in msg_custom
    assert_no_forbidden_words(msg_custom)

    msg_err = format_levels_error("Must specify between 2 and 5 alert levels")
    assert "Must specify between 2 and 5 alert levels" in msg_err
    assert_no_forbidden_words(msg_err)

    msg_saved = format_levels_saved(custom)
    assert "1.8 / 1.4 / 1.2" in msg_saved
    assert "re-armed" in msg_saved
    assert_no_forbidden_words(msg_saved)

    msg_reset = format_levels_reset()
    assert "1.4 / 1.2 / 1.1 / 1.05" in msg_reset
    assert_no_forbidden_words(msg_reset)
