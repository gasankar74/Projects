"""Near-term optimiser: improves the next couple of days of a constructive plan with OR-Tools CP-SAT.

The constructive pass places tasks one at a time, highest score first. That is fast and explainable, but greedy:
an important task may take the only morning slot that a high-energy task wanted, or one long task may hold up
several short ones. For the near term (default 48 hours) this module re-arranges the flexible blocks the
constructive pass produced, keeping every rule:

- the same blocks (same durations), each inside its task's free hours, windows, earliest start and hard deadline
- no overlaps with calendar events, fixed blocks (locked, pinned, frozen) or each other, with buffers, travel
  and transition time
- prerequisites finish before dependents start
- daily caps (global and per scheduling profile)

and minimises: weighted finish times (more important work earlier), missed time-of-day / energy preferences,
missed preferred dates, soft-deadline lateness and moves away from the previous plan (stability).

The solver runs single-threaded with a fixed seed and a deterministic time budget, so the same snapshot always
gives the same plan. Its answer is checked again before use, and the constructive plan is kept if it is not
strictly better or anything looks wrong. If OR-Tools is not installed the constructive plan is used unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .engine import _Planner

FIXED_KINDS = {"locked", "frozen", "pinned"}


def _weight(score: float) -> int:
    return int(round((max(score, 0.0) / 10.0) ** 2)) + 1


def optimize_near_term(p: "_Planner") -> int:
    """Re-arrange flexible blocks in the optimisation window. Returns how many blocks moved."""
    hours = getattr(p.st, "optimize_hours", 0) or 0
    if hours <= 0 or not p.blocks:
        return 0
    try:
        from ortools.sat.python import cp_model
    except ImportError:  # pragma: no cover - optional dependency
        return 0

    from .engine import _ceil, _subtract  # local import avoids a cycle

    grid = p.grid
    w0 = p.start
    w1 = min(p.end, p.start + timedelta(hours=hours))
    trans = p.st.transition_min

    movable: set[str] = set()
    for tid, bl in p.task_blocks.items():
        t = p.tasks[tid]
        if not bl or any(b.locked or b.reason.get("bound_by") in FIXED_KINDS for b in bl):
            continue
        if any(b.end > w1 for b in bl):
            continue
        _, _, one_per_day = p.session_bounds(t, t.remaining_min)
        if one_per_day and len(bl) > 1:
            continue
        movable.add(tid)

    prev_starts: dict[str, set[datetime]] = {}
    for b in p.snap.previous:
        if not b.locked:
            prev_starts.setdefault(b.task_id, set()).add(b.start)

    # Candidate starts for every movable block. A task whose current position is not among them (which would mean
    # the model is stricter than the constructive pass) is treated as fixed, and the rest are recomputed.
    for _ in range(5):
        base = list(p.free_after_events)
        for b in p.blocks:
            if b.task_id in movable:
                continue
            t = p.tasks.get(b.task_id)
            bb = t.buffer_before_min if t else 0
            ba = t.buffer_after_min if t else 0
            base = _subtract(base, (b.start - timedelta(minutes=bb), b.end + timedelta(minutes=ba + trans)))
        cands: dict[int, list[datetime]] = {}
        dropped = False
        items = [b for b in p.blocks if b.task_id in movable]
        for i, b in enumerate(items):
            t = p.tasks[b.task_id]
            dur = b.minutes
            hard = t.deadline if (t.deadline and t.deadline_type == "hard") else None
            until = min(hard, w1) if hard else w1
            after = None
            for pre in t.depends_on:
                if pre in movable or pre not in p.task_blocks:
                    continue
                e = max(x.end for x in p.task_blocks[pre])
                after = e if after is None or e > after else after
            spans = p.candidate_spans(t, until, base=base, after=after)
            starts: list[datetime] = []
            for ps, pe in spans:
                s = _ceil(ps + timedelta(minutes=t.buffer_before_min), grid)
                while s + timedelta(minutes=dur + t.buffer_after_min) <= pe:
                    starts.append(s)
                    s += timedelta(minutes=grid)
            if b.start not in starts:
                movable.discard(b.task_id)
                dropped = True
                break
            cands[i] = starts
        if not dropped:
            break
    else:
        return 0
    if not items:
        return 0

    m = cp_model.CpModel()
    unit = lambda dt: int((dt - w0).total_seconds() // 60)  # noqa: E731  minutes from window start
    start_v, end_v, cost_v, day_v = {}, {}, {}, {}
    intervals = []
    hint_cost = 0
    day_index: dict = {}
    # A preference outweighs any gain in finish time inside the window (as in the constructive pass, which tries
    # preferred hours on every day before any other hours); a soft deadline outweighs a preference.
    span_units = unit(w1) // grid + 1
    for i, b in enumerate(items):
        t = p.tasks[b.task_id]
        starts = cands[i]
        dur = b.minutes
        w = _weight(p.eff[t.id][0] if hasattr(p, "eff") else p.results[t.id].score)
        pref = p.preferred_hours(t)
        costs, days = [], []
        for s in starts:
            e = s + timedelta(minutes=dur)
            c = w * (unit(e) // grid)
            ls = s.astimezone(p.tz)
            if pref:
                p0 = datetime.combine(ls.date(), pref[0], p.tz)
                p1 = datetime.combine(ls.date(), pref[1], p.tz)
                if not (p0 <= ls and e.astimezone(p.tz) <= p1):
                    c += span_units * w
            if t.preferred_date and ls.date() != t.preferred_date:
                c += 2 * span_units * w
            if t.deadline and t.deadline_type == "soft" and e > t.deadline:
                c += 10 * span_units * w
            if t.id in prev_starts and s not in prev_starts[t.id]:
                c += 4 * w
            costs.append(c)
            d = ls.date()
            days.append(day_index.setdefault(d, len(day_index)))
        idx = m.NewIntVar(0, len(starts) - 1, f"idx{i}")
        sv = m.NewIntVar(min(map(unit, starts)), max(map(unit, starts)), f"s{i}")
        m.AddElement(idx, [unit(s) for s in starts], sv)
        cv = m.NewIntVar(min(costs), max(costs), f"c{i}")
        m.AddElement(idx, costs, cv)
        dv = m.NewIntVar(0, max(len(day_index), 1), f"d{i}")
        m.AddElement(idx, days, dv)
        ev = m.NewIntVar(0, unit(w1) + 24 * 60, f"e{i}")
        m.Add(ev == sv + dur)
        lo = m.NewIntVar(-24 * 60, unit(w1) + 24 * 60, f"lo{i}")
        hi = m.NewIntVar(-24 * 60, unit(w1) + 48 * 60, f"hi{i}")
        m.Add(lo == sv - t.buffer_before_min)
        m.Add(hi == ev + t.buffer_after_min + trans)
        size = dur + t.buffer_before_min + t.buffer_after_min + trans
        intervals.append(m.NewIntervalVar(lo, size, hi, f"iv{i}"))
        k = starts.index(b.start)
        m.AddHint(idx, k)
        hint_cost += costs[k]
        start_v[i], end_v[i], cost_v[i], day_v[i] = sv, ev, cv, dv
    m.AddNoOverlap(intervals)

    # Dependencies among movable tasks, and movable prerequisites of fixed dependents.
    by_task: dict[str, list[int]] = {}
    for i, b in enumerate(items):
        by_task.setdefault(b.task_id, []).append(i)
    for tid, idxs in by_task.items():
        for pre in p.tasks[tid].depends_on:
            if pre in by_task:
                for i in idxs:
                    for j in by_task[pre]:
                        m.Add(start_v[i] >= end_v[j])
    for tid, bl in p.task_blocks.items():
        if tid in by_task:
            continue
        first = min((x.start for x in bl), default=None)
        if first is None:
            continue
        for pre in p.tasks[tid].depends_on:
            for j in by_task.get(pre, []):
                m.Add(end_v[j] <= unit(first))

    # Daily caps.
    days_rev = {v: k for k, v in day_index.items()}

    def cap_constraint(cap: int, members: list[int], used_fixed) -> None:
        for di, d in days_rev.items():
            terms = []
            for i in members:
                on = m.NewBoolVar(f"on{i}_{di}")
                m.Add(day_v[i] == di).OnlyEnforceIf(on)
                m.Add(day_v[i] != di).OnlyEnforceIf(on.Not())
                terms.append(items[i].minutes * on)
            if terms:
                m.Add(sum(terms) <= max(0, cap - used_fixed(d)))

    if p.st.daily_cap_min is not None:
        cap_constraint(
            p.st.daily_cap_min,
            list(range(len(items))),
            lambda d: sum(x.minutes for x in p.blocks if x.task_id not in movable and p.local_day(x.start) == d),
        )
    for pid, prof in (p.st.profiles or {}).items():
        pcap = (prof or {}).get("daily_cap_min")
        members = [i for i, b in enumerate(items) if p.tasks[b.task_id].profile_id == pid]
        if pcap is None or not members:
            continue
        cap_constraint(
            pcap,
            members,
            lambda d, pid=pid: sum(
                x.minutes for x in p.blocks
                if x.task_id not in movable and p.tasks.get(x.task_id) and p.tasks[x.task_id].profile_id == pid and p.local_day(x.start) == d
            ),
        )

    m.Minimize(sum(cost_v.values()))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 7
    solver.parameters.max_deterministic_time = 2.0
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return 0
    if solver.ObjectiveValue() >= hint_cost:
        return 0

    new = {i: w0 + timedelta(minutes=solver.Value(start_v[i])) for i in range(len(items))}
    # Independent check before using the answer.
    spans = []
    for i, b in enumerate(items):
        t = p.tasks[b.task_id]
        s, e = new[i], new[i] + timedelta(minutes=b.minutes)
        if s not in cands[i]:
            return 0
        spans.append((s - timedelta(minutes=t.buffer_before_min), e + timedelta(minutes=t.buffer_after_min + trans)))
    spans.sort()
    if any(spans[k][1] > spans[k + 1][0] for k in range(len(spans) - 1)):
        return 0

    moved = 0
    for i, b in enumerate(items):
        if new[i] != b.start:
            t = p.tasks[b.task_id]
            pref = p.preferred_hours(t)
            dur = b.minutes
            b.start, b.end = new[i], new[i] + timedelta(minutes=dur)
            ls = b.start.astimezone(p.tz)
            fits_pref = bool(pref) and datetime.combine(ls.date(), pref[0], p.tz) <= ls and b.end.astimezone(p.tz) <= datetime.combine(ls.date(), pref[1], p.tz)
            msg = (
                "Arranged by the near-term optimiser so it lands in your preferred hours"
                if fits_pref
                else "Arranged by the near-term optimiser so more important work finishes sooner overall"
            )
            why = [x for x in b.reason.get("why", []) if not x.startswith("Kept its previous time")]
            b.reason["why"] = [msg] + why
            b.reason["optimised"] = True
            moved += 1
    if moved:
        for tid in by_task:
            bl = sorted(p.task_blocks[tid], key=lambda x: x.start)
            p.task_blocks[tid] = bl
            for n, x in enumerate(bl, 1):
                x.session_index, x.session_count = n, len(bl)
        p._recompute_usage()
    return moved
