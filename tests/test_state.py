from decimal import Decimal

from hfwb.state import (
    ALERT_L1,
    ALERT_L2,
    ALERT_L3,
    ALERT_L4,
    ALERT_RECOVERY,
    ALERT_REPEAT_L3,
    ALERT_REPEAT_L4,
    HYST,
    HYSTERESIS,
    LEVELS,
    REPEAT_INTERVAL,
    STATE_L1,
    STATE_L2,
    STATE_L3,
    STATE_L4,
    STATE_OK,
    step,
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
