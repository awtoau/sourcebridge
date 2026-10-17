from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from sourcebridge.engine_tests import import_catalogue
from sourcebridge.pipeline import EvidencePipelineError, import_json, initialize


class PipelineTests(unittest.TestCase):
    def test_imports_ollama_inventory_with_registered_fixture_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            fixture = fixtures / "scalar.pas"
            fixture.write_text("function Add: Integer; begin Result := 1 + 2; end;", encoding="utf-8")
            fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
            catalogue = {
                "schema_version": 1,
                "pack_id": "sourcebridge-delphi",
                "adapter": {"id": "fpc", "config": {"argv": ["fpc"]}},
                "tests": [
                    {
                        "id": 437,
                        "name": "scalar addition",
                        "fixture": "fixtures/scalar.pas",
                        "expected": {},
                    }
                ],
            }
            catalogue_path = root / "engine-tests.json"
            catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
            report = {
                "kind": "ollama_test_inventory",
                "pack_id": "sourcebridge-delphi",
                "test_id": 437,
                "fixture_path": "fixtures/scalar.pas",
                "fixture_sha256": fixture_digest,
                "model": {"name": "local", "digest": "sha256:model"},
                "prompt_template_version": "delphi-engine-test-inventory/v1",
                "prompt_template_digest": "c" * 64,
                "prompt": {"text": "classify fixture"},
                "prompt_sha256": "a" * 64,
                "raw_response": "{\"language_features\": [\"integer\"]}",
                "inventory": {"language_features": ["integer"]},
                "statuses": {
                    "source": {"status": "passed"},
                    "extraction": {"status": "passed"},
                    "target": {"status": "blocked"},
                    "oracle": {"status": "skipped"},
                },
                "status_source": "structured_adapter",
            }
            evidence_path = root / "evidence" / "ollama-test-inventory" / "model" / "437.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(json.dumps(report), encoding="utf-8")
            database = root / "evidence.db"

            initialize(database)
            import_catalogue(database, catalogue_path)
            first = import_json(database, root, repository="fixture")
            second = import_json(database, root, repository="fixture")
            with sqlite3.connect(database) as connection:
                inventory = connection.execute(
                    """
                    SELECT pack_id, test_id, fixture_digest, model_name, model_digest,
                           prompt_template_version, prompt_template_digest, prompt_json, raw_response_json,
                           inventory_json, statuses_json, status_source
                    FROM sourcebridge_ollama_test_inventory
                    """
                ).fetchone()

        self.assertEqual(first["projected"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(inventory[:6], (
            "sourcebridge-delphi",
            437,
            fixture_digest,
            "local",
            "sha256:model",
            "delphi-engine-test-inventory/v1",
        ))
        self.assertEqual(inventory[6], "c" * 64)
        self.assertEqual(json.loads(inventory[7]), {"text": "classify fixture"})
        self.assertEqual(json.loads(inventory[8]), report["raw_response"])
        self.assertEqual(json.loads(inventory[9]), report["inventory"])
        self.assertEqual(json.loads(inventory[10]), report["statuses"])
        self.assertEqual(inventory[11], "structured_adapter")

    def test_rejects_ollama_inventory_with_wrong_fixture_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.pas"
            fixture.write_text("begin end.", encoding="utf-8")
            catalogue = {
                "schema_version": 1,
                "pack_id": "sourcebridge-delphi",
                "adapter": {"id": "fpc", "config": {"argv": ["fpc"]}},
                "tests": [{"id": 437, "name": "fixture", "fixture": "fixture.pas", "expected": {}}],
            }
            catalogue_path = root / "engine-tests.json"
            catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
            (root / "report.json").write_text(
                json.dumps(
                    {
                        "kind": "ollama_test_inventory",
                        "pack_id": "sourcebridge-delphi",
                        "test_id": 437,
                        "fixture_path": "fixture.pas",
                        "fixture_sha256": "b" * 64,
                        "model": {"name": "local", "digest": "sha256:model"},
                        "prompt_template_version": "v1",
                        "prompt_template_digest": "c" * 64,
                        "prompt": {"text": "classify"},
                        "prompt_sha256": "a" * 64,
                        "raw_response": "{}",
                        "inventory": {},
                        "statuses": {},
                        "status_source": "structured_adapter",
                    }
                ),
                encoding="utf-8",
            )
            database = root / "evidence.db"
            initialize(database)
            import_catalogue(database, catalogue_path)

            with self.assertRaises(EvidencePipelineError):
                import_json(database, root, repository="fixture")

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

        self.assertEqual(version, "12")
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

        self.assertEqual(version, "12")
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
