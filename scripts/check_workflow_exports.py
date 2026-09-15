#!/usr/bin/env python3
"""Check committed n8n workflow exports for embedded credentials.

A regex over the raw JSON both over-reports and under-reports: it flags any long
`value` string, which legitimately includes URLs and expressions, and it misses a
credential stored under a key it does not know about.

So this parses the JSON and inspects the places a credential can actually land.
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
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "bearer literal": re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
    "basic auth in url": re.compile(r"https?://[^/\s:]+:[^/\s@]+@"),
    "long hex secret": re.compile(r"\b[a-f0-9]{32,}\b"),
}

# n8n stores a credential REFERENCE here, which is fine. A credential VALUE is
# not, so these keys are inspected wherever they appear.
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
            # Unreadable is a failure. Skipping it would let a corrupt or
            # deliberately malformed export through unscanned.
            failures.append(f"{path.name}: cannot read or parse ({exc})")
            continue

        for location, value in walk(document):
            if not isinstance(value, str) or not value:
                continue

            for name, pattern in PATTERNS.items():
                if pattern.search(value):
                    failures.append(f"{path.name}{location}: looks like a {name}")

            # An n8n expression starts with "=", so a LITERAL under a
            # credential-shaped key is the case worth flagging.
            leaf = location.rsplit(".", 1)[-1].split("[")[0].lower()
            if (
                leaf in SENSITIVE_KEYS
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
