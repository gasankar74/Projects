import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, MultiFernet
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AuthSession, User


# ---------- token encryption (OAuth tokens at rest) ----------

def _fernet() -> MultiFernet:
    keys = [k.strip() for k in get_settings().token_encryption_keys.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("MERIDIAN_TOKEN_ENCRYPTION_KEYS is not set; generate one with `python -m app.cli genkey`")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().decrypt(value.encode()).decode()


# ---------- sessions ----------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, response: Response, user_agent: str = "") -> None:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=s.session_days),
            user_agent=user_agent[:400],
        )
    )
    db.commit()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )


def destroy_session(db: Session, request: Request, response: Response) -> None:
    s = get_settings()
    token = request.cookies.get(s.session_cookie_name)
    if token:
        sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash(token)))
        if sess:
            db.delete(sess)
            db.commit()
    response.delete_cookie(s.session_cookie_name, path="/")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise HTTPException(401, "Not signed in")
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash(token)))
    if not sess or sess.expires_at < datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    user = db.get(User, sess.user_id)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


# ---------- signed OAuth state ----------

def _state_key() -> bytes:
    # Derive a signing key from the encryption key so there is one less secret to manage.
    keys = get_settings().token_encryption_keys.split(",")[0].strip()
    return hashlib.sha256(("oauth-state:" + keys).encode()).digest()


def sign_state(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_state_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_state(state: str) -> dict:
    try:
        raw, sig = state.rsplit(".", 1)
    except ValueError:
        raise HTTPException(400, "Bad OAuth state")
    expected = hmac.new(_state_key(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(400, "Bad OAuth state signature")
    data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    if data.get("exp", 0) < datetime.now(timezone.utc).timestamp():
        raise HTTPException(400, "OAuth state expired, please try again")
    return data
