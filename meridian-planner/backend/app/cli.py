"""Small admin commands.

  python -m app.cli genkey        print a new token-encryption key
  python -m app.cli sync          sync every connected Google account now (for cron)
"""

import sys

from cryptography.fernet import Fernet
from sqlalchemy import select


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "genkey":
        print(Fernet.generate_key().decode())
    elif cmd == "sync":
        from .db import session_factory
        from .models import GoogleAccount
        from .services.calendar_sync import sync_account
        from .services.google import GoogleClient

        db = session_factory()()
        client = GoogleClient()
        for acct in db.scalars(select(GoogleAccount).where(GoogleAccount.status != "needs_reconnect")).all():
            print(sync_account(db, acct, client))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
