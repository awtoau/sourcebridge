"""Minimal command adapter for the two-sided validation contract."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
if request["input"].get("case") == "unsupported":
    result = {
        "schema_version": 1,
        "source": {
            "status": "blocked",
            "classification": "unsupported",
            "evidence": {
                "path": "source/unsupported.txt",
                "raw": "fixture construct is outside this pack",
            },
            "details": {"reason": "unsupported construct"},
        },
        "target": {
            "status": "blocked",
            "evidence": {
                "path": "target/not-emitted.txt",
                "raw": "target validation was not attempted",
            },
            "details": {"reason": "source input blocked"},
        },
    }
else:
    result = {
        "schema_version": 1,
        "source": {
            "status": "passed",
            "classification": "clean",
            "evidence": {
                "path": "source/compiler.json",
                "raw": "source compiler, formatter, and analysis passed",
            },
            "details": {"baseline": "fixture-source-v1"},
        },
        "target": {
            "status": "passed",
            "evidence": {
                "path": "target/rustc.json",
                "raw": "rustc, clippy, and conformance passed",
            },
            "details": {"baseline": "fixture-target-v1"},
        },
        "oracle": {
            "status": "passed",
            "evidence": {
                "path": "oracle/trace.json",
                "raw": "source and target traces agree",
            },
            "details": {"trace": "fixture-trace-v1"},
        },
    }

print(json.dumps(result, sort_keys=True))
