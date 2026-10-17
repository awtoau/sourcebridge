"""Minimal pack-owned adapter fixture for the rule-runtime contract."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
replacement = next(
    rule for rule in request["rules"]
    if rule["directive"] == "replace"
)
configuration = next(
    rule["configuration"]
    for rule in request["rules"]
    if rule["directive"] == "configure"
)
if replacement["implementation"]["path"] != "rules/project_logging.py":
    raise SystemExit("project replacement was not selected")
if configuration != {"receiver": "machine"}:
    raise SystemExit("project configuration was not passed")
if request["settings"] != [
    {
        "key": "runtime_adapter",
        "schema_version": 1,
        "value": {"id": "fixture-runtime-v1"},
        "rationale": "Use the fixture runtime adapter.",
    }
]:
    raise SystemExit("resolved settings were not passed")

print(
    json.dumps(
        {
            "status": "passed",
            "used_rule": replacement["implementation"]["path"],
            "configuration": configuration,
            "input": request["input"],
            "profile_digest": request["profile"]["resolved_digest"],
        }
    )
)
