"""Unit tests for the pure scheduling engine (acceptance tests 1, 2, 3, 5, 6, 7, 8, 10, 12, 13 at engine level)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scheduler.engine import BlockInput, Settings, Snapshot, TaskInput, plan

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
# Monday 2026-11-16, 8:00 AM New York. Work hours default to Mon-Fri 9-17.
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=NY)


def at(day: int, h: int, m: int = 0, tz=NY) -> datetime:
    """Day 0 = Monday Nov 16."""
    d = date(2026, 11, 16) + timedelta(days=day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)


def snap(tasks, busy=(), previous=(), now=NOW, **settings):
    return Snapshot(now=now, settings=Settings(**settings), tasks=tuple(tasks), busy=tuple(busy), previous=tuple(previous))


def blocks_of(p, tid):
    return [b for b in p.blocks if b.task_id == tid]


def kinds(p):
    return {w.kind for w in p.warnings}


def no_overlaps(p, busy=()):
    spans = sorted((b.start, b.end) for b in p.blocks)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2, f"blocks overlap: {s1}-{e1} and {s2}-{e2}"
    for b in p.blocks:
        for s, e in busy:
            assert not (b.start < e and s < b.end), f"block {b.start}-{b.end} overlaps busy {s}-{e}"


# -- Test 1 ----------------------------------------------------------------------------------------------------


def test_two_hour_task_scheduled_before_deadline():
    t = TaskInput("a", "Write brief", 120, deadline=at(2, 17))
    p = plan(snap([t]))
    bl = blocks_of(p, "a")
    assert len(bl) == 1 and bl[0].start == at(0, 9) and bl[0].end == at(0, 11)
    assert p.tasks["a"].status == "scheduled" and not p.tasks["a"].at_risk
    assert any("Hard deadline" in r for r in bl[0].reason["why"])


def test_respects_work_hours_and_skips_weekend():
    # Friday 4 PM now; a 2-hour task goes to Friday 4-5 + ... no: unsplittable, so Monday 9-11.
    now = datetime(2026, 11, 20, 16, 0, tzinfo=NY)
    p = plan(snap([TaskInput("a", "Report", 120)], now=now))
    assert blocks_of(p, "a")[0].start == datetime(2026, 11, 23, 9, 0, tzinfo=NY)


# -- Test 2 ----------------------------------------------------------------------------------------------------


def test_new_meeting_moves_task_not_meeting():
    t = TaskInput("a", "Write brief", 120, deadline=at(2, 17))
    first = plan(snap([t]))
    prev = [BlockInput(b.task_id, b.start, b.end) for b in first.blocks]
    meeting = (at(0, 10), at(0, 11))
    second = plan(snap([t], busy=[meeting], previous=prev))
    bl = blocks_of(second, "a")
    assert bl and bl[0].start >= at(0, 11)  # first 2-hour gap after the meeting
    no_overlaps(second, [meeting])
    assert "busy on your calendar" in bl[0].reason["why"][0]


def test_unchanged_task_keeps_previous_slot():
    t = TaskInput("a", "Write brief", 60, deadline=at(4, 17))
    busy = [(at(0, 9), at(0, 10))]
    prev = [BlockInput("a", at(0, 11), at(0, 12))]  # 10:00 is free now, but only an hour earlier
    p = plan(snap([t], busy=busy, previous=prev))
    assert blocks_of(p, "a")[0].start == at(0, 11)
    assert p.blocks[0].reason["why"][0].startswith("Kept its previous time")


# -- Test 3 ----------------------------------------------------------------------------------------------------


def test_urgent_task_displaces_lower_priority_flexible_work():
    low = TaskInput("low", "Tidy files", 120, priority="low")
    first = plan(snap([low]))
    assert blocks_of(first, "low")[0].start == at(0, 9)
    prev = [BlockInput(b.task_id, b.start, b.end) for b in first.blocks]
    urgent = TaskInput("urg", "Court filing", 120, priority="urgent", deadline=at(0, 17))
    second = plan(snap([low, urgent], previous=prev))
    assert blocks_of(second, "urg")[0].start == at(0, 9)
    assert blocks_of(second, "low")[0].start >= at(0, 11)
    no_overlaps(second)
    assert "now needs that time" in blocks_of(second, "low")[0].reason["why"][0]


def test_high_score_task_with_far_deadline_does_not_starve_tight_deadline():
    far = TaskInput("far", "Annual plan", 420, priority="high", deadline=at(25, 17))
    tight = TaskInput("tight", "Reply to opposing counsel", 420, priority="medium", deadline=at(0, 17))
    p = plan(snap([far, tight]))
    assert sum(b.minutes for b in blocks_of(p, "tight")) == 420 or "cannot_meet_deadline" not in kinds(p)
    assert max(b.end for b in blocks_of(p, "tight")) <= at(0, 17)


# -- Test 5 ----------------------------------------------------------------------------------------------------


def test_impossible_hard_deadline_alerts_instead_of_scheduling_late():
    t = TaskInput("a", "Appeal brief", 600, deadline=at(0, 17))  # 10h of work, 8h of time
    p = plan(snap([t]))
    assert "cannot_meet_deadline" in kinds(p)
    assert all(b.end <= at(0, 17) for b in blocks_of(p, "a"))
    w = next(w for w in p.warnings if w.kind == "cannot_meet_deadline")
    assert w.task_id == "a" and "not scheduled after the deadline" in w.message


def test_impossible_unsplittable_task_is_left_unscheduled():
    t = TaskInput("a", "Deposition", 240, deadline=at(0, 12))  # only 3h before noon
    p = plan(snap([t]))
    assert not blocks_of(p, "a")
    assert p.tasks["a"].status == "unscheduled" and "cannot_meet_deadline" in kinds(p)


def test_repair_moves_lower_priority_work_to_meet_hard_deadline():
    # Low task gets Monday morning first (previous plan); a hard-deadline task that needs all of Monday arrives.
    low = TaskInput("low", "Newsletter", 120, priority="low")
    hard = TaskInput("hard", "Motion response", 480, priority="medium", deadline=at(0, 17), can_split=True)
    prev = [BlockInput("low", at(0, 9), at(0, 11))]
    p = plan(snap([low, hard], previous=prev))
    assert sum(b.minutes for b in blocks_of(p, "hard")) == 480
    assert all(b.start >= at(1, 0) for b in blocks_of(p, "low"))
    assert "cannot_meet_deadline" not in kinds(p)


# -- Test 6 ----------------------------------------------------------------------------------------------------


def test_dependent_task_waits_for_prerequisite():
    pre = TaskInput("pre", "Gather exhibits", 120, priority="low")
    dep = TaskInput("dep", "Draft motion", 60, priority="urgent", depends_on=("pre",))
    p = plan(snap([pre, dep]))
    assert blocks_of(p, "dep")[0].start >= max(b.end for b in blocks_of(p, "pre"))
    assert blocks_of(p, "dep")[0].reason["bound_by"] == "prerequisite"


def test_done_prerequisite_does_not_block():
    dep = TaskInput("dep", "Draft motion", 60, depends_on=("pre",))
    s = Snapshot(now=NOW, settings=Settings(), tasks=(dep,), done_task_ids=frozenset({"pre"}))
    assert blocks_of(plan(s), "dep")[0].start == at(0, 9)


# -- Test 7 ----------------------------------------------------------------------------------------------------


def test_partially_completed_task_schedules_only_remaining():
    t = TaskInput("a", "Research memo", 90, completion_pct=25)  # 120 total, 90 left
    prev = [BlockInput("a", at(0, 9), at(0, 11))]
    p = plan(snap([t], previous=prev))
    assert sum(b.minutes for b in blocks_of(p, "a")) == 90
    assert any("remaining time" in r for r in blocks_of(p, "a")[0].reason["why"])


# -- Test 8 ----------------------------------------------------------------------------------------------------


def test_overlapping_events_from_several_calendars_block_time():
    busy = [(at(0, 9), at(0, 10, 30)), (at(0, 10), at(0, 11)), (at(0, 11), at(0, 12))]  # overlapping + adjacent
    p = plan(snap([TaskInput("a", "Prep", 60)], busy=busy))
    assert blocks_of(p, "a")[0].start == at(0, 12)
    no_overlaps(p, busy)


# -- Test 10 ---------------------------------------------------------------------------------------------------


def test_locked_block_survives_replan_even_with_better_slot():
    t = TaskInput("a", "Client call prep", 60)
    prev = [BlockInput("a", at(3, 15), at(3, 16), locked=True)]
    p = plan(Snapshot(now=NOW, settings=Settings(), tasks=(t,), previous=tuple(prev), fresh=True))
    bl = blocks_of(p, "a")
    assert len(bl) == 1 and bl[0].start == at(3, 15) and bl[0].locked


def test_locked_block_overlapping_new_meeting_is_kept_and_flagged():
    t = TaskInput("a", "Client call prep", 60)
    prev = [BlockInput("a", at(1, 10), at(1, 11), locked=True)]
    p = plan(snap([t], busy=[(at(1, 10), at(1, 11))], previous=prev))
    assert blocks_of(p, "a")[0].start == at(1, 10)
    assert "locked_conflict" in kinds(p)


def test_other_tasks_flow_around_locked_block():
    locked = BlockInput("a", at(0, 9), at(0, 12), locked=True)
    p = plan(snap([TaskInput("a", "Locked work", 180), TaskInput("b", "Other", 60, priority="urgent")], previous=[locked]))
    assert blocks_of(p, "b")[0].start == at(0, 12)


# -- Test 12 ---------------------------------------------------------------------------------------------------


def test_overload_detected_with_deferral_recommendation():
    friday = at(4, 17)
    tasks = [
        TaskInput("t1", "Trial prep", 900, priority="urgent", deadline=friday, can_split=True),
        TaskInput("t2", "Board report", 900, priority="high", deadline=friday, can_split=True),
        TaskInput("t3", "Website copy", 600, priority="low", deadline=friday, can_split=True),
    ]  # 40h of work, 40h of time minus nothing... add meetings to overload
    busy = [(at(d, 9), at(d, 11)) for d in range(5)]  # 10h of meetings: 30h free for 40h of work
    p = plan(snap(tasks, busy=busy))
    assert "cannot_meet_deadline" in kinds(p)
    assert p.overload and p.overload["feasible_after_defer"]
    deferred = [d["task_id"] for d in p.overload["defer"]]
    assert deferred == ["t3"]  # the lowest-scored task, and only it
    assert "more work than free time" in p.overload["message"]


def test_no_overload_when_everything_fits():
    p = plan(snap([TaskInput("a", "Small", 60, deadline=at(4, 17))]))
    assert p.overload is None


# -- Test 13 ---------------------------------------------------------------------------------------------------


def test_time_zone_change_uses_new_local_work_hours():
    la = ZoneInfo("America/Los_Angeles")
    t = TaskInput("a", "Prep", 60)
    p = plan(Snapshot(now=NOW, settings=Settings(timezone="America/Los_Angeles"), tasks=(t,)))
    assert blocks_of(p, "a")[0].start == datetime(2026, 11, 16, 9, 0, tzinfo=la)  # 9 AM Pacific = noon Eastern


def test_dst_change_keeps_local_work_hours():
    # US clocks spring forward Sunday 2027-03-14. Monday's 9 AM local is 13:00 UTC (was 14:00 UTC the week before).
    now = datetime(2027, 3, 12, 18, 0, tzinfo=NY)  # Friday after hours
    p = plan(Snapshot(now=now, settings=Settings(), tasks=(TaskInput("a", "Prep", 60),)))
    assert blocks_of(p, "a")[0].start.astimezone(UTC) == datetime(2027, 3, 15, 13, 0, tzinfo=UTC)


# -- Splitting, windows, preferences ---------------------------------------------------------------------------


def test_split_respects_session_bounds():
    busy = [(at(0, 10), at(0, 11)), (at(0, 12), at(0, 13))]
    t = TaskInput("a", "Long doc", 180, can_split=True, min_session_min=45, max_session_min=90)
    p = plan(snap([t], busy=busy))
    bl = blocks_of(p, "a")
    assert sum(b.minutes for b in bl) == 180
    assert all(45 <= b.minutes <= 90 for b in bl)
    assert [b.session_index for b in bl] == list(range(1, len(bl) + 1))


def test_sessions_with_fixed_length_go_on_separate_days():
    t = TaskInput("a", "Trauma talk", 270, can_split=True, min_session_min=90, max_session_min=90)
    p = plan(snap([t]))
    days = [b.start.astimezone(NY).date() for b in blocks_of(p, "a")]
    assert len(days) == 3 and len(set(days)) == 3


def test_unsplittable_task_needs_contiguous_gap():
    busy = [(at(0, 10), at(0, 11)), (at(0, 13), at(0, 14))]
    p = plan(snap([TaskInput("a", "Workshop", 150)], busy=busy))
    assert blocks_of(p, "a")[0].start == at(0, 14)  # 14:00-16:30 is the first 2.5h gap


def test_task_window_and_allowed_days():
    t = TaskInput("a", "QuickBooks", 30, windows=({"days": [4], "start": "09:00", "end": "12:00"},))
    p = plan(snap([t]))
    assert blocks_of(p, "a")[0].start == at(4, 9)


def test_preferred_time_of_day_afternoon():
    p = plan(snap([TaskInput("a", "Calls", 60, preferred_time_of_day="afternoon")]))
    assert blocks_of(p, "a")[0].start == at(0, 12)


def test_earliest_start_and_pinned():
    a = TaskInput("a", "Later", 60, earliest_start=at(2, 13))
    b = TaskInput("b", "Pinned", 60, pinned_start=at(0, 9))
    c = TaskInput("c", "Other", 60, priority="urgent")
    p = plan(snap([a, b, c]))
    assert blocks_of(p, "a")[0].start == at(2, 13)
    assert blocks_of(p, "b")[0].start == at(0, 9) and blocks_of(p, "b")[0].locked
    assert blocks_of(p, "c")[0].start == at(0, 10)


def test_buffers_and_transition():
    busy = [(at(0, 9), at(0, 10))]
    p = plan(snap([TaskInput("a", "A", 60, buffer_before_min=15), TaskInput("b", "B", 60, priority="low")], busy=busy, transition_min=15))
    a, b = blocks_of(p, "a")[0], blocks_of(p, "b")[0]
    assert a.start == at(0, 10, 15)
    assert b.start >= a.end + timedelta(minutes=15)


def test_daily_cap():
    p = plan(snap([TaskInput("a", "Big", 360, can_split=True)], daily_cap_min=240))
    per_day = {}
    for b in blocks_of(p, "a"):
        d = b.start.astimezone(NY).date()
        per_day[d] = per_day.get(d, 0) + b.minutes
    assert max(per_day.values()) <= 240 and sum(per_day.values()) == 360


def test_at_risk_warning():
    busy = [(at(0, 9), at(0, 15))]
    p = plan(snap([TaskInput("a", "Filing", 90, deadline=at(0, 17))], busy=busy))
    assert p.tasks["a"].at_risk and "at_risk" in kinds(p)


def test_soft_deadline_may_be_late_with_warning():
    p = plan(snap([TaskInput("a", "Blog", 600, deadline=at(0, 17), deadline_type="soft", can_split=True)]))
    assert sum(b.minutes for b in blocks_of(p, "a")) == 600
    assert "will_miss_soft_deadline" in kinds(p) and p.tasks["a"].status == "late"


def test_deterministic_and_hash_stable():
    tasks = [TaskInput(str(i), f"T{i}", 30 + 15 * i, priority=["low", "medium", "high"][i % 3]) for i in range(12)]
    a, b = plan(snap(tasks)), plan(snap(tasks))
    assert [(x.task_id, x.start) for x in a.blocks] == [(x.task_id, x.start) for x in b.blocks]
    assert a.input_hash == b.input_hash
    assert plan(snap(tasks, now=NOW + timedelta(seconds=40))).input_hash == a.input_hash
    no_overlaps(a)


def test_frozen_block_about_to_start_is_kept():
    t = TaskInput("a", "Now", 60)
    now = at(0, 10, 50)
    prev = [BlockInput("a", at(0, 11), at(0, 12))]
    urgent = TaskInput("u", "Urgent", 60, priority="urgent")
    p = plan(snap([t, urgent], previous=prev, now=now))
    assert blocks_of(p, "a")[0].start == at(0, 11)
    assert blocks_of(p, "u")[0].start == at(0, 12)


def test_preferred_date():
    p = plan(snap([TaskInput("a", "Prep", 60, preferred_date=date(2026, 11, 18))]))
    assert blocks_of(p, "a")[0].start == at(2, 9)


def test_frozen_block_trimmed_to_remaining_work():
    t = TaskInput("a", "Memo", 60)  # was 120, half done
    now = at(0, 10, 50)
    p = plan(snap([t], previous=[BlockInput("a", at(0, 11), at(0, 13))], now=now))
    bl = blocks_of(p, "a")
    assert [(b.start, b.end) for b in bl] == [(at(0, 11), at(0, 12))]


def test_prerequisite_inherits_urgency_of_dependent():
    pre = TaskInput("pre", "Gather exhibits", 60, priority="low")
    dep = TaskInput("dep", "File response", 120, priority="urgent", deadline=at(0, 13), depends_on=("pre",))
    other = TaskInput("other", "Medium work", 120, priority="high")
    p = plan(snap([pre, dep, other]))
    assert blocks_of(p, "pre")[0].start == at(0, 9)
    assert blocks_of(p, "dep")[0].start == at(0, 10)
    assert "cannot_meet_deadline" not in kinds(p)


def test_kept_slot_moves_when_much_earlier_time_opens():
    t = TaskInput("a", "Report", 60)
    prev = [BlockInput("a", at(3, 9), at(3, 10))]  # Thursday; Monday morning is now free
    p = plan(snap([t], previous=prev))
    assert blocks_of(p, "a")[0].start == at(0, 9)
    assert "time opened up sooner" in blocks_of(p, "a")[0].reason["why"][0]
    # A small gain (under the threshold) doesn't move it.
    prev = [BlockInput("a", at(0, 10), at(0, 11))]
    assert blocks_of(plan(snap([t], previous=prev)), "a")[0].start == at(0, 10)


def deps_ok(p, tasks):
    for t in tasks:
        for d in t.depends_on:
            pre, dep = blocks_of(p, d), blocks_of(p, t.id)
            if pre and dep:
                assert min(b.start for b in dep) >= max(b.end for b in pre), f"{t.title} starts before its prerequisite ends"


def test_repair_never_moves_a_prerequisite_behind_its_dependent():
    # Reproduces a browser-test failure: a short urgent task needed room, and the repair moved a prerequisite
    # to after the task that depends on it.
    exhibits = TaskInput("ex", "Gather exhibits", 60, priority="medium")
    motion = TaskInput("mo", "File response to motion", 600, priority="urgent", deadline=at(1, 12), can_split=True, depends_on=("ex",))
    clerk = TaskInput("cl", "Call court clerk", 15, priority="urgent", deadline=at(0, 16))
    memo = TaskInput("me", "Settlement memo", 210, priority="high", deadline=at(5, 17), can_split=True)
    busy = [(at(0, 15, 30), at(0, 16, 30))]
    prev = [
        BlockInput("me", at(0, 9), at(0, 9, 45)),
        BlockInput("mo", at(0, 9, 45), at(0, 15, 30)),
        BlockInput("mo", at(0, 16, 30), at(0, 17)),
        BlockInput("mo", at(1, 9), at(1, 12)),
        BlockInput("ex", at(1, 12), at(1, 13)),
    ]
    tasks = [exhibits, motion, clerk, memo]
    p = plan(snap(tasks, busy=busy, previous=prev))
    no_overlaps(p, busy)
    deps_ok(p, tasks)
    assert blocks_of(p, "cl") and max(b.end for b in blocks_of(p, "cl")) <= at(0, 16)


def test_repair_does_not_trade_one_missed_deadline_for_another():
    a = TaskInput("a", "A", 420, priority="high", deadline=at(0, 17), can_split=True)  # needs 7 of 8 hours
    b = TaskInput("b", "B", 120, priority="medium", deadline=at(0, 12))  # earlier deadline, needs the morning
    p = plan(snap([a, b]))
    late = [w.task_id for w in p.warnings if w.kind == "cannot_meet_deadline"]
    assert len(late) <= 1
    for t in (a, b):
        assert all(x.end <= t.deadline for x in blocks_of(p, t.id))


def test_invariants_on_random_workloads():
    """Many random weeks: whatever happens, the plan never breaks a hard rule."""
    import random

    for seed in range(60):
        rnd = random.Random(seed)
        tasks = []
        for i in range(rnd.randint(3, 14)):
            dl = at(rnd.randint(0, 9), rnd.choice([10, 12, 15, 17])) if rnd.random() < 0.7 else None
            split = rnd.random() < 0.5
            deps = (f"t{rnd.randrange(i)}",) if i and rnd.random() < 0.25 else ()
            tasks.append(TaskInput(
                f"t{i}", f"Task {i}", rnd.choice([15, 30, 45, 60, 90, 120, 180, 240, 360]),
                priority=rnd.choice(["urgent", "high", "medium", "low"]), deadline=dl,
                deadline_type=rnd.choice(["hard", "hard", "soft"]), can_split=split,
                min_session_min=30 if split else None, depends_on=deps,
                preferred_time_of_day=rnd.choice([None, None, "morning", "afternoon"]),
            ))
        busy = []
        for _ in range(rnd.randint(0, 10)):
            d, h = rnd.randint(0, 9), rnd.randint(8, 16)
            busy.append((at(d, h), at(d, h) + timedelta(minutes=rnd.choice([30, 60, 90, 120]))))
        prev = [BlockInput(f"t{rnd.randrange(len(tasks))}", at(d, 9), at(d, 10)) for d in range(rnd.randint(0, 3))]
        p = plan(snap(tasks, busy=busy, previous=prev))
        no_overlaps(p, busy)
        deps_ok(p, tasks)
        by_id = {t.id: t for t in tasks}
        for b in p.blocks:
            t = by_id[b.task_id]
            loc_s, loc_e = b.start.astimezone(NY), b.end.astimezone(NY)
            assert loc_s.weekday() < 5 and 9 <= loc_s.hour and (loc_e.hour < 17 or (loc_e.hour == 17 and loc_e.minute == 0)), (seed, b)
            if t.deadline and t.deadline_type == "hard":
                assert b.end <= t.deadline, (seed, t.title)
            assert b.start >= NOW
        for t in tasks:
            got = sum(b.minutes for b in blocks_of(p, t.id))
            assert got <= t.remaining_min + 15, (seed, t.title)
            if not t.can_split:
                assert len(blocks_of(p, t.id)) <= 1
        assert len({(w.kind, w.task_id) for w in p.warnings}) == len(p.warnings), (seed, [(w.kind, w.task_id) for w in p.warnings])


# -- Phase 3: scheduling profiles, travel, energy, near-term optimiser -------------------------------------------

EVENINGS = {"name": "Personal", "work_hours": {d: [["18:00", "21:00"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}


def test_profile_hours_replace_work_hours():
    t = TaskInput("p", "Call the plumber", 60, profile_id="personal")
    w = TaskInput("w", "Draft motion", 60)
    p = plan(snap([t, w], profiles={"personal": EVENINGS}))
    assert blocks_of(p, "p")[0].start == at(0, 18)
    assert blocks_of(p, "w")[0].start == at(0, 9)
    assert any("Personal" in x for x in blocks_of(p, "p")[0].reason["why"])


def test_profile_daily_cap_spreads_work():
    tasks = [TaskInput(f"p{i}", f"Errand {i}", 60, profile_id="personal") for i in range(3)]
    prof = dict(EVENINGS, daily_cap_min=60)
    p = plan(snap(tasks, profiles={"personal": prof}))
    days = sorted(b.start.astimezone(NY).date() for b in p.blocks)
    assert len(set(days)) == 3


def test_unknown_profile_falls_back_to_work_hours():
    t = TaskInput("p", "Thing", 60, profile_id="gone")
    p = plan(snap([t], profiles={}))
    assert blocks_of(p, "p")[0].start == at(0, 9)


def test_travel_time_kept_around_task():
    busy = [(at(0, 9), at(0, 10))]
    t = TaskInput("v", "Site visit", 60, travel_min=30)
    p = plan(snap([t], busy=busy))
    b = blocks_of(p, "v")[0]
    assert b.start == at(0, 10, 30)
    assert any("travel" in x for x in b.reason["why"])


def test_energy_preferences_place_high_and_low_energy_work():
    hi = TaskInput("h", "Deep research", 60, energy="high")
    lo = TaskInput("l", "Expense report", 60, energy="low")
    p = plan(snap([hi, lo], optimize_hours=0))
    assert blocks_of(p, "h")[0].start == at(0, 9)
    assert blocks_of(p, "l")[0].start == at(0, 13)


def test_optimiser_improves_greedy_plan_and_keeps_rules():
    big = TaskInput("a", "Urgent filing", 180, priority="urgent")
    small = TaskInput("b", "Outline talk", 60, priority="low", energy="high")
    greedy = plan(snap([big, small], optimize_hours=0))
    assert blocks_of(greedy, "a")[0].start == at(0, 9)
    assert blocks_of(greedy, "b")[0].start == at(1, 9)  # morning gone today, so tomorrow morning
    p = plan(snap([big, small]))
    assert blocks_of(p, "b")[0].start == at(0, 9)
    assert blocks_of(p, "a")[0].start == at(0, 10)
    assert blocks_of(p, "b")[0].reason.get("optimised")
    no_overlaps(p)


def test_optimiser_never_moves_locked_or_breaks_dependencies():
    a = TaskInput("a", "Research", 120, priority="low")
    b = TaskInput("b", "Write", 60, priority="urgent", depends_on=("a",))
    c = TaskInput("c", "Call", 60, priority="high", energy="high")
    prev = [BlockInput("c", at(0, 15), at(0, 16), locked=True)]
    p = plan(snap([a, b, c], previous=prev))
    assert blocks_of(p, "c")[0].start == at(0, 15)
    assert blocks_of(p, "b")[0].start >= blocks_of(p, "a")[-1].end
    no_overlaps(p)


def test_optimiser_is_deterministic():
    import random

    rnd = random.Random(3)
    tasks = [
        TaskInput(f"t{i}", f"T{i}", rnd.choice([30, 60, 90]), priority=rnd.choice(["urgent", "high", "medium", "low"]),
                  energy=rnd.choice([None, "high", "low"]))
        for i in range(10)
    ]
    a = plan(snap(tasks))
    b = plan(snap(tasks))
    assert [(x.task_id, x.start) for x in a.blocks] == [(x.task_id, x.start) for x in b.blocks]
