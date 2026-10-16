"""Tests for grouped and occurrence-level evidence reporting."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
import unittest

from sourcebridge.reporting import diagnostic_groups, diagnostic_occurrences, execution_activity


class ReportingTests(unittest.TestCase):
    def test_group_drills_into_source_and_generated_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            schema = (Path(__file__).parents[1] / "core" / "schema" / "evidence.sql").read_text()
            connection = sqlite3.connect(database)
            connection.executescript(schema)
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus VALUES ('c', 'fixture', 'r1', NULL, 'd', 't');
                INSERT INTO sourcebridge_variant VALUES ('v', 'c', 'frontend', NULL, NULL, NULL, NULL, 'd', NULL, 't');
                INSERT INTO sourcebridge_profile VALUES ('p', 'profile', 'frontend', NULL, 'backend', 'd', 'd', 'd', 't');
                INSERT INTO sourcebridge_source_unit VALUES ('u', 'v', 'Setter', 'setter.pas', '2:1-2:12', 'd', 'method');
                INSERT INTO sourcebridge_component_decision VALUES ('d', 'p', 'u', 'direct_translation', 'backend', NULL, NULL, 'test', 'tester', 't');
                INSERT INTO sourcebridge_generated_artifact VALUES ('a', 'd', 'setter.rs', 'd', 'g', 't');
                INSERT INTO sourcebridge_validation_result VALUES ('r', 'd', 'target', 'rustc', 'failed', 'log.txt', 'd', '{}', 't');
                INSERT INTO sourcebridge_evidence_fragment VALUES ('fs', 'c', 'source', 'setter.pas', 2, 1, 2, 12, 'SetBits(9, 1)', 'd', 't');
                INSERT INTO sourcebridge_evidence_fragment VALUES ('fg', 'c', 'generated', 'setter.rs', 8, 1, 8, 20, 'unimplemented!()', 'd2', 't');
                INSERT INTO sourcebridge_diagnostic_occurrence
                    VALUES ('x', 'r', 'u', 'fs', 'a', 'fg', 'generation', 'emitter', 'SB001', 'error',
                            'setter:unsupported-bit-range', 'cannot lower bit range', '{}', 't');
                """
            )
            connection.close()
            groups = diagnostic_groups(database)
            occurrences = diagnostic_occurrences(database, "setter:unsupported-bit-range")
            activity = execution_activity(database)
        self.assertEqual(groups[0]["occurrence_count"], 1)
        self.assertEqual(occurrences[0]["source_excerpt"], "SetBits(9, 1)")
        self.assertEqual(occurrences[0]["generated_excerpt"], "unimplemented!()")
        self.assertEqual(occurrences[0]["evidence_path"], "log.txt")
        self.assertEqual(activity, [])

    def test_execution_activity_includes_run_and_worker_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            schema = (Path(__file__).parents[1] / "core" / "schema" / "evidence.sql").read_text()
            connection = sqlite3.connect(database)
            connection.executescript(schema)
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus VALUES ('c', 'fixture', 'r1', NULL, 'd', 't');
                INSERT INTO sourcebridge_variant VALUES ('v', 'c', 'frontend', NULL, NULL, NULL, NULL, 'd', NULL, 't');
                INSERT INTO sourcebridge_profile VALUES ('p', 'profile', 'frontend', NULL, 'backend', 'd', 'd', 'd', 't');
                INSERT INTO sourcebridge_execution_run VALUES
                    ('run', 'p', 'v', 'analyze', 1, 'passed', '{"argv":["tool"]}', 'tester', 't', 't', 't');
                INSERT INTO sourcebridge_execution_worker VALUES
                    ('worker', 'run', 0, 'completed', NULL, NULL, 1, 't');
                INSERT INTO sourcebridge_execution_event VALUES
                    ('run-event', 'run', NULL, '2026-10-16T00:00:00+00:00', 'progress', 'starting', '{}'),
                    ('worker-event', 'run', 'worker', '2026-10-16T00:01:00+00:00', 'completed', 'done', '{}');
                """
            )
            connection.close()
            activity = execution_activity(database)

        self.assertEqual(
            [(item["worker_slot"], item["event_kind"], item["message"]) for item in activity],
            [(None, "progress", "starting"), (0, "completed", "done")],
        )
