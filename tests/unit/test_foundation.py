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


def test_every_service_call_in_bill_processing_checks_its_status() -> None:
    """With neverError set, a 409 or 503 flows on as if it were data. Each call
    must branch on statusCode, or a failure lands in Closed Without Review."""
    workflow = json.loads((REPO / "n8n" / "workflows" / "02-bill-processing.json").read_text())
    nodes = {n["name"]: n for n in workflow["nodes"]}
    for name, node in nodes.items():
        if node["type"] != "n8n-nodes-base.httpRequest":
            continue
        (target,) = [c["node"] for c in workflow["connections"][name]["main"][0]]
        condition = nodes[target]["parameters"]["conditions"]["conditions"][0]
        assert condition["leftValue"] == "={{ $json.statusCode }}", name


def test_the_notify_key_does_not_depend_on_the_previous_node() -> None:
    """After Semantic Review, $json is that node's response, which has no
    run_id. A key built from it is "undefined-notify" for every bill."""
    workflow = json.loads((REPO / "n8n" / "workflows" / "02-bill-processing.json").read_text())
    (notify,) = [n for n in workflow["nodes"] if n["name"] == "Notify"]
    (key,) = [
        h["value"]
        for h in notify["parameters"]["headerParameters"]["parameters"]
        if h["name"] == "Idempotency-Key"
    ]
    assert "$('Reconcile')" in key


def test_no_export_points_at_one_instance_sub_workflow() -> None:
    """A sub-workflow id exists only on the instance that exported it. The
    committed file carries the placeholder n8n/README.md tells you to replace."""
    for path in sorted((REPO / "n8n" / "workflows").glob("*.json")):
        for node in json.loads(path.read_text())["nodes"]:
            if node["type"] != "n8n-nodes-base.executeWorkflow":
                continue
            reference = node["parameters"]["workflowId"]
            assert reference["value"].startswith("REPLACE_WITH_"), path.name
            assert "cachedResultUrl" not in reference, path.name


def test_notify_succeeded_requires_a_card_to_exist() -> None:
    """A 200 alone is not proof a card was posted."""
    workflow = json.loads((REPO / "n8n" / "workflows" / "02-bill-processing.json").read_text())
    (node,) = [n for n in workflow["nodes"] if n["name"] == "Notify Succeeded"]
    conditions = node["parameters"]["conditions"]
    assert conditions["combinator"] == "and"
    left = [c["leftValue"] for c in conditions["conditions"]]
    assert "={{ $json.statusCode }}" in left
    assert any("body.posted" in v and "body.already" in v for v in left)


def test_failures_in_01_and_02_go_to_the_error_workflow() -> None:
    """Without it, a failed bill showed up only in n8n's execution list."""
    for name in ("01-bill-polling.json", "02-bill-processing.json"):
        workflow = json.loads((REPO / "n8n" / "workflows" / name).read_text())
        assert workflow["settings"]["errorWorkflow"] == "REPLACE_WITH_03_WORKFLOW_ID", name


def test_the_error_workflow_ends_by_alerting_someone() -> None:
    workflow = json.loads((REPO / "n8n" / "workflows" / "03-error-handler.json").read_text())
    nodes = {n["name"]: n for n in workflow["nodes"]}
    targets = {c["node"] for out in workflow["connections"].values() for c in out["main"][0]}
    (last,) = [name for name in nodes if name not in workflow["connections"]]
    assert last in targets
    parameters = nodes[last]["parameters"]
    assert nodes[last]["type"] == "n8n-nodes-base.httpRequest"
    assert parameters["url"].endswith("/alerts")
    assert any(h["name"] == "Idempotency-Key" for h in parameters["headerParameters"]["parameters"])


def test_every_failure_in_bill_processing_names_its_run() -> None:
    """Without it, an alert said a bill failed but not which one."""
    workflow = json.loads((REPO / "n8n" / "workflows" / "02-bill-processing.json").read_text())
    for node in workflow["nodes"]:
        if node["type"] == "n8n-nodes-base.stopAndError":
            message = node["parameters"]["errorMessage"]
            assert "$('Execution Input').item.json.run_id" in message, node["name"]


def test_the_alert_retries_and_its_key_survives_an_n8n_reset() -> None:
    workflow = json.loads((REPO / "n8n" / "workflows" / "03-error-handler.json").read_text())
    (alert,) = [n for n in workflow["nodes"] if n["name"] == "Alert"]
    assert alert["retryOnFail"] is True and alert["maxTries"] >= 2
    (key,) = [
        h["value"]
        for h in alert["parameters"]["headerParameters"]["parameters"]
        if h["name"] == "Idempotency-Key"
    ]
    # Execution ids restart from 1 if n8n's database is reset; the time does not.
    assert "failed_at" in key


def test_no_export_carries_pinned_data() -> None:
    """Pinned output is one instance's run ids and responses, and n8n replays it
    instead of calling the service on a manual run."""
    for path in sorted((REPO / "n8n" / "workflows").glob("*.json")):
        assert not json.loads(path.read_text()).get("pinData"), path.name
