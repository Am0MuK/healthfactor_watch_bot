from decimal import Decimal

from hfwb.schedule import (
    INTERVAL_FAR,
    INTERVAL_MID,
    INTERVAL_NEAR,
    backoff_for,
    combine,
    poll_interval_for,
)
from hfwb.state import DEFAULT_LEVELS


def test_constants():
    assert INTERVAL_FAR == 300
    assert INTERVAL_MID == 120
    assert INTERVAL_NEAR == 60


def test_poll_interval_no_debt():
    assert poll_interval_for(None, DEFAULT_LEVELS) == 300


def test_poll_interval_default_levels():
    # DEFAULT_LEVELS = (1.4, 1.2, 1.1, 1.05)
    # Safe far position
    assert poll_interval_for(Decimal("2.5"), DEFAULT_LEVELS) == 300
    assert poll_interval_for(Decimal("1.71"), DEFAULT_LEVELS) == 300  # d = 0.31 >= 0.30, hf >= 1.4

    # Approaching levels[0] (1.4): d < 0.30
    assert poll_interval_for(Decimal("1.69"), DEFAULT_LEVELS) == 120  # d = 0.29 < 0.30
    assert poll_interval_for(Decimal("1.50"), DEFAULT_LEVELS) == 120  # d = 0.10 >= 0.10, d < 0.30

    # Near levels[0] (1.4): d < 0.10
    assert poll_interval_for(Decimal("1.49"), DEFAULT_LEVELS) == 60  # d = 0.09 < 0.10
    assert poll_interval_for(Decimal("1.40"), DEFAULT_LEVELS) == 60  # d = 0.00 < 0.10

    # Between 1.4 and 1.2: next level is 1.2
    assert poll_interval_for(Decimal("1.35"), DEFAULT_LEVELS) == 120  # d = 0.15, hf < 1.4
    assert poll_interval_for(Decimal("1.29"), DEFAULT_LEVELS) == 60  # d = 0.09 < 0.10
    assert poll_interval_for(Decimal("1.20"), DEFAULT_LEVELS) == 60  # d = 0.00 < 0.10

    # Between 1.2 and 1.1: next level is 1.1
    assert poll_interval_for(Decimal("1.19"), DEFAULT_LEVELS) == 60  # d = 0.09 < 0.10
    assert poll_interval_for(Decimal("1.15"), DEFAULT_LEVELS) == 60  # d = 0.05 < 0.10
    assert poll_interval_for(Decimal("1.10"), DEFAULT_LEVELS) == 60  # d = 0.00 < 0.10

    # Below 1.15 is always 60
    assert poll_interval_for(Decimal("1.14"), DEFAULT_LEVELS) == 60
    assert poll_interval_for(Decimal("1.12"), DEFAULT_LEVELS) == 60
    assert poll_interval_for(Decimal("1.05"), DEFAULT_LEVELS) == 60
    assert poll_interval_for(Decimal("1.02"), DEFAULT_LEVELS) == 60
    assert poll_interval_for(Decimal("1.00"), DEFAULT_LEVELS) == 60
    assert poll_interval_for(Decimal("0.90"), DEFAULT_LEVELS) == 60


