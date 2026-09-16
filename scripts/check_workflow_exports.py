#!/usr/bin/env python3
"""Check committed n8n workflow exports for embedded credentials.

This parses the JSON and checks credential patterns and sensitive fields.
Workflow references are exempt from the generic literal-value rule, but
are still checked for known secret patterns.

A file that cannot be read or parsed is a failure, not a pass.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / "n8n" / "workflows"

# Credential-shaped values, by pattern rather than by length.
PATTERNS = {
    "slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "bearer literal": re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
    "basic auth in url": re.compile(r"https?://[^/\s:]+:[^/\s@]+@"),
    "long hex secret": re.compile(r"\b[a-f0-9]{32,}\b"),
}

SENSITIVE_KEYS = {
    "value",
    "password",
    "token",
    "secret",
    "apikey",
    "api_key",
    "accesstoken",
    "authorization",
    "privatekey",
}


def walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")
    else:
        yield path, node


def main() -> int:
    if not WORKFLOWS.exists():
        print("no workflow directory, nothing to check")
        return 0

    failures: list[str] = []
    checked = 0

    for path in sorted(WORKFLOWS.glob("*.json")):
        checked += 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Unreadable or malformed exports must fail the check.
            failures.append(f"{path.name}: cannot read or parse ({exc})")
            continue

        for location, value in walk(document):
            if not isinstance(value, str) or not value:
                continue

            # Check every string, including workflow references.
            for name, pattern in PATTERNS.items():
                if pattern.search(value):
                    failures.append(f"{path.name}{location}: looks like a {name}")

            # An n8n expression starts with "=".
            # Workflow IDs are references, so exclude their specific field
            # from this generic literal-value rule.
            leaf = location.rsplit(".", 1)[-1].split("[")[0].lower()
            if (
                leaf in SENSITIVE_KEYS
                and not re.fullmatch(r"\.nodes\[\d+\]\.parameters\.workflowId\.value", location)
                and not value.startswith("=")
                and len(value) >= 16
                and " " not in value
            ):
                failures.append(
                    f"{path.name}{location}: literal value under a credential-shaped key"
                )

    if failures:
        print(f"checked {checked} export(s), found problems:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"checked {checked} workflow export(s), no embedded credentials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
