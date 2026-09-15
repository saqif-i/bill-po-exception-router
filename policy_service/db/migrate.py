"""Forward-only migration runner.

Numbered SQL migrations recorded in `schema_migrations`, refusing to run out of
order (Volume 03 section 9.1). Applied as bpr_owner; the runtime roles never
receive DDL authority.

Files are applied in lexical order of what exists on disk. The sequence is
deliberately NOT asserted to be contiguous: v1 creates 001, 003, 004 and 005,
and 002 belongs to deferred Volume 05. See BUILD-SCOPE-v1.md section 4.
"""

from __future__ import annotations

import os
import pathlib
import sys

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"

CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover() -> list[pathlib.Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


# Migration 001 creates the runtime login roles. Their passwords arrive as
# session settings rather than being written into the SQL file, so no
# credential is ever committed.
ROLE_PASSWORD_SETTINGS = {
    "bpr.app_password": "BPR_APP_PASSWORD",
    "bpr.retention_password": "BPR_RETENTION_PASSWORD",
}


def apply_all(owner_url: str) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(owner_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for setting, env_var in ROLE_PASSWORD_SETTINGS.items():
                value = os.environ.get(env_var)
                if not value:
                    raise RuntimeError(
                        f"{env_var} is required: migration 001 creates a login role "
                        f"and a role without a password cannot authenticate."
                    )
                # SET does not accept a bound parameter; set_config does, which
                # keeps the credential out of the SQL text.
                cur.execute("SELECT set_config(%s, %s, false)", (setting, value))
            cur.execute(CREATE_LEDGER)
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            already = [row[0] for row in cur.fetchall()]

        on_disk = discover()
        pending = [p for p in on_disk if p.name not in already]

        # Forward-only: nothing may be applied that sorts before something
        # already applied. This catches a migration added with a lower number
        # after the fact, without requiring the sequence to be contiguous.
        if already and pending and min(p.name for p in pending) < max(already):
            raise RuntimeError(
                f"out-of-order migration: {min(p.name for p in pending)} sorts before "
                f"the already-applied {max(already)}. A correction ships as a new "
                f"forward migration, never as an edit to an applied one."
            )

        for path in pending:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


def main() -> int:
    owner_url = os.environ.get("BPR_OWNER_DATABASE_URL")
    if not owner_url:
        print("BPR_OWNER_DATABASE_URL is required. Migrations run as bpr_owner.", file=sys.stderr)
        return 2
    applied = apply_all(owner_url)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
