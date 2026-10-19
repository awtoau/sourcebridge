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


class EngineTestOutcomeTests(unittest.TestCase):
    def _envelope(self, target: str, extraction: str = "passed", oracle: str = "skipped"):
        return {
            "schema_version": 1,
            "source": {"status": "passed"},
            "extraction": {"status": extraction, "diagnostic": "unsupported primitive"},
            "target": {"status": target, "reason": "unsupported fact"},
            "oracle": {"status": oracle},
        }

    def _record(self, database: Path, test_id: int, rule: str, outcome: dict) -> dict:
        from sourcebridge.conversion_census import record_engine_test_outcome

        return record_engine_test_outcome(
            database,
            pack_id="sourcebridge-delphi",
            profile_name="delphi-to-rust",
            source_frontend_pack_id="sourcebridge-delphi",
            target_backend_pack_id="sourcebridge-rust",
            source_language="delphi",
            fixture_path=f"fixtures/f{test_id}.pas",
            fixture_digest="d" * 64,
            rule_key=rule,
            test_id=test_id,
            logical_name=rule,
            adapter_result=outcome,
            variant={"compiler": {"target_cpu": "x86_64", "target_os": "linux"}},
            generated_artifact_content="pub fn f() {}" if outcome["target"]["status"] == "passed" else None,
        )

    def test_writes_real_rows_into_designed_tables(self) -> None:
        from sourcebridge.conversion_census import profile_report

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            self._record(database, 1, "rule.validated", self._envelope("passed", oracle="passed"))
            self._record(database, 2, "rule.partial", self._envelope("passed"))
            self._record(database, 3, "rule.blocked", self._envelope("blocked"))
            self._record(database, 4, "rule.unimplemented", self._envelope("blocked", extraction="failed"))
            with sqlite3.connect(database) as connection:
                artifacts = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_generated_artifact"
                ).fetchone()[0]
                validations = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_validation_result"
                ).fetchone()[0]
                extraction_gaps = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_diagnostic_occurrence WHERE phase='extraction'"
                ).fetchone()[0]
            report = profile_report(database, profile_name="delphi-to-rust")

        self.assertEqual(artifacts, 2)
        self.assertEqual(validations, 12)
        self.assertEqual(extraction_gaps, 1)
        self.assertEqual(report["declared_files"], 4)
        self.assertEqual(report["coverage"]["target"], {"passed": 2, "denominator": 4, "percent": 50.0})
        self.assertEqual(
            report["lifecycle_counts"],
            {"blocked": 1, "partial": 1, "unimplemented": 1, "validated": 1},
        )


if __name__ == "__main__":
    unittest.main()


class CatalogueCensusDriverTests(unittest.TestCase):
    def test_profile_driven_driver_records_designed_tables(self) -> None:
        import sys as _sys
        from sourcebridge.conversion_census import record_catalogue_census

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "f.pas"
            fixture.write_text("program F; begin end.", encoding="utf-8")
            adapter = root / "adapter.py"
            adapter.write_text(
                "import json\n"
                "print(json.dumps({'schema_version':1,"
                "'source':{'status':'passed'},'extraction':{'status':'passed'},"
                "'target':{'status':'passed','artifact_content':'pub fn f(){}'},"
                "'oracle':{'status':'passed'}}))\n",
                encoding="utf-8",
            )
            catalogue = root / "engine-tests.json"
            catalogue.write_text(json.dumps({
                "schema_version": 1, "pack_id": "demo-pack",
                "adapter": {"id": "json-command",
                            "config": {"argv": [_sys.executable, str(adapter)], "timeout_seconds": 5}},
                "tests": [{"id": 701, "name": "demo", "fixture": "f.pas",
                           "expected": {s: {"status": "passed"} for s in ("source","extraction","target","oracle")}}],
            }), encoding="utf-8")
            profile = root / "demo.profile.json"
            profile.write_text(json.dumps({
                "schema_version": 1, "id": "demo-to-rust",
                "source_frontend": "sourcebridge-demo", "target_backend": "sourcebridge-rust",
                "rule_layers": [], "runtime_adapters": [], "options": {"source_language": "demo"},
            }), encoding="utf-8")
            curriculum = root / "rule-curriculum.json"
            curriculum.write_text(json.dumps({"entries": [{"test_id": 701, "rule": "demo.rule"}]}), encoding="utf-8")
            database = root / "evidence.db"
            initialize(database)

            result = record_catalogue_census(
                database, profile_manifest=profile, catalogue=catalogue, curriculum=curriculum
            )
            with sqlite3.connect(database) as connection:
                validations = connection.execute("SELECT COUNT(*) FROM sourcebridge_validation_result").fetchone()[0]
                artifacts = connection.execute("SELECT COUNT(*) FROM sourcebridge_generated_artifact").fetchone()[0]

        self.assertEqual(result["profile"], "demo-to-rust")
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(validations, 3)
        self.assertEqual(artifacts, 1)
        self.assertEqual(result["report"]["lifecycle_counts"], {"validated": 1})
        self.assertEqual(result["report"]["rule_status_counts"], {"validated": 1})


