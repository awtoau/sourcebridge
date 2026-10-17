"""Run approved profile actions and persist worker activity."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from uuid import uuid4

from sourcebridge.database import read_transaction, write_transaction


_ACTIVE_RUN_STATUSES = {"running", "stopping"}
_TERMINAL_RUN_STATUSES = {"passed", "failed", "cancelled"}
_TERMINAL_WORKER_STATUSES = {"completed", "failed", "stopped"}


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


def _claim_run(database: Path, run_id: str) -> tuple[list[str], str | None]:
    """Claim one queued run without doing external work under the writer lease."""

    if not database.is_file():
        raise ValueError(f"{database}: evidence database does not exist")
    with write_transaction(database) as connection:
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
        claimed = connection.execute(
            "UPDATE sourcebridge_execution_run SET status = 'running', started_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (_now(), run_id),
        )
        if claimed.rowcount != 1:
            raise ValueError(
                f"execution run `{run_id}` was claimed by another worker; "
                "run-approved is single-owner"
            )
    return argv, cwd


def _claimed_command(
    database: Path, run_id: str
) -> tuple[list[str], str | None, str]:
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT command_json, status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
    if run is None:
        raise ValueError(f"execution run `{run_id}` does not exist")
    if run["status"] not in _ACTIVE_RUN_STATUSES:
        return [], None, run["status"]
    argv, cwd = _command(run["command_json"])
    return argv, cwd, run["status"]


def _finalize_run(
    database: Path,
    run_id: str,
    *,
    run_status: str,
    worker_status: str,
) -> None:
    with write_transaction(database) as connection:
        finished_at = _now()
        run = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None or run[0] not in _ACTIVE_RUN_STATUSES:
            return
        stop_requested = run[0] == "stopping"
        if stop_requested:
            # A stop request is authoritative even if the approved command exits
            # successfully after receiving SIGTERM.  Already-terminal workers
            # retain their terminal result; only unfinished work is stopped.
            connection.execute(
                "UPDATE sourcebridge_execution_worker "
                "SET status = 'stopped', updated_at = ? "
                "WHERE run_id = ? AND status IN ('idle', 'running')",
                (finished_at, run_id),
            )
        else:
            connection.execute(
                "UPDATE sourcebridge_execution_worker "
                "SET status = ?, updated_at = ? "
                "WHERE run_id = ? AND status IN ('idle', 'running')",
                (worker_status, finished_at, run_id),
            )
        connection.execute(
            "UPDATE sourcebridge_execution_run SET status = ?, completed_at = ? "
            "WHERE id = ? AND status IN ('running', 'stopping')",
            ("cancelled" if stop_requested else run_status, finished_at, run_id),
        )


def _record_started_event(
    database: Path, run_id: str, process: subprocess.Popen[str]
) -> None:
    """Persist the process group needed by a later reconnectable stop request."""

    metrics = {
        "pid": process.pid,
        "process_group_id": process.pid,
    }
    with write_transaction(database) as connection:
        status = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if status is None or status[0] not in _ACTIVE_RUN_STATUSES:
            return
        connection.execute(
            "INSERT INTO sourcebridge_execution_event "
            "(id, run_id, worker_id, occurred_at, kind, message, metrics_json) "
            "VALUES (?, ?, NULL, ?, 'started', ?, ?)",
            (
                str(uuid4()),
                run_id,
                _now(),
                "approved command started",
                json.dumps(metrics, sort_keys=True),
            ),
        )


def _stop_requested(database: Path, run_id: str) -> bool:
    with read_transaction(database) as connection:
        row = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
    return row is not None and row[0] == "stopping"


def _terminate_process(process: subprocess.Popen[str]) -> bool:
    """Ask one approved process group to stop without escalating to a kill."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            return False
    return True


def _execute_claimed(database: Path, run_id: str) -> int:
    argv, cwd, status = _claimed_command(database, run_id)
    if status not in _ACTIVE_RUN_STATUSES:
        # A queued launch can be stopped before its monitor gets scheduled.
        # It must not resurrect a cancelled run.
        return 0
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, shell=False, start_new_session=True,
        )
    except OSError:
        _finalize_run(
            database,
            run_id,
            run_status="failed",
            worker_status="failed",
        )
        raise
    _record_started_event(database, run_id, process)
    if _stop_requested(database, run_id):
        _terminate_process(process)
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


def run_approved(database: Path, run_id: str) -> int:
    """Run one queued approved action in the foreground."""

    _claim_run(database, run_id)
    return _execute_claimed(database, run_id)


