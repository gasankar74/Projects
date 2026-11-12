"""Rule-based natural-language quick add.

Deterministic and offline (no AI call), so confidential text never leaves the server. Phase 4 adds an
optional Claude fallback for phrasing these rules miss; the rules stay the first pass.

Examples:
  "Prepare CLE proposal by Friday, high priority, 3 hours."
  "Review QuickBooks every Friday for 30 minutes."
  "Email Laura tomorrow, 10 minutes."
  "Work on trauma presentation for 90 minutes three times this week."
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_SHORT = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RRULE_DAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "once": 1, "twice": 2}
DEFAULT_DUE = time(17, 0)

WD = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?"
MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
TIME = r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?"

# Category guesses from keywords. Names match the default categories; the user can always change them.
CATEGORY_KEYWORDS = {
    "Legal and Mediation": ["mediation", "mediate", "court", "hearing", "brief", "motion", "client matter", "deposition", "settlement", "contract", "legal"],
    "Speaking and Presentations": ["cle", "presentation", "keynote", "talk", "speaking", "slides", "webinar", "panel", "rehearse"],
    "Business Administration": ["admin", "payroll", "hr", "hire", "onboard", "policy", "operations", "license", "renewal"],
    "Marketing and Networking": ["marketing", "linkedin", "newsletter", "networking", "coffee with", "social media", "website", "podcast"],
    "Financial and Insurance": ["quickbooks", "invoice", "bills", "bill", "tax", "taxes", "insurance", "budget", "reconcile", "bank", "payment"],
    "Education": ["course", "class", "lecture", "grading", "syllabus", "student", "research", "paper", "study", "academic"],
    "Home and Family": ["kids", "school pickup", "family", "groceries", "home", "house", "dinner", "mom", "dad"],
    "Personal": ["gym", "doctor", "dentist", "workout", "haircut", "run ", "meditate"],
}


@dataclass
class QuickAddResult:
    title: str
    priority: str | None = None
    duration_min: int | None = None
    deadline: datetime | None = None
    deadline_type: str | None = None
    can_split: bool | None = None
    min_session_min: int | None = None
    max_session_min: int | None = None
    recurrence: dict | None = None
    category_name: str | None = None
    tags: list[str] = field(default_factory=list)
    confidential: bool = False
    understood: list[str] = field(default_factory=list)


def _weekday_index(token: str) -> int:
    t = token.lower().rstrip("s")
    for i, short in enumerate(WD_SHORT):
        if t.startswith(short):
            return i
    raise ValueError(token)


def _month_index(token: str) -> int:
    t = token.lower()[:3]
    return [m[:3] for m in MONTHS].index(t) + 1


def _at(d: date, tz: ZoneInfo, hh: str | None = None, mm: str | None = None, ampm: str | None = None) -> datetime:
    if hh is None:
        t = DEFAULT_DUE
    else:
        h = int(hh)
        if ampm:
            ampm = ampm.lower()
            if ampm == "pm" and h < 12:
                h += 12
            if ampm == "am" and h == 12:
                h = 0
        elif h < 8:  # "at 3" most likely means 3 pm in a work context
            h += 12
        t = time(h, int(mm or 0))
    return datetime.combine(d, t, tzinfo=tz)


def _end_of_week(today: date) -> date:
    # Friday of this week; on a weekend, Sunday.
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    return friday if today.weekday() <= 4 else today + timedelta(days=6 - today.weekday())


def _resolve_date(expr: str, today: date) -> date | None:
    e = expr.lower().strip()
    if e in ("today", "tonight", "eod", "end of day"):
        return today
    if e == "yesterday":
        return today - timedelta(days=1)
    if e == "tomorrow":
        return today + timedelta(days=1)
    if e in ("end of week", "end of the week", "this week", "eow"):
        return _end_of_week(today)
    if e == "next week":
        return today + timedelta(days=(4 - today.weekday()) % 7 + 7) if today.weekday() <= 4 else today + timedelta(days=(4 - today.weekday()) % 7)
    if e in ("end of month", "end of the month", "eom", "this month"):
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return first_next - timedelta(days=1)
    m = re.fullmatch(r"in (\d+|a|one|two|three) (day|week)s?", e)
    if m:
        n = 1 if m.group(1) in ("a", "one") else NUMBER_WORDS.get(m.group(1)) or int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2) == "week" else 1))
    m = re.fullmatch(rf"(?:(next|this)\s+)?{WD}", e)
    if m:
        wd = _weekday_index(m.group(2))
        ahead = (wd - today.weekday()) % 7
        if m.group(1) == "next":
            # "next Friday" = Friday of next week
            ahead = ahead + 7 if ahead <= (6 - today.weekday()) else ahead
            if ahead == 0:
                ahead = 7
        return today + timedelta(days=ahead)
    m = re.fullmatch(rf"{MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", e)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        d = date(y, _month_index(m.group(1)), int(m.group(2)))
        if not m.group(3) and d < today:
            d = d.replace(year=y + 1)
        return d
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", e)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        d = date(y, int(m.group(1)), int(m.group(2)))
        if not m.group(3) and d < today:
            d = d.replace(year=y + 1)
        return d
    return None


DATE_EXPR = (
    rf"(today|tonight|tomorrow|yesterday|eod|end of (?:the )?(?:day|week|month)|eow|eom|this week|next week|this month"
    rf"|in (?:\d+|a|one|two|three) (?:day|week)s?"
    rf"|(?:(?:next|this)\s+)?{WD}"
    rf"|{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    rf"|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)"
)


def parse(text: str, now: datetime) -> QuickAddResult:
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo("UTC")
    today = now.date()
    s = " " + text.strip() + " "
    r = QuickAddResult(title="")

    def take(pattern: str, flags=re.I):
        nonlocal s
        m = re.search(pattern, s, flags)
        if m:
            s = s[: m.start()] + " " + s[m.end():]
        return m

    # Tags and confidentiality
    for tag in re.findall(r"#([\w-]+)", s):
        r.tags.append(tag.lower())
    s = re.sub(r"#[\w-]+", " ", s)
    if take(r"\b(confidential|privileged)\b"):
        r.confidential = True
        r.understood.append("confidential")

    # Priority
    m = take(r"\b(urgent|asap)\b(?:\s+priority)?")
    if m:
        r.priority = "urgent"
    else:
        m = take(r"\b(?:priority[:\s]+(urgent|high|medium|low)|(high|medium|low|med)[\s-]+(?:priority|pri))\b")
        if m:
            p = (m.group(1) or m.group(2)).lower()
            r.priority = "medium" if p == "med" else p
        elif take(r"\bimportant\b"):
            r.priority = "high"
    if r.priority:
        r.understood.append(f"priority {r.priority}")

    # Sessions per period: "three times this week", "twice a week"
    sessions = None
    m = take(r"\b(once|twice|one|two|three|four|five|six|seven|\d+)\s*(?:times|x)?\s+(this week|a week|per week|each week|every week|next week)\b")
    if m:
        n = NUMBER_WORDS.get(m.group(1).lower()) or int(m.group(1))
        sessions = (n, m.group(2).lower())

    # Duration
    dur = None
    m = take(r"\b(?:for\s+)?(\d+(?:\.\d+)?|one|two|three|four|five|six)\s*(?:hours?|hrs?|h)\b(?:\s*(?:and\s*)?(\d+)\s*(?:minutes?|mins?|m)\b)?")
    if m:
        n = NUMBER_WORDS.get(m.group(1).lower()) or float(m.group(1))
        dur = round(n * 60) + int(m.group(2) or 0)
    else:
        m = take(r"\b(?:for\s+)?(\d+)\s*(?:minutes?|mins?|m)\b")
        if m:
            dur = int(m.group(1))
        else:
            m = take(r"\b(?:for\s+)?(half an hour|an hour|one hour|a couple of hours)\b")
            if m:
                dur = {"half an hour": 30, "an hour": 60, "one hour": 60, "a couple of hours": 120}[m.group(1).lower()]

    # Recurrence (before deadlines, so "every Friday" is not read as "by Friday")
    rule = None
    rec_text = None
    rec_extra: dict = {}
    # Completion-based: "every 2 weeks after it's done", "3 days after completion"
    m = re.search(r"\b(?:every\s+)?(\d+|one|two|three|four|six|a)\s+(day|week|month|year)s?\s+after\s+(?:it'?s\s+|it\s+is\s+|each\s+one\s+is\s+)?(?:done|completed?|completion|finished)\b", s, re.I)
    if m:
        n = 1 if m.group(1).lower() == "a" else (NUMBER_WORDS.get(m.group(1).lower()) or int(m.group(1)))
        rec_extra = {"kind": "after_completion", "every": n, "unit": m.group(2).lower() + "s"}
        rec_text = m.group(0).strip()
        s = s[: m.start()] + " " + s[m.end():]
    patterns = [] if rec_extra else [
        (r"\b(?:on\s+)?the\s+last\s+(?:business\s+day|weekday|working\s+day)\s+of\s+(?:every|each|the)\s+month\b", lambda m: "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"),
        (r"\b(?:on\s+)?the\s+first\s+(?:business\s+day|weekday|working\s+day)\s+of\s+(?:every|each|the)\s+month\b", lambda m: "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1"),
        (rf"\b(?:on\s+)?the\s+(first|second|third|fourth|last)\s+{WD}\s+of\s+(?:every|each|the)\s+month\b", lambda m: "FREQ=MONTHLY;BYDAY=" + {"first": "1", "second": "2", "third": "3", "fourth": "4", "last": "-1"}[m.group(1).lower()] + RRULE_DAY[_weekday_index(m.group(2))]),
        (r"\bevery\s+(?:weekday|business\s+day|work\s*day)s?\b|\bweekdays\b", lambda m: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        (rf"\bevery\s+other\s+{WD}\b", lambda m: f"FREQ=WEEKLY;INTERVAL=2;BYDAY={RRULE_DAY[_weekday_index(m.group(1))]}"),
        (rf"\b(?:every|each)\s+{WD}(?:\s+and\s+{WD})?\b", lambda m: "FREQ=WEEKLY;BYDAY=" + ",".join(RRULE_DAY[_weekday_index(g)] for g in m.groups() if g)),
        (r"\bevery\s+(\d+|two|three|four|six)\s+(day|week|month|year)s\b", lambda m: f"FREQ={ {'day':'DAILY','week':'WEEKLY','month':'MONTHLY','year':'YEARLY'}[m.group(2).lower()] };INTERVAL={NUMBER_WORDS.get(m.group(1).lower()) or int(m.group(1))}"),
        (r"\b(?:every\s+other\s+week|biweekly|fortnightly)\b", lambda m: "FREQ=WEEKLY;INTERVAL=2"),
        (r"\b(?:every\s+day|daily)\b", lambda m: "FREQ=DAILY"),
        (r"\b(?:every\s+week|each\s+week|per\s+week|weekly)\b", lambda m: "FREQ=WEEKLY"),
        (r"\b(?:every\s+month|monthly)\b", lambda m: "FREQ=MONTHLY"),
        (r"\b(?:every\s+quarter|quarterly)\b", lambda m: "FREQ=MONTHLY;INTERVAL=3"),
        (r"\b(?:every\s+year|annually|yearly)\b", lambda m: "FREQ=YEARLY"),
    ]
    for pat, build in patterns:
        m = re.search(pat, s, re.I)
        if m:
            rule = build(m)
            rec_text = m.group(0).strip()
            s = s[: m.start()] + " " + s[m.end():]
            if re.search(r"business|working", rec_text, re.I):
                # Weekends are already excluded by the rule; this also steps around holidays.
                rec_extra = {"adjust": "previous" if re.search(r"\blast\b", rec_text, re.I) else "next"}
            break
    if sessions and sessions[1] in ("a week", "per week", "each week", "every week") and not rule:
        rule = "FREQ=WEEKLY"
        rec_text = f"{sessions[0]} times a week"

    # Deadline
    m = take(rf"\b(by|due|before|until|deadline)\s+(?:on\s+)?{DATE_EXPR}{TIME}(?=\W)")
    if m:
        d = _resolve_date(m.group(2), today)
        if d:
            groups = m.groups()
            r.deadline = _at(d, tz, groups[-3], groups[-2], groups[-1])
            r.deadline_type = "hard"
    if r.deadline is None:
        m = take(rf"\b(?:on\s+)?{DATE_EXPR}{TIME}(?=\W)")
        if m:
            d = _resolve_date(m.group(1), today)
            if d:
                groups = m.groups()
                r.deadline = _at(d, tz, groups[-3], groups[-2], groups[-1])
                r.deadline_type = "soft"

    if sessions:
        n, period = sessions
        if dur:
            r.min_session_min = r.max_session_min = dur
            dur = dur * n
        r.can_split = n > 1
        if period == "this week" and r.deadline is None:
            r.deadline = _at(_end_of_week(today), tz)
            r.deadline_type = "soft"
        elif period == "next week" and r.deadline is None:
            r.deadline = _at(_resolve_date("next week", today), tz)  # type: ignore[arg-type]
            r.deadline_type = "soft"
        r.understood.append(f"{n} sessions {period}")

    if dur:
        r.duration_min = dur
        r.understood.append(f"{dur} min")
        if r.can_split is None and dur > 120:
            # Long work can be split into sessions of at least 30 minutes by default.
            r.can_split = True
            r.min_session_min = r.min_session_min or 30

    if rec_extra.get("kind") == "after_completion":
        r.recurrence = {**rec_extra, "text": rec_text}
        r.understood.append(f"repeats: {rec_text}")
    if rule:
        first = None
        try:
            start = datetime.combine(today, DEFAULT_DUE, tzinfo=tz)
            first = rrulestr(f"RRULE:{rule}", dtstart=start).after(start - timedelta(seconds=1), inc=True)
        except Exception:
            pass
        r.recurrence = {"rrule": rule, "text": rec_text, "kind": "fixed", **rec_extra}
        if first and r.deadline is None:
            r.deadline = first
            r.deadline_type = "hard"
        r.understood.append(f"repeats: {rec_text}")

    if r.deadline:
        r.understood.append(f"{'due' if r.deadline_type == 'hard' else 'target'} {r.deadline.strftime('%a %b %d %H:%M')}")

    # Title cleanup
    title = re.sub(r"\s+", " ", s)
    title = re.sub(r"\s+([,.;])", r"\1", title)
    title = re.sub(r"([,.;:])\1+", r"\1", title)
    for _ in range(3):
        title = re.sub(r"(?i)[\s,.;:-]+(for|by|on|at|and|with|,|due)?[\s,.;:-]*$", "", title.strip())
        title = re.sub(r"(?i)^[\s,.;:-]+", "", title)
    title = re.sub(r"\s*,\s*,\s*", ", ", title).strip(" ,.;")
    r.title = (title[:1].upper() + title[1:]) if title else text.strip()

    low = " " + r.title.lower() + " "
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w.strip())}s?\b", low) for w in words):
            r.category_name = cat
            break
    return r
