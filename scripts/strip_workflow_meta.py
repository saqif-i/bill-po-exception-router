#!/usr/bin/env python3
"""Remove instance-specific noise from n8n workflow exports.

n8n writes `meta.instanceId` into every export: a 64-character hex derived from
that instance's encryption key. It is not a credential, but it identifies your
instance, it differs for every person who imports the workflow, and it changes
the diff on every re-export for no reason.

Run this after downloading a workflow and before committing it.

    python scripts/strip_workflow_meta.py
"""

from __future__ import annotations

import json
import pathlib

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / "n8n" / "workflows"

# Written by n8n on export, meaningless to anyone else, and not worth committing.
STRIP_FROM_META = ("instanceId", "templateCredsSetupCompleted")

# Instance-specific top-level keys. `id` and `versionId` change on every import,
# so committing them makes every re-export look like a change.
STRIP_TOP_LEVEL = (
    "id",
    "versionId",
    "createdAt",
    "updatedAt",
    "active",
    "triggerCount",
    "shared",
    "staticData",
)


def main() -> int:
    changed = 0
    for path in sorted(WORKFLOWS.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        before = json.dumps(document, sort_keys=True)

        for key in STRIP_TOP_LEVEL:
            document.pop(key, None)

        meta = document.get("meta")
        if isinstance(meta, dict):
            for key in STRIP_FROM_META:
                meta.pop(key, None)
            if not meta:
                document.pop("meta", None)

        if json.dumps(document, sort_keys=True) != before:
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            print(f"  stripped {path.name}")
            changed += 1

    print(f"checked {len(list(WORKFLOWS.glob('*.json')))} export(s), {changed} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
