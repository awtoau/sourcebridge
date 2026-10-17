"""Database-backed orchestration for pack-owned engine-test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
from uuid import uuid4

from sourcebridge.pipeline import initialize


class EngineTestError(ValueError):
    """Raised when an engine test cannot be registered or run."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(path: Path) -> str:
    if not path.is_file():
        raise EngineTestError(f"{path}: fixture must be an existing file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise EngineTestError(f"{label}: invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise EngineTestError(f"{label}: expected a JSON object")
    return parsed


def register(
    database: Path,
    *,
    pack_id: str,
    adapter: str,
    name: str,
    fixture: Path,
    expected_json: str,
    adapter_config_json: str,
    test_id: int | None = None,
) -> int:
    """Register one fixture without storing its source code in SQLite."""
    if not pack_id or not adapter or not name:
        raise EngineTestError("pack ID, adapter, and name must be non-empty")
    expected = _json_object(expected_json, "expected result")
    config = _json_object(adapter_config_json, "adapter configuration")
    fixture = fixture.resolve()
    digest = _digest(fixture)
    initialize(database)

    try:
        with sqlite3.connect(database) as connection:
            if test_id is None:
                result = connection.execute(
                    """
                    INSERT INTO sourcebridge_engine_test
                        (pack_id, adapter, name, fixture_path, fixture_digest,
                         expected_json, adapter_config_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pack_id,
                        adapter,
                        name,
                        str(fixture),
                        digest,
                        json.dumps(expected, sort_keys=True),
                        json.dumps(config, sort_keys=True),
                        _now(),
                    ),
                )
                return int(result.lastrowid)
            connection.execute(
                """
                INSERT INTO sourcebridge_engine_test
                    (id, pack_id, adapter, name, fixture_path, fixture_digest,
                     expected_json, adapter_config_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test_id,
                    pack_id,
                    adapter,
                    name,
                    str(fixture),
                    digest,
                    json.dumps(expected, sort_keys=True),
                    json.dumps(config, sort_keys=True),
                    _now(),
                ),
            )
            return test_id
    except sqlite3.IntegrityError as error:
        raise EngineTestError(f"cannot register engine test: {error}") from error


def list_tests(database: Path, *, pack_id: str | None = None) -> list[dict[str, Any]]:
    """List discoverable test metadata without running adapters."""
    initialize(database)
    query = """
        SELECT id, pack_id, adapter, name, fixture_path, fixture_digest, expected_json
        FROM sourcebridge_engine_test
    """
    parameters: tuple[str, ...] = ()
    if pack_id:
        query += " WHERE pack_id = ?"
        parameters = (pack_id,)
    query += " ORDER BY id"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [
        {
            "id": row[0],
            "pack_id": row[1],
            "adapter": row[2],
            "name": row[3],
            "fixture_path": row[4],
            "fixture_digest": row[5],
            "expected": json.loads(row[6]),
        }
        for row in rows
    ]


def run(database: Path, test_id: int) -> dict[str, Any]:
    """Run a registered adapter and persist the normalized result."""
    initialize(database)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT adapter, fixture_path, fixture_digest, expected_json, adapter_config_json
            FROM sourcebridge_engine_test WHERE id = ?
            """,
            (test_id,),
        ).fetchone()
    if row is None:
        raise EngineTestError(f"unknown engine test ID `{test_id}`")

    adapter, fixture_text, expected_digest, expected_text, config_text = row
    fixture = Path(fixture_text)
    actual_digest = _digest(fixture)
    if actual_digest != expected_digest:
        raise EngineTestError(
            f"{fixture}: fixture digest changed; register a new expected fixture digest first"
        )
    expected = json.loads(expected_text)
    config = json.loads(config_text)
    started_at = _now()
    if adapter != "command":
        raise EngineTestError(f"engine test `{test_id}` uses unsupported adapter `{adapter}`")
    result, command = _run_command(test_id, fixture, expected, config)
    completed_at = _now()
    run_id = f"engine-test-run-{uuid4()}"
    try:
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO sourcebridge_engine_test_run
                    (id, test_id, status, command_json, result_json, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    test_id,
                    result["status"],
                    json.dumps(command),
                    json.dumps(result, sort_keys=True),
                    started_at,
                    completed_at,
                ),
            )
    except sqlite3.Error as error:
        raise EngineTestError(f"cannot record engine test run: {error}") from error
    return {"run_id": run_id, "test_id": test_id, **result}


def _run_command(
    test_id: int, fixture: Path, expected: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    argv = config.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
        raise EngineTestError(f"engine test `{test_id}`: command adapter requires string `argv`")
    values = {
        "case_id": str(test_id),
        "fixture_path": str(fixture),
        "fixture_dir": str(fixture.parent),
    }
    try:
        command = [value.format(**values) for value in argv]
    except KeyError as error:
        raise EngineTestError(
            f"engine test `{test_id}`: unsupported command placeholder `{error.args[0]}`"
        ) from error
    timeout = config.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or timeout <= 0:
        raise EngineTestError(f"engine test `{test_id}`: timeout_seconds must be a positive integer")
    try:
        process = subprocess.run(
            command,
            cwd=fixture.parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as error:
        raise EngineTestError(f"engine test `{test_id}`: cannot execute adapter: {error}") from error
    except subprocess.TimeoutExpired as error:
        return (
            {
                "status": "blocked",
                "expected": expected,
                "actual": {"timeout_seconds": timeout, "stdout": error.stdout or "", "stderr": error.stderr or ""},
            },
            command,
        )
    actual = {
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    checks = {
        key: actual.get(key) == value
        for key, value in expected.items()
        if key in {"exit_code", "stdout", "stderr"}
    }
    if not checks:
        raise EngineTestError(
            f"engine test `{test_id}`: expected JSON must declare exit_code, stdout, or stderr"
        )
    return (
        {"status": "passed" if all(checks.values()) else "failed", "expected": expected, "actual": actual},
        command,
    )
