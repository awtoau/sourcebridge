"""Repeatable SQLite contention benchmark used by tests and operations."""

from __future__ import annotations

from multiprocessing import get_all_start_methods, get_context
from pathlib import Path
import queue
import sqlite3
import time
from typing import Any

from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize
from sourcebridge.workers import DEFAULT_WORKERS


def _contention_worker(
    database: str,
    worker_id: int,
    writes: int,
    hold_seconds: float,
    start_event: Any,
    results: Any,
) -> None:
    start_event.wait()
    durations: list[float] = []
    errors: list[str] = []
    for sequence in range(writes):
        started = time.perf_counter()
        try:
            with write_transaction(Path(database)) as connection:
                connection.execute(
                    """
                    INSERT INTO sourcebridge_concurrency_probe
                        (id, worker_id, sequence, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (f"{worker_id}:{sequence}", worker_id, sequence, time.time()),
                )
                # Hold the writer lease briefly to make lock acquisition visible
                # without simulating an unrealistically long workflow.
                if hold_seconds:
                    time.sleep(hold_seconds)
        except sqlite3.Error as error:
            errors.append(str(error))
        durations.append((time.perf_counter() - started) * 1_000)
    results.put({"durations_ms": durations, "errors": errors})


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * percentile + 0.999999) - 1))
    return round(ordered[index], 3)


def run_contention_benchmark(
    database: Path,
    *,
    workers: int = DEFAULT_WORKERS,
    writes_per_worker: int = 25,
    hold_seconds: float = 0.001,
) -> dict[str, object]:
    """Run parallel independent writer processes and return JSON-safe evidence."""

    if workers <= 0 or writes_per_worker <= 0:
        raise ValueError("workers and writes_per_worker must be positive")
    if hold_seconds < 0:
        raise ValueError("hold_seconds must not be negative")
    initialize(database)
    with write_transaction(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sourcebridge_concurrency_probe (
                id TEXT PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                recorded_at REAL NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM sourcebridge_concurrency_probe")

    context = get_context("fork" if "fork" in get_all_start_methods() else "spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_contention_worker,
            args=(
                str(database),
                worker_id,
                writes_per_worker,
                hold_seconds,
                start_event,
                results,
            ),
        )
        for worker_id in range(workers)
    ]
    started = time.perf_counter()
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join()
    elapsed_ms = (time.perf_counter() - started) * 1_000

    outcomes: list[dict[str, Any]] = []
    for _ in processes:
        try:
            outcomes.append(results.get(timeout=1))
        except queue.Empty:
            outcomes.append({"durations_ms": [], "errors": ["worker produced no result"]})
    with read_transaction(database) as connection:
        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sourcebridge_concurrency_probe"
            ).fetchone()[0]
        )
    with write_transaction(database) as connection:
        connection.execute("DROP TABLE sourcebridge_concurrency_probe")

    durations = [
        duration
        for outcome in outcomes
        for duration in outcome["durations_ms"]
    ]
    errors = [
        error
        for outcome in outcomes
        for error in outcome["errors"]
    ]
    expected = workers * writes_per_worker
    return {
        "workers": workers,
        "writes_per_worker": writes_per_worker,
        "expected_writes": expected,
        "successful_writes": expected - len(errors),
        "failed_writes": len(errors),
        "row_count": row_count,
        "elapsed_ms": round(elapsed_ms, 3),
        "writer_transaction_p95_ms": _percentile(durations, 0.95),
        "writer_transaction_p99_ms": _percentile(durations, 0.99),
        "writer_transaction_max_ms": round(max(durations, default=0.0), 3),
        "errors": errors[:10],
    }
