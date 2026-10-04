import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hfwb.depeg import (
    DEPEG_LEVELS,
    ORACLE_NOT_INDEPENDENT,
    RECOVER_BELOW,
    STABLES,
    DepegTracker,
    PriceError,
    PriceReading,
    depeg_step,
    deviation,
    fetch_prices,
    oracle_price,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_stables_constants():
    assert len(STABLES) == 12
    expected = {
        "USDC", "USDT", "DAI", "USDS", "GHO", "USDe",
        "PYUSD", "RLUSD", "USDG", "frxUSD", "crvUSD", "FDUSD"
    }
    assert set(STABLES.keys()) == expected
    assert DEPEG_LEVELS == (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
    assert RECOVER_BELOW == Decimal("0.003")
    assert ORACLE_NOT_INDEPENDENT == {"GHO", "USDe"}


def test_deviation():
    assert deviation(Decimal("0.98")) == Decimal("0.02")
    assert deviation(Decimal("1.03")) == Decimal("0.03")
    assert deviation(Decimal("1.00")) == Decimal("0.00")
    assert deviation(Decimal("1.000000")) == Decimal("0.000000")


def test_fetch_prices_real_fixture():
    fixture_path = FIXTURES_DIR / "llama_prices_12_stables.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "coins.llama.fi/prices/current/" in str(request.url)
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    # Real prices refresh in steps of about 5 minutes and some tokens are normally 11 to 15 minutes
    # old, so the freshness limit is 20 minutes; at fixture time + 900 s every token is usable.
    readings = fetch_prices(client=client, now=1791112010)
    assert len(readings) == 12
    assert {"USDC", "USDT", "GHO", "frxUSD", "crvUSD", "USDe", "FDUSD"} <= set(readings)

    for sym, r in readings.items():
        assert isinstance(r, PriceReading)
        assert r.symbol == sym
        assert isinstance(r.price, Decimal)
        assert r.confidence >= 0.9
        assert r.timestamp > 0

    assert readings["USDC"].price == Decimal(str(data["coins"]["ethereum:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"]["price"]))

    # If now is set to when GHO was captured (1791111120), GHO is fresh
    readings_gho = fetch_prices(client=client, now=1791111120)
    assert "GHO" in readings_gho
    assert readings_gho["GHO"].price == Decimal(str(data["coins"]["ethereum:0x40D16FC0246aD3160Ccc09B8D0D3A2cD28aE6C2f"]["price"]))


def test_fetch_prices_empty_200_trap():
    fixture_path = FIXTURES_DIR / "llama_empty_http200.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    client = httpx.Client(transport=transport)

    with pytest.raises(PriceError, match="empty"):
        fetch_prices(client=client, now=1000)


@pytest.mark.parametrize(
    "status,content",
    [
        (500, b"Internal Server Error"),
        (404, b"Not Found"),
        (200, b"not json"),
        (200, b"[]"),
        (200, b"null"),
        (200, json.dumps({"coins": "not-a-dict"}).encode("utf-8")),
        (200, json.dumps({"other_key": {}}).encode("utf-8")),
    ],
)
def test_fetch_prices_hostile_responses(status, content):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, content=content))
    client = httpx.Client(transport=transport)
    with pytest.raises(PriceError):
        fetch_prices(client=client, now=1000)


def test_fetch_prices_network_error():
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(failing_handler)
    client = httpx.Client(transport=transport)
    with pytest.raises(PriceError, match="Network error"):
        fetch_prices(client=client, now=1000)


def test_fetch_prices_drops_invalid_and_stale_entries():
    now_ts = 10000
    mock_coins = {
        # Valid USDC
        f"ethereum:{STABLES['USDC']}": {
            "decimals": 6,
            "symbol": "USDC",
            "price": 0.999,
            "timestamp": now_ts - 100,
            "confidence": 0.99,
        },
        # Low confidence
        f"ethereum:{STABLES['USDT']}": {
            "decimals": 6,
            "symbol": "USDT",
            "price": 0.999,
            "timestamp": now_ts - 100,
            "confidence": 0.85,
        },
        # Stale (> 1200s)
        f"ethereum:{STABLES['DAI']}": {
            "decimals": 18,
            "symbol": "DAI",
            "price": 0.999,
            "timestamp": now_ts - 1201,
            "confidence": 0.99,
        },
        # Non-numeric price
        f"ethereum:{STABLES['USDS']}": {
            "decimals": 18,
            "symbol": "USDS",
            "price": "abc",
            "timestamp": now_ts - 50,
            "confidence": 0.99,
        },
        # Non-positive price
        f"ethereum:{STABLES['GHO']}": {
            "decimals": 18,
            "symbol": "GHO",
            "price": -1.0,
            "timestamp": now_ts - 50,
            "confidence": 0.99,
        },
        # Zero price
        f"ethereum:{STABLES['USDe']}": {
            "decimals": 18,
            "symbol": "USDe",
            "price": 0.0,
            "timestamp": now_ts - 50,
            "confidence": 0.99,
        },
        # NaN price
        f"ethereum:{STABLES['PYUSD']}": {
            "decimals": 6,
            "symbol": "PYUSD",
            "price": "NaN",
            "timestamp": now_ts - 50,
            "confidence": 0.99,
        },
        # Not a dict
        f"ethereum:{STABLES['RLUSD']}": 12345,
    }

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"coins": mock_coins}))
    client = httpx.Client(transport=transport)

    readings = fetch_prices(client=client, now=now_ts)
    # Only USDC was valid
    assert len(readings) == 1
    assert "USDC" in readings
    assert readings["USDC"].price == Decimal("0.999")


