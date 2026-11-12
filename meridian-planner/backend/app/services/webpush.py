"""Browser push (Web Push with VAPID). The payload is encrypted end to end to the browser; the push service
(Google, Mozilla, Apple) only relays it."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PushSubscription, User

log = logging.getLogger("meridian.push")
_test_sent: list | None = None


def set_push_for_tests(sent: list | None) -> None:
    global _test_sent
    _test_sent = sent


def enabled() -> bool:
    s = get_settings()
    return _test_sent is not None or bool(s.vapid_public_key and s.vapid_private_key)


def _send_one(sub: PushSubscription, payload: dict) -> int:
    """Returns the HTTP status from the push service (201 = accepted)."""
    if _test_sent is not None:
        _test_sent.append({"endpoint": sub.endpoint, "payload": payload})
        return 410 if "gone" in sub.endpoint else 201
    from pywebpush import WebPushException, webpush

    s = get_settings()
    try:
        r = webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=json.dumps(payload),
            vapid_private_key=s.vapid_private_key,
            vapid_claims={"sub": s.vapid_subject},
            ttl=3600,
            timeout=10,
        )
        return getattr(r, "status_code", 201)
    except WebPushException as e:
        return getattr(e.response, "status_code", 0) if e.response is not None else 0
    except Exception as e:
        log.warning("push failed: %s", e)
        return 0


def send(db: Session, user: User, payload: dict) -> int:
    """Send to every browser the user subscribed. Returns how many accepted it. Gone subscriptions are removed."""
    if not enabled():
        return 0
    ok = 0
    now = datetime.now(timezone.utc)
    for sub in db.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)).all():
        status = _send_one(sub, payload)
        if 200 <= status < 300:
            ok += 1
            sub.last_ok_at = now
            sub.failures = 0
        elif status in (404, 410):
            db.delete(sub)  # the browser unsubscribed or the subscription expired
        else:
            sub.failures += 1
            if sub.failures >= 10:
                db.delete(sub)
    db.commit()
    return ok


def generate_vapid() -> tuple[str, str]:
    """(public key for the browser, private key for the server), both base64url."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return b64(pub), b64(priv)
