from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from sourcebridge.pipeline import import_json, initialize


class PipelineTests(unittest.TestCase):
    def test_simultaneous_initialization_serializes_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "ALTER TABLE sourcebridge_engine_test DROP COLUMN catalogue_path"
                )
                connection.execute(
                    "ALTER TABLE sourcebridge_engine_test DROP COLUMN catalogue_digest"
                )
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '3' "
                    "WHERE key = 'schema_version'"
                )
                connection.commit()
            finally:
                connection.close()

            start = threading.Barrier(4)

            def initialize_concurrently() -> None:
                start.wait()
                initialize(database)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(initialize_concurrently)
                    for _ in range(4)
                ]
                for future in futures:
                    future.result()

            connection = sqlite3.connect(database)
            try:
                version = connection.execute(
                    "SELECT value FROM sourcebridge_schema_metadata "
                    "WHERE key = 'schema_version'"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_engine_test)"
                    )
                }
            finally:
                connection.close()

        self.assertEqual(version, "9")
        self.assertTrue({"catalogue_path", "catalogue_digest"} <= columns)

    def test_initialize_recovers_interrupted_v3_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)

            connection = sqlite3.connect(database)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_engine_test)"
                    )
                }
                self.assertTrue({"catalogue_path", "catalogue_digest"} <= columns)
                # This is the state left by the previous implementation after
                # ALTER TABLE committed but before schema metadata advanced.
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '3' "
                    "WHERE key = 'schema_version'"
                )
                connection.commit()
            finally:
                connection.close()

            initialize(database)

            connection = sqlite3.connect(database)
            try:
                version = connection.execute(
                    "SELECT value FROM sourcebridge_schema_metadata "
                    "WHERE key = 'schema_version'"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_engine_test)"
                    )
                }
            finally:
                connection.close()

        self.assertEqual(version, "9")
        self.assertTrue({"catalogue_path", "catalogue_digest"} <= columns)

    def test_imports_legacy_json_with_provenance_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = {
                "cluster_id": "scalar-v1",
                "source_language": "delphi",
                "variant": {"compiler": {"target_cpu": "x86_64", "target_os": "linux"}},
                "source_facts": {
                    "source_path": "scalars.pas",
                    "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
                },
                "observed_behavior": {"non_overflow_examples": [{"result": 3}]},
                "active_rule_digest": "none",
                "target_context": {"backend": "rust"},
            }
            proposal = {
                "status": "unvalidated-llm-proposal",
                "payload_sha256": "a" * 64,
                "model": {"name": "local", "digest": "sha256:test"},
                "prompt_template_version": "v1",
                "proposal": {},
                "raw_response": {},
            }
            profile = {
                "id": "delphi-to-rust",
                "source_frontend": "sourcebridge-delphi",
                "target_backend": "sourcebridge-rust",
                "rule_layers": [],
                "runtime_adapters": [],
                "options": {},
            }
            (root / "evidence").mkdir()
            (root / "evidence" / "facts.json").write_text(json.dumps(evidence), encoding="utf-8")
            (root / "evidence" / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
            (root / "examples").mkdir()
            (root / "examples" / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            database = root / "evidence.db"

            initialize(database)
            first = import_json(database, root, repository="fixture")
            second = import_json(database, root, repository="fixture")
            profile["options"] = {"updated": True}
            (root / "examples" / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            updated = import_json(database, root, repository="fixture")
            with sqlite3.connect(database) as connection:
                imported = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_imported_record"
                ).fetchone()[0]
                proposals = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_rule_proposal"
                ).fetchone()[0]
                units = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_source_unit"
                ).fetchone()[0]

        self.assertEqual(
            first, {"repository": "fixture", "imported": 3, "projected": 3, "skipped": 0}
        )
        self.assertEqual(
            second, {"repository": "fixture", "imported": 0, "projected": 3, "skipped": 3}
        )
        self.assertEqual(
            updated, {"repository": "fixture", "imported": 1, "projected": 3, "skipped": 2}
        )
        self.assertEqual((imported, proposals, units), (3, 1, 1))
