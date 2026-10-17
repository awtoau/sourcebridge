"""Focused operational checks for approved-run launch, reconnect, and stop."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sourcebridge.cli import main
from sourcebridge.execution import stop_run
from sourcebridge.pipeline import initialize
from sourcebridge.reporting import execution_activity, execution_status
from sourcebridge.database import write_transaction


class ExecutionControlTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        database = root / "evidence.db"
        initialize(database)
        with write_transaction(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    VALUES ('c', 'linux', 'r1', NULL, 'manifest', 'created');
                INSERT INTO sourcebridge_variant
                    VALUES ('v', 'c', 'frontend', 'clang-18', 'x86_64',
                            64, 'little', 'config', 'generated', 'created');
                INSERT INTO sourcebridge_profile
                    VALUES ('p', 'linux-rust', 'frontend', NULL, 'backend',
                            'rules', 'adapters', 'options', 'created');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('u', 'v', 'fs/ext4/inode.c', 'fs/ext4/inode.c',
                            NULL, 'source', 'unit-digest');
                """
            )
        return database

    def _queue_run(self, database: Path) -> None:
        command = (
            "import json,time; "
            "print(json.dumps({'kind':'progress','worker_slot':0,"
            "'source_unit_id':'u','units_completed':3,"
            "'metrics':{'shard_id':'shard-0','shard_start':0,'shard_end':10}}),"
            "flush=True); time.sleep(30)"
        )
        with write_transaction(database) as connection:
            connection.execute(
                """
                INSERT INTO sourcebridge_execution_run
                    (id, profile_id, variant_id, action, requested_workers,
                     status, command_json, requested_by, started_at,
                     completed_at, created_at)
                VALUES (?, 'p', 'v', 'analyze', 1, 'queued', ?, 'test',
                        NULL, NULL, 'created')
                """,
                (
                    "run",
                    json.dumps({"argv": [sys.executable, "-c", command]}),
                ),
            )

    def _wait_for(self, database: Path, status: str) -> dict[str, object]:
        deadline = time.monotonic() + 8
        snapshot = execution_status(database, "run")
        while time.monotonic() < deadline:
            snapshot = execution_status(database, "run")
            if snapshot["status"] == status:
                return snapshot
            time.sleep(0.05)
        self.fail(f"run did not reach {status}: {snapshot}")

    def test_cli_launch_status_activity_and_controlled_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            self._queue_run(database)

            output = StringIO()
            with patch.object(
                sys,
                "argv",
                ["sourcebridge", "launch", "--database", str(database), "run"],
            ), redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "running")

            running = self._wait_for(database, "running")
            self.assertEqual(running["variant"]["id"], "v")
            self.assertEqual(running["variant"]["compiler_identity"], "clang-18")

            activity = []
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                activity = execution_activity(
                    database, "run", include_active=True
                )
                if any(item["event_kind"] == "progress" for item in activity):
                    break
                time.sleep(0.05)
            self.assertTrue(any(item["event_kind"] == "progress" for item in activity))
            running = execution_status(database, "run")
            self.assertEqual(running["shards"][0]["source_unit_id"], "u")

            activity_output = StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "sourcebridge",
                    "activity",
                    "--database",
                    str(database),
                    "--run",
                    "run",
                ],
            ), redirect_stdout(activity_output):
                self.assertEqual(main(), 0)
            self.assertTrue(
                any(
                    item["event_kind"] == "progress"
                    for item in json.loads(activity_output.getvalue())
                )
            )

            status_output = StringIO()
            with patch.object(
                sys,
                "argv",
                ["sourcebridge", "status", "--database", str(database), "run"],
            ), redirect_stdout(status_output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(status_output.getvalue())["status"], "running")

            stop_output = StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "sourcebridge",
                    "stop",
                    "--database",
                    str(database),
                    "run",
                    "--reason",
                    "prototype stop",
                ],
            ), redirect_stdout(stop_output):
                self.assertEqual(main(), 0)
            stop_result = json.loads(stop_output.getvalue())
            self.assertEqual(stop_result["status"], "stopping")
            self.assertTrue(stop_result["requested"])

            stopped = self._wait_for(database, "cancelled")
            self.assertEqual(stopped["shards"][0]["status"], "stopped")
            self.assertEqual(stopped["shards"][0]["source_unit_id"], "u")
            self.assertEqual(
                stopped["shards"][0]["latest_event"]["metrics"]["shard_id"],
                "shard-0",
            )
            activity = execution_activity(database, "run", include_active=True)
            self.assertTrue(
                any(
                    item["event_kind"] == "stopped"
                    and item["message"] == "prototype stop"
                    for item in activity
                )
            )

    def test_stop_of_queued_run_is_terminal_without_launching_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            self._queue_run(database)

            result = stop_run(database, "run", reason="not started")
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse(result["signal_sent"])
            self.assertEqual(execution_status(database, "run")["status"], "cancelled")
            self.assertEqual(execution_activity(database, "run", include_active=True)[0]["event_kind"], "stopped")
