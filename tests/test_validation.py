from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sourcebridge.cli import _validation
from sourcebridge.conversion_profiles import create_project_profile_revision
from sourcebridge.pipeline import initialize
from sourcebridge.validation import get_validation_case, run_validation


class ValidationContractTests(unittest.TestCase):
    _fixture_root = Path(__file__).parent / "fixtures" / "validation"
    _pack_manifest = _fixture_root / "sourcebridge-validation.pack.json"

    def _database(self, root: Path) -> tuple[Path, str, str]:
        database = root / "evidence.db"
        initialize(database)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    (id, source_name, revision, source_remote, manifest_digest, created_at)
                VALUES ('corpus-1', 'fixture', 'r1', NULL, 'manifest-1', 'now');
                INSERT INTO sourcebridge_variant
                    (id, corpus_id, frontend_pack_id, compiler_identity, target_abi,
                     pointer_width, byte_order, configuration_digest, generated_input_digest,
                     created_at)
                VALUES ('variant-1', 'corpus-1', 'sourcebridge-validation-fixture',
                        'fixture-compiler', NULL, NULL, NULL, 'configuration-1',
                        'input-1', 'now');
                INSERT INTO sourcebridge_source_unit
                    (id, variant_id, logical_name, source_path, source_span,
                     source_digest, kind)
                VALUES ('unit-1', 'variant-1', 'fixture', 'fixture.src', NULL,
                        'source-1', 'source');
                INSERT INTO sourcebridge_profile
                    (id, profile_name, source_frontend_pack_id, corpus_pack_id,
                     target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
                     options_digest, created_at)
                VALUES ('profile-1', 'fixture-to-rust', 'sourcebridge-validation-fixture',
                        NULL, 'sourcebridge-rust', 'rules-1', 'runtime-1', 'options-1',
                        'now');
                INSERT INTO sourcebridge_component_decision
                    (id, profile_id, source_unit_id, disposition, target_backend_pack_id,
                     runtime_adapter_id, boundary_contract_id, rationale, decided_by, created_at)
                VALUES ('decision-1', 'profile-1', 'unit-1', 'direct_translation',
                        'sourcebridge-rust', NULL, NULL, 'fixture decision', 'test', 'now');
                """
            )
        profile = create_project_profile_revision(
            database,
            project_id="fixture-project",
            profile_name="fixture-to-rust",
            base_profile_id="profile-1",
            required_validation_tier="smoke",
            toolchain_digest="toolchain-fixture-v1",
            rationale="Validation contract smoke profile.",
            created_by="test",
        )
        return database, str(profile["profile_revision_id"]), "decision-1"

    def test_passing_adapter_records_all_sides_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, profile_revision_id, decision_id = self._database(Path(temporary))
            result = run_validation(
                database,
                profile_revision_id=profile_revision_id,
                decision_id=decision_id,
                pack_manifest=self._pack_manifest,
                input_payload={"case": "passing"},
            )
            recorded = get_validation_case(database, result["id"])
            with sqlite3.connect(database) as connection:
                side_count = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_validation_result"
                ).fetchone()[0]
                provenance_count = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_conversion_provenance"
                ).fetchone()[0]

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["source_classification"], "clean")
        self.assertEqual([item["side"] for item in result["evidence"]], [
            "source",
            "target",
            "oracle",
        ])
        self.assertEqual(recorded["raw_response_digest"], result["raw_response_digest"])
        self.assertIn("source compiler", recorded["evidence"][0]["raw_evidence"])
        self.assertEqual(
            recorded["evidence"][0]["provenance"]["profile_revision_id"],
            profile_revision_id,
        )
        self.assertEqual(side_count, 3)
        self.assertEqual(provenance_count, 3)

    def test_unsupported_source_is_visible_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, profile_revision_id, decision_id = self._database(Path(temporary))
            result = run_validation(
                database,
                profile_revision_id=profile_revision_id,
                decision_id=decision_id,
                pack_manifest=self._pack_manifest,
                input_payload={"case": "unsupported"},
            )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["source_classification"], "unsupported")
        self.assertEqual([item["side"] for item in result["evidence"]], ["source", "target"])
        self.assertEqual(result["evidence"][0]["status"], "blocked")

    def test_cli_runs_and_lists_a_validation_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, profile_revision_id, decision_id = self._database(Path(temporary))
            output = StringIO()
            with redirect_stdout(output):
                exit_code = _validation(
                    [
                        "run",
                        "--database",
                        str(database),
                        "--profile-revision",
                        profile_revision_id,
                        "--decision",
                        decision_id,
                        "--pack-manifest",
                        str(self._pack_manifest),
                        "--input-json",
                        json.dumps({"case": "passing"}),
                    ]
                )
            result = json.loads(output.getvalue())
            listed = _validation(
                [
                    "list",
                    "--database",
                    str(database),
                    "--decision",
                    decision_id,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(listed, 0)


if __name__ == "__main__":
    unittest.main()