class ActiveRuleResolutionTests(unittest.TestCase):
    def test_only_active_rules_are_resolved_for_emission(self) -> None:
        from sourcebridge.conversion_census import resolve_active_rules
        from sourcebridge.conversion_profiles import (
            register_rule_revision, set_rule_revision_state,
        )

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            active = register_rule_revision(
                database, rule_key="demo.active", source_frontend_pack_id="sourcebridge-demo",
                definition={"rule_key": "demo.active", "output_shape": {"target": "rust", "body": "{op0}"}},
                rationale="r", created_by="dan",
            )
            set_rule_revision_state(
                database, active["rule_revision_id"], state="active",
                rationale="go", decided_by="dan",
            )
            # A second rule stays draft and must not govern emission.
            register_rule_revision(
                database, rule_key="demo.draft", source_frontend_pack_id="sourcebridge-demo",
                definition={"rule_key": "demo.draft", "output_shape": {"target": "rust", "body": "{op0}"}},
                rationale="r", created_by="dan",
            )
            resolved = resolve_active_rules(database, "sourcebridge-demo")

        keys = [r["rule_key"] for r in resolved]
        self.assertEqual(keys, ["demo.active"])


class RuleTokenCostTests(unittest.TestCase):
    def test_tokens_attributed_to_rule_show_in_profile_report(self) -> None:
        from sourcebridge.conversion_census import profile_report, record_engine_test_outcome
        from sourcebridge.conversion_profiles import register_rule_revision
        from sourcebridge.pipeline import import_json

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "evidence.db"
            initialize(database)
            revision = register_rule_revision(
                database, rule_key="demo.rule", source_frontend_pack_id="sourcebridge-demo",
                definition={"rule_key": "demo.rule", "output_shape": {"target": "rust", "body": "{op0}"}},
                rationale="r", created_by="dan",
            )
            record_engine_test_outcome(
                database, pack_id="demo", profile_name="demo-to-rust",
                source_frontend_pack_id="sourcebridge-demo", target_backend_pack_id="sourcebridge-rust",
                source_language="demo", fixture_path="fixtures/f.pas", fixture_digest="d" * 64,
                rule_key="demo.rule", test_id=1, logical_name="f",
                adapter_result={
                    "source": {"status": "passed"}, "extraction": {"status": "passed"},
                    "target": {"status": "passed"}, "oracle": {"status": "passed"},
                },
                generated_artifact_content="pub fn f() {}",
            )
            usage = root / "agent_usage" / "u.json"
            usage.parent.mkdir(parents=True)
            usage.write_text(json.dumps({
                "kind": "agent_usage", "provider": "host", "session_id": None,
                "model_name": None, "model_digest": None,
                "input_tokens": 1000, "output_tokens": 200,
                "cache_read_tokens": None, "cache_write_tokens": None,
                "duration_ms": None, "local_only": True,
                "proposal_worker_request_id": None,
                "rule_revision_id": revision["rule_revision_id"], "work_item_id": None,
            }), encoding="utf-8")
            import_json(database, root, repository="host")
            report = profile_report(database, profile_name="demo-to-rust")

        rule = next(r for r in report["rules"] if r["rule_key"] == "demo.rule")
        self.assertTrue(rule["tokens"]["attributed"])
        self.assertEqual(rule["tokens"]["total"], 1200)