def test_depeg_step_escalation_confirmation():
    now_ts = 1000.0
    # Dev 0.006 -> D1
    dev = Decimal("0.006")

    # ok -> D1 without confirmation stays ok
    new_state, alert = depeg_step("ok", dev, now_ts, None, confirmed=False)
    assert new_state == "ok"
    assert alert is None

    # ok -> D1 with confirmation escalates
    new_state, alert = depeg_step("ok", dev, now_ts, None, confirmed=True)
    assert new_state == "D1"
    assert alert == "D1"

    # Jump ok -> D4 needs confirmation
    dev_d4 = Decimal("0.06")
    new_state, alert = depeg_step("ok", dev_d4, now_ts, None, confirmed=False)
    assert new_state == "ok"
    assert alert is None

    new_state, alert = depeg_step("ok", dev_d4, now_ts, None, confirmed=True)
    assert new_state == "D4"
    assert alert == "D4"

    # D1 -> D2 escalation needs confirmation
    dev_d2 = Decimal("0.015")
    new_state, alert = depeg_step("D1", dev_d2, now_ts, now_ts, confirmed=False)
    assert new_state == "D1"
    assert alert is None

    new_state, alert = depeg_step("D1", dev_d2, now_ts, now_ts, confirmed=True)
    assert new_state == "D2"
    assert alert == "D2"


def test_depeg_step_exact_boundaries():
    now_ts = 1000.0

    # 0.499% -> below 0.5% threshold -> candidate is ok
    assert depeg_step("ok", Decimal("0.00499"), now_ts, None, confirmed=True) == ("ok", None)
    # 0.5% exact -> D1
    assert depeg_step("ok", Decimal("0.005"), now_ts, None, confirmed=True) == ("D1", "D1")

    # 0.999% -> D1
    assert depeg_step("D1", Decimal("0.00999"), now_ts, now_ts, confirmed=True) == ("D1", None)
    # 1.0% exact -> D2
    assert depeg_step("D1", Decimal("0.01"), now_ts, now_ts, confirmed=True) == ("D2", "D2")

    # 1.999% -> D2
    assert depeg_step("D2", Decimal("0.01999"), now_ts, now_ts, confirmed=True) == ("D2", None)
    # 2.0% exact -> D3
    assert depeg_step("D2", Decimal("0.02"), now_ts, now_ts, confirmed=True) == ("D3", "D3")

    # 4.999% -> D3
    assert depeg_step("D3", Decimal("0.04999"), now_ts, now_ts, confirmed=True) == ("D3", None)
    # 5.0% exact -> D4
    assert depeg_step("D3", Decimal("0.05"), now_ts, now_ts, confirmed=True) == ("D4", "D4")


