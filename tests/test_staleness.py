from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sourcebridge.cli import main
from sourcebridge.conversion_profiles import (
    create_project_profile_revision,
    promote_project_profile,
    record_project_profile_certification,
    register_rule_revision,
    set_rule_revision_state,
)
from sourcebridge.database import write_transaction
from sourcebridge.pipeline import initialize
from sourcebridge.reporting import staleness_report
from sourcebridge.staleness import link_conversion_artifact, link_validation_evidence


class StalenessTests(unittest.TestCase):
    def test_replacement_marks_only_old_links_stale_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with write_transaction(database) as connection:
                connection.executescript(
                    """
                    INSERT INTO sourcebridge_corpus
                        VALUES ('c', 'fixture', 'r1', NULL, 'corpus-r1', 'now');
                    INSERT INTO sourcebridge_variant
                        VALUES ('v', 'c', 'frontend', 'compiler', 'abi', 64, 'little',
                                'variant-r1', NULL, 'now');
                    INSERT INTO sourcebridge_profile
                        VALUES ('base', 'fixture', 'frontend', NULL, 'backend',
                                'layers-r1', 'runtime-r1', 'options-r1', 'now');
                    INSERT INTO sourcebridge_source_unit
                        VALUES ('u', 'v', 'Example', 'example.src', NULL,
                                'source-r1', 'function');
                    INSERT INTO sourcebridge_component_decision
                        VALUES ('d', 'base', 'u', 'direct_translation', 'backend',
                                NULL, NULL, 'fixture', 'fixture', 'now');
                    INSERT INTO sourcebridge_generated_artifact
                        VALUES ('artifact-old', 'd', 'example.rs', 'artifact-old',
                                'generator-old', 'now');
                    INSERT INTO sourcebridge_validation_result
                        VALUES ('validation-old', 'd', 'target', 'fixture-validator',
                                'passed', 'old.log', 'validation-old', '{}', 'now');
                    """
                )

            original = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id="frontend",
                definition={"lowering": "old"},
                rationale="Initial rule.",
                created_by="fixture",
            )
            set_rule_revision_state(
                database,
                str(original["rule_revision_id"]),
                state="active",
                rationale="Initial rule is active.",
                decided_by="fixture",
            )
            first = create_project_profile_revision(
                database,
                project_id="fixture-project",
                profile_name="fixture-profile",
                base_profile_id="base",
                required_validation_tier="fixture-full",
                toolchain_digest="toolchain-v1",
                rationale="Initial profile.",
                created_by="fixture",
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "enable",
                        "rule_revision_id": original["rule_revision_id"],
                        "rationale": "Use initial rule.",
                    }
                ],
            )
            first_id = str(first["profile_revision_id"])
            record_project_profile_certification(
                database,
                first_id,
                validation_tier="fixture-full",
                status="passed",
                evidence_reference="fixture-full-v1",
                evidence_digest="a" * 64,
                details={"suite": "fixture-full"},
                certified_by="fixture",
            )
            promote_project_profile(
                database,
                first_id,
                rationale="Initial profile is current.",
                decided_by="fixture",
            )
            old_artifact_link = link_conversion_artifact(
                database,
                "artifact-old",
                profile_revision_id=first_id,
            )
            old_validation_link = link_validation_evidence(
                database,
                "validation-old",
                profile_revision_id=first_id,
            )

            replacement = register_rule_revision(
                database,
                rule_key="logging",
                source_frontend_pack_id="frontend",
                definition={"lowering": "new"},
                rationale="Replace the initial rule.",
                created_by="fixture",
                supersedes_revision_id=str(original["rule_revision_id"]),
            )
            set_rule_revision_state(
                database,
                str(replacement["rule_revision_id"]),
                state="active",
                rationale="Replacement is active.",
                decided_by="fixture",
            )
            second = create_project_profile_revision(
                database,
                project_id="fixture-project",
                profile_name="fixture-profile",
                base_profile_id="base",
                required_validation_tier="fixture-full",
                toolchain_digest="toolchain-v2",
                rationale="Use replacement rule and toolchain.",
                created_by="fixture",
                supersedes_profile_revision_id=first_id,
                rule_overrides=[
                    {
                        "rule_key": "logging",
                        "directive": "enable",
                        "rule_revision_id": replacement["rule_revision_id"],
                        "rationale": "Use replacement rule.",
                    }
                ],
            )
            second_id = str(second["profile_revision_id"])
            before_certification = staleness_report(
                database,
                profile_revision_id=second_id,
            )
            self.assertEqual(
                before_certification["required_validation_tier"],
                "fixture-full",
            )
            self.assertEqual(before_certification["certification"]["status"], "required")

            with write_transaction(database) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_generated_artifact
                        VALUES ('artifact-current', 'd', 'example.rs', 'artifact-current',
                                'generator-current', 'now');
                    """
                )
            record_project_profile_certification(
                database,
                second_id,
                validation_tier="fixture-full",
                status="passed",
                evidence_reference="fixture-full-v2",
                evidence_digest="b" * 64,
                details={"suite": "fixture-full"},
                certified_by="fixture",
            )
            promote_project_profile(
                database,
                second_id,
                rationale="Replacement profile is current after full validation.",
                decided_by="fixture",
            )
            link_conversion_artifact(
                database,
                "artifact-current",
                profile_revision_id=second_id,
            )

            report = staleness_report(
                database,
                project_id="fixture-project",
                profile_name="fixture-profile",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                previous_argv = __import__("sys").argv
                try:
                    __import__("sys").argv = [
                        "sourcebridge",
                        "report-staleness",
                        "--database",
                        str(database),
                        "--project",
                        "fixture-project",
                        "--profile-name",
                        "fixture-profile",
                    ]
                    self.assertEqual(main(), 0)
                finally:
                    __import__("sys").argv = previous_argv

            with sqlite3.connect(database) as connection:
                old_rows = connection.execute(
                    """
                    SELECT profile_revision_id, resolved_profile_digest, rule_digest,
                           toolchain_digest
                    FROM sourcebridge_conversion_provenance
                    WHERE id IN (?, ?)
                    ORDER BY id
                    """,
                    (old_artifact_link["id"], old_validation_link["id"]),
                ).fetchall()
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        """
                        UPDATE sourcebridge_conversion_provenance
                        SET rule_digest = 'changed'
                        WHERE id = ?
                        """,
                        (old_artifact_link["id"],),
                    )
                historical_artifact = connection.execute(
                    """
                    SELECT artifact_digest
                    FROM sourcebridge_generated_artifact
                    WHERE id = 'artifact-old'
                    """
                ).fetchone()[0]

        self.assertEqual(report["profile"]["id"], second_id)
        self.assertEqual(report["required_validation_tier"], "fixture-full")
        self.assertEqual(report["certification"]["status"], "passed")
        self.assertEqual(report["summary"], {"total": 3, "current": 1, "stale": 2})
        self.assertEqual(
            {
                result["status"]
                for result in report["results"]
                if result["historical_profile"]["id"] == first_id
            },
            {"stale_for_current_profile"},
        )
        self.assertEqual(
            {
                result["status"]
                for result in report["results"]
                if result["historical_profile"]["id"] == second_id
            },
            {"current"},
        )
        self.assertEqual(
            json.loads(output.getvalue())["summary"],
            {"total": 3, "current": 1, "stale": 2},
        )
        self.assertEqual(len(old_rows), 2)
        self.assertEqual(old_rows[0][0], first_id)
        self.assertEqual(old_rows[1][0], first_id)
        self.assertEqual(historical_artifact, "artifact-old")
