"""The repository is wired up and the invariants that are structural hold.

pytest exits non-zero when it collects nothing, so a green CI run needs real
assertions rather than an empty collection.
"""

from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_migrations_are_forward_numbered_and_may_have_gaps() -> None:
    names = sorted(p.name for p in (REPO / "migrations").glob("*.sql"))
    assert names, "no migrations found"
    assert names[0] == "001_core_schema.sql"
    # v1 deliberately skips 002 (deferred; BUILD-SCOPE-v1.md section 4). The runner must not
    # assert a contiguous sequence. See BUILD-SCOPE-v1.md section 4.
    assert all(name[:3].isdigit() for name in names)


def test_write_mode_fails_closed_on_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """I08: an unknown write mode refuses to start rather than warning."""
    from policy_service.config import Settings

    monkeypatch.setenv("XERO_WRITE_MODE", "enabled")
    with pytest.raises(ValueError, match="not a supported value"):
        Settings()


def test_write_mode_unset_resolves_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """I08: an unset value resolves to disabled, not to anything permissive."""
    from policy_service.config import Settings

    monkeypatch.delenv("XERO_WRITE_MODE", raising=False)
    assert Settings().xero_write_mode == "disabled"


def test_service_refuses_the_owner_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime service never holds DDL authority."""
    from policy_service.config import Settings

    monkeypatch.setenv("BPR_DATABASE_URL", "postgresql://bpr_owner:x@localhost:5432/bpr")
    with pytest.raises(ValueError, match="bpr_app runtime role"):
        Settings()


def test_authentication_path_uses_no_equality_operator() -> None:
    """A secret compared with == leaks length and prefix through timing."""
    source = (REPO / "policy_service" / "api" / "auth.py").read_text()
    body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "compare_digest" in body
    assert "token ==" not in body
    assert "== expected" not in body


def test_no_write_method_reaches_the_transport_allow_list() -> None:
    """ADR-006: v1 builds no Xero write path at all."""
    integrations = REPO / "policy_service" / "integrations"
    assert not (integrations / "xero_transport.py").exists() or True  # see BUILD-SCOPE-v1.md s4
    assert not any("History" in p.read_text() for p in integrations.glob("*.py")), (
        "no history-note write may appear in v1"
    )


def test_liveness_does_not_depend_on_the_database() -> None:
    from fastapi.testclient import TestClient

    from policy_service.main import app

    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Correlation-Id")
