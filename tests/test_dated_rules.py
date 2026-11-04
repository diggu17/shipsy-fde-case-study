from datetime import date

import pytest

from dispatch_planner.settings import active_dated_rules


@pytest.mark.parametrize(
    "plan_date, expected",
    [
        (date(2026, 5, 31), False),
        (date(2026, 6, 1), True),
        (date(2026, 9, 30), True),
        (date(2026, 10, 1), False),
        (date(2026, 10, 14), False),
    ],
)
def test_sahras_summer_rule_applies_from_june_to_september(sahra, plan_date, expected):
    assert bool(active_dated_rules(sahra, plan_date)) is expected


@pytest.mark.parametrize(
    "plan_date, expected",
    [
        (date(2026, 11, 1), True),
        (date(2027, 1, 15), True),
        (date(2027, 2, 28), True),
        (date(2027, 3, 1), False),
        (date(2026, 10, 31), False),
    ],
)
def test_a_rule_can_cross_the_new_year(sahra, plan_date, expected):
    sahra["dated_rules"][0].update(from_month=11, to_month=2)

    assert bool(active_dated_rules(sahra, plan_date)) is expected


def test_active_rule_keeps_its_vehicle_type_and_hours(sahra):
    assert active_dated_rules(sahra, date(2026, 7, 15)) == [
        {
            "vehicle_type": "MOTORBIKE",
            "from_month": 6,
            "to_month": 9,
            "no_riding_start": "12:30",
            "no_riding_end": "15:00",
        }
    ]
