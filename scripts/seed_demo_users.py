"""Idempotent demo-account seeder for deployments whose database never got
populated (a fresh pg_data volume ships schema-only: migrations create tables
but no users). The original dev database's demo accounts were created by hand
and exist ONLY there, so a deployed stack answers 401 for them (auth.py login).

Creates the roster below with the GIVEN password (no default on purpose) and
skips usernames that already exist - safe to re-run, never touches real rows.

Usage on the deploy host (inside the backend container, code is bind-mounted):
    docker compose exec -e SEED_PASSWORD='ChooseSomething!' backend \
        python scripts/seed_demo_users.py
or with an explicit roster subset:
    docker compose exec -e SEED_PASSWORD='...' backend \
        python scripts/seed_demo_users.py --users demoacc

The script reads DATABASE_URL from the container environment exactly like the
app does (services.pg_service), so it targets the deployment's own database.
"""
import argparse
import os
import sys
import uuid

import bcrypt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
from services import pg_service  # noqa: E402  (import path mirrors uvicorn api.main)

ROSTER = [
    ("demoacc", "sekretaris perusahaan"),
    ("demoaccc", "sekretaris perusahaan"),
    ("smoke_kg", "manajer"),
    ("smoke_m3", "pengguna"),
]


def hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default=os.environ.get("SEED_PASSWORD", ""),
                        help="password for every seeded account (or SEED_PASSWORD env)")
    parser.add_argument("--users", default="",
                        help="comma-separated subset of the roster to seed; "
                             "usernames outside the roster are created with --role")
    parser.add_argument("--role", default="pengguna",
                        help="role for usernames outside the roster (default pengguna)")
    parser.add_argument("--email-domain", default="example.com")
    args = parser.parse_args()

    if not args.password:
        print("ERROR: pass --password or SEED_PASSWORD; there is no default.")
        return 2

    roles = dict(ROSTER)
    wanted = [u.strip() for u in args.users.split(",") if u.strip()] or list(roles)

    created = skipped = 0
    for username in wanted:
        role = roles.get(username, args.role)
        if pg_service.query_one("SELECT id FROM users WHERE username = %s", (username,)):
            print(f"skip    {username} (already exists)")
            skipped += 1
            continue
        pg_service.execute(
            "INSERT INTO users (id, username, password_hash, email, role) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), username, hash_pw(args.password),
             f"{username}@{args.email_domain}", role),
        )
        print(f"created {username} (role: {role})")
        created += 1

    print(f"done: {created} created, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
