from decimal import Decimal

import pytest

from hfwb.state import (
    ALERT_L1,
    ALERT_L2,
    ALERT_L3,
    ALERT_L4,
    ALERT_RECOVERY,
    ALERT_REPEAT_L3,
    ALERT_REPEAT_L4,
    DEFAULT_LEVELS,
    HYST,
    HYSTERESIS,
    LEVELS,
    REPEAT_INTERVAL,
    STATE_L1,
    STATE_L2,
    STATE_L3,
    STATE_L4,
    STATE_OK,
    hyst_for,
    step,
    validate_levels,
)


def test_constants():
    assert LEVELS == (Decimal("1.4"), Decimal("1.2"), Decimal("1.1"), Decimal("1.05"))
    assert HYSTERESIS == Decimal("0.03")
    assert HYST == Decimal("0.03")
    assert REPEAT_INTERVAL == 1800
    assert STATE_OK == "ok"
    assert STATE_L1 == "L1"
    assert STATE_L2 == "L2"
    assert STATE_L3 == "L3"
    assert STATE_L4 == "L4"


def test_initial_ok_remains_ok():
    new_state, alert = step(STATE_OK, Decimal("2.0"), now_ts=1000.0, last_alert_ts=None)
    assert new_state == STATE_OK
    assert alert is None


def test_boundaries_downward():
    # Exactly at 1.4: ok
    s, a = step(STATE_OK, Decimal("1.4"), 1000.0, None)
    assert s == STATE_OK and a is None

    # Below 1.4: L1
    s, a = step(STATE_OK, Decimal("1.39"), 1000.0, None)
    assert s == STATE_L1 and a == ALERT_L1

    # Exactly at 1.2: L1
    s, a = step(STATE_L1, Decimal("1.2"), 1000.0, None)
    assert s == STATE_L1 and a is None

    # Below 1.2: L2
    s, a = step(STATE_L1, Decimal("1.19"), 1000.0, None)
    assert s == STATE_L2 and a == ALERT_L2

    # Exactly at 1.1: L2
    s, a = step(STATE_L2, Decimal("1.1"), 1000.0, None)
    assert s == STATE_L2 and a is None

    # Below 1.1: L3
    s, a = step(STATE_L2, Decimal("1.09"), 1000.0, None)
    assert s == STATE_L3 and a == ALERT_L3

    # Exactly at 1.05: L3
    s, a = step(STATE_L3, Decimal("1.05"), 1000.0, 1000.0)
    assert s == STATE_L3 and a is None

    # Below 1.05: L4
    s, a = step(STATE_L3, Decimal("1.04"), 1000.0, None)
    assert s == STATE_L4 and a == ALERT_L4


def test_jump_ok_to_l4():
    # Jump over several levels sends one alert for deepest level (L4)
    s, a = step(STATE_OK, Decimal("1.02"), 1000.0, None)
    assert s == STATE_L4 and a == ALERT_L4


def test_slow_recovery_l4_to_ok():
    # L4 -> L3 (threshold 1.05 + 0.03 = 1.08)
    # At 1.07: still L4
    s, a = step(STATE_L4, Decimal("1.07"), 1000.0, 1000.0)
    assert s == STATE_L4 and a is None
    # At 1.08: recovers to L3, no alert
    s, a = step(STATE_L4, Decimal("1.08"), 1000.0, 1000.0)
    assert s == STATE_L3 and a is None

    # L3 -> L2 (threshold 1.10 + 0.03 = 1.13)
    # At 1.12: still L3
    s, a = step(STATE_L3, Decimal("1.12"), 1000.0, 1000.0)
    assert s == STATE_L3 and a is None
    # At 1.13: recovers to L2, no alert
    s, a = step(STATE_L3, Decimal("1.13"), 1000.0, 1000.0)
    assert s == STATE_L2 and a is None

    # L2 -> L1 (threshold 1.20 + 0.03 = 1.23)
    # At 1.22: still L2
    s, a = step(STATE_L2, Decimal("1.22"), 1000.0, None)
    assert s == STATE_L2 and a is None
    # At 1.23: recovers to L1, no alert
    s, a = step(STATE_L2, Decimal("1.23"), 1000.0, None)
    assert s == STATE_L1 and a is None

    # L1 -> ok (threshold 1.40 + 0.03 = 1.43)
    # At 1.42: still L1
    s, a = step(STATE_L1, Decimal("1.42"), 1000.0, None)
    assert s == STATE_L1 and a is None
    # At 1.43: recovers to ok with recovery alert
    s, a = step(STATE_L1, Decimal("1.43"), 1000.0, None)
    assert s == STATE_OK and a == ALERT_RECOVERY


def test_direct_recovery_from_l4_straight_to_ok():
    s, a = step(STATE_L4, Decimal("1.50"), 1000.0, None)
    assert s == STATE_OK and a == ALERT_RECOVERY


