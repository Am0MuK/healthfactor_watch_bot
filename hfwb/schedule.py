from collections.abc import Iterable, Sequence
from decimal import Decimal

INTERVAL_FAR = 300
INTERVAL_MID = 120
INTERVAL_NEAR = 60

_HF_THRESHOLD_NEAR = Decimal("1.15")
_HF_THRESHOLD_MID = Decimal("1.4")
_DIST_THRESHOLD_NEAR = Decimal("0.10")
_DIST_THRESHOLD_MID = Decimal("0.30")
_ONE = Decimal("1.00")


def poll_interval_for(hf: Decimal | None, levels: Sequence[Decimal]) -> int:
    """Compute adaptive polling interval in seconds for a health factor and alert levels.

    - hf is None (no debt) -> 300s.
    - d is the distance to the next downward level crossing:
      - if hf >= levels[0]: d = hf - levels[0]
      - otherwise: take highest level L_k with L_k <= hf (levels are descending), d = hf - L_k
      - below lowest level: d = hf - 1.00 (can be <= 0)
    - Rules in order:
      - hf < 1.15 or d < 0.10 -> 60s
      - d < 0.30 or hf < 1.4 -> 120s
      - else -> 300s
    """
    if hf is None:
        return INTERVAL_FAR

    hf_dec = Decimal(str(hf)) if not isinstance(hf, Decimal) else hf
    levels_dec = [Decimal(str(x)) if not isinstance(x, Decimal) else x for x in levels]

    if not levels_dec:
        return INTERVAL_FAR

    if hf_dec >= levels_dec[0]:
        d = hf_dec - levels_dec[0]
    else:
        matching = [lvl for lvl in levels_dec if lvl <= hf_dec]
        if matching:
            d = hf_dec - matching[0]
        else:
            d = hf_dec - _ONE

    if hf_dec < _HF_THRESHOLD_NEAR or d < _DIST_THRESHOLD_NEAR:
        return INTERVAL_NEAR
    if d < _DIST_THRESHOLD_MID or hf_dec < _HF_THRESHOLD_MID:
        return INTERVAL_MID
    return INTERVAL_FAR


def combine(intervals: Iterable[int]) -> int:
    """Return the minimum interval among multiple watching chats."""
    return min(intervals)


def backoff_for(fail_streak: int) -> int:
    """Compute backoff interval in seconds based on consecutive read failure streak."""
    if fail_streak <= 1:
        return 60
    if fail_streak == 2:
        return 120
    if fail_streak == 3:
        return 240
    return 300