def launch_approved(database: Path, run_id: str) -> dict[str, object]:
    """Launch one queued approved action and return without owning its monitor."""

    _claim_run(database, run_id)
    monitor = [
        sys.executable,
        "-m",
        "sourcebridge.execution",
        "--run-claimed",
        str(database),
        run_id,
    ]
    try:
        subprocess.Popen(
            monitor,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        _finalize_run(
            database,
            run_id,
            run_status="failed",
            worker_status="failed",
        )
        raise
    return {"run_id": run_id, "status": "running"}


def stop_run(
    database: Path,
    run_id: str,
    *,
    reason: str = "Controlled stop requested.",
) -> dict[str, object]:
    """Request a controlled stop and signal the current approved process group."""

    if not database.is_file():
        raise ValueError(f"{database}: evidence database does not exist")
    if not reason.strip():
        raise ValueError("stop reason must be a non-empty string")
    process_group_id: int | None = None
    with write_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT status FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"execution run `{run_id}` does not exist")
        current_status = run["status"]
        if current_status in _TERMINAL_RUN_STATUSES:
            return {"run_id": run_id, "status": current_status, "requested": False}
        if current_status == "queued":
            final_status = "cancelled"
            now = _now()
            connection.execute(
                "UPDATE sourcebridge_execution_run "
                "SET status = 'cancelled', completed_at = ? WHERE id = ?",
                (now, run_id),
            )
            connection.execute(
                "UPDATE sourcebridge_execution_worker "
                "SET status = 'stopped', updated_at = ? "
                "WHERE run_id = ? AND status IN ('idle', 'running')",
                (now, run_id),
            )
        elif current_status in _ACTIVE_RUN_STATUSES:
            final_status = "stopping"
            if current_status == "running":
                connection.execute(
                    "UPDATE sourcebridge_execution_run SET status = 'stopping' "
                    "WHERE id = ? AND status = 'running'",
                    (run_id,),
                )
            started = connection.execute(
                "SELECT metrics_json FROM sourcebridge_execution_event "
                "WHERE run_id = ? AND kind = 'started' "
                "AND message = 'approved command started' "
                "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if started is not None:
                try:
                    metrics = json.loads(started["metrics_json"])
                except (TypeError, json.JSONDecodeError):
                    metrics = {}
                if isinstance(metrics, dict):
                    candidate = metrics.get("process_group_id", metrics.get("pid"))
                    if isinstance(candidate, int) and candidate > 0:
                        process_group_id = candidate
        else:
            raise ValueError(f"execution run `{run_id}` has unsupported status `{current_status}`")
        now = _now()
        connection.execute(
            "INSERT INTO sourcebridge_execution_event "
            "(id, run_id, worker_id, occurred_at, kind, message, metrics_json) "
            "VALUES (?, ?, NULL, ?, 'stopped', ?, '{}')",
            (str(uuid4()), run_id, now, reason.strip()),
        )
    signal_sent = False
    if process_group_id is not None:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
            signal_sent = True
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(process_group_id, signal.SIGTERM)
                signal_sent = True
            except OSError:
                pass
    return {
        "run_id": run_id,
        "status": final_status,
        "requested": True,
        "signal_sent": signal_sent,
    }


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
        if run_state is None or run_state[0] not in _ACTIVE_RUN_STATUSES:
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
            source_unit_id = event.get("source_unit_id")
            if not isinstance(source_unit_id, str) or not source_unit_id.strip():
                source_unit_id = None
            elif connection.execute(
                "SELECT 1 FROM sourcebridge_source_unit "
                "WHERE id = ? AND variant_id = "
                "(SELECT variant_id FROM sourcebridge_execution_run WHERE id = ?)",
                (source_unit_id, run_id),
            ).fetchone() is None:
                source_unit_id = None
            current_work_item_id = event.get("current_work_item_id")
            if not isinstance(current_work_item_id, str) or not current_work_item_id.strip():
                current_work_item_id = None
            elif connection.execute(
                "SELECT 1 FROM sourcebridge_work_item WHERE id = ?",
                (current_work_item_id,),
            ).fetchone() is None:
                current_work_item_id = None
            existing_worker = connection.execute(
                "SELECT status, source_unit_id, current_work_item_id "
                "FROM sourcebridge_execution_worker "
                "WHERE run_id = ? AND worker_slot = ?",
                (run_id, worker_slot),
            ).fetchone()
            if existing_worker is not None:
                if existing_worker[0] in _TERMINAL_WORKER_STATUSES:
                    worker_status = existing_worker[0]
                source_unit_id = source_unit_id or existing_worker[1]
                current_work_item_id = current_work_item_id or existing_worker[2]
                connection.execute(
                    "UPDATE sourcebridge_execution_worker "
                    "SET status = ?, source_unit_id = ?, current_work_item_id = ?, "
                    "units_completed = ?, updated_at = ? "
                    "WHERE run_id = ? AND worker_slot = ?",
                    (
                        worker_status,
                        source_unit_id,
                        current_work_item_id,
                        units_completed,
                        _now(),
                        run_id,
                        worker_slot,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO sourcebridge_execution_worker "
                    "(id, run_id, worker_slot, status, source_unit_id, current_work_item_id, "
                    "units_completed, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        worker_id,
                        run_id,
                        worker_slot,
                        worker_status,
                        source_unit_id,
                        current_work_item_id,
                        units_completed,
                        _now(),
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


def _worker_main(arguments: list[str]) -> int:
    if len(arguments) != 3 or arguments[0] != "--run-claimed":
        print(
            "usage: python -m sourcebridge.execution --run-claimed DATABASE RUN_ID",
            file=sys.stderr,
        )
        return 2
    database = Path(arguments[1])
    run_id = arguments[2]
    try:
        return _execute_claimed(database, run_id)
    except BaseException as error:
        try:
            _finalize_run(
                database,
                run_id,
                run_status="failed",
                worker_status="failed",
            )
        except (OSError, sqlite3.Error):
            pass
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
