"""Focused tests for read-only cross-pack committed-evidence reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sourcebridge.reporting import report_packs


class PackReportingTests(unittest.TestCase):
    def test_groups_known_artifacts_and_deduplicates_overlapping_roots(self) -> None:
        project_root = Path(__file__).parents[1]
        scratch_root = project_root / "tmp"
        scratch_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_root) as temporary:
            root = Path(temporary) / "fixture-pack"
            self._write(root / "evidence" / "unit.clang.facts.json", {
                "schema_version": 1,
                "fact_schema": "sourcebridge.c.ast-facts.v1",
            })
            self._write(root / "evidence" / "fpc-compiler-tests-manifest.json", {
                "schema_version": 1, "source_name": "fpc-tests",
            })
            self._write(root / "evidence" / "fpc-compiler-tests-quick-run.json", {
                "schema_version": 1, "run": {"summary": {"total": 1}},
            })
            self._write(root / "oracle" / "traces" / "manifest.json", {
                "peripherals": {"timer": {"accesses": 1}},
            })
            self._write(root / "docs" / "status" / "compile_clean_set.json", {
                "min_modules": 1, "modules": ["timer"],
            })
            self._write(root / "docs" / "status" / "compile_baseline.json", {
                "errors": 99,
            })
            proposal = {
                "payload_sha256": "a" * 64,
                "proposal": {},
                "raw_response": {},
                "model": {},
                "generation_options": {},
            }
            self._write(root / "evidence" / "proposals" / "staged.json", proposal)
            self._write(root / "evidence" / "proposals" / "rejected" / "bad.json", proposal)
            self._write(root / "evidence" / "unknown.json", {"unexpected": True})

            report = report_packs([root / "evidence", root])

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(len(report["fact_extraction"]), 1)
        self.assertEqual(len(report["source_validation"]), 2)
        self.assertEqual(len(report["target_emission_oracle"]), 1)
        self.assertEqual(len(report["compiler_metrics"]), 1)
        self.assertEqual(
            [item["kind"] for item in report["ollama_proposals"]],
            ["ollama_rejected_proposal", "ollama_staged_proposal"],
        )
        unrecognized = {item["path"]: item for item in report["unrecognized_artifacts"]}
        self.assertEqual(unrecognized["docs/status/compile_baseline.json"]["kind"],
                         "historical_compile_baseline")
        fact = report["fact_extraction"][0]
        self.assertEqual(fact["repository"], "fixture-pack")
        self.assertEqual(fact["path"], "evidence/unit.clang.facts.json")
        self.assertEqual(fact["sha256"], hashlib.sha256(
            json.dumps({"schema_version": 1, "fact_schema": "sourcebridge.c.ast-facts.v1"},
                       indent=2, sort_keys=True).encode("utf-8") + b"\n").hexdigest())
        self.assertEqual(fact["schema_version"], 1)

    @staticmethod
    def _write(path: Path, record: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