def test_poll_interval_exact_boundaries():
    # Boundary d = 0.10:
    # levels=(2.0, 1.5, 1.25). hf = 2.10 -> d = 0.10
    # Rule 1: hf < 1.15 (False), d < 0.10 (False, 0.10 is not < 0.10).
    # Rule 2: d < 0.30 (True, 0.10 < 0.30). -> 120
    custom = (Decimal("2.0"), Decimal("1.5"), Decimal("1.25"))
    assert poll_interval_for(Decimal("2.10"), custom) == 120
    assert poll_interval_for(Decimal("2.099"), custom) == 60  # d < 0.10 -> 60

    # Boundary d = 0.30:
    # hf = 2.30 -> d = 0.30.
    # Rule 1: False.
    # Rule 2: d < 0.30 (False, 0.30 is not < 0.30), hf < 1.4 (False). -> 300
    assert poll_interval_for(Decimal("2.30"), custom) == 300
    assert poll_interval_for(Decimal("2.299"), custom) == 120  # d < 0.30 -> 120

    # Boundary hf = 1.15:
    # levels=(1.05, 1.01). hf = 1.15 -> d = 0.10
    # Rule 1: hf < 1.15 (False, 1.15 is not < 1.15), d < 0.10 (False).
    # Rule 2: d < 0.30 (True, 0.10 < 0.30). -> 120
    low_levels = (Decimal("1.05"), Decimal("1.01"))
    assert poll_interval_for(Decimal("1.15"), low_levels) == 120
    assert poll_interval_for(Decimal("1.149"), low_levels) == 60  # hf < 1.15 -> 60

    # Boundary hf = 1.4:
    # levels=(1.05, 1.01). hf = 1.40 -> d = 0.35 >= 0.30
    # Rule 1: False
    # Rule 2: d < 0.30 (False), hf < 1.4 (False, 1.4 is not < 1.4). -> 300
    assert poll_interval_for(Decimal("1.40"), low_levels) == 300
    assert poll_interval_for(Decimal("1.399"), low_levels) == 120  # hf < 1.4 -> 120


def test_poll_interval_custom_levels_2_0_1_5_1_25():
    levels = (Decimal("2.0"), Decimal("1.5"), Decimal("1.25"))
    assert poll_interval_for(Decimal("2.5"), levels) == 300
    assert poll_interval_for(Decimal("2.2"), levels) == 120
    assert poll_interval_for(Decimal("2.05"), levels) == 60
    assert poll_interval_for(Decimal("1.80"), levels) == 300  # d = 1.80 - 1.50 = 0.30, hf >= 1.4 -> 300
    assert poll_interval_for(Decimal("1.75"), levels) == 120  # d = 0.25 < 0.30 -> 120
    assert poll_interval_for(Decimal("1.55"), levels) == 60  # d = 0.05 < 0.10 -> 60
    assert poll_interval_for(Decimal("1.35"), levels) == 120  # hf < 1.4 -> 120
    assert poll_interval_for(Decimal("1.10"), levels) == 60  # hf < 1.15 -> 60


def test_poll_interval_custom_levels_1_5_1_46():
    levels = (Decimal("1.5"), Decimal("1.46"))
    assert poll_interval_for(Decimal("1.80"), levels) == 300  # d = 0.30 -> 300
    assert poll_interval_for(Decimal("1.70"), levels) == 120  # d = 0.20 -> 120
    assert poll_interval_for(Decimal("1.55"), levels) == 60  # d = 0.05 -> 60
    assert poll_interval_for(Decimal("1.48"), levels) == 60  # d = 1.48 - 1.46 = 0.02 -> 60
    # Below lowest level 1.46: d = hf - 1.00
    assert poll_interval_for(Decimal("1.45"), levels) == 300  # d = 0.45, hf >= 1.4 -> 300
    assert poll_interval_for(Decimal("1.35"), levels) == 120  # hf < 1.4 -> 120
    assert poll_interval_for(Decimal("1.05"), levels) == 60  # hf < 1.15 -> 60


def test_poll_interval_hf_below_or_equal_one():
    for levels in [DEFAULT_LEVELS, (Decimal("2.0"), Decimal("1.5")), (Decimal("1.5"), Decimal("1.46"))]:
        assert poll_interval_for(Decimal("1.0"), levels) == 60
        assert poll_interval_for(Decimal("0.9"), levels) == 60
        assert poll_interval_for(Decimal("0.5"), levels) == 60
        assert poll_interval_for(Decimal("0.0"), levels) == 60
        assert poll_interval_for(Decimal("-1.0"), levels) == 60


def test_combine():
    assert combine([300, 120]) == 120
    assert combine([300, 60, 120]) == 60
    assert combine([300]) == 300
    assert combine((60, 60)) == 60
    assert combine([120, 120]) == 120


def test_backoff_for():
    assert backoff_for(0) == 60
    assert backoff_for(1) == 60
    assert backoff_for(2) == 120
    assert backoff_for(3) == 240
    assert backoff_for(4) == 300
    assert backoff_for(5) == 300
    assert backoff_for(10) == 300
