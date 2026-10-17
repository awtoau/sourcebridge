from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from sourcebridge.conversion_profiles import create_project_profile_revision
from sourcebridge.dispositions import (
    DispositionError,
    OutputError,
    query_source_unit_dispositions,
    record_source_unit_disposition,
    render_annotated_review,
    render_clean_release,
    write_conversion_output,
)
from sourcebridge.pipeline import initialize


class DispositionOutputTests(unittest.TestCase):
    def _fixture(self, database: Path) -> str:
        initialize(database)
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                INSERT INTO sourcebridge_corpus
                    VALUES ('corpus', 'fixture', 'r1', NULL, 'corpus-digest', 'now');
                INSERT INTO sourcebridge_variant
                    VALUES ('variant', 'corpus', 'fixture-frontend', 'compiler',
                            'x86_64-linux', 64, 'little', 'variant-digest', NULL, 'now');
                INSERT INTO sourcebridge_profile
                    VALUES ('base', 'fixture-profile', 'fixture-frontend', NULL,
                            'fixture-rust', 'layers', 'runtime', 'options', 'now');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('unit-direct', 'variant', 'Direct', 'direct.src', '1:1-1:10',
                            'source-direct', 'function');
                INSERT INTO sourcebridge_source_unit
                    VALUES ('unit-blocked', 'variant', 'Blocked', 'blocked.src', '2:1-2:10',
                            'source-blocked', 'function');
                """
            )
        profile = create_project_profile_revision(
            database,
            project_id="fixture-project",
            profile_name="fixture-profile",
            base_profile_id="base",
            required_validation_tier="fixture-full",
            toolchain_digest="fixture-toolchain-v1",
            rationale="Fixture profile for output tests.",
            created_by="fixture",
        )
        return str(profile["profile_revision_id"])

    def test_records_and_queries_each_source_unit_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            profile_revision_id = self._fixture(database)
            direct = record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-direct",
                disposition="direct_translation",
                rationale="Covered by the fixture backend.",
                decided_by="fixture",
            )
            blocked = record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-blocked",
                disposition="blocked",
                rationale="The source dependency is unavailable.",
                decided_by="fixture",
            )
            rows = query_source_unit_dispositions(
                database,
                profile_revision_id=profile_revision_id,
            )

        self.assertTrue(direct["created"])
        self.assertTrue(blocked["created"])
        self.assertEqual(
            [(row["source_unit_id"], row["disposition"]) for row in rows],
            [("unit-blocked", "blocked"), ("unit-direct", "direct_translation")],
        )
        self.assertEqual(rows[0]["profile"]["profile_revision_id"], profile_revision_id)
        self.assertEqual(rows[1]["source_unit"]["source_path"], "direct.src")

    def test_annotated_review_names_provenance_and_incomplete_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            profile_revision_id = self._fixture(database)
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-blocked",
                disposition="blocked",
                rationale="Waiting for a source dependency.",
                decided_by="fixture",
            )
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-direct",
                disposition="direct_translation",
                rationale="Covered.",
                decided_by="fixture",
            )
            result = render_annotated_review(
                database,
                profile_revision_id=profile_revision_id,
                artifacts=[
                    {
                        "source_unit_id": "unit-blocked",
                        "artifact_path": "blocked.rs",
                        "content": "fn blocked() {}",
                        "rule_id": "fixture-rule",
                    },
                    {
                        "source_unit_id": "unit-direct",
                        "artifact_path": "direct.rs",
                        "content": "fn direct() {}",
                        "rule_id": "fixture-rule",
                    },
                ],
            )

        self.assertIn("sourcebridge source:", result["output"])
        self.assertIn("fixture-rule", result["output"])
        self.assertIn("SOURCEBRIDGE INCOMPLETE boundary", result["output"])
        self.assertEqual(result["provenance"]["summary"]["incomplete_boundary_count"], 1)

    def test_clean_release_refuses_blocked_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            profile_revision_id = self._fixture(database)
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-blocked",
                disposition="blocked",
                rationale="Waiting for a source dependency.",
                decided_by="fixture",
            )
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-direct",
                disposition="direct_translation",
                rationale="Covered.",
                decided_by="fixture",
            )
            with self.assertRaisesRegex(
                OutputError, "refuses unresolved incomplete/blocked"
            ):
                render_clean_release(
                    database,
                    profile_revision_id=profile_revision_id,
                    artifacts=[
                        {
                            "source_unit_id": "unit-blocked",
                            "artifact_path": "blocked.rs",
                            "content": "fn blocked() {}",
                        },
                        {
                            "source_unit_id": "unit-direct",
                            "artifact_path": "direct.rs",
                            "content": "fn direct() {}",
                        },
                    ],
                )

    def test_clean_release_writes_provenance_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "evidence.db"
            profile_revision_id = self._fixture(database)
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-direct",
                disposition="direct_translation",
                rationale="Covered.",
                decided_by="fixture",
            )
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-blocked",
                disposition="excluded",
                rationale="Outside the selected release boundary.",
                decided_by="fixture",
            )
            result = write_conversion_output(
                database,
                profile_revision_id=profile_revision_id,
                artifacts=[
                    {
                        "source_unit_id": "unit-direct",
                        "artifact_path": "direct.rs",
                        "content": "fn direct() {}",
                    }
                ],
                mode="clean-release",
                output_path=root / "release.rs",
            )
            sidecar = Path(str(root / "release.rs") + ".provenance.json")

            self.assertEqual(result["sidecar_path"], str(sidecar))
            self.assertEqual((root / "release.rs").read_text(), "fn direct() {}\n")
            self.assertIn(profile_revision_id, sidecar.read_text())
            self.assertIn('"disposition": "excluded"', sidecar.read_text())

    def test_disposition_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            profile_revision_id = self._fixture(database)
            record_source_unit_disposition(
                database,
                profile_revision_id=profile_revision_id,
                source_unit_id="unit-direct",
                disposition="direct_translation",
                rationale="Covered.",
                decided_by="fixture",
            )
            with self.assertRaisesRegex(DispositionError, "immutable"):
                record_source_unit_disposition(
                    database,
                    profile_revision_id=profile_revision_id,
                    source_unit_id="unit-direct",
                    disposition="excluded",
                    rationale="Changed my mind.",
                    decided_by="fixture",
                )