def test_repeat_in_l3_and_l4():
    # In L3: repeats every 1800s
    s, a = step(STATE_L3, Decimal("1.09"), 2799.0, 1000.0)
    assert s == STATE_L3 and a is None
    s, a = step(STATE_L3, Decimal("1.09"), 2800.0, 1000.0)
    assert s == STATE_L3 and a == ALERT_REPEAT_L3

    # In L4: repeats every 1800s
    s, a = step(STATE_L4, Decimal("1.02"), 2799.0, 1000.0)
    assert s == STATE_L4 and a is None
    s, a = step(STATE_L4, Decimal("1.02"), 2800.0, 1000.0)
    assert s == STATE_L4 and a == ALERT_REPEAT_L4


def test_no_debt():
    s, a = step(STATE_OK, None, 1000.0, None)
    assert s == STATE_OK and a is None

    s, a = step(STATE_L4, None, 1000.0, None)
    assert s == STATE_OK and a == ALERT_RECOVERY


def test_default_levels_and_alias():
    assert DEFAULT_LEVELS == (Decimal("1.4"), Decimal("1.2"), Decimal("1.1"), Decimal("1.05"))
    assert LEVELS == DEFAULT_LEVELS


def test_hyst_for():
    # Default levels: min gap is 1.1 - 1.05 = 0.05. 0.6 * 0.05 = 0.03. min(0.03, 0.03) = 0.03
    assert hyst_for(DEFAULT_LEVELS) == Decimal("0.03")

    # Gap of 0.04: 0.6 * 0.04 = 0.024. min(0.03, 0.024) = 0.024
    levels_04 = (Decimal("1.50"), Decimal("1.46"))
    assert hyst_for(levels_04) == Decimal("0.024")

    # Large gap: min gap is 0.5. 0.6 * 0.5 = 0.3. min(0.03, 0.3) = 0.03
    levels_large = (Decimal("2.0"), Decimal("1.5"))
    assert hyst_for(levels_large) == Decimal("0.03")


def test_validate_levels_valid():
    # 2 levels
    l2 = validate_levels(["2.0", "1.5"])
    assert l2 == (Decimal("2.0"), Decimal("1.5"))

    # 3 levels
    l3 = validate_levels([Decimal("1.8"), Decimal("1.4"), Decimal("1.2")])
    assert l3 == (Decimal("1.8"), Decimal("1.4"), Decimal("1.2"))

    # 4 levels (defaults)
    l4 = validate_levels(DEFAULT_LEVELS)
    assert l4 == DEFAULT_LEVELS

    # 5 levels
    l5 = validate_levels(["2.5", "2.0", "1.6", "1.3", "1.1"])
    assert l5 == (Decimal("2.5"), Decimal("2.0"), Decimal("1.6"), Decimal("1.3"), Decimal("1.1"))

    # Boundary values: 5.0 and 1.01
    l_bound = validate_levels(["5.0", "1.01"])
    assert l_bound == (Decimal("5.0"), Decimal("1.01"))

    # 2 decimals max
    l_dec = validate_levels(["1.55", "1.45", "1.05"])
    assert l_dec == (Decimal("1.55"), Decimal("1.45"), Decimal("1.05"))


def test_validate_levels_invalid():
    # Too few (< 2)
    with pytest.raises(ValueError, match="between 2 and 5"):
        validate_levels(["1.4"])

    # Too many (> 5)
    with pytest.raises(ValueError, match="between 2 and 5"):
        validate_levels(["2.0", "1.8", "1.6", "1.4", "1.2", "1.05"])

    # Not strictly descending (ascending)
    with pytest.raises(ValueError, match="strictly descending"):
        validate_levels(["1.2", "1.4"])

    # Not strictly descending (equal)
    with pytest.raises(ValueError, match="strictly descending"):
        validate_levels(["1.4", "1.4"])

    # Below minimum (1.01)
    with pytest.raises(ValueError, match="between 1.01 and 5.0"):
        validate_levels(["1.4", "1.00"])

    # Above maximum (5.0)
    with pytest.raises(ValueError, match="between 1.01 and 5.0"):
        validate_levels(["5.05", "1.4"])

    # Gap too small (< 0.04)
    with pytest.raises(ValueError, match="0.04"):
        validate_levels(["1.40", "1.37"])

    # More than 2 decimals
    with pytest.raises(ValueError, match="2 decimal"):
        validate_levels(["1.455", "1.20"])


