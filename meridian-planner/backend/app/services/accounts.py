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
