from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from sourcebridge.cli import _dev, _test
from sourcebridge.engine_tests import EngineTestError, import_catalogue, list_tests, register, run
from sourcebridge.pipeline import SCHEMA_VERSION, initialize


class EngineTestTests(unittest.TestCase):
    def test_command_adapter_records_a_passing_fixture_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "input.cs"
            fixture.write_text("class Test {}", encoding="utf-8")
            database = root / "evidence.db"
            test_id = register(
                database,
                pack_id="csharp-to-rust",
                adapter="command",
                name="source-compiles",
                fixture=fixture,
                expected_json=json.dumps({"exit_code": 0, "stdout": "42\n"}),
                adapter_config_json=json.dumps(
                    {"argv": [sys.executable, "-c", "print(42)"], "timeout_seconds": 5}
                ),
                test_id=434,
            )

            result = run(database, test_id)
            tests = list_tests(database, pack_id="csharp-to-rust")
            with sqlite3.connect(database) as connection:
                run_count = connection.execute(
                    "SELECT COUNT(*) FROM sourcebridge_engine_test_run"
                ).fetchone()[0]

        self.assertEqual(result["status"], "passed")
        self.assertEqual(tests[0]["id"], 434)
        self.assertEqual(tests[0]["fixture_path"], str(fixture.resolve()))
        self.assertEqual(run_count, 1)

    def test_run_rejects_fixture_changed_after_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "input.cs"
            fixture.write_text("class First {}", encoding="utf-8")
            database = root / "evidence.db"
            test_id = register(
                database,
                pack_id="csharp-to-rust",
                adapter="command",
                name="fixture-integrity",
                fixture=fixture,
                expected_json=json.dumps({"exit_code": 0}),
                adapter_config_json=json.dumps({"argv": [sys.executable, "-c", "pass"]}),
            )
            fixture.write_text("class Changed {}", encoding="utf-8")

            with self.assertRaisesRegex(EngineTestError, "fixture digest changed"):
                run(database, test_id)

    def test_initialize_upgrades_version_two_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE sourcebridge_engine_test_run")
                connection.execute("DROP TABLE sourcebridge_engine_test")
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '2' "
                    "WHERE key = 'schema_version'"
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM sourcebridge_schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                test_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'sourcebridge_engine_test'"
                ).fetchone()

        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertIsNotNone(test_table)

    def test_initialize_v6_worker_migration_includes_prompt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "DROP TABLE sourcebridge_proposal_worker_session_event"
                )
                connection.execute("DROP TABLE sourcebridge_proposal_worker_request")
                connection.execute("DROP TABLE sourcebridge_proposal_worker_session")
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '6' "
                    "WHERE key = 'schema_version'"
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_proposal_worker_session)"
                    )
                }

        self.assertIn("prompt_template_digest", columns)

    def test_initialize_repairs_v7_worker_session_without_prompt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "DROP TABLE sourcebridge_proposal_worker_session_event"
                )
                connection.execute("DROP TABLE sourcebridge_proposal_worker_request")
                connection.execute("DROP TABLE sourcebridge_proposal_worker_session")
                connection.executescript(
                    """
                    CREATE TABLE sourcebridge_proposal_worker_session (
                        id TEXT PRIMARY KEY,
                        batch_id TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        model_digest TEXT NOT NULL,
                        generation_options_json TEXT NOT NULL,
                        prompt_template_version TEXT NOT NULL,
                        active_rule_digest TEXT NOT NULL,
                        status TEXT NOT NULL,
                        summary_json TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE TABLE sourcebridge_proposal_worker_request (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sourcebridge_proposal_worker_session(id),
                        request_index INTEGER NOT NULL,
                        evidence_sha256 TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('staged', 'rejected')),
                        elapsed_ms REAL NOT NULL,
                        load_duration_ms REAL NOT NULL,
                        generation_ms REAL NOT NULL,
                        generated_tokens INTEGER,
                        rejection_reasons_json TEXT NOT NULL,
                        proposal_json TEXT NOT NULL,
                        raw_response_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (session_id, request_index)
                    );
                    """
                )
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = '7' "
                    "WHERE key = 'schema_version'"
                )

            initialize(database)
            with sqlite3.connect(database) as connection:
                session_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_proposal_worker_session)"
                    )
                }
                request_sql = connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = 'sourcebridge_proposal_worker_request'
                    """
                ).fetchone()[0]
                proposal_trigger_sql = connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'trigger'
                      AND name = 'sourcebridge_model_authored_rule_requires_human_review'
                    """
                ).fetchone()[0]

        self.assertIn("prompt_template_digest", session_columns)
        self.assertIn("'failed'", request_sql)
        self.assertNotIn("UNIQUE (session_id, request_index)", request_sql)
        self.assertIn("proposal.model_digest", proposal_trigger_sql)

    def test_imports_versioned_catalogue_with_relative_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            fixture = fixtures / "case.py"
            fixture.write_text("print('catalogue')", encoding="utf-8")
            catalogue = root / "engine-tests.json"
            catalogue.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pack_id": "csharp-to-rust",
                        "adapter": {
                            "id": "command",
                            "config": {"argv": [sys.executable, "{fixture_path}"]},
                        },
                        "tests": [
                            {
                                "id": 434,
                                "name": "catalogue-fixture",
                                "fixture": "fixtures/case.py",
                                "expected": {"exit_code": 0, "stdout": "catalogue\n"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            database = root / "evidence.db"

            first = import_catalogue(database, catalogue)
            result = run(database, 434)
            second = import_catalogue(database, catalogue)
            tests = list_tests(database)

        self.assertEqual((first["created"], first["updated"]), (1, 0))
        self.assertEqual(result["status"], "passed")
        self.assertEqual((second["created"], second["updated"]), (0, 1))
        self.assertEqual(tests[0]["catalogue_path"], "engine-tests.json")

    def test_catalogue_rejects_fixture_outside_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalogue = root / "engine-tests.json"
            catalogue.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pack_id": "csharp-to-rust",
                        "adapter": {"id": "command", "config": {"argv": ["true"]}},
                        "tests": [
                            {
                                "id": 1,
                                "name": "escape",
                                "fixture": "../outside.cs",
                                "expected": {"exit_code": 0},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EngineTestError, "must stay below"):
                import_catalogue(root / "evidence.db", catalogue)

    def test_json_command_records_expected_blocked_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "result.py"
            fixture.write_text(
                "import json\n"
                "print(json.dumps({"
                "'schema_version': 1, "
                "'source': {'status': 'passed'}, "
                "'extraction': {'status': 'blocked'}, "
                "'target': {'status': 'blocked'}, "
                "'oracle': {'status': 'skipped'}"
                "}))\n",
                encoding="utf-8",
            )
            database = root / "evidence.db"
            test_id = register(
                database,
                pack_id="csharp-to-rust",
                adapter="json-command",
                name="structured-result",
                fixture=fixture,
                expected_json=json.dumps(
                    {
                        "source": {"status": "passed"},
                        "extraction": {"status": "blocked"},
                        "target": {"status": "blocked"},
                        "oracle": {"status": "skipped"},
                    }
                ),
                adapter_config_json=json.dumps({"argv": [sys.executable, "{fixture_path}"]}),
            )

            result = run(database, test_id)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["actual"]["adapter_result"]["target"]["status"], "blocked")

    def test_cli_imports_catalogue_declared_by_pack_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "case.py"
            fixture.write_text("print('pack')", encoding="utf-8")
            catalogue = root / "engine-tests.json"
            catalogue.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pack_id": "sourcebridge-csharp",
                        "adapter": {
                            "id": "command",
                            "config": {"argv": [sys.executable, "{fixture_path}"]},
                        },
                        "tests": [
                            {
                                "id": 1,
                                "name": "manifest-catalogue",
                                "fixture": "case.py",
                                "expected": {"exit_code": 0, "stdout": "pack\n"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "sourcebridge-csharp.pack.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "sourcebridge-csharp",
                        "kind": "frontend",
                        "version": "0.1.0",
                        "provides": {
                            "source_languages": ["csharp"],
                            "target_environments": [],
                        },
                        "entrypoints": {"engine_tests": "engine-tests.json"},
                    }
                ),
                encoding="utf-8",
            )
            database = root / "evidence.db"

            with redirect_stdout(StringIO()):
                exit_code = _test(
                    ["import-pack", "--database", str(database), str(manifest)]
                )
            result = run(database, 1)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "passed")

    def test_dev_command_imports_pack_and_runs_one_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "case.py"
            fixture.write_text("print('dev')", encoding="utf-8")
            catalogue = root / "engine-tests.json"
            catalogue.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pack_id": "sourcebridge-csharp",
                        "adapter": {
                            "id": "command",
                            "config": {"argv": [sys.executable, "{fixture_path}"]},
                        },
                        "tests": [
                            {
                                "id": 434,
                                "name": "dev-case",
                                "fixture": "case.py",
                                "expected": {"exit_code": 0, "stdout": "dev\n"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "sourcebridge-csharp.pack.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "sourcebridge-csharp",
                        "kind": "frontend",
                        "version": "0.1.0",
                        "provides": {
                            "source_languages": ["csharp"],
                            "target_environments": [],
                        },
                        "entrypoints": {"engine_tests": "engine-tests.json"},
                    }
                ),
                encoding="utf-8",
            )
            database = root / "evidence.db"

            with redirect_stdout(StringIO()):
                exit_code = _dev(
                    [
                        "sourcebridge-csharp",
                        "test",
                        "434",
                        "--database",
                        str(database),
                        "--pack-manifest",
                        str(manifest),
                    ]
                )

        self.assertEqual(exit_code, 0)
