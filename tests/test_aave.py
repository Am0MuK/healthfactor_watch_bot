import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hfwb.aave import (
    AccountData,
    ReadError,
    normalize_address,
    read_market,
)
from hfwb.markets import Market

FIXTURES_DIR = Path(__file__).parent / "fixtures"

DUMMY_V3_MARKET = Market(
    protocol="aave_v3",
    chain="Arbitrum",
    chain_id=42161,
    name="Main market",
    address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    rpcs=("http://rpc.test",),
    best_effort=False,
)

DUMMY_V4_MARKET = Market(
    protocol="aave_v4",
    chain="Ethereum",
    chain_id=1,
    name="Main Spoke",
    address="0x973a023A77420ba610f06b3858aD991Df6d85A08",
    rpcs=("http://rpc.test",),
    best_effort=True,
)


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def test_normalize_address_valid_lowercase():
    addr = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
    assert normalize_address(addr) == "0x794a61358d6845594f94dc1db02a252b5b4814ad"


def test_normalize_address_valid_uppercase():
    addr = "0x794A61358D6845594F94DC1DB02A252B5B4814AD"
    assert normalize_address(addr) == "0x794a61358d6845594f94dc1db02a252b5b4814ad"


def test_normalize_address_valid_checksum():
    addr = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
    assert normalize_address(addr) == "0x794a61358d6845594f94dc1db02a252b5b4814ad"


def test_normalize_address_invalid_checksum():
    # Flip one case bit in a mixed-case address
    addr = "0x794A61358D6845594F94dc1DB02A252b5b4814aD"
    with pytest.raises(ValueError, match="Invalid checksum"):
        normalize_address(addr)


def test_normalize_address_invalid_format():
    with pytest.raises(ValueError):
        normalize_address("0x123")
    with pytest.raises(ValueError):
        normalize_address("not_an_address")
    with pytest.raises(ValueError):
        normalize_address("794a61358d6845594f94dc1db02a252b5b4814ad")
    with pytest.raises(ValueError):
        normalize_address("0x794a61358d6845594f94dc1db02a252b5b4814ag")


def test_read_market_v3_debt_position():
    fixture_data = load_fixture("aave_debt_position.json")

    def handler(request: httpx.Request) -> httpx.Response:
        req_json = json.loads(request.content)
        assert req_json["method"] == "eth_call"
        assert req_json["params"][0]["to"].lower() == "0x794a61358d6845594f94dc1db02a252b5b4814ad"
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)

    assert isinstance(account, AccountData)
    assert account.collateral_usd == Decimal(10000)
    assert account.debt_usd == Decimal(5000)
    assert account.hf == Decimal("1.56")
    assert account.no_debt is False
    assert account.has_position is True


def test_read_market_v3_no_debt_collateral_only():
    fixture_data = load_fixture("aave_no_debt_collateral_only.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)

    assert isinstance(account, AccountData)
    assert account.collateral_usd == Decimal("0.01782485")
    assert account.debt_usd == Decimal(0)
    assert account.hf is None
    assert account.no_debt is True
    assert account.has_position is True


def test_read_market_v3_no_position():
    def handler(request: httpx.Request) -> httpx.Response:
        zero_word = "0" * 64
        max_word = "f" * 64
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + zero_word * 5 + max_word})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V3_MARKET, "0x000000000000000000000000000000000000dead", client=client)

    assert account.collateral_usd == Decimal(0)
    assert account.debt_usd == Decimal(0)
    assert account.hf is None
    assert account.no_debt is True
    assert account.has_position is False


def test_read_market_v4_debt_position():
    fixture_data = load_fixture("aave_v4_position.json")

    def handler(request: httpx.Request) -> httpx.Response:
        req_json = json.loads(request.content)
        assert req_json["method"] == "eth_call"
        assert req_json["params"][0]["to"].lower() == "0x973a023a77420ba610f06b3858ad991df6d85a08"
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V4_MARKET, "0x000000000000000000000000000000000000dead", client=client)

    assert isinstance(account, AccountData)
    assert account.collateral_usd is None
    assert account.debt_usd is None
    assert account.hf == Decimal("1.25")
    assert account.no_debt is False
    assert account.has_position is True


