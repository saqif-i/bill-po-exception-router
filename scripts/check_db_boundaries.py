#!/usr/bin/env python3
"""Assert the n8n database boundary against the running container.

Volume 01 section 9.9. ADR-002 says the boundary is enforced by role grants
rather than by configuration; this proves it.
"""

from __future__ import annotations

import os
import sys

import psycopg


def _url(user: str, password: str, database: str) -> str:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    # 5433 by default: a local PostgreSQL commonly holds 5432.
    port = os.environ.get("POSTGRES_PORT", "5433")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def main() -> int:
    owner_url = os.environ.get("BPR_OWNER_DATABASE_URL")
    n8n_user = os.environ.get("N8N_DB_USER", "n8n_app")
    n8n_password = os.environ.get("N8N_DB_PASSWORD")
    app_db = os.environ.get("APP_DB_NAME", "bpr")
    n8n_db = os.environ.get("N8N_DB_NAME", "n8n")

    if not owner_url or not n8n_password:
        print("BPR_OWNER_DATABASE_URL and N8N_DB_PASSWORD are required", file=sys.stderr)
        return 2

    failures: list[str] = []

    # 1 and 2: both databases exist.
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", ([app_db, n8n_db],))
        found = {row[0] for row in cur.fetchall()}
    for name in (app_db, n8n_db):
        if name not in found:
            failures.append(f"database {name} does not exist")

    # 3: n8n_app reaches its own database.
    try:
        with psycopg.connect(_url(n8n_user, n8n_password, n8n_db)):
            pass
    except Exception as exc:
        failures.append(f"{n8n_user} cannot connect to {n8n_db}: {type(exc).__name__}")

    # 4: n8n_app is refused on the application database.
    try:
        with psycopg.connect(_url(n8n_user, n8n_password, app_db)):
            failures.append(f"BOUNDARY BREACH: {n8n_user} connected to {app_db}")
    except Exception:  # noqa: S110 - refusal is the expected outcome
        pass

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1

    print(
        f"ok: {app_db} and {n8n_db} exist; {n8n_user} reaches {n8n_db} and is refused on {app_db}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
