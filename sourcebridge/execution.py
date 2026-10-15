"""Run approved profile actions and persist worker activity."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _command(value: str) -> tuple[list[str], str | None]:
    try:
        contract = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid approved command contract: {error}") from error
    if not isinstance(contract, dict) or set(contract) - {"argv", "cwd"}:
        raise ValueError("approved command must contain only argv and optional cwd")
    argv = contract.get("argv")
    cwd = contract.get("cwd")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("approved command argv must be a non-empty string list")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("approved command cwd must be a string")
    return argv, cwd


def run_approved(database: Path, run_id: str) -> int:
    if not database.is_file():
        raise ValueError(f"{database}: evidence database does not exist")
    engine = create_engine(f"sqlite+pysqlite:///{database.resolve()}")
    with engine.connect() as connection:
        run = connection.execute(
            text("SELECT command_json, status FROM sourcebridge_execution_run WHERE id = :id"),
            {"id": run_id},
        ).mappings().one_or_none()
    if run is None:
        raise ValueError(f"execution run `{run_id}` does not exist")
    if run["status"] != "queued":
        raise ValueError(f"execution run `{run_id}` is not queued")
    argv, cwd = _command(run["command_json"])
    now = _now()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sourcebridge_execution_run SET status = 'running', started_at = :now "
                "WHERE id = :id AND status = 'queued'"
            ),
            {"id": run_id, "now": now},
        )
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, shell=False,
        )
    except OSError:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sourcebridge_execution_run "
                    "SET status = 'failed', completed_at = :now WHERE id = :id"
                ),
                {"id": run_id, "now": _now()},
            )
        raise
    assert process.stdout is not None
    for line in process.stdout:
        _record_event(engine, run_id, line.rstrip())
    result = process.wait()
    status = "passed" if result == 0 else "failed"
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sourcebridge_execution_run SET status = :status, completed_at = :now "
                "WHERE id = :id"
            ),
            {"id": run_id, "status": status, "now": _now()},
        )
    return result


def _record_event(engine: Any, run_id: str, line: str) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        event = {"kind": "progress", "message": line, "metrics": {}}
    if not isinstance(event, dict):
        event = {"kind": "progress", "message": line, "metrics": {}}
    kind = event.get("kind", "progress")
    message = event.get("message", line)
    if kind not in {"started", "progress", "diagnostic", "retry", "blocked", "completed", "failed", "stopped"}:
        kind = "progress"
    if not isinstance(message, str):
        message = line
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sourcebridge_execution_event "
                "(id, run_id, worker_id, occurred_at, kind, message, metrics_json) "
                "VALUES (:id, :run_id, NULL, :now, :kind, :message, :metrics)"
            ),
            {
                "id": str(uuid4()), "run_id": run_id, "now": _now(), "kind": kind,
                "message": message, "metrics": json.dumps(event.get("metrics", {}), sort_keys=True),
            },
        )