def test_step_two_levels():
    levels = (Decimal("2.0"), Decimal("1.5"))
    # hyst_for is min(0.03, 0.6 * 0.5 = 0.3) = 0.03

    # Initial ok
    s, a = step("ok", Decimal("2.5"), 1000.0, None, levels=levels)
    assert s == "ok" and a is None

    # Down to L1 (< 2.0)
    s, a = step("ok", Decimal("1.99"), 1000.0, None, levels=levels)
    assert s == "L1" and a == "L1"

    # Jump straight to L2 (< 1.5)
    s, a = step("ok", Decimal("1.40"), 1000.0, None, levels=levels)
    assert s == "L2" and a == "L2"

    # Repeat rule: lowest TWO levels. For n=2, both L1 and L2 repeat!
    s, a = step("L1", Decimal("1.80"), 2800.0, 1000.0, levels=levels)
    assert s == "L1" and a == "repeat_L1"

    s, a = step("L2", Decimal("1.40"), 2800.0, 1000.0, levels=levels)
    assert s == "L2" and a == "repeat_L2"

    # Recovery: L2 -> L1 at 1.5 + 0.03 = 1.53
    s, a = step("L2", Decimal("1.52"), 1000.0, 1000.0, levels=levels)
    assert s == "L2" and a is None
    s, a = step("L2", Decimal("1.53"), 1000.0, 1000.0, levels=levels)
    assert s == "L1" and a is None

    # Recovery: L1 -> ok at 2.0 + 0.03 = 2.03
    s, a = step("L1", Decimal("2.02"), 1000.0, 1000.0, levels=levels)
    assert s == "L1" and a is None
    s, a = step("L1", Decimal("2.03"), 1000.0, 1000.0, levels=levels)
    assert s == "ok" and a == "recovery"


def test_step_three_levels():
    levels = (Decimal("1.8"), Decimal("1.4"), Decimal("1.2"))
    # lowest TWO levels are L2 and L3 (k >= 3 - 1 = 2)

    # L1 (< 1.8) does NOT repeat
    s, a = step("L1", Decimal("1.6"), 2800.0, 1000.0, levels=levels)
    assert s == "L1" and a is None

    # L2 (< 1.4) does repeat
    s, a = step("L2", Decimal("1.3"), 2800.0, 1000.0, levels=levels)
    assert s == "L2" and a == "repeat_L2"

    # L3 (< 1.2) does repeat
    s, a = step("L3", Decimal("1.1"), 2800.0, 1000.0, levels=levels)
    assert s == "L3" and a == "repeat_L3"


def test_step_five_levels():
    levels = (Decimal("2.5"), Decimal("2.0"), Decimal("1.6"), Decimal("1.3"), Decimal("1.1"))
    # lowest TWO levels are L4 and L5 (k >= 5 - 1 = 4)

    # ok -> L5 jump
    s, a = step("ok", Decimal("1.05"), 1000.0, None, levels=levels)
    assert s == "L5" and a == "L5"

    # L1, L2, L3 do NOT repeat
    for st, val in [("L1", Decimal("2.2")), ("L2", Decimal("1.8")), ("L3", Decimal("1.4"))]:
        s, a = step(st, val, 2800.0, 1000.0, levels=levels)
        assert s == st and a is None

    # L4 and L5 DO repeat
    s, a = step("L4", Decimal("1.2"), 2800.0, 1000.0, levels=levels)
    assert s == "L4" and a == "repeat_L4"
    s, a = step("L5", Decimal("1.05"), 2800.0, 1000.0, levels=levels)
    assert s == "L5" and a == "repeat_L5"

    # Step-by-step recovery from L5:
    # L5 threshold 1.1 + 0.03 = 1.13 -> recovers to L4
    s, a = step("L5", Decimal("1.13"), 1000.0, None, levels=levels)
    assert s == "L4" and a is None

    # L4 threshold 1.3 + 0.03 = 1.33 -> recovers to L3
    s, a = step("L4", Decimal("1.33"), 1000.0, None, levels=levels)
    assert s == "L3" and a is None


def test_step_hysteresis_small_gap_04():
    # Gap of 0.04: min_gap is 0.04, hyst = 0.6 * 0.04 = 0.024
    levels = (Decimal("1.50"), Decimal("1.46"))
    assert hyst_for(levels) == Decimal("0.024")

    # Down to L2
    s, a = step("ok", Decimal("1.45"), 1000.0, None, levels=levels)
    assert s == "L2" and a == "L2"

    # Recovery threshold for L2 -> L1 is 1.46 + 0.024 = 1.484
    # At 1.483: still L2
    s, a = step("L2", Decimal("1.483"), 1000.0, 1000.0, levels=levels)
    assert s == "L2" and a is None
    # At 1.484: recovers to L1
    s, a = step("L2", Decimal("1.484"), 1000.0, 1000.0, levels=levels)
    assert s == "L1" and a is None


import pytest as _pytest_nonfinite


@_pytest_nonfinite.mark.parametrize("bad", ["NaN", "nan", "inf", "-inf", "Infinity", "sNaN"])
def test_validate_levels_rejects_non_finite_with_value_error(bad):
    from hfwb.state import validate_levels

    with _pytest_nonfinite.raises(ValueError):
        validate_levels([bad, "1.2"])
    with _pytest_nonfinite.raises(ValueError):
        validate_levels(["1.5", bad])
