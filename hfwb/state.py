from decimal import Decimal

# Module configuration
LEVELS = (Decimal("1.4"), Decimal("1.2"), Decimal("1.1"), Decimal("1.05"))
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
) -> tuple[str, str | None]:
    """Advance the health factor state machine generically over LEVELS.

    States: ok (0), L1 (< LEVELS[0]), L2 (< LEVELS[1]), ...
    - Downward crossing: alerts once for the deepest level reached.
    - Upward hysteresis: HYST (0.03) per level.
    - Recovery alert sent only when returning to ok (HF >= LEVELS[0] + HYST).
    - While in L3 or L4: repeat alert every 30 minutes (1800 s).
    - hf=None (no debt): maps to ok (recovery alert if previously non-ok).
    """
    prev_level = _state_to_level(prev_state)

    if hf is None:
        if prev_level > 0:
            return STATE_OK, ALERT_RECOVERY
        return STATE_OK, None

    # Determine downward target level based on thresholds
    target_down = 0
    for i, thresh in enumerate(LEVELS, start=1):
        if hf < thresh:
            target_down = i
        else:
            break

    # Downward crossing: deeper level reached
    if target_down > prev_level:
        new_state = _level_to_state(target_down)
        return new_state, new_state

    # Upward recovery or same level
    if prev_level > 0 and hf >= (LEVELS[prev_level - 1] + HYST):
        # Position recovered above prev_level threshold + hysteresis
        if hf >= (LEVELS[0] + HYST):
            return STATE_OK, ALERT_RECOVERY

        for k in range(1, prev_level):
            if hf >= (LEVELS[k] + HYST):
                return _level_to_state(k), None
        return _level_to_state(prev_level - 1), None

    # Still in prev_level
    current_state = _level_to_state(prev_level)
    # Repeat alerts every 30 min while in L3 or L4
    if prev_level in (3, 4):
        if last_alert_ts is None or (now_ts - last_alert_ts) >= REPEAT_INTERVAL:
            return current_state, f"repeat_{current_state}"
        return current_state, None

    return current_state, None
