"""Focused tests for the evidence database concurrency policy."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest

from sourcebridge.concurrency import run_contention_benchmark
from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.execution import recover_interrupted, run_approved
from sourcebridge.pipeline import initialize
from sourcebridge.reporting import EvidenceReportError, execution_activity


class SQLiteConcurrencyTests(unittest.TestCase):
    def test_initialize_enables_wal_and_readers_are_query_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                connection.close()
            with read_transaction(database) as connection:
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_parallel_writers_leave_exactly_one_complete_row_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_contention_benchmark(
                Path(temporary) / "evidence.db",
                workers=4,
                writes_per_worker=8,
                hold_seconds=0.0005,
            )

        self.assertEqual(result["failed_writes"], 0)
        self.assertEqual(result["successful_writes"], result["expected_writes"])
        self.assertEqual(result["row_count"], result["expected_writes"])
        self.assertEqual(result["errors"], [])

    def test_reader_omits_non_terminal_run_and_rejects_ambiguous_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with write_transaction(database) as connection:
                connection.executescript(
                    """
                    INSERT INTO sourcebridge_corpus
                        VALUES ('c', 'fixture', 'r1', NULL, 'd', 't');
                    INSERT INTO sourcebridge_variant
                        VALUES ('v', 'c', 'frontend', NULL, NULL, NULL, NULL, 'd', NULL, 't');
                    INSERT INTO sourcebridge_profile
                        VALUES ('p', 'profile', 'frontend', NULL, 'backend',
                                'd', 'd', 'd', 't');
                    INSERT INTO sourcebridge_execution_run
                        VALUES ('running', 'p', 'v', 'analyze', 1, 'running',
                                '{}', 'tester', 't', NULL, 't');
                    """
                )
            self.assertEqual(execution_activity(database), [])

            with write_transaction(database) as connection:
                connection.execute(
                    "UPDATE sourcebridge_execution_run "
                    "SET status = 'passed' WHERE id = 'running'"
                )
            with self.assertRaisesRegex(EvidenceReportError, "ambiguous run state"):
                execution_activity(database)

    def test_recovery_makes_interrupted_run_reportable_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with write_transaction(database) as connection:
                connection.executescript(
                    """
                    INSERT INTO sourcebridge_corpus
                        VALUES ('c', 'fixture', 'r1', NULL, 'd', 't');
                    INSERT INTO sourcebridge_variant
                        VALUES ('v', 'c', 'frontend', NULL, NULL, NULL, NULL, 'd', NULL, 't');
                    INSERT INTO sourcebridge_profile
                        VALUES ('p', 'profile', 'frontend', NULL, 'backend',
                                'd', 'd', 'd', 't');
                    INSERT INTO sourcebridge_execution_run
                        VALUES ('run', 'p', 'v', 'analyze', 1, 'running',
                                '{}', 'tester', 't', NULL, 't');
                    """
                )

            self.assertEqual(
                recover_interrupted(database, "run", reason="worker was interrupted"),
                {"run_id": "run", "status": "cancelled"},
            )
            activity = execution_activity(database)

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["run_status"], "cancelled")
        self.assertEqual(activity[0]["event_kind"], "stopped")
        self.assertEqual(activity[0]["message"], "worker was interrupted")

    def test_approved_run_is_claimed_once_and_committed_before_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with write_transaction(database) as connection:
                connection.execute(
                    "INSERT INTO sourcebridge_corpus VALUES (?, ?, ?, NULL, ?, ?)",
                    ("c", "fixture", "r1", "d", "t"),
                )
                connection.execute(
                    "INSERT INTO sourcebridge_variant "
                    "VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, ?)",
                    ("v", "c", "frontend", "d", "t"),
                )
                connection.execute(
                    "INSERT INTO sourcebridge_profile "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    ("p", "profile", "frontend", "backend", "d", "d", "d", "t"),
                )
                connection.execute(
                    "INSERT INTO sourcebridge_execution_run "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                    (
                        "run",
                        "p",
                        "v",
                        "analyze",
                        1,
                        "queued",
                        json.dumps({"argv": [sys.executable, "-c", "print('done')"]}),
                        "tester",
                        "t",
                    ),
                )

            self.assertEqual(run_approved(database, "run"), 0)
            with self.assertRaisesRegex(ValueError, "not queued"):
                run_approved(database, "run")
            activity = execution_activity(database)

        self.assertEqual(activity[0]["run_status"], "passed")
        self.assertEqual(activity[0]["event_kind"], "progress")
