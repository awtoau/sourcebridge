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


def import_catalogue(database: Path, catalogue: Path) -> dict[str, Any]:
    """Import a versioned pack catalogue into the operational test registry."""
    try:
        raw = catalogue.read_bytes()
    except OSError as error:
        raise EngineTestError(f"{catalogue}: cannot read test catalogue: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EngineTestError(f"{catalogue}: invalid test catalogue JSON: {error}") from error
    if not isinstance(payload, dict):
        raise EngineTestError(f"{catalogue}: test catalogue must be a JSON object")
    required = {"schema_version", "pack_id", "adapter", "tests"}
    unknown = set(payload) - required
    if required - set(payload) or unknown:
        raise EngineTestError(f"{catalogue}: test catalogue has missing or unsupported fields")
    if payload["schema_version"] != 1 or not isinstance(payload["pack_id"], str):
        raise EngineTestError(f"{catalogue}: unsupported test catalogue schema or pack ID")
    adapter = payload["adapter"]
    if not isinstance(adapter, dict) or set(adapter) != {"id", "config"}:
        raise EngineTestError(f"{catalogue}: adapter must contain only `id` and `config`")
    if not isinstance(adapter["id"], str) or not isinstance(adapter["config"], dict):
        raise EngineTestError(f"{catalogue}: adapter ID and config must be a string and object")
    cases = payload["tests"]
    if not isinstance(cases, list) or not cases:
        raise EngineTestError(f"{catalogue}: tests must be a non-empty list")

    root = catalogue.parent.resolve()
    catalogue_digest = hashlib.sha256(raw).hexdigest()
    seen_ids: set[int] = set()
    records: list[tuple[int, str, Path, dict[str, Any]]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or set(case) != {"id", "name", "fixture", "expected"}:
            raise EngineTestError(f"{catalogue}: test {index} has missing or unsupported fields")
        test_id = case["id"]
        if not isinstance(test_id, int) or test_id <= 0 or test_id in seen_ids:
            raise EngineTestError(f"{catalogue}: test {index} has an invalid or duplicate numeric ID")
        if not isinstance(case["name"], str) or not isinstance(case["fixture"], str):
            raise EngineTestError(f"{catalogue}: test {index} name and fixture must be strings")
        if not isinstance(case["expected"], dict):
            raise EngineTestError(f"{catalogue}: test {index} expected result must be an object")
        relative = Path(case["fixture"])
        if relative.is_absolute() or ".." in relative.parts:
            raise EngineTestError(f"{catalogue}: test {index} fixture must stay below the catalogue")
        fixture = (root / relative).resolve()
        try:
            fixture.relative_to(root)
        except ValueError as error:
            raise EngineTestError(f"{catalogue}: test {index} fixture escapes the catalogue") from error
        _digest(fixture)
        seen_ids.add(test_id)
        records.append((test_id, case["name"], fixture, case["expected"]))

    initialize(database)
    created = updated = 0
    try:
        with sqlite3.connect(database) as connection:
            for test_id, name, fixture, expected in records:
                current = connection.execute(
                    "SELECT pack_id FROM sourcebridge_engine_test WHERE id = ?",
                    (test_id,),
                ).fetchone()
                if current is not None and current[0] != payload["pack_id"]:
                    raise EngineTestError(
                        f"{catalogue}: test ID `{test_id}` already belongs to pack `{current[0]}`"
                    )
                values = (
                    payload["pack_id"],
                    adapter["id"],
                    name,
                    str(fixture),
                    _digest(fixture),
                    json.dumps(expected, sort_keys=True),
                    json.dumps(adapter["config"], sort_keys=True),
                    catalogue.name,
                    catalogue_digest,
                    _now(),
                    test_id,
                )
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO sourcebridge_engine_test
                            (pack_id, adapter, name, fixture_path, fixture_digest,
                             expected_json, adapter_config_json, catalogue_path,
                             catalogue_digest, created_at, id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    created += 1
                else:
                    connection.execute(
                        """
                        UPDATE sourcebridge_engine_test
                        SET pack_id = ?, adapter = ?, name = ?, fixture_path = ?,
                            fixture_digest = ?, expected_json = ?, adapter_config_json = ?,
                            catalogue_path = ?, catalogue_digest = ?, created_at = ?
                        WHERE id = ?
                        """,
                        values,
                    )
                    updated += 1
    except sqlite3.Error as error:
        raise EngineTestError(f"{catalogue}: cannot import test catalogue: {error}") from error
    return {
        "pack_id": payload["pack_id"],
        "catalogue_digest": catalogue_digest,
        "created": created,
        "updated": updated,
    }


def list_tests(database: Path, *, pack_id: str | None = None) -> list[dict[str, Any]]:
    """List discoverable test metadata without running adapters."""
    initialize(database)
    query = """
        SELECT id, pack_id, adapter, name, fixture_path, fixture_digest, expected_json,
               catalogue_path, catalogue_digest
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
            "catalogue_path": row[7],
            "catalogue_digest": row[8],
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
    if adapter == "command":
        result, command = _run_command(test_id, fixture, expected, config)
    elif adapter == "json-command":
        result, command = _run_json_command(test_id, fixture, expected, config)
    else:
        raise EngineTestError(f"engine test `{test_id}` uses unsupported adapter `{adapter}`")
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
    actual, command = _execute_command(test_id, fixture, config)
    if "timeout_seconds" in actual:
        return ({"status": "blocked", "expected": expected, "actual": actual}, command)
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


def _run_json_command(
    test_id: int, fixture: Path, expected: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    actual, command = _execute_command(test_id, fixture, config)
    if "timeout_seconds" in actual:
        return ({"status": "blocked", "expected": expected, "actual": actual}, command)
    if actual["exit_code"] != 0:
        return ({"status": "failed", "expected": expected, "actual": actual}, command)
    try:
        envelope = json.loads(actual["stdout"])
    except json.JSONDecodeError as error:
        raise EngineTestError(
            f"engine test `{test_id}`: json-command emitted invalid JSON: {error}"
        ) from error
    _validate_result_envelope(test_id, envelope)
    expected_sides = {
        side: value
        for side, value in expected.items()
        if side in {"source", "extraction", "target", "oracle"}
    }
    if not expected_sides:
        raise EngineTestError(
            f"engine test `{test_id}`: JSON adapter expected result must declare validation sides"
        )
    matches = {
        side: envelope[side]["status"] == expectation.get("status")
        for side, expectation in expected_sides.items()
        if isinstance(expectation, dict)
    }
    if len(matches) != len(expected_sides):
        raise EngineTestError(
            f"engine test `{test_id}`: each expected validation side must be an object"
        )
    return (
        {
            "status": "passed" if all(matches.values()) else "failed",
            "expected": expected,
            "actual": {**actual, "adapter_result": envelope},
        },
        command,
    )


def _validate_result_envelope(test_id: int, envelope: Any) -> None:
    sides = {"source", "extraction", "target", "oracle"}
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", *sides}:
        raise EngineTestError(f"engine test `{test_id}`: invalid JSON adapter result envelope")
    if envelope["schema_version"] != 1:
        raise EngineTestError(f"engine test `{test_id}`: unsupported adapter result schema")
    valid_statuses = {"passed", "failed", "blocked", "skipped"}
    for side in sides:
        value = envelope[side]
        if not isinstance(value, dict) or not isinstance(value.get("status"), str):
            raise EngineTestError(f"engine test `{test_id}`: `{side}` result needs a status")
        if value["status"] not in valid_statuses:
            raise EngineTestError(
                f"engine test `{test_id}`: `{side}` has invalid status `{value['status']}`"
            )


def _execute_command(
    test_id: int, fixture: Path, config: dict[str, Any]
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
                "timeout_seconds": timeout,
                "stdout": error.stdout or "",
                "stderr": error.stderr or "",
            },
            command,
        )
    return (
        {
            "exit_code": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        },
        command,
    )
