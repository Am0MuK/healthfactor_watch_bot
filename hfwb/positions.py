"""Per-asset position reader and tie-out guard for Aave V3 markets."""

import functools
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import httpx

from hfwb.aave import AccountData, ReadError, _keccak_256, normalize_address
from hfwb.markets import Market

MAX_DECIMALS = 36
DEFAULT_BUDGET_S = 10.0  # whole read; a hung RPC must not stall the alert or command loop
CALL_TIMEOUT_S = 5.0
MAX_RESERVE_ID = 127  # Aave V3 caps reserves at 128 (UserConfigurationMap holds 2 bits each)
MAX_SYMBOL_LEN = 16


def _clean_symbol(raw: str) -> str:
    """Keep printable characters only and cap the length; symbols come from the chain."""
    cleaned = "".join(c for c in raw if c.isprintable()).strip()
    return cleaned[:MAX_SYMBOL_LEN]


@dataclass(frozen=True)
class AssetPosition:
    """Position details for a single asset in a user's account."""

    symbol: str
    asset: str
    decimals: int
    collateral: Decimal
    debt: Decimal
    price_usd: Decimal
    liq_threshold: Decimal
    used_as_collateral: bool


@dataclass(frozen=True)
class PositionBreakdown:
    """Full account position breakdown across all active reserves."""

    assets: tuple[AssetPosition, ...]
    emode_category: int
    collateral_usd: Decimal
    weighted_collateral_usd: Decimal
    debt_usd: Decimal
    hf: Decimal | None


def _selector(sig: str) -> str:
    """Compute 4-byte selector (with 0x prefix) from function signature."""
    return "0x" + _keccak_256(sig.encode("ascii")).hex()[:8]


def _encode_address(address: str) -> str:
    """Encode an address as a 32-byte (64 hex char) ABI word."""
    return address.lower().removeprefix("0x").rjust(64, "0")


def _decode_reserve_tokens(hex_str: str) -> dict[str, str]:
    """Decode getAllReservesTokens() return value: (string symbol, address tokenAddress)[].

    Returns a mapping from lowercase 0x address to token symbol.
    """
    hex_str = hex_str.removeprefix("0x")
    result: dict[str, str] = {}
    try:
        if len(hex_str) < 64:
            return result
        array_offset = int(hex_str[0:64], 16)
        if len(hex_str) < (array_offset + 32) * 2:
            return result
        n = int(hex_str[array_offset * 2 : (array_offset + 32) * 2], 16)
        elements_start = array_offset + 32
        for j in range(n):
            head_start = (elements_start + 32 * j) * 2
            head_end = (elements_start + 32 * (j + 1)) * 2
            if len(hex_str) < head_end:
                break
            elem_rel = int(hex_str[head_start:head_end], 16)
            elem_abs = elements_start + elem_rel
            if len(hex_str) < (elem_abs + 64) * 2:
                continue
            str_rel = int(hex_str[elem_abs * 2 : (elem_abs + 32) * 2], 16)
            token_word = hex_str[(elem_abs + 32) * 2 : (elem_abs + 64) * 2]
            token_addr = "0x" + token_word[24:64].lower()

            str_abs = elem_abs + str_rel
            if len(hex_str) < (str_abs + 32) * 2:
                continue
            str_len = int(hex_str[str_abs * 2 : (str_abs + 32) * 2], 16)
            str_bytes_offset = str_abs + 32
            str_end = str_bytes_offset + str_len
            if len(hex_str) < str_end * 2:
                continue
            str_raw = bytes.fromhex(hex_str[str_bytes_offset * 2 : str_end * 2])
            sym = str_raw.decode("utf-8", errors="replace")
            result[token_addr] = sym
    except (ValueError, IndexError):
        return result
    return result


