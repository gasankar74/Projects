from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Category, User, Workspace

DEFAULT_CATEGORIES = [
    ("Legal and Mediation", "#7c3aed", "high"),
    ("Speaking and Presentations", "#db2777", "high"),
    ("Business Administration", "#0891b2", "medium"),
    ("Marketing and Networking", "#ea580c", "medium"),
    ("Financial and Insurance", "#16a34a", "medium"),
    ("Education", "#2563eb", "medium"),
    ("Home and Family", "#ca8a04", "medium"),
    ("Personal", "#64748b", "low"),
]


def get_or_create_user(db: Session, email: str, name: str = "", google_sub: str | None = None, tz: str | None = None) -> tuple[User, bool]:
    user = None
    if google_sub:
        user = db.scalar(select(User).where(User.google_sub == google_sub))
    if user is None:
        user = db.scalar(select(User).where(User.email == email.lower()))
    if user:
        if google_sub and not user.google_sub:
            user.google_sub = google_sub
        if name and not user.name:
            user.name = name
        db.commit()
        return user, False
    user = User(email=email.lower(), name=name, google_sub=google_sub)
    if tz:
        user.timezone = tz
    db.add(user)
    db.flush()
    db.add(Workspace(user_id=user.id, name="My workspace"))
    for i, (n, color, prio) in enumerate(DEFAULT_CATEGORIES):
        db.add(Category(user_id=user.id, name=n, color=color, position=i, default_priority=prio))
    db.commit()
    return user, True


def remove_google_footprint(db: Session, user: User, client, max_deletes: int = 500) -> dict:
    """Before an account is deleted: remove the task blocks Meridian wrote to Google Calendar and revoke
    Meridian's access to every connected Google account. Best effort; failures never block the deletion."""
    import logging

    from ..models import BlockEventLink, Calendar, GoogleAccount
    from ..security import decrypt
    from .calendar_sync import access_token_for

    log = logging.getLogger("meridian.accounts")
    out = {"events_removed": 0, "event_errors": 0, "accounts_revoked": 0}
    tokens: dict = {}
    links = db.scalars(select(BlockEventLink).where(BlockEventLink.user_id == user.id, BlockEventLink.written_start.is_not(None))).all()
    for link in links[:max_deletes]:
        cal = db.get(Calendar, link.calendar_id)
        if not cal or not cal.writable:
            continue
        acct = db.get(GoogleAccount, cal.google_account_id)
        if not acct or acct.status == "needs_reconnect":
            continue
        try:
            if acct.id not in tokens:
                tokens[acct.id] = access_token_for(db, acct, client)
            client.delete_event(tokens[acct.id], cal.google_calendar_id, link.google_event_id)
            out["events_removed"] += 1
        except Exception as e:  # already gone, network...
            out["event_errors"] += 1
            log.info("could not remove block event %s: %s", link.google_event_id, e)
    for acct in db.scalars(select(GoogleAccount).where(GoogleAccount.user_id == user.id)).all():
        try:
            tok = decrypt(acct.refresh_token_enc) if acct.refresh_token_enc else None
            if tok and client.revoke(tok):
                out["accounts_revoked"] += 1
        except Exception as e:
            log.info("could not revoke Google access for %s: %s", acct.id, e)
    return out
