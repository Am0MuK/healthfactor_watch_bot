import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MARKETS_JSON = Path(__file__).parent / "markets.json"


@dataclass(frozen=True)
class Market:
    protocol: str
    chain: str
    chain_id: int
    name: str
    address: str
    rpcs: tuple[str, ...]
    best_effort: bool = False

    @property
    def key(self) -> str:
        return f"{self.protocol}:{self.chain_id}:{self.address.lower()}"


def market_key(m: Market) -> str:
    """Return the unique string key for a market: {protocol}:{chain_id}:{address.lower()}."""
    return m.key


_MARKETS_CACHE: dict[Path, list[Market]] = {}


def load_markets(path: Path | str | None = None) -> list[Market]:
    """Load markets from JSON file into a list of frozen Market objects."""
    json_path = Path(path) if path is not None else DEFAULT_MARKETS_JSON
    json_path = json_path.resolve()

    if json_path in _MARKETS_CACHE:
        return _MARKETS_CACHE[json_path]

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    markets: list[Market] = []
    for item in data.get("markets", []):
        market = Market(
            protocol=item["protocol"],
            chain=item["chain"],
            chain_id=int(item["chain_id"]),
            name=item["name"],
            address=item["address"],
            rpcs=tuple(item["rpcs"]),
            best_effort=bool(item.get("best_effort", False)),
        )
        markets.append(market)

    _MARKETS_CACHE[json_path] = markets
    return markets


def get_market(key: str, path: Path | str | None = None) -> Market | None:
    """Look up a market by its unique key."""
    markets = load_markets(path)
    for m in markets:
        if m.key == key:
            return m
    return None
