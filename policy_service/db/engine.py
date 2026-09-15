"""Connection pool and the readiness probes it backs.

Volume 03 section 9.0: from that volume onward a missing or wrong-role URL
prevents startup, a configured but unreachable database returns 503
DATABASE_UNAVAILABLE, and missing migrations return 503 MIGRATIONS_NOT_READY.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from policy_service.config import get_settings

_pool: ConnectionPool | None = None

# Every migration this build expects to find applied.
# Part 5 adds 003 here. Readiness reports MIGRATIONS_NOT_READY until every
# listed migration is applied, so this list grows with the schema.
REQUIRED_MIGRATIONS = ("001_core_schema.sql",)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(settings.bpr_database_url, min_size=1, max_size=8, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def database_reachable() -> bool:
    try:
        with get_pool().connection(timeout=2.0) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception:
        return False


def missing_migrations() -> list[str]:
    """Return the required migrations that have not been applied."""
    try:
        with get_pool().connection(timeout=2.0) as conn, conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
    except Exception:
        return list(REQUIRED_MIGRATIONS)
    return [name for name in REQUIRED_MIGRATIONS if name not in applied]


# 003 is added by Volume 06. Readiness reports MIGRATIONS_NOT_READY until it is
# applied, so a half-migrated database never serves a reconciliation request.