def test_read_market_v4_no_position():
    fixture_data = load_fixture("aave_v4_no_position.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V4_MARKET, "0x000000000000000000000000000000000000dead", client=client)

    assert isinstance(account, AccountData)
    assert account.collateral_usd is None
    assert account.debt_usd is None
    assert account.hf is None
    assert account.no_debt is True
    assert account.has_position is False


def test_read_market_v4_collateral_only():
    # word 0: riskPremium = 0
    # word 1: avgCollateralFactor = 8000
    # word 2: healthFactor = 2**256 - 1
    # word 3: totalCollateralValue = 1000
    # word 4: totalDebtValueRay = 0
    # word 5: activeCollateralCount = 1
    # word 6: borrowCount = 0
    zero_word = "0" * 64
    max_word = "f" * 64
    cf_word = hex(8000)[2:].zfill(64)
    col_word = hex(1000)[2:].zfill(64)
    act_col_word = hex(1)[2:].zfill(64)
    result = "0x" + zero_word + cf_word + max_word + col_word + zero_word + act_col_word + zero_word

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(DUMMY_V4_MARKET, "0x000000000000000000000000000000000000dead", client=client)

    assert account.collateral_usd is None
    assert account.debt_usd is None
    assert account.hf is None
    assert account.no_debt is True
    assert account.has_position is True


def test_read_market_jsonrpc_error_fixture():
    fixture_data = load_fixture("ankr_unauthorized_http200.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(ReadError, match="Unauthorized"):
        read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)


def test_read_market_empty_result_wrong_contract_fixture():
    fixture_data = load_fixture("empty_result_wrong_contract.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture_data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(ReadError, match="(?i)empty result"):
        read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)


def test_read_market_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(ReadError):
        read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)


def test_read_market_fallback_on_primary_failure():
    primary_called = False
    fallback_called = False
    fixture_data = load_fixture("aave_debt_position.json")

    market_with_fallback = Market(
        protocol="aave_v3",
        chain="Arbitrum",
        chain_id=42161,
        name="Main market",
        address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        rpcs=("http://primary.rpc.test", "http://fallback.rpc.test"),
        best_effort=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_called, fallback_called
        if "primary" in str(request.url):
            primary_called = True
            return httpx.Response(500, text="Gateway Timeout")
        if "fallback" in str(request.url):
            fallback_called = True
            return httpx.Response(200, json=fixture_data)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = read_market(
        market_with_fallback,
        "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        client=client,
    )
    assert primary_called is True
    assert fallback_called is True
    assert account.hf == Decimal("1.56")


def test_read_market_never_returns_zeros_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(ReadError):
        read_market(DUMMY_V3_MARKET, "0x794a61358D6845594F94dc1DB02A252b5b4814aD", client=client)


@pytest.mark.parametrize("body", ["[]", '{"error":"boom"}', '{"result":123}', "not json", "null"])
def test_hostile_bodies_raise_read_error_only(body):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=body))
    with pytest.raises(ReadError):
        read_market(
            DUMMY_V3_MARKET,
            "0x000000000000000000000000000000000000dead",
            client=httpx.Client(transport=transport),
        )


def test_read_market_short_result():
    # Less than 384 hex chars for V3
    short_data = "0x" + "0" * 380
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": short_data}))
    with pytest.raises(ReadError, match="short result"):
        read_market(DUMMY_V3_MARKET, "0x000000000000000000000000000000000000dead", client=httpx.Client(transport=transport))

    # Less than 448 hex chars for V4
    short_v4 = "0x" + "0" * 440
    transport_v4 = httpx.MockTransport(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": short_v4}))
    with pytest.raises(ReadError, match="short result"):
        read_market(DUMMY_V4_MARKET, "0x000000000000000000000000000000000000dead", client=httpx.Client(transport=transport_v4))


def test_checksum_address_matches_the_official_eip55_vectors():
    from hfwb.aave import checksum_address

    vectors = [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ]
    for v in vectors:
        assert checksum_address(v.lower()) == v
        assert checksum_address(v) == v  # already checksummed input is accepted and unchanged
    assert checksum_address("0xf903f91565da30b90b57b259fafd3705e667be1a") == "0xf903f91565dA30B90B57B259fAfd3705E667be1A"


def test_checksum_address_rejects_invalid_input():
    import pytest

    from hfwb.aave import checksum_address

    for bad in ["0x123", "hello", "0x" + "g" * 40, "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"]:  # last: bad checksum
        with pytest.raises(ValueError):
            checksum_address(bad)
