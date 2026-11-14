"""Read tasks straight from Motion with the user's Motion API key, for the import review screen.

The key is used for these requests only. It is never saved, logged or sent anywhere except api.usemotion.com.
The tasks are turned into the same CSV that Motion's own export produces, so they go through the normal import
review (duplicate checks, column mapping, row choices). Tasks keep Motion's id ("motion:<id>"), so importing again,
or importing a Motion CSV export later, adds nothing twice.

Motion limits API keys to a few requests a minute, so a 429 waits and retries, within an overall time limit.
"""

import csv
import html
import io
import re
import time

import httpx

BASE = "https://api.usemotion.com/v1"
MAX_PAGES = 100
TIME_LIMIT_S = 25  # the web app's proxy gives up on a request after 30 s
HEADER = [
    "ID", "Name", "Description", "Status", "Completed", "Priority", "Due Date", "Deadline Type",
    "Duration", "Start Date", "Project", "Workspace", "Labels", "Completed Time", "Motion due date (no deadline)",
]


class MotionError(Exception):
    """A problem the user can act on; the message is shown as is."""


class MotionClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.http = httpx.Client(base_url=BASE, transport=transport, timeout=20.0, headers={"X-API-Key": api_key, "Accept": "application/json"})
        self.sleep = sleep
        self.started = time.monotonic()

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(6):
            try:
                r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
            except httpx.HTTPError:
                raise MotionError("Couldn't reach Motion. Check the server's internet connection and try again.")
            if r.status_code == 429:
                wait = min(float(r.headers.get("retry-after") or 0) or 5.0 * (attempt + 1), 20.0)
                if time.monotonic() - self.started + wait > TIME_LIMIT_S:
                    break
                self.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise MotionError("Motion didn't accept that API key. Create a new key in Motion under Settings > API and paste it here.")
            if r.status_code >= 400:
                raise MotionError(f"Motion answered with an error ({r.status_code}). Try again in a minute.")
            try:
                return r.json()
            except ValueError:
                raise MotionError("Motion sent an answer this app can't read.")
        raise MotionError("Motion is limiting how fast this key can be used. Wait a minute and try again.")

    def pages(self, path: str, key: str, params: dict) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            data = self.get(path, {**params, "cursor": cursor})
            out += data.get(key) or []
            cursor = (data.get("meta") or {}).get("nextCursor")
            if not cursor:
                break
        return out

    def tasks(self, include_completed: bool) -> list[dict]:
        found: dict[str, dict] = {}
        for ws in self.pages("/workspaces", "workspaces", {}):
            params = {"workspaceId": ws.get("id"), "includeAllStatuses": "true" if include_completed else None}
            for t in self.pages("/tasks", "tasks", params):
                t.setdefault("workspace", {"id": ws.get("id"), "name": ws.get("name")})
                if t.get("id"):
                    found[t["id"]] = t
        return list(found.values())


def _text(v) -> str:
    """Motion descriptions are HTML; keep the words and line breaks."""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", str(v or ""))
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _row(t: dict) -> list[str]:
    status = t.get("status") or {}
    status_name = status.get("name", "") if isinstance(status, dict) else str(status)
    done = bool(t.get("completed")) or (isinstance(status, dict) and bool(status.get("isResolvedStatus")))
    dur = t.get("duration")
    duration = "0" if dur == "REMINDER" else "" if dur in (None, "NONE") else str(dur)
    dtype = (t.get("deadlineType") or "").upper()
    due = t.get("dueDate") or ""
    no_deadline = dtype == "NONE"
    labels = [lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in t.get("labels") or []]
    return [
        str(t.get("id", "")),
        str(t.get("name", "")),
        _text(t.get("description")),
        status_name,
        "yes" if done else "",
        str(t.get("priority") or ""),
        "" if no_deadline else due,
        {"HARD": "hard", "SOFT": "soft"}.get(dtype, ""),
        duration,
        str(t.get("startOn") or ""),
        str((t.get("project") or {}).get("name") or ""),
        str((t.get("workspace") or {}).get("name") or ""),
        ", ".join(x for x in labels if x),
        str(t.get("completedTime") or ""),
        due if no_deadline else "",
    ]


def tasks_to_csv(tasks: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for t in tasks:
        w.writerow(_row(t))
    return buf.getvalue()


def fetch_csv(api_key: str, include_completed: bool, transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> tuple[str, int]:
    key = (api_key or "").strip()
    if not key:
        raise MotionError("Paste your Motion API key first.")
    client = MotionClient(key, transport=transport, sleep=sleep)
    try:
        tasks = client.tasks(include_completed)
    finally:
        client.http.close()
    if not tasks:
        raise MotionError("Motion returned no tasks for this key." + ("" if include_completed else " Tick “Include completed tasks” to fetch finished ones too."))
    return tasks_to_csv(tasks), len(tasks)