def test_depeg_step_step_down_and_recovery():
    now_ts = 1000.0

    # D4: threshold 0.05. 20% hysteresis: 0.05 * 0.8 = 0.040.
    # dev >= 0.040 stays in D4
    assert depeg_step("D4", Decimal("0.045"), now_ts, now_ts, confirmed=False) == ("D4", None)
    assert depeg_step("D4", Decimal("0.040"), now_ts, now_ts, confirmed=False) == ("D4", None)
    # dev < 0.040 steps down to D3 (immediate, no alert)
    assert depeg_step("D4", Decimal("0.039"), now_ts, now_ts, confirmed=False) == ("D3", None)

    # D3: threshold 0.02. 0.02 * 0.8 = 0.016.
    assert depeg_step("D3", Decimal("0.017"), now_ts, now_ts, confirmed=False) == ("D3", None)
    assert depeg_step("D3", Decimal("0.016"), now_ts, now_ts, confirmed=False) == ("D3", None)
    assert depeg_step("D3", Decimal("0.015"), now_ts, now_ts, confirmed=False) == ("D2", None)

    # D2: threshold 0.01. 0.01 * 0.8 = 0.008.
    assert depeg_step("D2", Decimal("0.009"), now_ts, now_ts, confirmed=False) == ("D2", None)
    assert depeg_step("D2", Decimal("0.008"), now_ts, now_ts, confirmed=False) == ("D2", None)
    assert depeg_step("D2", Decimal("0.007"), now_ts, now_ts, confirmed=False) == ("D1", None)

    # D1: recovery below 0.3% (0.003)
    # dev >= 0.003 stays in D1
    assert depeg_step("D1", Decimal("0.004"), now_ts, now_ts, confirmed=False) == ("D1", None)
    assert depeg_step("D1", Decimal("0.003"), now_ts, now_ts, confirmed=False) == ("D1", None)
    # dev < 0.003 recovers to ok with "recovery" alert
    assert depeg_step("D1", Decimal("0.00299"), now_ts, now_ts, confirmed=False) == ("ok", "recovery")

    # Immediate recovery to ok from D4
    assert depeg_step("D4", Decimal("0.001"), now_ts, now_ts, confirmed=False) == ("ok", "recovery")


def test_depeg_step_repeats():
    now_ts = 10000.0

    # In D3, reminder repeats every 60 min (3600 s)
    assert depeg_step("D3", Decimal("0.025"), now_ts, now_ts - 3599, confirmed=False) == ("D3", None)
    assert depeg_step("D3", Decimal("0.025"), now_ts, now_ts - 3600, confirmed=False) == ("D3", "repeat_D3")
    assert depeg_step("D3", Decimal("0.025"), now_ts, None, confirmed=False) == ("D3", "repeat_D3")

    # In D4, reminder repeats every 60 min
    assert depeg_step("D4", Decimal("0.06"), now_ts, now_ts - 3599, confirmed=False) == ("D4", None)
    assert depeg_step("D4", Decimal("0.06"), now_ts, now_ts - 3600, confirmed=False) == ("D4", "repeat_D4")

    # In D1 and D2, no repeat reminders
    assert depeg_step("D1", Decimal("0.007"), now_ts, now_ts - 7200, confirmed=False) == ("D1", None)
    assert depeg_step("D2", Decimal("0.015"), now_ts, now_ts - 7200, confirmed=False) == ("D2", None)



def test_oracle_price():
    fixture_price = FIXTURES_DIR / "aave_oracle_usdc_price.json"
    data_price = json.loads(fixture_price.read_text(encoding="utf-8"))

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data_price))
    client = httpx.Client(transport=transport)

    price = oracle_price("USDC", ["https://eth.example.com"], client=client)
    assert price is not None
    # 0x05f5d516 = 99996950 / 10^8 = 0.9999695
    assert price == Decimal("0.9999695")


def test_oracle_price_revert_and_errors():
    fixture_revert = FIXTURES_DIR / "aave_oracle_revert.json"
    data_revert = json.loads(fixture_revert.read_text(encoding="utf-8"))

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data_revert))
    client = httpx.Client(transport=transport)

    # Revert returns None
    assert oracle_price("USDC", ["https://eth.example.com"], client=client) is None

    # Unknown symbol returns None
    assert oracle_price("UNKNOWN", ["https://eth.example.com"], client=client) is None

    # Empty rpcs returns None
    assert oracle_price("USDC", [], client=client) is None

    # Network error returns None (never raises)
    failing_client = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("fail"))))
    assert oracle_price("USDC", ["https://eth.example.com"], client=failing_client) is None

    # Malformed result returns None
    bad_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"result": "0x12"})))
    assert oracle_price("USDC", ["https://eth.example.com"], client=bad_client) is None


import json as _json_hostile
from pathlib import Path as _Path_hostile

import httpx as _httpx_hostile
import pytest as _pytest_hostile

_FX = _Path_hostile(__file__).parent / "fixtures" / "llama_prices_12_stables.json"


def _fetch_with_usdc_entry(mutate, raw=None):
    from hfwb.depeg import STABLES, fetch_prices

    base = _json_hostile.loads(_FX.read_text())
    key = "ethereum:" + STABLES["USDC"]
    ts = base["coins"][key]["timestamp"]
    mutate(base["coins"][key], ts)
    text = _json_hostile.dumps(base)
    if raw:
        text = raw(text)
    transport = _httpx_hostile.MockTransport(lambda r: _httpx_hostile.Response(200, text=text))
    return fetch_prices(_httpx_hostile.Client(transport=transport), now=ts + 60)