def _eth_call(
    client: httpx.Client,
    rpcs: tuple[str, ...],
    to: str,
    data: str,
    min_hex_len: int = 64,
    deadline: float | None = None,
) -> str:
    """Execute eth_call trying rpcs in sequence. Returns hex string without '0x' prefix."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": to,
                "data": data,
            },
            "latest",
        ],
        "id": 1,
    }
    errors: list[str] = []
    for url in rpcs:
        if deadline is not None and time.monotonic() > deadline:
            errors.append("time budget exceeded")
            break
        try:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                errors.append(f"{url} returned HTTP {resp.status_code}: {resp.text[:200]}")
                continue

            body = resp.json()
            if not isinstance(body, dict):
                errors.append(f"{url} returned a non-object JSON body")
                continue
            if "error" in body:
                err = body["error"]
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                errors.append(f"{url} returned JSON-RPC error: {err_msg}")
                continue

            result = body.get("result")
            if not isinstance(result, str) or result == "0x":
                errors.append(f"{url} returned empty result or wrong contract ('0x')")
                continue

            if not result.startswith("0x"):
                errors.append(f"{url} returned result not starting with 0x")
                continue

            hex_data = result[2:]
            if len(hex_data) < min_hex_len:
                errors.append(
                    f"{url} returned short result ({len(hex_data)} chars, expected at least {min_hex_len})"
                )
                continue

            try:
                bytes.fromhex(hex_data)
            except ValueError as exc:
                errors.append(f"{url} returned non-hex data: {exc}")
                continue

            return hex_data

        except (httpx.RequestError, ValueError) as exc:
            errors.append(f"{url} failed: {exc}")
            continue

    raise ReadError(f"All RPC endpoints failed: {'; '.join(errors)}")


def recompute(
    assets: Sequence[AssetPosition],
) -> tuple[Decimal, Decimal, Decimal, Decimal | None]:
    """Recompute position aggregates from a sequence of AssetPosition objects.

    Returns:
        (collateral_usd, weighted_collateral_usd, debt_usd, hf)
    """
    collateral_usd = Decimal(0)
    weighted_collateral_usd = Decimal(0)
    debt_usd = Decimal(0)

    for a in assets:
        if a.used_as_collateral and a.collateral > 0:
            collateral_usd += a.collateral * a.price_usd
            if a.liq_threshold > 0:
                weighted_collateral_usd += a.collateral * a.price_usd * a.liq_threshold
        debt_usd += a.debt * a.price_usd

    hf = (weighted_collateral_usd / debt_usd) if debt_usd > 0 else None
    return collateral_usd, weighted_collateral_usd, debt_usd, hf


def tie_out(
    breakdown: PositionBreakdown,
    account: AccountData,
    tolerance: Decimal = Decimal("0.005"),
) -> bool:
    """Check if breakdown figures match contract account figures within tolerance.

    Returns True only if:
    - account.collateral_usd, debt_usd, hf are all non-None
    - account collateral > 0 and debt > 0
    - breakdown.hf is not None
    - relative difference |ours - contract| / contract <= tolerance for collateral, debt, and hf.
    Otherwise False. Never raises.
    """
    try:
        if account.collateral_usd is None or account.debt_usd is None or account.hf is None:
            return False
        if account.collateral_usd <= 0 or account.debt_usd <= 0:
            return False
        if breakdown.hf is None:
            return False
        if account.hf <= 0:
            return False

        diff_coll = abs(breakdown.collateral_usd - account.collateral_usd) / account.collateral_usd
        if diff_coll > tolerance:
            return False

        diff_debt = abs(breakdown.debt_usd - account.debt_usd) / account.debt_usd
        if diff_debt > tolerance:
            return False

        diff_hf = abs(breakdown.hf - account.hf) / account.hf
        return diff_hf <= tolerance
    except (TypeError, ValueError, ZeroDivisionError, AttributeError, ArithmeticError):
        return False


def read_positions(
    market: Market,
    address: str,
    client: httpx.Client | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
) -> PositionBreakdown:
    """Read per-asset positions and recomputed health factor for an address on Aave V3.

    Raises:
        ReadError: if protocol is not aave_v3 or RPC calls fail.
        ValueError: if address format or checksum is invalid.
    """
    if market.protocol != "aave_v3":
        raise ReadError(f"Unsupported protocol: {market.protocol}")

    if not market.rpcs:
        raise ReadError(f"No RPC URLs provided for market {market.name}")

    normalized_user = normalize_address(address)
    encoded_user = _encode_address(normalized_user)

    own_client = client is None
    active_client = client or httpx.Client(timeout=CALL_TIMEOUT_S)
    call = functools.partial(_eth_call, deadline=time.monotonic() + budget_s)

    try:
        # 1. Pool getUserConfiguration(address) -> uint256 bitmap
        bitmap_hex = call(
            active_client,
            market.rpcs,
            market.address,
            _selector("getUserConfiguration(address)") + encoded_user,
            min_hex_len=64,
        )
        bitmap = int(bitmap_hex[0:64], 16)

        # 2. Pool getUserEMode(address) -> uint256
        emode_hex = call(
            active_client,
            market.rpcs,
            market.address,
            _selector("getUserEMode(address)") + encoded_user,
            min_hex_len=64,
        )
        emode = int(emode_hex[0:64], 16)
        if emode > 255:
            raise ReadError(f"eMode category {emode} is out of uint8 range")

        # Parse bitmap for active reserves
        active_reserves: list[tuple[int, bool]] = []
        i = 0
        while (bitmap >> (2 * i)) > 0:
            pair = (bitmap >> (2 * i)) & 3
            if pair != 0:
                if i > MAX_RESERVE_ID:
                    raise ReadError(f"Reserve id {i} in user configuration is out of range")
                is_collateral = bool(pair & 2)
                active_reserves.append((i, is_collateral))
            i += 1

        if not active_reserves:
            return PositionBreakdown(
                assets=(),
                emode_category=emode,
                collateral_usd=Decimal(0),
                weighted_collateral_usd=Decimal(0),
                debt_usd=Decimal(0),
                hf=None,
            )

        # eMode configuration (Aave V3.2+ layout)
        emode_lt: int = 0
        emode_bitmap: int = 0
        if emode != 0:
            config_hex = call(
                active_client,
                market.rpcs,
                market.address,
                _selector("getEModeCategoryCollateralConfig(uint8)") + format(emode, "064x"),
                min_hex_len=192,
            )
            emode_lt = int(config_hex[64:128], 16)
            if emode_lt > 10000:
                raise ReadError(f"eMode liquidation threshold above 100%: {emode_lt}")

            bitmap_res_hex = call(
                active_client,
                market.rpcs,
                market.address,
                _selector("getEModeCategoryCollateralBitmap(uint8)") + format(emode, "064x"),
                min_hex_len=64,
            )
            emode_bitmap = int(bitmap_res_hex[0:64], 16)

        # 3. Pool ADDRESSES_PROVIDER() -> provider; getPoolDataProvider() and getPriceOracle()
        provider_hex = call(
            active_client,
            market.rpcs,
            market.address,
            _selector("ADDRESSES_PROVIDER()"),
            min_hex_len=64,
        )
        provider = normalize_address("0x" + provider_hex[24:64])

        data_provider_hex = call(
            active_client,
            market.rpcs,
            provider,
            _selector("getPoolDataProvider()"),
            min_hex_len=64,
        )
        data_provider = normalize_address("0x" + data_provider_hex[24:64])

        oracle_hex = call(
            active_client,
            market.rpcs,
            provider,
            _selector("getPriceOracle()"),
            min_hex_len=64,
        )
        oracle = normalize_address("0x" + oracle_hex[24:64])

        # 4. Data provider getAllReservesTokens() -> (string symbol, address)[]
        reserves_tokens_hex = call(
            active_client,
            market.rpcs,
            data_provider,
            _selector("getAllReservesTokens()"),
            min_hex_len=128,
        )
        symbols = _decode_reserve_tokens(reserves_tokens_hex)

        # 5. For each active reserve id:
        assets: list[AssetPosition] = []
        for rid, is_collateral in active_reserves:
            # Pool getReserveAddressById(uint16)
            reserve_addr_hex = call(
                active_client,
                market.rpcs,
                market.address,
                _selector("getReserveAddressById(uint16)") + format(rid, "064x"),
                min_hex_len=64,
            )
            asset = normalize_address("0x" + reserve_addr_hex[24:64])
            encoded_asset = _encode_address(asset)

            # Data provider getUserReserveData(asset, user)
            user_data_hex = call(
                active_client,
                market.rpcs,
                data_provider,
                _selector("getUserReserveData(address,address)") + encoded_asset + encoded_user,
                min_hex_len=576,
            )
            a_token_raw = int(user_data_hex[0:64], 16)
            stable_debt_raw = int(user_data_hex[64:128], 16)
            variable_debt_raw = int(user_data_hex[128:192], 16)
            debt_raw = stable_debt_raw + variable_debt_raw

            # Data provider getReserveConfigurationData(asset)
            config_data_hex = call(
                active_client,
                market.rpcs,
                data_provider,
                _selector("getReserveConfigurationData(address)") + encoded_asset,
                min_hex_len=640,
            )
            decimals = int(config_data_hex[0:64], 16)
            lt_raw = int(config_data_hex[128:192], 16)

            # Oracle getAssetPrice(asset)
            price_hex = call(
                active_client,
                market.rpcs,
                oracle,
                _selector("getAssetPrice(address)") + encoded_asset,
                min_hex_len=64,
            )
            price_raw = int(price_hex[0:64], 16)
            if price_raw == 0:
                raise ReadError(f"Oracle returned zero price for asset {asset}")

            if decimals > MAX_DECIMALS:
                raise ReadError(f"Implausible decimals {decimals} for asset {asset}")
            if lt_raw > 10000:
                raise ReadError(f"Implausible liquidation threshold {lt_raw} for asset {asset}")

            symbol = _clean_symbol(symbols.get(asset, "")) or asset[:6]
            collateral = Decimal(a_token_raw) / Decimal(10**decimals)
            debt = Decimal(debt_raw) / Decimal(10**decimals)
            price_usd = Decimal(price_raw) / Decimal(10**8)
            if emode != 0 and ((emode_bitmap >> rid) & 1 == 1):
                liq_threshold = Decimal(emode_lt) / Decimal(10000)
            else:
                liq_threshold = Decimal(lt_raw) / Decimal(10000)

            assets.append(
                AssetPosition(
                    symbol=symbol,
                    asset=asset,
                    decimals=decimals,
                    collateral=collateral,
                    debt=debt,
                    price_usd=price_usd,
                    liq_threshold=liq_threshold,
                    used_as_collateral=is_collateral,
                )
            )

        collateral_usd, weighted_collateral_usd, debt_usd, hf = recompute(assets)

        return PositionBreakdown(
            assets=tuple(assets),
            emode_category=emode,
            collateral_usd=collateral_usd,
            weighted_collateral_usd=weighted_collateral_usd,
            debt_usd=debt_usd,
            hf=hf,
        )

    finally:
        if own_client:
            active_client.close()
