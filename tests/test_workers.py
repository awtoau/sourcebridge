"""Tests for the canonical worker-default policy."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sourcebridge.cli import main
from sourcebridge.concurrency import run_contention_benchmark
from sourcebridge.workers import (
    DEFAULT_WORKERS,
    WORKER_POLICIES,
    audit_json,
)
from tools.sqlite_contention_benchmark import build_parser


class WorkerAuditTests(unittest.TestCase):
    def test_canonical_cli_emits_stable_complete_audit(self) -> None:
        output = StringIO()
        with patch.object(sys, "argv", ["sourcebridge", "audit-workers"]):
            with redirect_stdout(output):
                self.assertEqual(main(), 0)

        self.assertEqual(output.getvalue(), audit_json() + "\n")
        audit = json.loads(output.getvalue())
        self.assertEqual(audit["schema_version"], 1)
        self.assertEqual(audit["default_workers"], DEFAULT_WORKERS)
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(
            [item["operation"] for item in audit["operations"]],
            [policy.operation for policy in WORKER_POLICIES],
        )
        self.assertEqual(
            [item["operation"] for item in audit["operations"] if item["parallel_stage"]],
            ["sqlite-contention-benchmark"],
        )
        self.assertEqual(
            len(audit["exceptions"]),
            len(audit["operations"]) - 1,
        )
        for exception in audit["exceptions"]:
            self.assertTrue(exception["exception"])
            self.assertTrue(exception["reason"])
            self.assertTrue(exception["revisit_evidence"])

    def test_parallel_api_and_cli_defaults_are_explicitly_24(self) -> None:
        self.assertEqual(
            inspect.signature(run_contention_benchmark).parameters["workers"].default,
            DEFAULT_WORKERS,
        )
        workers_action = next(
            action for action in build_parser()._actions if "--workers" in action.option_strings
        )
        self.assertEqual(workers_action.default, DEFAULT_WORKERS)
        parsed = build_parser().parse_args(["--database", "evidence.db"])
        self.assertEqual(parsed.workers, DEFAULT_WORKERS)

    def test_one_worker_and_default_have_the_same_success_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            one = run_contention_benchmark(
                database,
                workers=1,
                writes_per_worker=1,
                hold_seconds=0,
            )
            default = run_contention_benchmark(
                database,
                workers=DEFAULT_WORKERS,
                writes_per_worker=1,
                hold_seconds=0,
            )

        for result in (one, default):
            self.assertEqual(result["successful_writes"], result["expected_writes"])
            self.assertEqual(result["failed_writes"], 0)
            self.assertEqual(result["row_count"], result["expected_writes"])
            self.assertEqual(result["errors"], [])
