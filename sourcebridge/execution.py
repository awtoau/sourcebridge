"""Run approved profile actions and persist worker activity."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
from uuid import uuid4

from sourcebridge.database import read_transaction, write_transaction


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


def _finalize_run(
    database: Path,
    run_id: str,
    *,
    run_status: str,
    worker_status: str,
) -> None:
    with write_transaction(database) as connection:
        finished_at = _now()
        connection.execute(
            "UPDATE sourcebridge_execution_worker "
            "SET status = ?, updated_at = ? "
            "WHERE run_id = ? AND status IN ('idle', 'running')",
            (worker_status, finished_at, run_id),
        )
        connection.execute(
            "UPDATE sourcebridge_execution_run SET status = ?, completed_at = ? "
            "WHERE id = ? AND status = 'running'",
            (run_status, finished_at, run_id),
        )


def run_approved(database: Path, run_id: str) -> int:
    if not database.is_file():
        raise ValueError(f"{database}: evidence database does not exist")
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT command_json, status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
    if run is None:
        raise ValueError(f"execution run `{run_id}` does not exist")
    if run["status"] != "queued":
        raise ValueError(f"execution run `{run_id}` is not queued")
    argv, cwd = _command(run["command_json"])
    now = _now()
    with write_transaction(database) as connection:
        claimed = connection.execute(
            "UPDATE sourcebridge_execution_run SET status = 'running', started_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (now, run_id),
        )
        if claimed.rowcount != 1:
            raise ValueError(
                f"execution run `{run_id}` was claimed by another worker; "
                "run-approved is single-owner"
            )
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, shell=False,
        )
    except OSError:
        _finalize_run(
            database,
            run_id,
            run_status="failed",
            worker_status="failed",
        )
        raise
    assert process.stdout is not None
    for line in process.stdout:
        _record_event(database, run_id, line.rstrip())
    process.stdout.close()
    result = process.wait()
    status = "passed" if result == 0 else "failed"
    _finalize_run(
        database,
        run_id,
        run_status=status,
        worker_status="completed" if result == 0 else "failed",
    )
    return result


def recover_interrupted(
    database: Path,
    run_id: str,
    *,
    reason: str = "Recovered after an interrupted run.",
) -> dict[str, str]:
    """Mark a non-terminal run cancelled so it can no longer be reported as complete."""

    if not database.is_file():
        raise ValueError(f"{database}: evidence database does not exist")
    if not reason.strip():
        raise ValueError("recovery reason must be a non-empty string")
    with write_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"execution run `{run_id}` does not exist")
        if run["status"] in {"passed", "failed", "cancelled"}:
            raise ValueError(f"execution run `{run_id}` is already terminal")
        now = _now()
        connection.execute(
            "UPDATE sourcebridge_execution_run "
            "SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (now, run_id),
        )
        connection.execute(
            "UPDATE sourcebridge_execution_worker "
            "SET status = 'stopped', updated_at = ? "
            "WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'stopped')",
            (now, run_id),
        )
        connection.execute(
            "INSERT INTO sourcebridge_execution_event "
            "(id, run_id, worker_id, occurred_at, kind, message, metrics_json) "
            "VALUES (?, ?, NULL, ?, 'stopped', ?, '{}')",
            (str(uuid4()), run_id, now, reason.strip()),
        )
    return {"run_id": run_id, "status": "cancelled"}


def _record_event(database: Path, run_id: str, line: str) -> None:
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
    with write_transaction(database) as connection:
        run_state = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run_state is None or run_state[0] != "running":
            # A recovery command may have cancelled the run while the external
            # process was still draining stdout.  Do not append post-recovery
            # events to a terminal snapshot.
            return
        worker_id = None
        worker_slot = event.get("worker_slot")
        if isinstance(worker_slot, int) and worker_slot >= 0:
            worker_id = f"{run_id}:worker:{worker_slot}"
            worker_status = {
                "blocked": "blocked",
                "failed": "failed",
                "stopped": "stopped",
                "completed": "completed",
            }.get(kind, "running")
            units_completed = event.get("units_completed", 0)
            if not isinstance(units_completed, int) or units_completed < 0:
                units_completed = 0
            connection.execute(
                "INSERT INTO sourcebridge_execution_worker "
                "(id, run_id, worker_slot, status, source_unit_id, current_work_item_id, "
                "units_completed, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?) "
                "ON CONFLICT(run_id, worker_slot) DO UPDATE SET "
                "status = excluded.status, units_completed = excluded.units_completed, "
                "updated_at = excluded.updated_at",
                (
                    worker_id, run_id, worker_slot, worker_status,
                    units_completed, _now(),
                ),
            )
        connection.execute(
            "INSERT INTO sourcebridge_execution_event "
            "(id, run_id, worker_id, occurred_at, kind, message, metrics_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()), run_id, worker_id, _now(), kind, message,
                json.dumps(event.get("metrics", {}), sort_keys=True),
            ),
        )