@_pytest_hostile.mark.parametrize(
    "mutate",
    [
        lambda e, ts: e.update(confidence=float("nan")),
        lambda e, ts: e.update(confidence=float("inf")),
        lambda e, ts: e.update(confidence=-1),
        lambda e, ts: e.update(timestamp=float("inf")),
        lambda e, ts: e.update(timestamp=float("nan")),
        lambda e, ts: e.update(timestamp=ts + 10**9),  # far future (clock skew or garbage)
        lambda e, ts: e.update(timestamp=ts - 10**6),  # very stale
    ],
)
def test_unusable_entry_is_dropped_without_raising(mutate):
    readings = _fetch_with_usdc_entry(mutate)
    assert "USDC" not in readings
    assert "USDT" in readings  # the other tokens are unaffected


def test_small_clock_skew_into_the_future_is_tolerated():
    readings = _fetch_with_usdc_entry(lambda e, ts: e.update(timestamp=ts + 120))  # now = ts + 60
    assert "USDC" in readings


def test_tracker_observe_confirms_with_the_lower_of_the_last_two_readings():
    tracker = DepegTracker()
    # first reading: nothing to confirm against yet
    assert tracker.observe("USDC", Decimal("0.010"), timestamp=1) == (Decimal("0.010"), False)
    # worsening: the confirmed deviation is the LOWER of the two (both readings were at least that bad)
    assert tracker.observe("USDC", Decimal("0.030"), timestamp=2) == (Decimal("0.010"), True)
    assert tracker.observe("USDC", Decimal("0.070"), timestamp=3) == (Decimal("0.030"), True)
    # improving: confirmed deviation follows the current (lower) reading
    assert tracker.observe("USDC", Decimal("0.002"), timestamp=4) == (Decimal("0.002"), True)
    # an unavailable reading forgets the symbol, so the next one is a "first" reading again
    tracker.forget("USDC")
    assert tracker.observe("USDC", Decimal("0.200"), timestamp=5) == (Decimal("0.200"), False)
    tracker.reset()
    assert tracker.observe("USDC", Decimal("0.200"), timestamp=5) == (Decimal("0.200"), False)


def test_depeg_step_escalates_on_escalate_dev_but_recovers_on_current_dev():
    now = 10_000.0
    # current reading is 7% but only 1% is confirmed -> D2 (1%), not D4
    assert depeg_step("ok", Decimal("0.07"), now, None, confirmed=True, escalate_dev=Decimal("0.01")) == ("D2", "D2")
    # nothing confirmed yet (first reading) -> no escalation whatever the current deviation is
    assert depeg_step("ok", Decimal("0.07"), now, None, confirmed=False, escalate_dev=Decimal("0.07")) == ("ok", None)
    # recovery still uses the current deviation
    assert depeg_step("D3", Decimal("0.001"), now, now - 10, confirmed=True, escalate_dev=Decimal("0.001")) == (
        "ok",
        "recovery",
    )


def test_price_age_limit_is_twenty_minutes():
    from hfwb.depeg import MAX_PRICE_AGE_S

    assert MAX_PRICE_AGE_S == 1200
    assert "USDC" in _fetch_with_usdc_entry(lambda e, ts: e.update(timestamp=ts - 1100 + 60))  # now = ts + 60 -> age 1100
    assert "USDC" not in _fetch_with_usdc_entry(lambda e, ts: e.update(timestamp=ts - 1200))  # age 1260


def test_tracker_counts_distinct_data_points_not_polls():
    tracker = DepegTracker()
    # the same data point polled again is not a second confirmation
    assert tracker.observe("USDC", Decimal("0.010"), timestamp=1000) == (Decimal("0.010"), False)
    assert tracker.observe("USDC", Decimal("0.010"), timestamp=1000) == (Decimal("0.010"), False)
    assert tracker.observe("USDC", Decimal("0.010"), timestamp=1000) == (Decimal("0.010"), False)
    # a new data point confirms (lower of the two data points)
    assert tracker.observe("USDC", Decimal("0.020"), timestamp=1300) == (Decimal("0.010"), True)
    # polling that data point again returns the same answer and does not shift the history
    assert tracker.observe("USDC", Decimal("0.020"), timestamp=1300) == (Decimal("0.010"), True)
    assert tracker.observe("USDC", Decimal("0.050"), timestamp=1600) == (Decimal("0.020"), True)
