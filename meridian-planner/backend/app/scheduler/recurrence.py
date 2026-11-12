"""Recurrence rules: pure date arithmetic, no database.

Two kinds of series:

- fixed: occurrences follow a calendar rule (an RFC 5545 RRULE such as "every Friday" or "the last weekday of
  every month"), whether or not earlier ones were done.
- after_completion: the next occurrence is due a set interval after the previous one was completed
  ("2 weeks after I finish it").

Business-day handling: `adjust` moves an occurrence that lands on a weekend or holiday to the previous or next
business day ("previous" is what "last business day of the month" needs when the 31st is a public holiday).
All dates are local dates in the series' time zone; the due time is applied by the caller.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr

UNITS = ("days", "weeks", "months", "years")
ADJUST = ("none", "previous", "next")
MISSED = ("keep", "skip", "latest_only")
MAX_SCAN = 1000  # safety cap on how many raw occurrences are examined in one call


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    kind: str = "fixed"  # fixed | after_completion
    rrule: str | None = None  # fixed: e.g. "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"
    every: int = 1  # after_completion: interval
    unit: str = "weeks"  # after_completion: days | weeks | months | years
    due_time: str = "17:00"
    adjust: str = "none"  # none | previous | next  (weekends and holidays)
    lead_days: int | None = None  # how many days before the due date work may start; None = default by frequency
    until: date | None = None
    count: int | None = None
    holidays: tuple[date, ...] = field(default_factory=tuple)

    @staticmethod
    def from_json(d: dict, holidays: tuple[date, ...] = ()) -> "Rule":
        kind = d.get("kind") or "fixed"
        if kind not in ("fixed", "after_completion"):
            raise RuleError("Repeat type must be fixed or after_completion")
        rr = d.get("rrule")
        if kind == "fixed":
            if not rr:
                raise RuleError("A fixed repeat needs a rule")
            rr = rr.removeprefix("RRULE:")
            try:
                rrulestr(f"RRULE:{rr}", dtstart=datetime(2026, 1, 1))
            except (ValueError, TypeError) as e:
                raise RuleError(f"Couldn't read the repeat rule: {e}") from None
            if "DTSTART" in rr.upper():
                raise RuleError("Give the start date separately, not inside the rule")
        unit = d.get("unit") or "weeks"
        if unit not in UNITS:
            raise RuleError(f"Unit must be one of {', '.join(UNITS)}")
        every = int(d["every"]) if d.get("every") is not None else 1
        if every < 1 or every > 366:
            raise RuleError("Interval must be between 1 and 366")
        adjust = d.get("adjust") or "none"
        if adjust not in ADJUST:
            raise RuleError("Business-day adjustment must be none, previous or next")
        due = d.get("due_time") or "17:00"
        try:
            hh, mm = due.split(":")
            time(int(hh), int(mm))
        except Exception:
            raise RuleError("Due time must look like 17:00") from None
        lead = d.get("lead_days")
        if lead is not None and not (0 <= int(lead) <= 366):
            raise RuleError("Lead days must be between 0 and 366")
        until = d.get("until")
        count = d.get("count")
        if count is not None and int(count) < 1:
            raise RuleError("Count must be at least 1")
        return Rule(
            kind=kind,
            rrule=rr,
            every=every,
            unit=unit,
            due_time=due,
            adjust=adjust,
            lead_days=int(lead) if lead is not None else None,
            until=date.fromisoformat(until) if isinstance(until, str) else until,
            count=int(count) if count is not None else None,
            holidays=tuple(holidays),
        )

    # -- helpers ----------------------------------------------------------------------------------------------

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def adjusted(self, d: date) -> date:
        if self.adjust == "none":
            return d
        step = -1 if self.adjust == "previous" else 1
        for _ in range(30):
            if self.is_business_day(d):
                return d
            d += timedelta(days=step)
        return d

    def due_clock(self) -> time:
        hh, mm = self.due_time.split(":")
        return time(int(hh), int(mm))

    def frequency(self) -> str:
        if self.kind == "after_completion":
            return {"days": "DAILY", "weeks": "WEEKLY", "months": "MONTHLY", "years": "YEARLY"}[self.unit]
        for part in (self.rrule or "").upper().split(";"):
            if part.startswith("FREQ="):
                return part[5:]
        return "WEEKLY"

    def interval(self) -> int:
        if self.kind == "after_completion":
            return self.every
        for part in (self.rrule or "").upper().split(";"):
            if part.startswith("INTERVAL="):
                return int(part[9:])
        return 1

    def default_lead_days(self) -> int:
        """How early work on an occurrence may start, when the series doesn't say."""
        if self.lead_days is not None:
            return self.lead_days
        freq, n = self.frequency(), self.interval()
        if freq == "DAILY":
            return 0
        if freq == "WEEKLY":
            # A weekly task can be done any working day of its week, a fortnightly one in its last week.
            return min(6, 7 * n - 1) if n == 1 else 6
        if freq == "MONTHLY":
            return 6
        return 13

    # -- occurrences ------------------------------------------------------------------------------------------

    def fixed_dates(self, anchor: date, start: date, end: date) -> list[date]:
        """Occurrence dates (after business-day adjustment) between start and end inclusive, counting the series
        from `anchor` (its first possible date). `count` and `until` apply to the raw rule."""
        if self.kind != "fixed":
            raise RuleError("Not a fixed rule")
        rule = rrulestr(f"RRULE:{self.rrule}", dtstart=datetime.combine(anchor, time(12)))
        out: list[date] = []
        n = 0
        for dt in rule:
            n += 1
            if n > MAX_SCAN or (self.count is not None and n > self.count):
                break
            raw = dt.date()
            if self.until and raw > self.until:
                break
            d = self.adjusted(raw)
            if d > end and raw > end + timedelta(days=30):
                break
            if start <= d <= end and (not out or out[-1] != d):
                out.append(d)
        return sorted(set(out))

    def next_fixed(self, anchor: date, after: date) -> date | None:
        """First occurrence strictly after `after`."""
        dates = self.fixed_dates(anchor, after + timedelta(days=1), after + timedelta(days=800))
        return dates[0] if dates else None

    def next_after_completion(self, completed_on: date) -> date:
        if self.kind != "after_completion":
            raise RuleError("Not a completion-based rule")
        d = completed_on
        if self.unit == "days":
            d += timedelta(days=self.every)
        elif self.unit == "weeks":
            d += timedelta(weeks=self.every)
        else:
            months = self.every * (12 if self.unit == "years" else 1)
            y, m = divmod(d.month - 1 + months, 12)
            y, m = d.year + y, m + 1
            d = date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
        return self.adjusted(d)

    def describe(self) -> str:
        if self.kind == "after_completion":
            unit = self.unit[:-1] if self.every == 1 else self.unit
            n = "" if self.every == 1 else f"{self.every} "
            return f"{n}{unit} after each one is done".strip()
        return self.rrule or ""


def due_datetime(d: date, rule: Rule, tz) -> datetime:
    return datetime.combine(d, rule.due_clock(), tz)


def earliest_start(d: date, rule: Rule, tz) -> datetime:
    return datetime.combine(d - timedelta(days=rule.default_lead_days()), time(0), tz)
