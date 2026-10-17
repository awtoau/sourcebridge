from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sourcebridge.toolchains import (
    ToolchainReportError,
    report_toolchains,
    validate_toolchain_report,
)


def _report(*, proposals: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "report_type": "sourcebridge.toolchain_currency",
        "pack": {"id": "sourcebridge-csharp", "kind": "frontend"},
        "observations": {
            "installed": {"dotnet": {"version": "9.0.121"}},
            "declared": {"packages": [{"id": "Roslyn", "version": "4.11.0"}]},
            "resolved": {"packages": []},
            "latest": {"packages": []},
        },
        "conversion_capability": {
            "status": "not_claimed",
            "reason": "currency observations are not conversion evidence",
        },
        "upgrade_proposals": proposals or [],
    }


class ToolchainReportTests(unittest.TestCase):
    def test_report_is_machine_readable_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            path = root / "evidence" / "toolchain-report.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(_report(), sort_keys=True), encoding="utf-8")

            first = report_toolchains([root])
            second = report_toolchains([root])

        self.assertEqual(first, second)
        self.assertEqual(first["reports"][0]["report"]["pack"]["id"], "sourcebridge-csharp")
        self.assertEqual(first["upgrade_proposals"], [])

    def test_upgrade_requires_compatibility_validation_and_decision_evidence(self) -> None:
        proposal = {
            "id": "roslyn-411-to-414",
            "component": "Microsoft.CodeAnalysis.CSharp.Workspaces",
            "from": "4.11.0",
            "to": "4.14.0",
            "compatibility_evidence": [{"reference": "roslyn-api-review-001"}],
            "validation": {"command": "dotnet test", "result": "not run"},
            "decision": {"status": "defer", "evidence": "review-2026-10-17"},
        }
        validate_toolchain_report(_report(proposals=[proposal]))

        proposal.pop("decision")
        with self.assertRaisesRegex(ToolchainReportError, "decision"):
            validate_toolchain_report(_report(proposals=[proposal]))

    def test_conversion_capability_must_not_be_inferred(self) -> None:
        report = _report()
        report["conversion_capability"] = {"status": "supported"}
        with self.assertRaisesRegex(ToolchainReportError, "not_claimed"):
            validate_toolchain_report(report)

    def test_schema_version_rejects_boolean_values(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                report = _report()
                report["schema_version"] = value
                with self.assertRaisesRegex(ToolchainReportError, "schema_version"):
                    validate_toolchain_report(report)
