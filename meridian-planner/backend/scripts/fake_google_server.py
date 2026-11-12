"""Local stand-in for Google OAuth + Calendar, for demos and end-to-end tests without real Google credentials.

Run:   python -m scripts.fake_google_server     (listens on :8765)
Then start the API with:
  MERIDIAN_GOOGLE_AUTH_URL=http://localhost:8765/o/oauth2/v2/auth
  MERIDIAN_GOOGLE_TOKEN_URL=http://localhost:8765/token
  MERIDIAN_GOOGLE_USERINFO_URL=http://localhost:8765/userinfo
  MERIDIAN_GOOGLE_CALENDAR_API=http://localhost:8765/calendar/v3

It seeds two accounts (personal and law firm) with overlapping events this week.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fake_google import FakeGoogle  # noqa: E402

TZ = ZoneInfo("America/New_York")
fake = FakeGoogle()


def at(day_offset: int, h: int, m: int = 0) -> datetime:
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    return (monday + timedelta(days=day_offset)).replace(hour=h, minute=m)


def seed():
    fake.add_account("code-personal", "sub-p", "greg.personal@example.com", "Greg")
    fake.add_calendar("sub-p", "greg.personal@example.com", "Personal", primary=True, color="#0b8043")
    fake.add_calendar("sub-p", "family@group.calendar.google.com", "Family", role="writer", color="#f6bf26")
    fake.add_calendar("sub-p", "court@group.calendar.google.com", "Court dates (shared)", role="reader", color="#d50000")
    fake.add_account("code-biz", "sub-b", "greg@lawfirm.example.com", "Greg")
    fake.add_calendar("sub-b", "greg@lawfirm.example.com", "Law firm", primary=True, color="#3f51b5")
    fake.add_calendar("sub-b", "speaking@lawfirm.example.com", "Speaking", role="owner", color="#8e24aa")

    def ev(cal, id, start, end, summary, **kw):
        fake.put_event(cal, {"id": id, "summary": summary, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}, **kw})

    for d in range(5):
        ev("greg@lawfirm.example.com", f"standup{d}", at(d, 9), at(d, 9, 30), "Firm stand-up")
    ev("greg@lawfirm.example.com", "mediation", at(1, 10), at(1, 13), "Mediation: Harper v. Lin")
    ev("court@group.calendar.google.com", "hearing", at(2, 14), at(2, 15, 30), "Status hearing (Dept 12)")
    ev("greg.personal@example.com", "dentist", at(2, 15), at(2, 16), "Dentist")
    ev("family@group.calendar.google.com", "pickup", at(3, 15, 30), at(3, 16, 30), "School pickup")
    ev("speaking@lawfirm.example.com", "cle", at(4, 12), at(4, 13), "CLE planning call")
    ev("greg.personal@example.com", "gym", at(0, 7), at(0, 8), "Gym (free)", transparency="transparent")
    ev("greg@lawfirm.example.com", "client", at(3, 11), at(3, 12), "Client meeting (declined)", attendees=[{"email": "g", "self": True, "responseStatus": "declined"}])
    fake.put_event("family@group.calendar.google.com", {"id": "trip", "summary": "Family trip", "start": {"date": at(5, 0).date().isoformat()}, "end": {"date": at(7, 0).date().isoformat()}, "transparency": "opaque"})


async def authorize(request: Request):
    q = request.query_params
    # A tiny account chooser instead of Google's consent screen.
    links = "".join(
        f'<p><a href="{q["redirect_uri"]}?{urlencode({"code": code, "state": q["state"]})}">{ident["email"]}</a></p>'
        for code, ident in fake.identities.items()
    )
    return HTMLResponse(f"<h3>Fake Google: choose an account</h3>{links}")


async def proxy(request: Request):
    body = await request.body()
    host = {"token": "oauth2.googleapis.com", "userinfo": "openidconnect.googleapis.com"}.get(request.url.path.strip("/").split("/")[0], "www.googleapis.com")
    path = "/token" if host == "oauth2.googleapis.com" else request.url.path
    url = httpx.URL(f"https://{host}{path}", query=request.url.query.encode())
    resp = fake.handle(httpx.Request(request.method, url, headers=request.headers.raw, content=body))
    return Response(resp.content, status_code=resp.status_code, media_type="application/json")


app = Starlette(
    routes=[
        Route("/o/oauth2/v2/auth", authorize),
        Route("/{path:path}", proxy, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
    ]
)

if __name__ == "__main__":
    seed()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
