"""Outgoing email. Gmail works with an app password (smtp.gmail.com:587, STARTTLS).

Backends: smtp, file (writes .eml files, for testing), log, none. Tests swap in a list with set_mailer_for_tests.
"""

import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from pathlib import Path

from ..config import get_settings

log = logging.getLogger("meridian.mail")
_test_outbox: list[EmailMessage] | None = None


def set_mailer_for_tests(outbox: list | None) -> None:
    global _test_outbox
    _test_outbox = outbox


def backend() -> str:
    s = get_settings()
    if _test_outbox is not None:
        return "test"
    return s.email_backend or ("smtp" if s.smtp_host else "none")


def enabled() -> bool:
    return backend() != "none"


def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one message. Returns False (and logs) on failure; never raises."""
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.email_from or s.smtp_user or "meridian@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4()}@meridian>"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    b = backend()
    try:
        if b == "test":
            _test_outbox.append(msg)  # type: ignore[union-attr]
        elif b == "smtp":
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        elif b == "file":
            d = Path(s.mail_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{uuid.uuid4()}.eml").write_bytes(bytes(msg))
        elif b == "log":
            log.info("email to %s: %s", to, subject)
        else:
            return False
        return True
    except Exception as e:  # network, auth...
        log.warning("email to %s failed: %s", to, e)
        return False
