from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.quick_add import parse

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=NY)  # Thursday


def test_cle_proposal():
    r = parse("Prepare CLE proposal by Friday, high priority, 3 hours.", NOW)
    assert r.title == "Prepare CLE proposal"
    assert r.priority == "high"
    assert r.duration_min == 180
    assert r.deadline == datetime(2026, 11, 13, 17, 0, tzinfo=NY)
    assert r.deadline_type == "hard"
    assert r.category_name == "Speaking and Presentations"
    assert r.can_split is True  # long work splits by default


def test_weekly_quickbooks():
    r = parse("Review QuickBooks every Friday for 30 minutes.", NOW)
    assert r.title == "Review QuickBooks"
    assert r.duration_min == 30
    assert r.recurrence["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert r.deadline.date().isoformat() == "2026-11-13"  # first occurrence
    assert r.category_name == "Financial and Insurance"


def test_email_tomorrow():
    r = parse("Email Laura tomorrow, 10 minutes.", NOW)
    assert r.title == "Email Laura"
    assert r.duration_min == 10
    assert r.deadline.date().isoformat() == "2026-11-13"
    assert r.deadline_type == "soft"


def test_sessions_this_week():
    r = parse("Work on trauma presentation for 90 minutes three times this week.", NOW)
    assert r.title == "Work on trauma presentation"
    assert r.duration_min == 270
    assert r.can_split is True
    assert r.min_session_min == r.max_session_min == 90
    assert r.deadline.date().isoformat() == "2026-11-13"


def test_last_weekday_of_month():
    r = parse("Review monthly bills on the last weekday of every month", NOW)
    assert r.title == "Review monthly bills"
    assert r.recurrence["rrule"] == "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"
    assert r.deadline.date().isoformat() == "2026-11-30"  # Mon Nov 30


def test_last_weekday_when_month_ends_on_weekend():
    # January 2027 ends on a Sunday; the last business day is Friday the 29th.
    now = datetime(2027, 1, 2, 9, 0, tzinfo=NY)
    r = parse("Pay payroll taxes on the last business day of each month", now)
    assert r.deadline.date().isoformat() == "2027-01-29"


@pytest.mark.parametrize(
    "text,rrule",
    [
        ("Standup notes every weekday", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        ("Check inbox daily", "FREQ=DAILY"),
        ("Board report quarterly", "FREQ=MONTHLY;INTERVAL=3"),
        ("Renew bar license annually", "FREQ=YEARLY"),
        ("Team sync every other Tuesday", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"),
        ("Water plants every 3 days", "FREQ=DAILY;INTERVAL=3"),
        ("Lunch with mentor the first Monday of every month", "FREQ=MONTHLY;BYDAY=1MO"),
        ("Work on speaking presentations for two hours each week", "FREQ=WEEKLY"),
    ],
)
def test_recurrence_rules(text, rrule):
    assert parse(text, NOW).recurrence["rrule"] == rrule


def test_two_hours_word_number():
    r = parse("Work on speaking presentations for two hours each week", NOW)
    assert r.duration_min == 120
    assert r.title == "Work on speaking presentations"


def test_priority_time_tags_confidential():
    r = parse("Call court clerk urgent by Nov 20 at 3pm 15 min #filing confidential", NOW)
    assert r.priority == "urgent"
    assert r.deadline == datetime(2026, 11, 20, 15, 0, tzinfo=NY)
    assert r.tags == ["filing"]
    assert r.confidential is True
    assert r.title == "Call court clerk"
    assert r.category_name == "Legal and Mediation"


def test_plain_title_untouched():
    r = parse("Think about strategy", NOW)
    assert r.title == "Think about strategy"
    assert r.deadline is None and r.duration_min is None and r.priority is None


def test_next_weekday_and_numeric_dates():
    assert parse("Send deck next Monday", NOW).deadline.date().isoformat() == "2026-11-16"
    assert parse("File motion by 12/1", NOW).deadline.date().isoformat() == "2026-12-01"
    assert parse("Book venue by Jan 5", NOW).deadline.date().isoformat() == "2027-01-05"
    assert parse("Draft agenda in 3 days", NOW).deadline.date().isoformat() == "2026-11-15"


def test_past_deadline_is_kept_so_it_shows_overdue():
    r = parse("File response to motion by yesterday, urgent", NOW)
    assert r.title == "File response to motion"
    assert r.deadline.date().isoformat() == "2026-11-11"


def test_completion_based_repeat():
    r = parse("Water office plants every 2 weeks after it's done, 15 minutes", NOW)
    assert r.title == "Water office plants"
    assert r.recurrence["kind"] == "after_completion" and r.recurrence["every"] == 2 and r.recurrence["unit"] == "weeks"
    assert r.duration_min == 15


def test_business_day_repeats_step_around_holidays():
    r = parse("Trust reconciliation on the last business day of every month", NOW)
    assert r.recurrence["adjust"] == "previous"
    r = parse("Payroll on the first business day of every month", NOW)
    assert r.recurrence["adjust"] == "next"
