"""Focused fixtures for the machine-readable agent diagnosis queue."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from sourcebridge.agent_queue import (
    changes_since,
    current_work_queue,
    link_work_item_evidence,
    record_run_provenance,
    record_work_item_state,
    work_item_evidence,
)
from sourcebridge.cli import main
from sourcebridge.database import write_transaction
from sourcebridge.pipeline import initialize
from sourcebridge.reporting import EvidenceReportError


class AgentQueueTests(unittest.TestCase):
    def _database(self, temporary: str) -> Path:
        database = Path(temporary) / "evidence.db"
        initialize(database)
        with write_transaction(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    VALUES ('c', 'fixture', 'source-r1', NULL, 'corpus-digest', '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_variant
                    VALUES ('v', 'c', 'frontend', 'compiler', 'x86_64-linux', 64, 'little',
                            'variant-digest', NULL, '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_profile
                    VALUES ('p', 'fixture-profile', 'frontend', NULL, 'backend',
                            'rules-r1', 'runtime-r1', 'options-r1',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('u', 'v', 'Example', 'example.src', '1:1-3:1',
                            'source-unit-digest', 'function');
                INSERT INTO sourcebridge_component_decision
                    VALUES ('d', 'p', 'u', 'blocked', 'backend', NULL, NULL,
                            'Needs a bounded converter rule.', 'reviewer',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_work_item
                    VALUES ('wi-1', 'd', 'converter-gap', 'blocked',
                            'https://example.invalid/21',
                            'Handle Example safely', '2026-10-17T00:00:00+00:00',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_validation_result
                    VALUES ('validation-1', 'd', 'target', 'fixture-validator', 'failed',
                            'evidence/target.log', 'validation-digest', '{}',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_evidence_fragment
                    VALUES ('fragment-1', 'c', 'source', 'example.src', 1, 1, 3, 1,
                            'Example();', 'fragment-digest',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_diagnostic_occurrence
                    VALUES ('diagnostic-1', 'validation-1', 'u', 'fragment-1', NULL, NULL,
                            'rule', 'fixture-tool', 'SB021', 'error',
                            'example:unsupported', 'cannot lower Example', '{}',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_execution_run
                    VALUES ('run-1', 'p', 'v', 'analyze', 1, 'passed',
                            '{"argv":["fixture"]}', 'agent-a',
                            '2026-10-17T00:00:00+00:00',
                            '2026-10-17T01:00:00+00:00',
                            '2026-10-17T00:00:00+00:00');
                INSERT INTO sourcebridge_execution_run
                    VALUES ('run-2', 'p', 'v', 'analyze', 1, 'passed',
                            '{"argv":["fixture"]}', 'agent-b',
                            '2026-10-17T02:00:00+00:00',
                            '2026-10-17T03:00:00+00:00',
                            '2026-10-17T02:00:00+00:00');
                """
            )
        record_run_provenance(
            database,
            "run-1",
            source_revision="source-r1",
            input_digest="input-r1",
            variant_digest="variant-r1",
            profile_digest="profile-r1",
            rule_digest="rule-r1",
            validation_state="complete",
            artifact_digest="artifact-r1",
            evidence_digest="evidence-r1",
            recorded_at="2026-10-17T01:00:00+00:00",
        )
        record_run_provenance(
            database,
            "run-2",
            source_revision="source-r1",
            input_digest="input-r2",
            variant_digest="variant-r1",
            profile_digest="profile-r2",
            rule_digest="rule-r2",
            validation_state="complete",
            artifact_digest="artifact-r2",
            evidence_digest="evidence-r2",
            recorded_at="2026-10-17T03:00:00+00:00",
        )
        record_work_item_state(
            database,
            "wi-1",
            status="open",
            owner="agent-a",
            ownership_scope="source-unit:u",
            disposition="known_converter_gap",
            rationale="Initial bounded claim.",
            recorded_at="2026-10-17T01:00:00+00:00",
        )
        record_work_item_state(
            database,
            "wi-1",
            status="blocked",
            owner="agent-b",
            ownership_scope="source-unit:u",
            disposition="needs_investigation",
            rationale="The second run still needs a rule decision.",
            recorded_at="2026-10-17T03:00:00+00:00",
        )
        link_work_item_evidence(
            database,
            "wi-1",
            "run-1",
            relation="primary-diagnostic",
            diagnostic_occurrence_id="diagnostic-1",
            recorded_at="2026-10-17T01:00:00+00:00",
        )
        link_work_item_evidence(
            database,
            "wi-1",
            "run-2",
            relation="primary-validation",
            validation_result_id="validation-1",
            evidence_fragment_id="fragment-1",
            recorded_at="2026-10-17T03:00:00+00:00",
        )
        return database

    def test_queue_evidence_and_changes_are_run_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)

            queue = current_work_queue(database)
            evidence = work_item_evidence(database, "wi-1")
            changes = changes_since(database, "run-1")

        self.assertEqual(queue["run"]["id"], "run-2")
        self.assertEqual(queue["count"], 1)
        self.assertEqual(queue["items"][0]["stable_key"], "wi-1")
        self.assertEqual(queue["items"][0]["status"], "blocked")
        self.assertEqual(queue["items"][0]["owner"], "agent-b")
        self.assertEqual(queue["items"][0]["evidence"]["validation_count"], 1)
        self.assertEqual(evidence["provenance"]["source_unit"]["source_path"], "example.src")
        self.assertEqual(
            evidence["work_item"]["evidence_details"][0]["validation"]["evidence_path"],
            "evidence/target.log",
        )
        self.assertEqual(changes["staleness"]["inputs_changed"], True)
        self.assertEqual(len(changes["items"]["changed"]), 1)
        self.assertEqual(changes["items"]["changed"][0]["stable_key"], "wi-1")

    def test_cli_queue_is_compact_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)
            stdout = io.StringIO()
            previous_argv = sys.argv
            with redirect_stdout(stdout):
                try:
                    sys.argv = [
                        "sourcebridge",
                        "report-work-queue",
                        "--database",
                        str(database),
                    ]
                    result = main()
                finally:
                    sys.argv = previous_argv
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertNotIn("\n", stdout.getvalue().rstrip("\n"))
        self.assertEqual(json.loads(stdout.getvalue())["run"]["id"], "run-2")

    def test_stale_or_incomplete_runs_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)
            with write_transaction(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_execution_run
                        VALUES ('run-stale', 'p', 'v', 'analyze', 1, 'passed',
                                '{"argv":["fixture"]}', 'agent-c',
                                '2026-10-17T04:00:00+00:00',
                                '2026-10-17T05:00:00+00:00',
                                '2026-10-17T04:00:00+00:00')
                    """
                )
            record_run_provenance(
                database,
                "run-stale",
                input_digest="input-stale",
                variant_digest="variant-stale",
                profile_digest="profile-stale",
                rule_digest="rule-stale",
                validation_state="stale",
                artifact_digest="artifact-stale",
                evidence_digest="evidence-stale",
            )
            with self.assertRaisesRegex(EvidenceReportError, "stale evidence"):
                current_work_queue(database)

            with write_transaction(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_execution_run
                        VALUES ('run-empty', 'p', 'v', 'analyze', 1, 'passed',
                                '{"argv":["fixture"]}', 'agent-e',
                                '2026-10-17T04:00:00+00:00',
                                '2026-10-17T05:00:00+00:00',
                                '2026-10-17T04:00:00+00:00')
                    """
                )
            record_run_provenance(
                database,
                "run-empty",
                input_digest="input-empty",
                variant_digest="variant-empty",
                profile_digest="profile-empty",
                rule_digest="rule-empty",
                validation_state="complete",
                artifact_digest="artifact-empty",
                evidence_digest="evidence-empty",
            )
            with self.assertRaisesRegex(EvidenceReportError, "no immutable evidence link"):
                current_work_queue(database, run_id="run-empty")

            with write_transaction(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_execution_run
                        VALUES ('run-incomplete', 'p', 'v', 'analyze', 1, 'passed',
                                '{"argv":["fixture"]}', 'agent-d',
                                '2026-10-17T06:00:00+00:00',
                                '2026-10-17T07:00:00+00:00',
                                '2026-10-17T06:00:00+00:00')
                    """
                )
            with self.assertRaisesRegex(EvidenceReportError, "immutable run provenance"):
                current_work_queue(database, run_id="run-incomplete")
