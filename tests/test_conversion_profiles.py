from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sourcebridge.cli import _profile
from sourcebridge.conversion_profiles import (
    ConversionProfileError,
    create_project_profile_revision,
    list_project_profile_revisions,
    promote_project_profile,
    record_project_profile_certification,
    register_rule_revision,
    resolve_project_profile,
    set_project_profile_state,
    set_rule_revision_state,
)
from sourcebridge.pipeline import initialize


class ConversionProfileTests(unittest.TestCase):
    def _add_base_profile(self, database: Path, profile_id: str = "csharp-rust") -> str:
        initialize(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO sourcebridge_profile
                    (id, profile_name, source_frontend_pack_id, corpus_pack_id,
                     target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
                     options_digest, created_at)
                VALUES (?, 'csharp-to-rust', 'sourcebridge-csharp', NULL,
                        'sourcebridge-rust', 'rules-v1', 'runtime-v1', 'options-v1', 'now')
                """,
                (profile_id,),
            )
        return profile_id

    def test_project_overlay_promotes_only_after_full_certification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._add_base_profile(database)
            original = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id="sourcebridge-csharp",
                definition={"lowering": "generic"},
                rationale="Initial generic lowering.",
                created_by="dan",
            )
            set_rule_revision_state(
                database,
                str(original["rule_revision_id"]),
                state="active",
                rationale="Generic lowering is validated.",
                decided_by="dan",
            )
            replacement = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id="sourcebridge-csharp",
                definition={"lowering": "renode-aware"},
                rationale="Support the Renode logging API.",
                created_by="dan",
                workspace_name="renode-logging",
                supersedes_revision_id=str(original["rule_revision_id"]),
            )
            set_rule_revision_state(
                database,
                str(replacement["rule_revision_id"]),
                state="active",
                rationale="The replacement is validated.",
                decided_by="dan",
            )
            profile = create_project_profile_revision(
                database,
                project_id="renode",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="renode-full",
                rationale="Use Renode-specific logging behavior.",
                created_by="dan",
                workspace_name="renode-logging",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "replace",
                        "rule_revision_id": replacement["rule_revision_id"],
                        "rationale": "Renode owns this API surface.",
                    },
                    {
                        "rule_key": "logging",
                        "directive": "configure",
                        "configuration": {"receiver": "machine"},
                        "rationale": "The Renode receiver is explicit.",
                    },
                ],
                setting_overrides=[
                    {
                        "key": "runtime_adapter",
                        "schema_version": 1,
                        "value": {"id": "renode-runtime-v1"},
                        "rationale": "Use the Renode runtime adapter.",
                    }
                ],
            )
            profile_id = str(profile["profile_revision_id"])
            resolved = resolve_project_profile(database, profile_id)

            with self.assertRaisesRegex(ConversionProfileError, "certification is required"):
                promote_project_profile(
                    database,
                    profile_id,
                    rationale="Attempted before the full corpus run.",
                    decided_by="dan",
                )
            set_project_profile_state(
                database,
                profile_id,
                state="candidate",
                rationale="Ready for full Renode validation.",
                decided_by="dan",
            )
            record_project_profile_certification(
                database,
                profile_id,
                validation_tier="renode-full",
                status="passed",
                evidence_reference="engine-test-run:renode-full-001",
                evidence_digest="a" * 64,
                details={"tests": [434, 435, 436]},
                certified_by="ci",
            )
            promoted = promote_project_profile(
                database,
                profile_id,
                rationale="Full Renode validation passed.",
                decided_by="dan",
            )

            with sqlite3.connect(database) as connection:
                original_state = connection.execute(
                    """
                    SELECT state
                    FROM sourcebridge_rule_revision_state
                    WHERE rule_revision_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (original["rule_revision_id"],),
                ).fetchone()[0]
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "UPDATE sourcebridge_rule_revision SET rationale = 'mutated' "
                        "WHERE id = ?",
                        (replacement["rule_revision_id"],),
                    )

        self.assertEqual(original_state, "deprecated")
        self.assertEqual(resolved["state"], "draft")
        self.assertEqual(resolved["rule_overrides"][0]["directive"], "configure")
        self.assertEqual(resolved["rule_overrides"][1]["directive"], "replace")
        self.assertEqual(resolved["setting_overrides"][0]["key"], "runtime_adapter")
        self.assertEqual(promoted["state"], "current")

    def test_replacement_profile_retires_prior_current_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._add_base_profile(database)
            first = create_project_profile_revision(
                database,
                project_id="renode",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="renode-full",
                rationale="Initial Renode profile.",
                created_by="dan",
            )
            first_id = str(first["profile_revision_id"])
            record_project_profile_certification(
                database,
                first_id,
                validation_tier="renode-full",
                status="passed",
                evidence_reference="engine-test-run:1",
                evidence_digest="b" * 64,
                details={"suite": "full"},
                certified_by="ci",
            )
            promote_project_profile(
                database,
                first_id,
                rationale="Initial profile is certified.",
                decided_by="dan",
            )
            second = create_project_profile_revision(
                database,
                project_id="renode",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="renode-full",
                rationale="Replace the profile settings.",
                created_by="dan",
                supersedes_profile_revision_id=first_id,
                setting_overrides=[
                    {
                        "key": "comment_policy",
                        "value": {"mode": "renode"},
                        "rationale": "Use Renode comments.",
                    }
                ],
            )
            second_id = str(second["profile_revision_id"])
            record_project_profile_certification(
                database,
                second_id,
                validation_tier="renode-full",
                status="passed",
                evidence_reference="engine-test-run:2",
                evidence_digest="c" * 64,
                details={"suite": "full"},
                certified_by="ci",
            )
            promote_project_profile(
                database,
                second_id,
                rationale="Replacement profile is certified.",
                decided_by="dan",
            )
            profiles = list_project_profile_revisions(database, project_id="renode")

        states = {str(profile["id"]): profile["state"] for profile in profiles}
        self.assertEqual(states[first_id], "retired")
        self.assertEqual(states[second_id], "current")

    def test_disabled_rule_requires_a_structured_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            base_profile_id = self._add_base_profile(database)
            register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id="sourcebridge-csharp",
                definition={"lowering": "generic"},
                rationale="Initial generic lowering.",
                created_by="dan",
            )

            with self.assertRaisesRegex(ConversionProfileError, "require a resolution"):
                create_project_profile_revision(
                    database,
                    project_id="renode",
                    profile_name="csharp-to-rust",
                    base_profile_id=base_profile_id,
                    required_validation_tier="renode-full",
                    rationale="Disable an inapplicable rule.",
                    created_by="dan",
                    rule_overrides=[
                        {
                            "rule_key": "logging",
                            "directive": "disable",
                            "rationale": "The Renode path does not use this rule.",
                        }
                    ],
                )

            profile = create_project_profile_revision(
                database,
                project_id="renode",
                profile_name="csharp-to-rust",
                base_profile_id=base_profile_id,
                required_validation_tier="renode-full",
                rationale="Disable an inapplicable rule.",
                created_by="dan",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "disable",
                        "resolution": {
                            "kind": "runtime_adapter",
                            "scope": "Renode peripheral logging",
                            "reference": "renode-runtime-v1",
                        },
                        "rationale": "The Renode runtime handles this behavior.",
                    }
                ],
            )

        self.assertEqual(profile["state"], "draft")

    def test_cli_creates_profile_from_versioned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "evidence.db"
            base_profile_id = self._add_base_profile(database)
            manifest = root / "renode-profile.json"
            manifest.write_text(
                json.dumps(
                    {
                        "project_id": "renode",
                        "profile_name": "csharp-to-rust",
                        "base_profile_id": base_profile_id,
                        "required_validation_tier": "renode-full",
                        "rationale": "Start a draft profile.",
                        "created_by": "agent",
                        "workspace_name": "renode-bootstrap",
                        "setting_overrides": [
                            {
                                "key": "runtime_adapter",
                                "value": {"id": "renode-runtime-v1"},
                                "rationale": "Use the Renode runtime adapter.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                result = _profile(["create", "--database", str(database), str(manifest)])

        self.assertEqual(result, 0)
        created = json.loads(output.getvalue())
        self.assertEqual(created["project_id"], "renode")
        self.assertEqual(created["state"], "draft")

    def test_initialize_upgrades_version_four_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE sourcebridge_project_profile_certification")
                connection.execute("DROP TABLE sourcebridge_project_profile_setting_override")
                connection.execute("DROP TABLE sourcebridge_project_profile_rule_override")
                connection.execute("DROP TABLE sourcebridge_project_profile_state")
                connection.execute("DROP TABLE sourcebridge_project_profile_revision")
                connection.execute("DROP TABLE sourcebridge_rule_revision_state")
                connection.execute("DROP TABLE sourcebridge_rule_revision")
                connection.execute("DROP TABLE sourcebridge_rule_family")
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '4' "
                    "WHERE key = 'schema_version'"
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM sourcebridge_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                profile_table = connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'sourcebridge_project_profile_revision'
                    """
                ).fetchone()
                immutability_trigger = connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'trigger'
                      AND name = 'sourcebridge_rule_revision_immutable_update'
                    """
                ).fetchone()

        self.assertEqual(version, "10")
        self.assertIsNotNone(profile_table)
        self.assertIsNotNone(immutability_trigger)

    def test_initialize_upgrades_version_five_database_with_rule_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE sourcebridge_project_profile_rule_override")
                connection.execute(
                    """
                    CREATE TABLE sourcebridge_project_profile_rule_override (
                        id TEXT PRIMARY KEY,
                        profile_revision_id TEXT NOT NULL,
                        rule_family_id TEXT NOT NULL,
                        directive TEXT NOT NULL,
                        selected_rule_revision_id TEXT,
                        configuration_json TEXT NOT NULL,
                        configuration_digest TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '5' "
                    "WHERE key = 'schema_version'"
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM sourcebridge_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_project_profile_rule_override)"
                    )
                }

        self.assertEqual(version, "10")
        self.assertIn("resolution_json", columns)

    def test_model_authored_rule_requires_human_review_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_imported_record
                        (id, source_repository, source_path, source_sha256,
                         kind, payload_json, imported_at)
                    VALUES
                        ('imported-proposal-1', 'tests', 'proposal-1.json',
                         'sha256:proposal', 'rule_proposal', '{}', 'now')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sourcebridge_rule_proposal
                        (id, imported_record_id, status, model_name, model_digest,
                         prompt_template_version, payload_sha256, proposal_json,
                         raw_response_json, created_at)
                    VALUES
                        ('proposal-1', 'imported-proposal-1',
                         'unvalidated-llm-proposal', 'qwen2.5-coder:14b',
                         'sha256:model', 'prompt-v1', 'sha256:payload',
                         '{}', '{}', 'now')
                    """
                )
            with self.assertRaisesRegex(ConversionProfileError, "human review"):
                register_rule_revision(
                    database,
                    rule_key="arithmetic",
                    source_frontend_pack_id="sourcebridge-delphi",
                    definition={"operation": "addition"},
                    rationale="Model candidate.",
                    created_by="ollama",
                    model_name="qwen2.5-coder:14b",
                    model_digest="sha256:model",
                    proposal_id="proposal-1",
                )
            accepted = register_rule_revision(
                database,
                rule_key="arithmetic",
                source_frontend_pack_id="sourcebridge-delphi",
                definition={"operation": "addition"},
                rationale="Human reviewed model candidate.",
                created_by="dan",
                model_name="qwen2.5-coder:14b",
                model_digest="sha256:model",
                proposal_id="proposal-1",
                human_reviewed_by="dan",
                human_reviewed_at="2026-10-17T00:00:00+00:00",
            )
        self.assertEqual(accepted["model_name"], "qwen2.5-coder:14b")
        self.assertEqual(accepted["proposal_id"], "proposal-1")

    def test_model_authored_rule_rejects_missing_or_mismatched_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            with self.assertRaisesRegex(ConversionProfileError, "persisted"):
                register_rule_revision(
                    database,
                    rule_key="arithmetic",
                    source_frontend_pack_id="sourcebridge-delphi",
                    definition={"operation": "addition"},
                    rationale="Model candidate.",
                    created_by="ollama",
                    model_name="qwen2.5-coder:14b",
                    model_digest="sha256:model",
                    proposal_id="missing",
                    human_reviewed_by="dan",
                    human_reviewed_at="2026-10-17T00:00:00+00:00",
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_imported_record
                        (id, source_repository, source_path, source_sha256,
                         kind, payload_json, imported_at)
                    VALUES
                        ('imported-proposal-2', 'tests', 'proposal-2.json',
                         'sha256:proposal-2', 'rule_proposal', '{}', 'now')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sourcebridge_rule_proposal
                        (id, imported_record_id, status, model_name, model_digest,
                         prompt_template_version, payload_sha256, proposal_json,
                         raw_response_json, created_at)
                    VALUES
                        ('proposal-2', 'imported-proposal-2',
                         'unvalidated-llm-proposal', 'qwen2.5-coder:14b',
                         'sha256:other', 'prompt-v1', 'sha256:payload-2',
                         '{}', '{}', 'now')
                    """
                )
            with self.assertRaisesRegex(ConversionProfileError, "does not match"):
                register_rule_revision(
                    database,
                    rule_key="arithmetic",
                    source_frontend_pack_id="sourcebridge-delphi",
                    definition={"operation": "addition"},
                    rationale="Model candidate.",
                    created_by="ollama",
                    model_name="qwen2.5-coder:14b",
                    model_digest="sha256:model",
                    proposal_id="proposal-2",
                    human_reviewed_by="dan",
                    human_reviewed_at="2026-10-17T00:00:00+00:00",
                )
