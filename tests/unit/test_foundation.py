"""Foundation checks that depend only on repository files, not application code.

`pytest` exits non-zero when it collects nothing, so a green CI run needs real
assertions rather than an empty suite. These assert things that are true the
moment the repository is scaffolded and stay true, and none of them imports
application code that later Parts supply.
"""

from __future__ import annotations

import json
import pathlib
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_the_required_directories_survive_a_clone() -> None:
    """Git does not track empty directories, so the ones that must exist after
    someone clones the repository carry a .gitkeep."""
    for folder in ("evaluations/results", "tests/fixtures", "n8n/workflows", "schemas"):
        path = REPO / folder
        assert path.is_dir(), f"{folder} is missing"
        assert any(path.iterdir()), f"{folder} is empty and will not survive a clone"


def test_the_dockerfile_installs_with_hashes_and_no_fallback() -> None:
    """A fallback would silently install an unverified dependency set the moment
    the lock file lost its hashes, which is the failure hashes exist to prevent."""
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile
    assert "|| pip install" not in dockerfile


def test_the_lock_target_generates_hashes() -> None:
    """The Dockerfile refuses a lock file without them, so a lock command that
    omits them produces an image that cannot be built."""
    makefile = (REPO / "Makefile").read_text()
    assert makefile.count("--generate-hashes") >= 2


def test_published_ports_are_bound_to_loopback() -> None:
    """Without an address Docker publishes on every interface, so a laptop on a
    shared network exposes the database."""
    compose = (REPO / "docker-compose.yml").read_text()
    for line in compose.splitlines():
        stripped = line.strip()
        if stripped.startswith('- "') and ":" in stripped and "CMD" not in stripped:
            assert "127.0.0.1:" in stripped, f"port published on all interfaces: {stripped}"


def test_the_secret_scanner_exempts_no_paths() -> None:
    """Exempting docs/, tests/fixtures/ or .env.example would mean a real
    credential pasted into any of them is never seen."""
    config = tomllib.loads((REPO / ".gitleaks.toml").read_text())
    assert not config["allowlist"].get("paths"), "path exemptions are too broad"
    assert config["allowlist"]["regexes"], "nothing is allowlisted by value"


def test_env_example_holds_no_real_looking_credential() -> None:
    """Every placeholder is <<UPPER_SNAKE_CASE>>, so nothing in the committed
    example looks like a credential to a scanner or to a reader."""
    import re

    for line in (REPO / ".env.example").read_text().splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].split("#")[0].strip()
        if not value:
            continue
        assert not re.search(r"sk-ant-[A-Za-z0-9]{8,}", value)
        assert not re.search(r"xox[baprs]-[A-Za-z0-9]{8,}", value)


def test_the_service_never_receives_the_owner_or_bootstrap_credential() -> None:
    compose = (REPO / "docker-compose.yml").read_text()
    service = compose.split("policy_service:", 1)[1]
    for forbidden in (
        "BPR_OWNER_PASSWORD",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "BPR_OWNER_DATABASE_URL",
    ):
        assert forbidden not in service, f"{forbidden} reaches the service"


def test_ci_declares_no_third_party_secret() -> None:
    """If a change ever makes CI require a Xero, Slack or Anthropic credential,
    that change is wrong: a fork could not run it."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    for forbidden in ("XERO_CLIENT_SECRET", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY"):
        assert forbidden not in workflow


def test_committed_workflow_exports_are_valid_json() -> None:
    for path in (REPO / "n8n" / "workflows").glob("*.json"):
        json.loads(path.read_text())
