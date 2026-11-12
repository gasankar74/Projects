"""The scheduling engine.

`plan(snapshot)` is a pure function: no database, network or clock access. Given the same snapshot it always
returns the same plan, which makes it testable, explainable and usable for what-if previews.

Stages
  1. Free-time map: working hours (per weekday, in the user's time zone, DST-correct) minus busy calendar
     events, locked blocks, pinned tasks and blocks frozen because they are about to start.
  2. Score: every open task gets a 0-100 score built from named parts (importance, urgency from slack,
     consequence, blocking, project, momentum, scarcity), kept so the UI can explain it.
  3. Placement: tasks are placed in dependency order, highest score first, at the earliest slot that satisfies
     all of their rules. A task that already had a valid slot keeps it (stability). A hard deadline is never
     exceeded: if a task can't fit, lower-scored flexible work is released and re-placed, and if it still can't
     fit the task is reported as `cannot_meet_deadline` rather than scheduled late.
  4. Risk and overload: each task gets a forecast finish and slack; when work exceeds capacity the engine
     simulates deferring the lowest-scored tasks and recommends the smallest set that makes the rest fit.

AI never calls into this module to enforce anything; it can only propose changes that are then re-planned here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

UTC = timezone.utc
PRIORITY_WEIGHT = {"urgent": 1.0, "high": 0.7, "medium": 0.4, "low": 0.1}
TIME_OF_DAY = {"morning": (time(0), time(12)), "afternoon": (time(12), time(17)), "evening": (time(17), time(23, 59))}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_WORK_HOURS = {d: [["09:00", "17:00"]] for d in DAY_NAMES[:5]} | {"sat": [], "sun": []}
DEFAULT_WEIGHTS = {"P": 0.30, "U": 0.30, "C": 0.10, "B": 0.10, "J": 0.05, "R": 0.05, "A": 0.10}

Span = tuple[datetime, datetime]


# ---------------------------------------------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class Settings:
    timezone: str = "America/New_York"
    work_hours: dict = field(default_factory=lambda: DEFAULT_WORK_HOURS)  # {"mon": [["09:00","17:00"]], ...}
    horizon_days: int = 56
    grid_min: int = 15
    transition_min: int = 0  # gap kept between consecutive task blocks
    daily_cap_min: int | None = None  # most task minutes per day
    default_min_session: int = 30
    freeze_min: int = 30  # blocks starting this soon keep their time
    stability_gain_min: int = 120  # a still-valid block only moves if it can start this much earlier
    risk_slack_min: int = 240  # finish closer than this (in working minutes) to a deadline = at risk
    weights: dict = field(default_factory=lambda: DEFAULT_WEIGHTS)


@dataclass(frozen=True)
class TaskInput:
    id: str
    title: str
    remaining_min: int
    priority: str = "medium"
    deadline: datetime | None = None
    deadline_type: str = "hard"
    earliest_start: datetime | None = None
    preferred_date: date | None = None
    can_split: bool = False
    min_session_min: int | None = None
    max_session_min: int | None = None
    allowed_days: tuple[int, ...] | None = None  # 0 = Monday
    windows: tuple[dict, ...] | None = None  # ({"days":[0..6], "start":"09:00", "end":"12:00"}, ...)
    preferred_time_of_day: str | None = None
    pinned_start: datetime | None = None
    depends_on: tuple[str, ...] = ()
    category_id: str | None = None
    project_priority: str | None = None
    project_deadline: datetime | None = None
    completion_pct: int = 0
    energy: str | None = None
    buffer_before_min: int = 0
    buffer_after_min: int = 0


@dataclass(frozen=True)
class BlockInput:
    """A block from the previous plan."""

    task_id: str
    start: datetime
    end: datetime
    locked: bool = False


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    settings: Settings
    tasks: tuple[TaskInput, ...]
    busy: tuple[Span, ...] = ()
    previous: tuple[BlockInput, ...] = ()
    done_task_ids: frozenset[str] = frozenset()  # prerequisites that are already finished
    fresh: bool = False  # ignore previous positions (manual "replan from scratch")

    def input_hash(self) -> str:
        def enc(o):
            if isinstance(o, (datetime, date, time)):
                return o.isoformat()
            if isinstance(o, frozenset):
                return sorted(o)
            raise TypeError(type(o))

        # `now` is rounded to the grid so that two replans a few seconds apart hash the same, and unlocked blocks
        # of the previous plan are left out (they are the output of the last run, not new input).
        d = asdict(replace(self, now=_floor(self.now, self.settings.grid_min), previous=tuple(b for b in self.previous if b.locked)))
        return hashlib.sha256(json.dumps(d, default=enc, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# Outputs


@dataclass
class Block:
    task_id: str
    start: datetime
    end: datetime
    session_index: int = 1
    session_count: int = 1
    locked: bool = False
    reason: dict = field(default_factory=dict)

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Warning_:
    kind: str  # cannot_meet_deadline | will_miss_soft_deadline | at_risk | unscheduled | blocked_by_prerequisite | locked_conflict | no_slot_long_enough
    task_id: str | None
    message: str
    data: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    score: float
    parts: dict
    scheduled_min: int = 0
    unscheduled_min: int = 0
    start: datetime | None = None
    finish: datetime | None = None
    slack_min: int | None = None
    at_risk: bool = False
    status: str = "scheduled"  # scheduled | partial | unscheduled | late


@dataclass
class Plan:
    blocks: list[Block]
    warnings: list[Warning_]
    tasks: dict[str, TaskResult]
    days: list[dict]
    overload: dict | None
    input_hash: str

    def to_json(self) -> dict:
        return {
            "blocks": [_block_json(b) for b in self.blocks],
            "warnings": [asdict(w) for w in self.warnings],
            "tasks": {k: _jsonify(asdict(v)) for k, v in self.tasks.items()},
            "days": self.days,
            "overload": self.overload,
            "input_hash": self.input_hash,
        }


def _block_json(b: Block) -> dict:
    return _jsonify(asdict(b))


def _jsonify(o):
    if isinstance(o, dict):
        return {k: _jsonify(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonify(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    return o


# ---------------------------------------------------------------------------------------------------------------
# Interval helpers (all spans are UTC, half-open [start, end))


def _floor(dt: datetime, grid: int) -> datetime:
    dt = dt.astimezone(UTC).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % grid)


def _ceil(dt: datetime, grid: int) -> datetime:
    dt = dt.astimezone(UTC)
    f = _floor(dt, grid)
    return f if f == dt else f + timedelta(minutes=grid)


def _minutes(spans: Iterable[Span]) -> int:
    return int(sum((e - s).total_seconds() for s, e in spans) // 60)


def _subtract(spans: list[Span], cut: Span) -> list[Span]:
    cs, ce = cut
    out: list[Span] = []
    for s, e in spans:
        if ce <= s or cs >= e:
            out.append((s, e))
            continue
        if s < cs:
            out.append((s, cs))
        if ce < e:
            out.append((ce, e))
    return out


def _intersect(a: list[Span], b: list[Span]) -> list[Span]:
    out: list[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _clip(spans: list[Span], start: datetime | None, end: datetime | None) -> list[Span]:
    out = []
    for s, e in spans:
        if start and s < start:
            s = start
        if end and e > end:
            e = end
        if s < e:
            out.append((s, e))
    return out


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _parse_hm(v: str) -> time:
    h, m = v.split(":")
    h, m = int(h), int(m)
    return time(23, 59) if h >= 24 else time(h, m)


def _local_spans(
    tz: ZoneInfo, first: date, last: date, per_day: dict[int, list[tuple[time, time]]]
) -> list[Span]:
    """Expand weekday rules into UTC spans, one local day at a time (DST-safe)."""
    out: list[Span] = []
    d = first
    while d <= last:
        for t0, t1 in per_day.get(d.weekday(), []):
            s = datetime.combine(d, t0, tz).astimezone(UTC)
            e = datetime.combine(d, t1, tz).astimezone(UTC)
            if s < e:
                out.append((s, e))
        d += timedelta(days=1)
    out.sort()
    return out


# ---------------------------------------------------------------------------------------------------------------
# The planner


class _Planner:
    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.st = snap.settings
        self.tz = ZoneInfo(self.st.timezone)
        self.grid = self.st.grid_min
        self.now = snap.now.astimezone(UTC)
        self.start = _ceil(self.now, self.grid)
        self.end = self.start + timedelta(days=self.st.horizon_days)
        self.first_day = self.start.astimezone(self.tz).date()
        self.last_day = self.end.astimezone(self.tz).date()
        self.tasks = {t.id: t for t in snap.tasks}
        self.warnings: list[Warning_] = []
        self.blocks: list[Block] = []
        self.task_blocks: dict[str, list[Block]] = {}
        self.day_used: dict[date, int] = {}

        wh = {
            i: [(_parse_hm(a), _parse_hm(b)) for a, b in self.st.work_hours.get(name, [])]
            for i, name in enumerate(DAY_NAMES)
        }
        self.work = _clip(_local_spans(self.tz, self.first_day, self.last_day, wh), self.start, self.end)
        self.busy = sorted((max(s, self.start), min(e, self.end)) for s, e in snap.busy if e > self.start and s < self.end)
        free = list(self.work)
        for b in self.busy:
            free = _subtract(free, b)
        self.free_after_events = free  # capacity before any task is placed
        self.free = list(free)

    # -- helpers --------------------------------------------------------------------------------------------

    def fmt(self, dt: datetime) -> str:
        loc = dt.astimezone(self.tz)
        return loc.strftime("%a %b %-d, %-I:%M %p").replace(":00 ", " ")

    def local_day(self, dt: datetime) -> date:
        return dt.astimezone(self.tz).date()

    def task_windows(self, t: TaskInput) -> list[Span] | None:
        """The task's own allowed time, or None when it may use any working time."""
        if not t.windows and t.allowed_days is None:
            return None
        if t.windows:
            per_day: dict[int, list[tuple[time, time]]] = {}
            for w in t.windows:
                days = w.get("days") or list(range(7))
                for d in days:
                    if t.allowed_days is not None and d not in t.allowed_days:
                        continue
                    per_day.setdefault(d, []).append((_parse_hm(w["start"]), _parse_hm(w["end"])))
        else:
            per_day = {d: [(time(0), time(23, 59))] for d in t.allowed_days or []}
        return _local_spans(self.tz, self.first_day, self.last_day, per_day)

    def reserve(self, block: Block, buffer_before: int = 0, buffer_after: int = 0) -> None:
        cut = (
            block.start - timedelta(minutes=buffer_before),
            block.end + timedelta(minutes=buffer_after + self.st.transition_min),
        )
        self.free = _subtract(self.free, cut)
        self.blocks.append(block)
        self.task_blocks.setdefault(block.task_id, []).append(block)
        d = self.local_day(block.start)
        self.day_used[d] = self.day_used.get(d, 0) + block.minutes

    def release(self, task_id: str) -> list[Block]:
        gone = self.task_blocks.pop(task_id, [])
        if not gone:
            return []
        self.blocks = [b for b in self.blocks if b.task_id != task_id]
        for b in gone:
            d = self.local_day(b.start)
            self.day_used[d] -= b.minutes
        self._rebuild_free()
        return gone

    def _rebuild_free(self) -> None:
        free = list(self.free_after_events)
        for b in self.blocks:
            t = self.tasks.get(b.task_id)
            bb = t.buffer_before_min if t else 0
            ba = t.buffer_after_min if t else 0
            free = _subtract(free, (b.start - timedelta(minutes=bb), b.end + timedelta(minutes=ba + self.st.transition_min)))
        self.free = free

    # -- stage 2: score -------------------------------------------------------------------------------------

    def score_all(self) -> dict[str, tuple[float, dict]]:
        open_ids = set(self.tasks)
        dependents: dict[str, int] = {}
        for t in self.tasks.values():
            for p in t.depends_on:
                if p in open_ids:
                    dependents[p] = dependents.get(p, 0) + 1
        n = max(1, len(self.tasks) - 1)
        total_work = max(1, _minutes(self.work))
        out = {}
        for t in self.tasks.values():
            parts = {"P": PRIORITY_WEIGHT.get(t.priority, 0.4)}
            rem = max(t.remaining_min, 1)
            if t.deadline:
                avail = self.allowed_free_before(t, t.deadline, use_events_only=True)
                slack = avail - rem
                if slack <= 0:
                    u = 1.0
                elif slack < 2 * rem:
                    u = 0.7 + 0.3 * (1 - slack / (2 * rem))
                else:
                    u = min(0.7, 0.7 * (2 * rem) / slack)
                parts["U"] = round(u, 3)
                parts["C"] = 1.0 if t.deadline_type == "hard" else 0.4
            else:
                parts["U"] = 0.02
                parts["C"] = 0.0
            parts["B"] = round(min(1.0, dependents.get(t.id, 0) / n * 3), 3)
            j = PRIORITY_WEIGHT.get(t.project_priority, 0.0) if t.project_priority else 0.0
            if t.project_deadline and t.project_deadline - self.now < timedelta(days=14):
                j = min(1.0, j + 0.3)
            parts["J"] = round(j, 3)
            parts["R"] = round(min(1.0, t.completion_pct / 100), 3)
            win = self.task_windows(t)
            parts["A"] = round(1 - min(1.0, _minutes(_intersect(win, self.work)) / total_work), 3) if win is not None else 0.0
            w = self.st.weights
            score = 100 * sum(w.get(k, 0) * v for k, v in parts.items()) / max(1e-9, sum(w.values()))
            out[t.id] = (round(score, 1), parts)
        return out

    def allowed_free_before(self, t: TaskInput, until: datetime | None, use_events_only: bool = False) -> int:
        spans = self.candidate_spans(t, until, base=self.free_after_events if use_events_only else self.free)
        return _minutes(spans)

    # -- stage 3: placement ---------------------------------------------------------------------------------

    def candidate_spans(self, t: TaskInput, until: datetime | None, base: list[Span] | None = None, after: datetime | None = None) -> list[Span]:
        spans = list(self.free if base is None else base)
        win = self.task_windows(t)
        if win is not None:
            spans = _intersect(spans, win)
        lo = self.start
        if t.earliest_start and t.earliest_start > lo:
            lo = t.earliest_start
        if after and after > lo:
            lo = after
        return _clip(spans, lo, until)

    def prereq_end(self, t: TaskInput) -> tuple[datetime | None, str | None, bool]:
        """Latest end of any prerequisite's blocks; flag False if a prerequisite is open but unscheduled."""
        latest, title = None, None
        for p in t.depends_on:
            if p in self.snap.done_task_ids or p not in self.tasks:
                continue
            blocks = self.task_blocks.get(p)
            pt = self.tasks[p]
            if not blocks or self.results[p].unscheduled_min > 0:
                return None, pt.title, False
            e = max(b.end for b in blocks)
            if latest is None or e > latest:
                latest, title = e, pt.title
        return latest, title, True

    def session_bounds(self, t: TaskInput, need: int) -> tuple[int, int, bool]:
        if not t.can_split:
            return need, need, False
        lo = t.min_session_min or min(need, self.st.default_min_session)
        hi = t.max_session_min or need
        lo = max(self.grid, min(lo, need))
        hi = max(lo, hi)
        one_per_day = bool(t.min_session_min and t.max_session_min and t.min_session_min == t.max_session_min)
        return lo, hi, one_per_day

    def try_place(self, t: TaskInput, need: int, until: datetime | None, after: datetime | None) -> list[Block]:
        """Find slots for `need` minutes. Returns blocks (not yet reserved); fewer minutes than asked if not all fit.
        A task that can't be split gets all of its time or nothing."""
        need = int(math.ceil(need / self.grid) * self.grid)
        lo, hi, one_per_day = self.session_bounds(t, need)
        spans = self.candidate_spans(t, until, after=after)
        pref = TIME_OF_DAY.get(t.preferred_time_of_day or "")
        order = spans
        if pref:
            # Preferred part of the day first, the rest after, both in time order (still before any deadline).
            in_pref, rest = [], []
            for s, e in spans:
                ls = s.astimezone(self.tz)
                p0 = datetime.combine(ls.date(), pref[0], self.tz).astimezone(UTC)
                p1 = datetime.combine(ls.date(), pref[1], self.tz).astimezone(UTC)
                a, b = max(s, p0), min(e, p1)
                if a < b:
                    in_pref.append((a, b))
                    rest.extend(_subtract([(s, e)], (a, b)))
                else:
                    rest.append((s, e))
            order = in_pref + sorted(rest)
        if t.preferred_date:
            # Try the preferred date onwards first, then earlier days.
            pd = datetime.combine(t.preferred_date, time(0), self.tz).astimezone(UTC)
            later = [(max(s, pd), e) for s, e in order if e > pd]
            earlier = [(s, min(e, pd)) for s, e in order if s < pd]
            order = later + earlier

        bb, ba = t.buffer_before_min, t.buffer_after_min
        picked: list[Span] = []
        used_days: set[date] = set()
        day_extra: dict[date, int] = {}
        left = need
        taken: list[Span] = []
        for s, e in order:
            if left <= 0:
                break
            # Remove anything already taken in this search (preferred/rest lists can overlap after clipping).
            pieces = [(s, e)]
            for tk in taken:
                pieces = [p for q in pieces for p in _subtract([q], tk)]
            for ps, pe in pieces:
                cur = _ceil(ps + timedelta(minutes=bb), self.grid)
                while left > 0:
                    avail = int((pe - cur).total_seconds() // 60) - ba
                    avail -= avail % self.grid
                    if avail <= 0:
                        break
                    day = self.local_day(cur)
                    if one_per_day and day in used_days:
                        break
                    cap = self.st.daily_cap_min
                    if cap is not None:
                        room = cap - self.day_used.get(day, 0) - day_extra.get(day, 0)
                        avail = min(avail, room - room % self.grid)
                    if not t.can_split:
                        if avail < left:
                            break
                        take = left
                    else:
                        if avail < min(lo, left):
                            break
                        take = min(avail, hi, left)
                        rest = left - take
                        if 0 < rest < lo:  # never leave a remainder shorter than the minimum session
                            take = left - lo if left - lo >= lo else take
                            if take < min(lo, left):
                                break
                    span = (cur, cur + timedelta(minutes=take))
                    picked.append(span)
                    taken.append((span[0] - timedelta(minutes=bb), span[1] + timedelta(minutes=ba)))
                    used_days.add(day)
                    day_extra[day] = day_extra.get(day, 0) + take
                    left -= take
                    cur = _ceil(span[1] + timedelta(minutes=ba + bb + self.st.transition_min), self.grid)
                    if one_per_day:
                        break
            if left <= 0:
                break
        return [Block(t.id, s, e) for s, e in sorted(picked)]

    # -- main -----------------------------------------------------------------------------------------------

    def run(self) -> Plan:
        st = self.st
        scores = self.score_all()
        self.results = {tid: TaskResult(score=s, parts=p) for tid, (s, p) in scores.items()}
        prev_by_task: dict[str, list[BlockInput]] = {}
        for b in self.snap.previous:
            prev_by_task.setdefault(b.task_id, []).append(b)

        # 1. Fixed things first: locked blocks, pinned tasks, blocks about to start.
        fixed_min: dict[str, int] = {}
        freeze_until = self.now + timedelta(minutes=st.freeze_min)
        for b in sorted(self.snap.previous, key=lambda b: b.start):
            if b.task_id not in self.tasks or b.end <= self.now:
                continue
            frozen = b.start <= freeze_until and not self.snap.fresh and not any(_overlaps((b.start, b.end), x) for x in self.busy)
            if not (b.locked or frozen):
                continue
            start = max(b.start, self.start) if b.start < self.now else b.start
            end = b.end
            if not b.locked:
                # A frozen block never holds more time than the task still needs (e.g. after progress was logged).
                room = self.tasks[b.task_id].remaining_min - fixed_min.get(b.task_id, 0)
                if room <= 0:
                    continue
                end = min(end, start + timedelta(minutes=room))
                if end <= start:
                    continue
            blk = Block(b.task_id, start, end, locked=b.locked)
            blk.reason = {"why": ["Locked by you" if b.locked else "Already started or starting within %d min, so it stays put" % st.freeze_min], "bound_by": "locked" if b.locked else "frozen"}
            if any(_overlaps((blk.start, blk.end), x) for x in self.busy) and b.locked:
                self.warnings.append(Warning_("locked_conflict", b.task_id, f"\"{self.tasks[b.task_id].title}\" is locked at {self.fmt(blk.start)} but now overlaps a calendar event. It was kept where you locked it.", {"start": blk.start.isoformat()}))
            self.reserve(blk)
            fixed_min[b.task_id] = fixed_min.get(b.task_id, 0) + blk.minutes
        for t in self.tasks.values():
            if t.pinned_start and t.id not in fixed_min and t.pinned_start + timedelta(minutes=t.remaining_min) > self.now:
                blk = Block(t.id, t.pinned_start, t.pinned_start + timedelta(minutes=t.remaining_min), locked=True)
                blk.reason = {"why": [f"Pinned by you to {self.fmt(t.pinned_start)}"], "bound_by": "pinned"}
                self.reserve(blk)
                fixed_min[t.id] = blk.minutes

        # 2. Order: dependencies first, then score (highest first), stable on deadline and title.
        order = self._topo_order(scores)

        for tid in order:
            self._place_task(self.tasks[tid], fixed_min.get(tid, 0), prev_by_task.get(tid, []), scores)

        self._finish_results(prev_by_task)
        days = self._day_summary()
        overload = self._overload() if not getattr(self, "_simulating", False) else None
        self.blocks.sort(key=lambda b: (b.start, b.task_id))
        return Plan(self.blocks, self.warnings, self.results, days, overload, self.snap.input_hash())

    def _topo_order(self, scores) -> list[str]:
        pending = {t.id: {p for p in t.depends_on if p in self.tasks} for t in self.tasks.values()}
        far = datetime.max.replace(tzinfo=UTC)
        # A prerequisite is as pressing as the most pressing task waiting on it (and inherits its deadline).
        eff = {tid: (s, self.tasks[tid].deadline or far) for tid, (s, _) in scores.items()}
        for _ in range(len(self.tasks)):
            changed = False
            for t in self.tasks.values():
                for p in pending[t.id]:
                    cand = (max(eff[p][0], eff[t.id][0]), min(eff[p][1], eff[t.id][1]))
                    if cand != eff[p]:
                        eff[p], changed = cand, True
            if not changed:
                break
        self.eff = eff
        key = lambda tid: (-eff[tid][0], eff[tid][1], self.tasks[tid].title, tid)  # noqa: E731
        order: list[str] = []
        ready = sorted([t for t, deps in pending.items() if not deps], key=key)
        done: set[str] = set()
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            done.add(tid)
            for t, deps in pending.items():
                if t not in done and t not in ready and tid in deps and deps <= done:
                    ready.append(t)
            ready.sort(key=key)
        # Anything left is in a cycle (the API prevents this); place it anyway, by score.
        order += sorted([t for t in pending if t not in done], key=key)
        return order

    def _place_task(self, t: TaskInput, fixed: int, prev: list[BlockInput], scores) -> None:
        res = self.results[t.id]
        need = max(0, t.remaining_min - fixed)
        if need == 0:
            return
        after, prereq_title, ok = self.prereq_end(t)
        if not ok:
            res.unscheduled_min = need
            res.status = "unscheduled"
            self.warnings.append(Warning_("blocked_by_prerequisite", t.id, f"\"{t.title}\" waits for \"{prereq_title}\", which isn't scheduled yet.", {"prerequisite": prereq_title}))
            return
        hard_until = t.deadline if (t.deadline and t.deadline_type == "hard") else None
        until = hard_until or self.end

        # Stability: keep the previous slots if they are all still free and still satisfy the rules.
        kept = None if self.snap.fresh else self._reuse_previous(t, need, prev, until, after)
        if kept:
            # ...unless a clearly earlier slot has opened up (more than the stability threshold earlier).
            cand = self.try_place(t, need, until, after)
            if cand and not self._short(cand, need) and cand[0].start <= kept[0].start - timedelta(minutes=self.st.stability_gain_min):
                kept = None
                placed = cand
                self._moved_earlier = True
            else:
                placed = kept
        else:
            placed = self.try_place(t, need, until, after)
        if hard_until and self._short(placed, need):
            repaired = self._repair(t, need, hard_until, after, scores)
            if repaired:
                placed = repaired
        self._commit(t, placed, prev, scores, after, prereq_title, kept is not None)
        got = sum(b.minutes for b in placed)
        if got < need:
            res.unscheduled_min = need - got
            if hard_until:
                self.warnings.append(Warning_(
                    "cannot_meet_deadline", t.id,
                    f"\"{t.title}\" can't be finished by its deadline ({self.fmt(hard_until)}). {_hm(need - got)} of {_hm(need)} has no room before then. It was not scheduled after the deadline.",
                    {"deadline": hard_until.isoformat(), "shortfall_min": need - got},
                ))
            elif not t.can_split and need > self._longest_gap(t):
                self.warnings.append(Warning_("no_slot_long_enough", t.id, f"\"{t.title}\" needs {_hm(need)} in one sitting and no free gap is that long. Allow splitting or shorten it.", {}))
            else:
                self.warnings.append(Warning_("unscheduled", t.id, f"\"{t.title}\" doesn't fit in the next {self.st.horizon_days} days ({_hm(need - got)} left over).", {}))

    def _short(self, blocks: list[Block], need: int) -> bool:
        return sum(b.minutes for b in blocks) < int(math.ceil(need / self.grid) * self.grid)

    def _longest_gap(self, t: TaskInput) -> int:
        spans = self.candidate_spans(t, self.end)
        return max((_minutes([s]) for s in spans), default=0)

    def _reuse_previous(self, t, need, prev, until, after) -> list[Block] | None:
        future = sorted([b for b in prev if not b.locked and b.start >= self.start], key=lambda b: b.start)
        if not future or sum(_minutes([(b.start, b.end)]) for b in future) != int(math.ceil(need / self.grid) * self.grid):
            return None
        allowed = self.candidate_spans(t, until, after=after)
        lo, hi, one_per_day = self.session_bounds(t, need)
        if len(future) > 1 and not t.can_split:
            return None
        for b in future:
            m = _minutes([(b.start, b.end)])
            if len(future) > 1 and not (lo <= m <= hi):
                return None
            need_span = (b.start - timedelta(minutes=t.buffer_before_min), b.end + timedelta(minutes=t.buffer_after_min))
            if not any(s <= need_span[0] and need_span[1] <= e for s, e in self.free):
                return None
            if not any(s <= b.start and b.end <= e for s, e in allowed):
                return None
        if self.st.daily_cap_min is not None:
            per: dict[date, int] = {}
            for b in future:
                d = self.local_day(b.start)
                per[d] = per.get(d, 0) + _minutes([(b.start, b.end)])
            if any(self.day_used.get(d, 0) + m > self.st.daily_cap_min for d, m in per.items()):
                return None
        return [Block(t.id, b.start, b.end) for b in future]

    def _repair(self, t, need, deadline, after, scores) -> list[Block] | None:
        """Make room for a hard deadline: release less pressing flexible work before the deadline, place `t`, then
        re-place what was released. Gives up (and restores everything) if a released task would then miss its own
        hard deadline, so a repair never trades one missed deadline for another."""
        if getattr(self, "_in_repair", False):
            return None
        far = datetime.max.replace(tzinfo=UTC)
        fixed = {b.task_id for b in self.blocks if b.locked or b.reason.get("bound_by") in ("locked", "frozen", "pinned")}
        # Never move a prerequisite of something already placed: it would end up after its dependent.
        prereqs: set[str] = set()
        stack = [d for tid in self.task_blocks for d in self.tasks[tid].depends_on] if self.task_blocks else []
        while stack:
            d = stack.pop()
            if d in prereqs or d not in self.tasks:
                continue
            prereqs.add(d)
            stack.extend(self.tasks[d].depends_on)
        cands = [
            tid for tid, bl in self.task_blocks.items()
            if tid != t.id and tid not in fixed and tid not in prereqs
            and any(b.start < deadline for b in bl)
            and not self._has_earlier_hard_deadline(tid, deadline)
        ]
        if not cands:
            return None
        cands.sort(key=lambda tid: (self.eff[tid][0], -(self.tasks[tid].deadline or far).timestamp() if self.tasks[tid].deadline else float("-inf"), tid))
        saved = (list(self.blocks), {k: list(v) for k, v in self.task_blocks.items()}, dict(self.day_used),
                 {k: (v.unscheduled_min, v.status) for k, v in self.results.items()}, list(self.warnings))

        def restore():
            self.blocks = list(saved[0])
            self.task_blocks = {k: list(v) for k, v in saved[1].items()}
            self.day_used = dict(saved[2])
            self.warnings = list(saved[4])
            for k, (u, st) in saved[3].items():
                self.results[k].unscheduled_min, self.results[k].status = u, st
            self._rebuild_free()

        released: list[str] = []
        was_short = {tid: self.results[tid].unscheduled_min > 0 for tid in cands}
        for v in cands:
            self.release(v)
            released.append(v)
            blocks = self.try_place(t, need, deadline, after)
            if not blocks or self._short(blocks, need):
                continue
            for b in blocks:
                self.reserve(b, t.buffer_before_min, t.buffer_after_min)
            self._in_repair = True
            ok = True
            try:
                for r in sorted(released, key=lambda x: (-self.eff[x][0], x)):
                    rt = self.tasks[r]
                    self.warnings = [w for w in self.warnings if w.task_id != r]
                    self.results[r].unscheduled_min, self.results[r].status = 0, "scheduled"
                    self._place_task(rt, 0, [], scores)
                    if rt.deadline and rt.deadline_type == "hard" and self.results[r].unscheduled_min > 0 and not was_short[r]:
                        ok = False  # it met its deadline before this repair, and wouldn't now
                        break
                    for blk in self.task_blocks.get(r, []):
                        blk.reason.setdefault("why", []).insert(0, f"Moved to make room for \"{t.title}\", which has an earlier hard deadline")
            finally:
                self._in_repair = False
            if not ok:
                restore()
                return None
            # Hand the blocks back so the caller commits them with their reasons.
            for b in blocks:
                self.blocks.remove(b)
                self.task_blocks[t.id].remove(b)
                self.day_used[self.local_day(b.start)] -= b.minutes
            if not self.task_blocks.get(t.id):
                self.task_blocks.pop(t.id, None)
            self._rebuild_free()
            return blocks
        restore()
        return None

    def _has_earlier_hard_deadline(self, tid: str, deadline: datetime) -> bool:
        o = self.tasks[tid]
        return bool(o.deadline and o.deadline_type == "hard" and o.deadline <= deadline)

    def _commit(self, t, blocks, prev, scores, after, prereq_title, kept) -> None:
        n = len(blocks)
        score, parts = scores[t.id]
        prev_future = sorted([b for b in prev if not b.locked and b.end > self.now], key=lambda b: b.start)
        why = self._why(t, parts, after, prereq_title, n)
        if kept:
            why.insert(0, "Kept its previous time (nothing better changed)")
        elif prev_future and blocks and (prev_future[0].start, prev_future[0].end) != (blocks[0].start, blocks[0].end):
            if getattr(self, "_moved_earlier", False):
                why.insert(0, f"Moved earlier from {self.fmt(prev_future[0].start)}: time opened up sooner")
            else:
                why.insert(0, self._why_moved(t, prev_future))
        self._moved_earlier = False
        for i, b in enumerate(blocks, 1):
            b.session_index, b.session_count = i, n
            b.reason = {"score": score, "parts": parts, "why": list(why), "bound_by": self._bound_by(t, after)}
            self.reserve(b, t.buffer_before_min, t.buffer_after_min)

    def _why(self, t: TaskInput, parts: dict, after, prereq_title, n) -> list[str]:
        out = []
        if t.deadline:
            kind = "Hard deadline" if t.deadline_type == "hard" else "Soft deadline"
            out.append(f"{kind} {self.fmt(t.deadline)}")
        out.append(f"{t.priority.capitalize()} priority")
        if parts.get("U", 0) >= 0.7:
            out.append("Little free time left before the deadline, so it goes early")
        if after and prereq_title:
            out.append(f"Starts after its prerequisite \"{prereq_title}\" finishes")
        if t.earliest_start and t.earliest_start > self.start:
            out.append(f"Not before {self.fmt(t.earliest_start)}")
        if t.preferred_time_of_day:
            out.append(f"Your preferred time: {t.preferred_time_of_day}")
        if t.windows or t.allowed_days is not None:
            out.append("Only in the hours you allowed for this task")
        if n > 1:
            out.append(f"Split into {n} sessions")
        if parts.get("B", 0) > 0:
            out.append("Other tasks are waiting on it")
        if parts.get("R", 0) > 0:
            out.append(f"Already {t.completion_pct}% done; only the remaining time is scheduled")
        return out

    def _why_moved(self, t, prev_future) -> str:
        p = prev_future[0]
        span = (p.start, p.end)
        if any(_overlaps(span, b) for b in self.busy):
            return f"Moved from {self.fmt(p.start)}: that time is now busy on your calendar"
        if any(_overlaps(span, (b.start, b.end)) for b in self.blocks if b.task_id != t.id):
            other = next(b for b in self.blocks if b.task_id != t.id and _overlaps(span, (b.start, b.end)))
            return f"Moved from {self.fmt(p.start)}: \"{self.tasks[other.task_id].title}\" now needs that time"
        return f"Moved from {self.fmt(p.start)}: its details or rules changed"

    def _bound_by(self, t, after) -> str:
        if after:
            return "prerequisite"
        if t.deadline and t.deadline_type == "hard":
            return "deadline"
        if t.windows or t.allowed_days is not None:
            return "window"
        return "earliest_free"

    def _finish_results(self, prev_by_task) -> None:
        for tid, res in self.results.items():
            t = self.tasks[tid]
            bl = self.task_blocks.get(tid, [])
            res.scheduled_min = sum(b.minutes for b in bl)
            if bl:
                res.start = min(b.start for b in bl)
                res.finish = max(b.end for b in bl)
            if res.unscheduled_min and res.scheduled_min:
                res.status = "partial"
            elif res.unscheduled_min:
                res.status = "unscheduled"
            if t.deadline:
                if res.finish and res.finish > t.deadline:
                    res.status = "late"
                    if t.deadline_type == "soft":
                        self.warnings.append(Warning_("will_miss_soft_deadline", tid, f"\"{t.title}\" is planned to finish {self.fmt(res.finish)}, after its soft deadline ({self.fmt(t.deadline)}).", {}))
                if res.finish and not res.unscheduled_min:
                    slack = _minutes(_clip(self.work, res.finish, t.deadline)) if res.finish < t.deadline else -int((res.finish - t.deadline).total_seconds() // 60)
                    res.slack_min = slack
                    if 0 <= slack < self.st.risk_slack_min and t.deadline > self.now:
                        res.at_risk = True
                        self.warnings.append(Warning_("at_risk", tid, f"\"{t.title}\" finishes {self.fmt(res.finish)}, only {_hm(slack)} of working time before its deadline. Any slip makes it late.", {"slack_min": slack}))
                elif res.unscheduled_min:
                    res.at_risk = True

    def _day_summary(self) -> list[dict]:
        out = []
        d = self.first_day
        work_by_day: dict[date, int] = {}
        free_by_day: dict[date, int] = {}
        for s, e in self.work:
            work_by_day[self.local_day(s)] = work_by_day.get(self.local_day(s), 0) + _minutes([(s, e)])
        for s, e in self.free_after_events:
            free_by_day[self.local_day(s)] = free_by_day.get(self.local_day(s), 0) + _minutes([(s, e)])
        while d <= self.last_day and len(out) < 14:
            out.append({"date": d.isoformat(), "work_min": work_by_day.get(d, 0), "free_min": free_by_day.get(d, 0), "planned_min": self.day_used.get(d, 0)})
            d += timedelta(days=1)
        return out

    def _overload(self) -> dict | None:
        failing = [w for w in self.warnings if w.kind == "cannot_meet_deadline"]
        if not failing:
            return None
        shortfall = sum(w.data.get("shortfall_min", 0) for w in failing)
        latest = max(datetime.fromisoformat(w.data["deadline"]) for w in failing)
        # Candidates: everything with work before the latest failing deadline, lowest score first.
        cands = sorted(
            {tid for tid, r in self.results.items() if r.start is None or r.start < latest},
            key=lambda tid: (self.results[tid].score, tid),
        )
        deferred: list[str] = []
        feasible = False
        for tid in cands:
            deferred.append(tid)
            if _simulate_ok(self.snap, deferred):
                feasible = True
                break
            if len(deferred) >= 12:
                break
        if feasible:  # trim to a minimal set
            for tid in list(deferred):
                trial = [x for x in deferred if x != tid]
                if trial and _simulate_ok(self.snap, trial):
                    deferred = trial
        return {
            "shortfall_min": shortfall,
            "message": f"You have about {_hm(shortfall)} more work than free time before {self.fmt(latest)}.",
            "defer": [{"task_id": tid, "title": self.tasks[tid].title, "minutes": self.tasks[tid].remaining_min, "score": self.results[tid].score} for tid in deferred] if feasible else [],
            "feasible_after_defer": feasible,
        }


def _simulate_ok(snap: Snapshot, deferred: list[str]) -> bool:
    trial = replace(snap, tasks=tuple(t for t in snap.tasks if t.id not in deferred))
    p = _Planner(trial)
    p._simulating = True
    plan = p.run()
    return not any(w.kind == "cannot_meet_deadline" for w in plan.warnings)


def _hm(m: int) -> str:
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h}h {mm}m"
    return f"{h}h" if h else f"{mm}m"


def plan(snapshot: Snapshot) -> Plan:
    return _Planner(snapshot).run()
