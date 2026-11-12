import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, GoogleAccount, User
from ..schemas import UserOut, UserUpdate
from ..security import create_session, current_user, destroy_session, encrypt, sign_state, verify_state
from ..services import google as g
from ..services.accounts import get_or_create_user
from ..services.calendar_sync import sync_account

router = APIRouter(prefix="/auth", tags=["auth"])
PKCE_COOKIE = "aasha_pkce"


def google_client() -> g.GoogleClient:
    """Dependency; tests override it with a client on a mock transport."""
    return g.GoogleClient()


def _user_or_none(request: Request, db: Session) -> User | None:
    try:
        return current_user(request, db)
    except HTTPException:
        return None


@router.get("/google/login")
def google_login(request: Request, mode: str = "signin", tz: str | None = None, db: Session = Depends(get_db)):
    """Start Google OAuth. mode=signin signs in (creating the account); mode=connect adds another
    Google account's calendars to the signed-in user."""
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(503, "Google sign-in is not configured (set AASHA_GOOGLE_CLIENT_ID and secret)")
    if mode == "connect" and not _user_or_none(request, db):
        raise HTTPException(401, "Sign in first")
    verifier, challenge = g.pkce_pair()
    state = sign_state({"mode": mode, "tz": tz, "exp": time.time() + 600, "n": verifier[:16]})
    resp = RedirectResponse(g.authorization_url(state, challenge))
    resp.set_cookie(PKCE_COOKIE, verifier, max_age=600, httponly=True, secure=s.cookie_secure, samesite="lax", path="/")
    return resp


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    client: g.GoogleClient = Depends(google_client),
):
    s = get_settings()
    if error:
        return RedirectResponse(f"{s.frontend_url}/login?error={error}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state")
    data = verify_state(state)
    verifier = request.cookies.get(PKCE_COOKIE)
    if not verifier or verifier[:16] != data.get("n"):
        raise HTTPException(400, "OAuth session mismatch; please try again")

    tokens = client.exchange_code(code, verifier)
    info = client.userinfo(tokens["access_token"])
    sub, email = info["sub"], info.get("email", "")

    signed_in = _user_or_none(request, db)
    if data["mode"] == "connect" and signed_in:
        user = signed_in
    else:
        tz = data.get("tz")
        try:
            ZoneInfo(tz) if tz else None
        except Exception:
            tz = None
        user, _ = get_or_create_user(db, email, info.get("name", ""), google_sub=sub, tz=tz)

    acct = db.scalar(select(GoogleAccount).where(GoogleAccount.user_id == user.id, GoogleAccount.google_sub == sub))
    if acct is None:
        acct = GoogleAccount(user_id=user.id, google_sub=sub, email=email)
        db.add(acct)
    acct.email = email
    acct.scopes = tokens.get("scope", "")
    acct.access_token_enc = encrypt(tokens["access_token"])
    acct.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    if tokens.get("refresh_token"):
        acct.refresh_token_enc = encrypt(tokens["refresh_token"])
    acct.status = "ok"
    acct.last_error = None
    db.add(AuditLog(user_id=user.id, action="google_connected", detail={"email": email, "mode": data["mode"]}))
    db.commit()

    # First sync right away so the calendar is populated when the user lands.
    sync_account(db, acct, client)

    resp = RedirectResponse(f"{s.frontend_url}/{'settings' if data['mode'] == 'connect' else ''}")
    resp.delete_cookie(PKCE_COOKIE, path="/")
    if not signed_in or data["mode"] != "connect":
        create_session(db, user, resp, request.headers.get("user-agent", ""))
    return resp


class DevLogin(BaseModel):
    email: EmailStr
    name: str = ""
    timezone: str | None = None


@router.post("/dev-login", response_model=UserOut)
def dev_login(body: DevLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    if not get_settings().dev_login_enabled:
        raise HTTPException(404, "Not found")
    user, _ = get_or_create_user(db, body.email, body.name, tz=body.timezone)
    create_session(db, user, response, request.headers.get("user-agent", ""))
    db.add(AuditLog(user_id=user.id, action="dev_login"))
    db.commit()
    return user


@router.get("/config")
def auth_config():
    s = get_settings()
    return {"google": bool(s.google_client_id), "dev_login": s.dev_login_enabled}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    destroy_session(db, request, response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: UserUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(user, k, v)
    db.commit()
    return user
