#!/usr/bin/env python3
"""Fail unless the integration tests ran, and none of them was skipped.

Every integration module skips itself when no database is configured, and in a
CI log a skipped test reads very like a passed one. Counting collected tests
does not help: a skipped test is still collected. So this reads the JUnit
report the pytest step wrote, and fails if no integration test ran or any was
skipped.

    python scripts/assert_integration_ran.py pytest-report.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

PREFIX = "tests.integration"


def main(report: str) -> int:
    # Our own pytest's output, not input from anyone else.
    cases = [
        case
        for case in ET.parse(report).iter("testcase")  # noqa: S314
        # A module skipped while it is being collected is written with an
        # empty classname and the module path as its name.
        if (case.get("classname") or "").startswith(PREFIX)
        or (case.get("name") or "").startswith(PREFIX)
    ]
    skipped = [
        f"{case.get('classname')}::{case.get('name')}"
        for case in cases
        if case.find("skipped") is not None
    ]
    if not cases:
        print("no integration test ran")
        return 1
    if skipped:
        print(f"{len(skipped)} integration test(s) skipped:")
        print("\n".join(f"  {name}" for name in skipped))
        return 1
    print(f"{len(cases)} integration tests ran, none skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "pytest-report.xml"))
