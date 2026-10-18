from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from sourcebridge.conversion_census import ConversionCensusError, record_outcome, report
from sourcebridge.cli import main
from sourcebridge.pipeline import initialize


class ConversionCensusTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        database = root / "evidence.db"
        initialize(database)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    VALUES ('corpus', 'fixture', 'r1', NULL, 'manifest', 'now');
                INSERT INTO sourcebridge_variant
                    VALUES ('variant', 'corpus', 'frontend', NULL, NULL, NULL, NULL,
                            'configuration', NULL, 'now');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('unit', 'variant', 'Fixture', 'fixture.pas', '1:1-1:20',
                            'source-digest', 'program');
                INSERT INTO sourcebridge_profile
                    VALUES ('profile', 'fixture-to-target', 'frontend', NULL, 'backend',
                            'rules', 'runtime', 'options', 'now');
                INSERT INTO sourcebridge_component_decision
                    VALUES ('decision', 'profile', 'unit', 'direct_translation', 'backend',
                            NULL, NULL, 'fixture', 'test', 'now');
                """
            )
        return database

    def _validations(self) -> list[dict[str, object]]:
        return [
            {
                "side": "source",
                "status": "passed",
                "validator": "fpc",
                "evidence_path": "evidence/source.log",
                "evidence_digest": "source-evidence",
            },
            {
                "side": "target",
                "status": "blocked",
                "validator": "rustc",
                "evidence_path": "evidence/target.log",
                "evidence_digest": "target-evidence",
            },
            {
                "side": "oracle",
                "status": "skipped",
                "validator": "output-compare",
                "evidence_path": "evidence/oracle.log",
                "evidence_digest": "oracle-evidence",
            },
        ]

    def test_records_generated_source_validations_and_gap_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            first = record_outcome(
                database,
                decision_id="decision",
                artifact_path="generated/fixture.rs",
                artifact_content="// generated\nfn main() {}\n",
                generator_digest="generator-v1",
                validations=self._validations(),
                gaps=[
                    {
                        "source_unit_id": "unit",
                        "tool_identity": "fixture-emitter",
                        "failure_signature": "unsupported:fixture-operation",
                        "message": "fixture operation is unsupported",
                    }
                ],
            )
            second = record_outcome(
                database,
                decision_id="decision",
                artifact_path="generated/fixture.rs",
                artifact_content="// generated\nfn main() {}\n",
                generator_digest="generator-v1",
                validations=self._validations(),
                gaps=[
                    {
                        "source_unit_id": "unit",
                        "tool_identity": "fixture-emitter",
                        "failure_signature": "unsupported:fixture-operation",
                        "message": "fixture operation is unsupported",
                    }
                ],
            )
            with sqlite3.connect(database) as connection:
                fragment = connection.execute(
                    "SELECT kind, logical_path, content FROM sourcebridge_evidence_fragment"
                ).fetchone()
                diagnostics = connection.execute(
                    "SELECT validation_result_id, generated_fragment_id "
                    "FROM sourcebridge_diagnostic_occurrence"
                ).fetchall()
                target_validation_id = connection.execute(
                    "SELECT id FROM sourcebridge_validation_result WHERE side = 'target'"
                ).fetchone()[0]
            census = report(database)

        self.assertEqual(first, second)
        self.assertEqual(fragment, ("generated", "generated/fixture.rs", "// generated\nfn main() {}\n"))
        self.assertEqual(diagnostics, [(target_validation_id, first["generated_fragment_id"])])
        self.assertEqual(census["source_units"], 1)
        self.assertEqual(census["generated_artifacts"], 1)
        self.assertEqual(census["validation_outcomes"], {
            "oracle:skipped": 1,
            "source:passed": 1,
            "target:blocked": 1,
        })
        self.assertEqual(census["gaps"], [{
            "failure_signature": "unsupported:fixture-operation",
            "phase": "generation",
            "severity": "error",
            "count": 1,
        }])

    def test_rejects_gap_without_target_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            with self.assertRaisesRegex(
                ConversionCensusError,
                "explicit gaps require a target validation result",
            ):
                record_outcome(
                    database,
                    decision_id="decision",
                    artifact_path="generated/fixture.rs",
                    artifact_content="fn main() {}",
                    generator_digest="generator-v1",
                    validations=[self._validations()[0]],
                    gaps=[{
                        "source_unit_id": "unit",
                        "tool_identity": "fixture-emitter",
                        "failure_signature": "unsupported:fixture-operation",
                        "message": "fixture operation is unsupported",
                    }],
                )

    def test_rejects_gap_for_a_different_source_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_source_unit
                        VALUES ('other-unit', 'variant', 'Other', 'other.pas', NULL,
                                'other-digest', 'program')
                    """
                )
            with self.assertRaisesRegex(
                ConversionCensusError,
                "gap source unit must match",
            ):
                record_outcome(
                    database,
                    decision_id="decision",
                    artifact_path="generated/fixture.rs",
                    artifact_content="fn main() {}",
                    generator_digest="generator-v1",
                    validations=self._validations(),
                    gaps=[{
                        "source_unit_id": "other-unit",
                        "tool_identity": "fixture-emitter",
                        "failure_signature": "unsupported:fixture-operation",
                        "message": "fixture operation is unsupported",
                    }],
                )

    def test_cli_reports_persisted_census(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            record_outcome(
                database,
                decision_id="decision",
                artifact_path="generated/fixture.rs",
                artifact_content="fn main() {}",
                generator_digest="generator-v1",
                validations=self._validations(),
            )
            output = StringIO()
            with redirect_stdout(output):
                with mock.patch(
                    "sys.argv",
                    ["sourcebridge", "report-conversion-census", "--database", str(database)],
                ):
                    exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["generated_artifacts"], 1)


if __name__ == "__main__":
    unittest.main()
