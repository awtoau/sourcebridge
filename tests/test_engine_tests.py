from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from sourcebridge.engine_tests import EngineTestError, list_tests, register, run
from sourcebridge.pipeline import initialize


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

        self.assertEqual(version, "3")
        self.assertIsNotNone(test_table)
