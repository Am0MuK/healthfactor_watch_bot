import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from hfwb.markets import Market

GET_USER_ACCOUNT_DATA_SELECTOR = "0xbf92857c"
UINT256_MAX = (1 << 256) - 1

_HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ReadError(Exception):
    """Raised when reading account data from an RPC node fails."""


@dataclass(frozen=True)
class AccountData:
    collateral_usd: Decimal | None
    debt_usd: Decimal | None
    hf: Decimal | None
    no_debt: bool
    has_position: bool = True


def _keccak_256(data: bytes) -> bytes:
    """Pure Python Keccak-256 implementation."""
    rc = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    r = [
        [0,  36,  3, 41, 18],
        [1,  44, 10, 45,  2],
        [62,  6, 43, 15, 61],
        [28, 55, 25, 21, 56],
        [27, 20, 39,  8, 14],
    ]
    state = [[0] * 5 for _ in range(5)]
    rate_bytes = 136

    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate_bytes != (rate_bytes - 1):
        padded.append(0x00)
    padded.append(0x80)

    for block_start in range(0, len(padded), rate_bytes):
        block = padded[block_start : block_start + rate_bytes]
        for i in range(rate_bytes // 8):
            state[i % 5][i // 5] ^= int.from_bytes(block[i * 8 : (i + 1) * 8], "little")

        for round_idx in range(24):
            c = [
                state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4]
                for x in range(5)
            ]
            d = [
                c[(x - 1) % 5]
                ^ (((c[(x + 1) % 5] << 1) & 0xFFFFFFFFFFFFFFFF) | (c[(x + 1) % 5] >> 63))
                for x in range(5)
            ]
            for x in range(5):
                for y in range(5):
                    state[x][y] ^= d[x]

            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    rot = r[x][y]
                    val = state[x][y]
                    b[y][(2 * x + 3 * y) % 5] = (
                        ((val << rot) & 0xFFFFFFFFFFFFFFFF) | (val >> (64 - rot) if rot else 0)
                    )

            for x in range(5):
                for y in range(5):
                    state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])

            state[0][0] ^= rc[round_idx]

    out = bytearray()
    for i in range(4):
        out.extend(state[i % 5][i // 5].to_bytes(8, "little"))
    return bytes(out)


def normalize_address(address: str) -> str:
    """Validate 0x-prefixed 40-hex address with EIP-55 checksum if mixed case.

    Returns lowercase address string.
    Raises ValueError on invalid format or invalid checksum.
    """
    if not isinstance(address, str) or not _HEX_ADDR_RE.match(address):
        raise ValueError(f"Invalid Ethereum address format: {address!r}")

    hex_part = address[2:]
    has_upper = any(c.isupper() for c in hex_part)
    has_lower = any(c.islower() for c in hex_part)

    if has_upper and has_lower:
        lower_hex = hex_part.lower()
        hash_hex = _keccak_256(lower_hex.encode("ascii")).hex()
        for i, char in enumerate(hex_part):
            expected_upper = int(hash_hex[i], 16) >= 8
            if expected_upper and char.islower():
                raise ValueError(f"Invalid checksum for address: {address}")
            if not expected_upper and char.isupper():
                raise ValueError(f"Invalid checksum for address: {address}")

    return "0x" + hex_part.lower()


def checksum_address(address: str) -> str:
    """Return the EIP-55 mixed-case form of a valid address (raises ValueError on invalid input)."""
    lowered = normalize_address(address)  # validates format and, for mixed case, the checksum
    digest = _keccak_256(lowered[2:].encode("ascii")).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(lowered[2:]))


def read_market(
    market: "Market",
    address: str,
    client: httpx.Client | None = None,
) -> AccountData:
    """Read account data for an address on a specific market (Aave V3 or V4).

    Tries market.rpcs in sequence and falls back on error.
    Raises ReadError on total failure.
    """
    if not market.rpcs:
        raise ReadError(f"No RPC URLs provided for market {market.name}")

    if market.protocol not in ("aave_v3", "aave_v4"):
        raise ReadError(f"Unsupported protocol: {market.protocol}")

    normalized = normalize_address(address)
    clean_addr = normalized[2:]
    padded_addr = clean_addr.zfill(64)
    calldata = GET_USER_ACCOUNT_DATA_SELECTOR + padded_addr

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": market.address,
                "data": calldata,
            },
            "latest",
        ],
        "id": 1,
    }

    errors: list[str] = []
    own_client = client is None
    active_client = client or httpx.Client(timeout=10.0)

    try:
        for url in market.rpcs:
            try:
                resp = active_client.post(url, json=payload)
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

                hex_data = result.removeprefix("0x")

                if market.protocol == "aave_v3":
                    if len(hex_data) < 384:
                        errors.append(f"{url} returned short result ({len(hex_data)} chars for V3)")
                        continue
                    try:
                        collateral_base = int(hex_data[0:64], 16)
                        debt_base = int(hex_data[64:128], 16)
                        health_factor = int(hex_data[320:384], 16)
                    except ValueError as exc:
                        errors.append(f"{url} returned non-hex data: {exc}")
                        continue

                    collateral_usd = Decimal(collateral_base) / Decimal(10**8)
                    debt_usd = Decimal(debt_base) / Decimal(10**8)
                    has_position = (collateral_base > 0) or (debt_base > 0)

                    if debt_base == 0 or health_factor == UINT256_MAX:
                        no_debt = True
                        hf = None
                    else:
                        no_debt = False
                        hf = Decimal(health_factor) / Decimal(10**18)

                    return AccountData(
                        collateral_usd=collateral_usd,
                        debt_usd=debt_usd,
                        hf=hf,
                        no_debt=no_debt,
                        has_position=has_position,
                    )

                else:  # aave_v4
                    if len(hex_data) < 448:
                        errors.append(f"{url} returned short result ({len(hex_data)} chars for V4)")
                        continue
                    try:
                        health_factor = int(hex_data[128:192], 16)
                        total_collateral_value = int(hex_data[192:256], 16)
                        active_collateral_count = int(hex_data[320:384], 16)
                        borrow_count = int(hex_data[384:448], 16)
                    except ValueError as exc:
                        errors.append(f"{url} returned non-hex data: {exc}")
                        continue

                    has_position = (
                        active_collateral_count > 0
                        or borrow_count > 0
                        or total_collateral_value > 0
                    )

                    if borrow_count == 0 or health_factor == UINT256_MAX:
                        no_debt = True
                        hf = None
                    else:
                        no_debt = False
                        hf = Decimal(health_factor) / Decimal(10**18)

                    return AccountData(
                        collateral_usd=None,
                        debt_usd=None,
                        hf=hf,
                        no_debt=no_debt,
                        has_position=has_position,
                    )

            except (httpx.RequestError, ValueError) as exc:
                errors.append(f"{url} failed: {exc}")
                continue
    finally:
        if own_client:
            active_client.close()

    raise ReadError(f"All RPC endpoints failed: {'; '.join(errors)}")
