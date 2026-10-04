from dataclasses import FrozenInstanceError

import pytest

from hfwb.aave import normalize_address
from hfwb.markets import Market, get_market, load_markets, market_key


def test_load_markets():
    markets = load_markets()
    assert len(markets) == 39


def test_market_counts_v3_and_v4():
    markets = load_markets()
    v3 = [m for m in markets if m.protocol == "aave_v3"]
    v4 = [m for m in markets if m.protocol == "aave_v4"]
    assert len(v3) == 20
    assert len(v4) == 19


def test_keys_unique():
    markets = load_markets()
    keys = [market_key(m) for m in markets]
    assert len(keys) == len(set(keys))
    assert len(keys) == 39


def test_valid_chain_id_and_addresses():
    markets = load_markets()
    for m in markets:
        assert isinstance(m.chain_id, int) and m.chain_id > 0
        norm = normalize_address(m.address)
        assert norm == m.address.lower()


def test_every_market_has_rpcs():
    markets = load_markets()
    for m in markets:
        assert len(m.rpcs) >= 1
        for url in m.rpcs:
            assert url.startswith(("http://", "https://"))


def test_get_market():
    # Arbitrum Aave V3 market
    arb_key = "aave_v3:42161:0x794a61358d6845594f94dc1db02a252b5b4814ad"
    market = get_market(arb_key)
    assert market is not None
    assert isinstance(market, Market)
    assert market.chain == "Arbitrum"
    assert market.protocol == "aave_v3"
    assert market.chain_id == 42161

    # Non-existent key
    assert get_market("nonexistent:123:0x123") is None


def test_market_is_frozen():
    markets = load_markets()
    m = markets[0]
    with pytest.raises(FrozenInstanceError):
        m.name = "Modified"  # type: ignore[misc]
