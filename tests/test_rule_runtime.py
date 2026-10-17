from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sourcebridge.conversion_profiles import (
    create_project_profile_revision,
    register_rule_revision,
)
from sourcebridge.pipeline import initialize
from sourcebridge.rule_runtime import RuleRuntimeError, execute


class RuleRuntimeTests(unittest.TestCase):
    _fixture_root = Path(__file__).parent / "fixtures" / "rule-runtime"
    _pack_id = "sourcebridge-csharp-rule-runtime-fixture"

    def _database(self, database: Path) -> str:
        initialize(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO sourcebridge_profile
                    (id, profile_name, source_frontend_pack_id, corpus_pack_id,
                     target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
                     options_digest, created_at)
                VALUES (
                    'generic-csharp-rust', 'csharp-to-rust', ?, NULL,
                    'sourcebridge-rust', 'generic-rules-v1', 'generic-runtime-v1',
                    'generic-options-v1', 'now'
                )
                """,
                (self._pack_id,),
            )
        return "generic-csharp-rust"

    @staticmethod
    def _implementation(path: Path) -> dict[str, str]:
        return {
            "pack_id": "sourcebridge-csharp-rule-runtime-fixture",
            "path": str(path.relative_to(RuleRuntimeTests._fixture_root)),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def test_project_replacement_and_configuration_run_without_mutating_generic_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._database(database)
            generic_path = self._fixture_root / "rules" / "generic_logging.py"
            project_path = self._fixture_root / "rules" / "project_logging.py"
            generic = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id=self._pack_id,
                definition={
                    "implementation": self._implementation(generic_path),
                    "name": "generic",
                },
                rationale="Generic logging lowering.",
                created_by="fixture",
                implementation=self._implementation(generic_path),
            )
            project = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id=self._pack_id,
                definition={
                    "implementation": self._implementation(project_path),
                    "name": "project",
                },
                rationale="Project logging lowering.",
                created_by="fixture",
                implementation=self._implementation(project_path),
            )
            profile = create_project_profile_revision(
                database,
                project_id="fixture-project",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="fixture",
                toolchain_digest="fixture-toolchain-v1",
                rationale="Select project logging behavior only for this project.",
                created_by="fixture",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "replace",
                        "rule_revision_id": project["rule_revision_id"],
                        "rationale": "Project owns the logging surface.",
                    },
                    {
                        "rule_key": "logging",
                        "directive": "configure",
                        "configuration": {"receiver": "machine"},
                        "rationale": "Use the project receiver.",
                    },
                ],
                setting_overrides=[
                    {
                        "key": "runtime_adapter",
                        "schema_version": 1,
                        "value": {"id": "fixture-runtime-v1"},
                        "rationale": "Use the fixture runtime adapter.",
                    }
                ],
            )
            with sqlite3.connect(database) as connection:
                generic_snapshot = connection.execute(
                    """
                    SELECT id, profile_name, source_frontend_pack_id, target_backend_pack_id,
                           rule_layers_digest, runtime_adapters_digest, options_digest
                    FROM sourcebridge_profile WHERE id = ?
                    """,
                    (base_profile_id,),
                ).fetchone()

            result = execute(
                database,
                str(profile["profile_revision_id"]),
                self._fixture_root / "sourcebridge-rule-runtime.pack.json",
                input_payload={"source": "Log(message)"},
                requested_by="fixture-test",
            )

            with sqlite3.connect(database) as connection:
                generic_after = connection.execute(
                    """
                    SELECT id, profile_name, source_frontend_pack_id, target_backend_pack_id,
                           rule_layers_digest, runtime_adapters_digest, options_digest
                    FROM sourcebridge_profile WHERE id = ?
                    """,
                    (base_profile_id,),
                ).fetchone()
                runtime_run = connection.execute(
                    """
                    SELECT status, adapter_id, adapter_digest, resolved_profile_digest,
                           rule_digest, implementation_digest, settings_digest
                    FROM sourcebridge_rule_runtime_run WHERE id = ?
                    """,
                    (result["run_id"],),
                ).fetchone()
                runtime_provenance = connection.execute(
                    """
                    SELECT profile_revision_id, resolved_profile_digest,
                           rule_digest, toolchain_digest
                    FROM sourcebridge_conversion_provenance
                    WHERE runtime_run_id = ?
                    """,
                    (result["run_id"],),
                ).fetchone()
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError, "provenance is append-only"
                ):
                    connection.execute(
                        """
                        UPDATE sourcebridge_rule_runtime_run
                        SET implementation_digest = 'tampered'
                        WHERE id = ?
                        """,
                        (result["run_id"],),
                    )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["result"]["used_rule"], "rules/project_logging.py")
        self.assertEqual(result["result"]["configuration"], {"receiver": "machine"})
        self.assertEqual(result["result"]["input"], {"source": "Log(message)"})
        self.assertEqual(generic_after, generic_snapshot)
        self.assertEqual(runtime_run[0], "passed")
        self.assertEqual(runtime_run[1], "fixture-rule-runtime-v1")
        self.assertEqual(runtime_run[2], result["provenance"]["adapter_digest"])
        self.assertEqual(runtime_run[3], profile["resolved_digest"])
        self.assertEqual(runtime_run[4], result["provenance"]["rule_digest"])
        self.assertEqual(runtime_run[5], result["provenance"]["implementation_digest"])
        self.assertEqual(runtime_run[6], result["provenance"]["settings_digest"])
        self.assertEqual(runtime_provenance[0], profile["profile_revision_id"])
        self.assertEqual(runtime_provenance[1], profile["resolved_digest"])
        self.assertEqual(runtime_provenance[2], result["provenance"]["rule_digest"])
        self.assertEqual(runtime_provenance[3], "fixture-toolchain-v1")
        self.assertNotEqual(generic["rule_revision_id"], project["rule_revision_id"])

    def test_adapter_rejects_configuration_outside_its_declared_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._database(database)
            implementation_path = self._fixture_root / "rules" / "project_logging.py"
            revision = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id=self._pack_id,
                definition={"implementation": self._implementation(implementation_path)},
                rationale="Project logging lowering.",
                created_by="fixture",
            )
            profile = create_project_profile_revision(
                database,
                project_id="fixture-project",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="fixture",
                rationale="Reject unsupported adapter configuration.",
                created_by="fixture",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "replace",
                        "rule_revision_id": revision["rule_revision_id"],
                        "rationale": "Project owns the logging surface.",
                    },
                    {
                        "rule_key": "logging",
                        "directive": "configure",
                        "configuration": {"unsupported": True},
                        "rationale": "This must be rejected.",
                    },
                ],
            )

            with self.assertRaisesRegex(RuleRuntimeError, "unsupported configuration key"):
                execute(
                    database,
                    str(profile["profile_revision_id"]),
                    self._fixture_root / "sourcebridge-rule-runtime.pack.json",
                    input_payload={},
                )
            with sqlite3.connect(database) as connection:
                rejected = connection.execute(
                    """
                    SELECT status, adapter_id, resolved_profile_digest
                    FROM sourcebridge_rule_runtime_run
                    WHERE profile_revision_id = ?
                    """,
                    (profile["profile_revision_id"],),
                ).fetchone()

        self.assertEqual(rejected[0], "rejected")
        self.assertEqual(rejected[1], "fixture-rule-runtime-v1")
        self.assertEqual(rejected[2], profile["resolved_digest"])

    def test_adapter_rejects_a_directive_outside_its_declared_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._database(database)
            revision = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id=self._pack_id,
                definition={"name": "generic"},
                rationale="Generic logging lowering.",
                created_by="fixture",
            )
            profile = create_project_profile_revision(
                database,
                project_id="fixture-project",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="fixture",
                rationale="Reject unsupported adapter directive.",
                created_by="fixture",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "disable",
                        "resolution": {
                            "kind": "replacement",
                            "scope": "fixture-project",
                            "reference": "fixture-runtime",
                        },
                        "rationale": "This must be rejected by the adapter contract.",
                    }
                ],
            )

            with self.assertRaisesRegex(RuleRuntimeError, "unsupported directive `disable`"):
                execute(
                    database,
                    str(profile["profile_revision_id"]),
                    self._fixture_root / "sourcebridge-rule-runtime.pack.json",
                    input_payload={},
                )


if __name__ == "__main__":
    unittest.main()
