"""Prototype diagnostic drill-down and append-only review checks."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from sourcebridge.agent_queue import record_diagnostic_review, record_run_provenance
from sourcebridge.cli import main
from sourcebridge.pipeline import initialize
from sourcebridge.reporting import (
    diagnostic_drilldown,
    diagnostic_review_history,
)
from sourcebridge.database import write_transaction


class DiagnosticQueueTests(unittest.TestCase):
    def _database(self, temporary: str) -> Path:
        database = Path(temporary) / "evidence.db"
        initialize(database)
        with write_transaction(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    VALUES ('c', 'fixture', 'source-r1', NULL, 'corpus-digest', 't');
                INSERT INTO sourcebridge_variant
                    VALUES ('v', 'c', 'frontend', 'compiler-1', 'x86_64-linux', 64,
                            'little', 'variant-config', NULL, 't');
                INSERT INTO sourcebridge_profile
                    VALUES ('p', 'fixture-profile', 'frontend', NULL, 'backend',
                            'rules-r1', 'runtime-r1', 'options-r1', 't');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('u', 'v', 'Example', 'example.src', '1:1-1:9',
                            'source-unit-digest', 'function');
                INSERT INTO sourcebridge_component_decision
                    VALUES ('d', 'p', 'u', 'blocked', 'backend', NULL, NULL,
                            'Needs review.', 'tester', 't');
                INSERT INTO sourcebridge_generated_artifact
                    VALUES ('a', 'd', 'example.target', 'artifact-digest',
                            'generator-digest', 't');
                INSERT INTO sourcebridge_validation_result
                    VALUES ('validation-1', 'd', 'target', 'fixture-validator', 'failed',
                            'evidence/target.log', 'validation-digest',
                            '{"exit_code":1}', 't');
                INSERT INTO sourcebridge_evidence_fragment
                    VALUES ('source-fragment', 'c', 'source', 'example.src', 1, 1, 1, 9,
                            'Example();', 'source-fragment-digest', 't');
                INSERT INTO sourcebridge_evidence_fragment
                    VALUES ('generated-fragment', 'c', 'generated', 'example.target',
                            8, 1, 8, 20, 'unimplemented!();',
                            'generated-fragment-digest', 't');
                INSERT INTO sourcebridge_diagnostic_occurrence
                    VALUES ('diagnostic-1', 'validation-1', 'u', 'source-fragment',
                            'a', 'generated-fragment', 'generation', 'fixture-tool',
                            'SB001', 'error', 'example:unsupported',
                            'cannot lower Example', '{"token":"Example"}', 't');
                INSERT INTO sourcebridge_execution_run
                    VALUES ('run-1', 'p', 'v', 'analyze', 1, 'failed',
                            '{"argv":["fixture"]}', 'tester', 't', 't', 't');
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
            recorded_at="t",
        )
        return database

    def test_happy_path_drilldown_and_review_creates_stable_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)

            report = diagnostic_drilldown(database, "example:unsupported")
            result = record_diagnostic_review(
                database,
                "diagnostic-1",
                disposition="known_converter_gap",
                rationale="The target emitter has no bounded mapping.",
                reviewed_by="reviewer",
                create_work_item=True,
                run_id="run-1",
                recorded_at="2026-10-17T01:00:00+00:00",
            )
            repeat = record_diagnostic_review(
                database,
                "diagnostic-1",
                disposition="needs_investigation",
                rationale="Keep the gap visible while the mapping is assessed.",
                reviewed_by="reviewer",
                create_work_item=True,
                run_id="run-1",
                recorded_at="2026-10-17T02:00:00+00:00",
            )
            history = diagnostic_review_history(database, "diagnostic-1")

        occurrence = report["occurrences"][0]
        self.assertEqual(report["summary"]["occurrence_count"], 1)
        self.assertEqual(occurrence["source"]["excerpt"]["content"], "Example();")
        self.assertEqual(
            occurrence["generated"]["excerpt"]["content"], "unimplemented!();"
        )
        self.assertEqual(occurrence["provenance"]["source_revision"], "source-r1")
        self.assertEqual(
            occurrence["provenance"]["profile"]["rule_layers_digest"], "rules-r1"
        )
        self.assertEqual(
            occurrence["generated"]["artifact"]["generator_digest"],
            "generator-digest",
        )
        self.assertEqual(occurrence["validation"]["evidence_path"], "evidence/target.log")
        self.assertEqual(result["work_item"]["created"], True)
        self.assertEqual(result["evidence_link"]["execution_run_id"], "run-1")
        self.assertEqual(result["work_item"]["id"], repeat["work_item"]["id"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["disposition"], "needs_investigation")

    def test_unknown_signature_is_empty_and_cli_reports_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)
            report = diagnostic_drilldown(database, "not-present")
            stdout = io.StringIO()
            previous_argv = sys.argv
            with redirect_stdout(stdout):
                try:
                    sys.argv = [
                        "sourcebridge",
                        "report-diagnostic",
                        "--database",
                        str(database),
                        "not-present",
                    ]
                    result = main()
                finally:
                    sys.argv = previous_argv

        self.assertEqual(report["occurrences"], [])
        self.assertEqual(report["summary"]["occurrence_count"], 0)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["occurrences"], [])

    def test_review_rows_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(temporary)
            record_diagnostic_review(
                database,
                "diagnostic-1",
                disposition="accepted_source_behavior",
                rationale="Fixture is intentionally unsupported.",
                reviewed_by="reviewer",
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "diagnostic_review is append-only"
            ):
                with write_transaction(database) as connection:
                    connection.execute(
                        """
                        UPDATE sourcebridge_diagnostic_review
                        SET rationale = 'changed'
                        WHERE diagnostic_occurrence_id = 'diagnostic-1'
                        """
                    )


if __name__ == "__main__":
    unittest.main()
