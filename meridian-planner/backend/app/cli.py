"""Small admin commands.

  python -m app.cli genkey        print a new token-encryption key
  python -m app.cli sync          sync every connected Google account now (for cron)
  python -m app.cli worker        run the background worker: debounced replans, calendar sync, hourly refresh
  python -m app.cli replan EMAIL  replan one user now and print the warnings
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
    elif cmd == "worker":
        from .worker import run_forever

        run_forever()
    elif cmd == "replan" and len(sys.argv) > 2:
        from .db import session_factory
        from .models import User
        from .services.scheduling import replan

        db = session_factory()()
        user = db.scalars(select(User).where(User.email == sys.argv[2])).one()
        run, plan = replan(db, user, trigger="cli", force=True)
        print(f"run {run.id}: {len(run.blocks)} blocks")
        for w in run.warnings:
            print("-", w["message"])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
