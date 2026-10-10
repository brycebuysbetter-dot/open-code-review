"""Operational CLI: bootstrap users and run the scheduled cycle (cron / systemd timer)."""
import argparse
import sys

from bravo import db
from bravo.api import deps
from bravo.enums import Role
from bravo.models import User


def create_user(email: str, role: str, name: str = "") -> str:
    token = deps.new_token()
    with db.session_scope() as s:
        s.add(User(email=email.lower(), name=name, role=Role(role).value, token_hash=deps.hash_token(token)))
    return token


def run_cycle() -> dict:
    from bravo.agents.followup import FollowUpAgent
    from bravo.services import outbound, voice

    with db.session_scope() as s:
        out = FollowUpAgent().run_cycle(s)
    with db.session_scope() as s:
        out["dispatched"] = len(outbound.dispatch_due(s)) + len(voice.dispatch_calls(s))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bravo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    u = sub.add_parser("create-user")
    u.add_argument("email")
    u.add_argument("--role", choices=[r.value for r in Role], default="admin")
    u.add_argument("--name", default="")
    sub.add_parser("run-cycle")
    e = sub.add_parser("encrypt-secret", help="print an enc:... value to put in .env instead of a plaintext secret")
    e.add_argument("value")
    args = ap.parse_args(argv)
    if args.cmd == "init-db":
        db.create_all()
        print("tables created")
    elif args.cmd == "encrypt-secret":
        from bravo.crypto import get_crypto

        print("enc:" + get_crypto().encrypt(args.value))
    elif args.cmd == "create-user":
        print(f"API token (shown once): {create_user(args.email, args.role, args.name)}")
    else:
        print(run_cycle())
    return 0


if __name__ == "__main__":
    sys.exit(main())
