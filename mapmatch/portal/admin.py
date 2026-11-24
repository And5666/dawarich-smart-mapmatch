"""Account recovery from the command line.

    PORTAL_DATA=/data python -m mapmatch.portal.admin list
    PORTAL_DATA=/data python -m mapmatch.portal.admin reset andre

A portal whose admin password is lost has no web path back in, so this is the
way: it reads the same database the portal uses (PORTAL_DATA, or the default
./portal-data) and sets a new password.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from . import auth
from .config import Config
from .db import Database


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mapmatch.portal.admin",
                                 description="Reset a portal password or list the accounts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    reset = sub.add_parser("reset", help="set a new password for an account")
    reset.add_argument("username")
    reset.add_argument("--password", help="the new password (otherwise you are prompted)")
    sub.add_parser("list", help="show the accounts and what they have")
    args = ap.parse_args(argv)

    db = Database(Config().db_path)
    if args.cmd == "list":
        for u in auth.users(db):
            role = "admin" if u["is_admin"] else "member"
            state = " disabled" if u["disabled"] else ""
            linked = "linked" if u["dawarich_linked"] else "no Dawarich key"
            print(f"{u['username']:<28} {role}{state:<10} {u['trips']:>5} trips  {linked}")
        return 0

    row = db.one("SELECT id FROM users WHERE username=?", (args.username,))
    if not row:
        print(f"no account called {args.username!r}", file=sys.stderr)
        return 1
    password: Any = args.password or getpass.getpass(f"New password for {args.username}: ")
    try:
        auth.set_password(db, int(row["id"]), str(password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"password changed for {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
