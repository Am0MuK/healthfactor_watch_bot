from collections.abc import Iterable, Sequence
from decimal import Decimal

# Module configuration
DEFAULT_LEVELS = (Decimal("1.4"), Decimal("1.2"), Decimal("1.1"), Decimal("1.05"))
LEVELS = DEFAULT_LEVELS
HYST = Decimal("0.03")
HYSTERESIS = HYST
REPEAT_INTERVAL = 1800  # seconds (30 minutes)
L3_REPEAT_INTERVAL = REPEAT_INTERVAL  # backwards-compatible alias

# States
STATE_OK = "ok"
STATE_L1 = "L1"
STATE_L2 = "L2"
STATE_L3 = "L3"
STATE_L4 = "L4"

# Alert types
ALERT_L1 = "L1"
ALERT_L2 = "L2"
ALERT_L3 = "L3"
ALERT_L4 = "L4"
ALERT_REPEAT_L3 = "repeat_L3"
ALERT_REPEAT_L4 = "repeat_L4"
ALERT_RECOVERY = "recovery"


def hyst_for(levels: Sequence[Decimal] = DEFAULT_LEVELS) -> Decimal:
    """Compute hysteresis threshold for a given sequence of levels.

    hyst = min(0.03, 0.6 * min_gap) where min_gap is the smallest difference
    between consecutive levels.
    """
    if len(levels) < 2:
        return Decimal("0.03")
    min_gap = min(levels[i] - levels[i + 1] for i in range(len(levels) - 1))
    return min(Decimal("0.03"), Decimal("0.6") * min_gap)


def validate_levels(values: Iterable[Decimal | str | float]) -> tuple[Decimal, ...]:
    """Validate and return alert levels tuple.

    Raises ValueError with a user-readable message if requirements are not met:
    - 2 to 5 values
    - strictly descending
    - each between 1.01 and 5.0 inclusive
    - at least 0.04 between consecutive values
    - at most 2 decimals each
    """
    parsed: list[Decimal] = []
    for val in values:
        try:
            d = Decimal(str(val).strip())
        except Exception as exc:
            raise ValueError(f"Invalid alert level '{val}': not a valid number") from exc

        if not d.is_finite():
            raise ValueError(f"Invalid alert level '{val}': not a finite number")

        if d.as_tuple().exponent < -2:
            raise ValueError(f"Alert level {d} has more than 2 decimal places")

        if not (Decimal("1.01") <= d <= Decimal("5.0")):
            raise ValueError(f"Alert level {d} must be between 1.01 and 5.0")

        parsed.append(d)

    if len(parsed) < 2 or len(parsed) > 5:
        raise ValueError(f"Must specify between 2 and 5 alert levels (got {len(parsed)})")

    for i in range(len(parsed) - 1):
        if parsed[i] <= parsed[i + 1]:
            raise ValueError(
                f"Alert levels must be in strictly descending order: {parsed[i]} is not greater than {parsed[i + 1]}"
            )
        gap = parsed[i] - parsed[i + 1]
        if gap < Decimal("0.04"):
            raise ValueError(
                f"Difference between {parsed[i]} and {parsed[i + 1]} is {gap}, which is less than the 0.04 minimum gap"
            )

    return tuple(parsed)


def _state_to_level(state: str) -> int:
    """Map state string to severity integer level (0=ok, 1=L1, 2=L2, ...)."""
    if not state or state == STATE_OK:
        return 0
    if state.startswith("L") and state[1:].isdigit():
        return int(state[1:])
    if state == "warn":
        return 1
    if state == "critical":
        return 2
    return 0


def _level_to_state(level: int) -> str:
    """Map severity integer level to state string."""
    if level <= 0:
        return STATE_OK
    return f"L{level}"


def step(
    prev_state: str,
    hf: Decimal | None,
    now_ts: float,
    last_alert_ts: float | None,
    levels: Sequence[Decimal] = DEFAULT_LEVELS,
) -> tuple[str, str | None]:
    """Advance the health factor state machine generically over levels.

    States: ok (0), L1 (< levels[0]), L2 (< levels[1]), ...
    - Downward crossing: alerts once for the deepest level reached.
    - Upward hysteresis: hyst_for(levels) per level.
    - Recovery alert sent only when returning to ok (HF >= levels[0] + hyst).
    - While in the lowest TWO levels (k >= n-1): repeat alert every 30 minutes (1800 s).
    - hf=None (no debt): maps to ok (recovery alert if previously non-ok).
    """
    n = len(levels)
    hyst = hyst_for(levels)
    prev_level = _state_to_level(prev_state)

    if hf is None:
        if prev_level > 0:
            return STATE_OK, ALERT_RECOVERY
        return STATE_OK, None

    # Determine downward target level based on thresholds
    target_down = 0
    for i, thresh in enumerate(levels, start=1):
        if hf < thresh:
            target_down = i
        else:
            break

    # Downward crossing: deeper level reached
    if target_down > prev_level:
        new_state = _level_to_state(target_down)
        return new_state, new_state

    # Upward recovery or same level
    if prev_level > 0:
        eff_idx = min(prev_level, n) - 1
        if hf >= (levels[eff_idx] + hyst):
            # Position recovered above threshold + hysteresis
            if hf >= (levels[0] + hyst):
                return STATE_OK, ALERT_RECOVERY

            for k in range(1, prev_level):
                if k <= n and hf >= (levels[k] + hyst):
                    return _level_to_state(k), None
            return _level_to_state(prev_level - 1), None

    # Still in prev_level
    current_state = _level_to_state(prev_level)
    # Repeat alerts every 30 min while in the lowest TWO levels (k >= n-1)
    if prev_level >= (n - 1) and prev_level > 0:
        if last_alert_ts is None or (now_ts - last_alert_ts) >= REPEAT_INTERVAL:
            return current_state, f"repeat_{current_state}"
        return current_state, None

    return current_state, None

