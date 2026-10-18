#!/usr/bin/env python3
"""Canonical agent-facing Sourcebridge development interface."""

from __future__ import annotations

import json
import subprocess
import sys


_COMMANDS = {
    "describe": "machine-readable approved agent command registry",
    "process-change": "open a validated port-process-change proposal",
    "select-next-rule": "select one evidence-backed rule work item",
    "rule-runtime": "run a declared rule runtime through its contract",
    "validation": "run declared source/target/oracle validation",
    "census": "report persisted emitted-artifact outcomes",
    "diagnostics": "report grouped compiler and conversion diagnostics",
    "diagnostic": "report one diagnostic signature",
    "packs": "report committed evidence across pack repositories",
    "toolchains": "report declared and observed toolchain evidence",
    "work-queue": "report current evidence-backed work items",
    "work-item": "report one work item's immutable evidence",
    "changes": "report changes since a completed run",
    "staleness": "report stale rule/profile/toolchain evidence",
    "tests": "list registered pack engine tests",
}

_FORWARD = {
    "census": "report-conversion-census",
    "diagnostics": "report-diagnostics",
    "diagnostic": "report-diagnostic",
    "packs": "report-packs",
    "toolchains": "report-toolchains",
    "work-queue": "report-work-queue",
    "work-item": "report-work-item",
    "changes": "report-changes",
    "staleness": "report-staleness",
    "tests": "test",
}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] == "describe":
        print(json.dumps({"schema_version": 1, "commands": _COMMANDS}, indent=2, sort_keys=True))
        return 0
    command = arguments[0]
    if command not in _COMMANDS:
        print(
            f"unapproved agent command `{command}`; run `python3 dev.py describe`",
            file=sys.stderr,
        )
        return 2
    result = subprocess.run(
        [sys.executable, "-m", "sourcebridge.cli", _FORWARD.get(command, command), *arguments[1:]],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
