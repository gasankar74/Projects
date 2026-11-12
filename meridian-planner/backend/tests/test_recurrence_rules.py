"""Recurrence rule arithmetic (acceptance test 11 and the date table from the plan)."""

from datetime import date

import pytest

from app.scheduler.recurrence import Rule, RuleError

LAST_BIZ = {"kind": "fixed", "rrule": "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", "adjust": "previous"}


def test_last_business_day_of_month_table():
    r = Rule.from_json(LAST_BIZ)
    got = r.fixed_dates(date(2026, 11, 1), date(2026, 11, 1), date(2027, 6, 30))
    assert got == [
        date(2026, 11, 30),  # Monday
        date(2026, 12, 31),  # Thursday
        date(2027, 1, 29),  # Jan 31 is a Sunday
        date(2027, 2, 26),  # Feb 28 is a Sunday
        date(2027, 3, 31),
        date(2027, 4, 30),
        date(2027, 5, 31),
        date(2027, 6, 30),
    ]


def test_last_business_day_skips_a_holiday():
    # Dec 31 2026 is a Thursday; if it's a firm holiday the occurrence moves to Wednesday Dec 30.
    r = Rule.from_json(LAST_BIZ, holidays=(date(2026, 12, 31),))
    assert r.fixed_dates(date(2026, 12, 1), date(2026, 12, 1), date(2026, 12, 31)) == [date(2026, 12, 30)]


def test_first_business_day_moves_forward():
    r = Rule.from_json({"kind": "fixed", "rrule": "FREQ=MONTHLY;BYMONTHDAY=1", "adjust": "next"}, holidays=(date(2027, 1, 1),))
    got = r.fixed_dates(date(2026, 11, 1), date(2026, 11, 1), date(2027, 3, 31))
    # Nov 1 2026 Sun -> Mon 2; Dec 1 Tue; Jan 1 holiday (Fri) -> Mon 4; Feb 1 Mon; Mar 1 Mon
    assert got == [date(2026, 11, 2), date(2026, 12, 1), date(2027, 1, 4), date(2027, 2, 1), date(2027, 3, 1)]


def test_first_monday_and_every_other_friday():
    r = Rule.from_json({"kind": "fixed", "rrule": "FREQ=MONTHLY;BYDAY=1MO"})
    assert r.fixed_dates(date(2026, 11, 1), date(2026, 11, 1), date(2027, 1, 31)) == [date(2026, 11, 2), date(2026, 12, 7), date(2027, 1, 4)]
    r = Rule.from_json({"kind": "fixed", "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"})
    assert r.fixed_dates(date(2026, 11, 13), date(2026, 11, 13), date(2026, 12, 31)) == [
        date(2026, 11, 13), date(2026, 11, 27), date(2026, 12, 11), date(2026, 12, 25),
    ]


def test_count_and_until():
    r = Rule.from_json({"kind": "fixed", "rrule": "FREQ=WEEKLY;BYDAY=MO", "count": 3})
    assert r.fixed_dates(date(2026, 11, 16), date(2026, 11, 1), date(2027, 12, 31)) == [date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 30)]
    r = Rule.from_json({"kind": "fixed", "rrule": "FREQ=WEEKLY;BYDAY=MO", "until": "2026-11-24"})
    assert r.fixed_dates(date(2026, 11, 16), date(2026, 11, 1), date(2027, 12, 31)) == [date(2026, 11, 16), date(2026, 11, 23)]
    assert r.next_fixed(date(2026, 11, 16), date(2026, 11, 23)) is None


def test_after_completion_intervals():
    r = Rule.from_json({"kind": "after_completion", "every": 2, "unit": "weeks"})
    assert r.next_after_completion(date(2026, 11, 12)) == date(2026, 11, 26)
    r = Rule.from_json({"kind": "after_completion", "every": 1, "unit": "months"})
    assert r.next_after_completion(date(2027, 1, 31)) == date(2027, 2, 28)  # clamped to month end
    r = Rule.from_json({"kind": "after_completion", "every": 3, "unit": "days", "adjust": "next"})
    assert r.next_after_completion(date(2026, 11, 12)) == date(2026, 11, 16)  # Sun 15 -> Mon 16
    assert r.describe() == "3 days after each one is done"


def test_lead_days_defaults():
    assert Rule.from_json({"kind": "fixed", "rrule": "FREQ=DAILY"}).default_lead_days() == 0
    assert Rule.from_json({"kind": "fixed", "rrule": "FREQ=WEEKLY;BYDAY=FR"}).default_lead_days() == 6
    assert Rule.from_json({"kind": "fixed", "rrule": "FREQ=MONTHLY"}).default_lead_days() == 6
    assert Rule.from_json({"kind": "fixed", "rrule": "FREQ=WEEKLY", "lead_days": 2}).default_lead_days() == 2


@pytest.mark.parametrize("bad", [
    {"kind": "sometimes"},
    {"kind": "fixed"},
    {"kind": "fixed", "rrule": "FREQ=NEVER"},
    {"kind": "after_completion", "unit": "fortnights"},
    {"kind": "after_completion", "every": 0},
    {"kind": "fixed", "rrule": "FREQ=DAILY", "due_time": "5pm"},
    {"kind": "fixed", "rrule": "FREQ=DAILY", "adjust": "sideways"},
])
def test_bad_rules_rejected(bad):
    with pytest.raises(RuleError):
        Rule.from_json(bad)
