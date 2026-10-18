"""Durable, bounded claims for the local-only worker queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import uuid4

from sourcebridge.database import write_transaction


def _now() -> datetime:
    return datetime.now(UTC)


def claim_ready_job(
    database: Path, *, owner: str, lease_seconds: int = 900
) -> dict[str, object] | None:
    """Atomically claim one dependency-satisfied job, or return ``None``.

    A lease expiry permits one bounded subsequent attempt.  Callers must append
    a terminal event; this helper never manufactures retries or jobs.
    """

    if not owner.strip():
        raise ValueError("owner must be non-empty")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = _now()
    now_text = now.isoformat()
    with write_transaction(database, enable_wal=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            WITH latest AS (
                SELECT event.* FROM sourcebridge_local_worker_job_event AS event
                JOIN (
                    SELECT job_id, MAX(sequence) AS sequence
                    FROM sourcebridge_local_worker_job_event
                    GROUP BY job_id
                ) AS newest
                  ON newest.job_id = event.job_id AND newest.sequence = event.sequence
            )
            SELECT job.*, latest.sequence, latest.status, latest.attempt
            FROM sourcebridge_local_worker_job AS job
            JOIN latest ON latest.job_id = job.id
            WHERE latest.attempt < job.max_attempts
              AND (
                  latest.status = 'queued'
                  OR (latest.status = 'claimed' AND latest.lease_expires_at < ?)
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM sourcebridge_local_worker_job_dependency AS dependency
                  LEFT JOIN latest AS prerequisite
                    ON prerequisite.job_id = dependency.prerequisite_job_id
                  WHERE dependency.job_id = job.id
                    AND (prerequisite.status IS NULL OR prerequisite.status != 'completed')
              )
            ORDER BY job.created_at, job.id
            LIMIT 1
            """,
            (now_text,),
        ).fetchone()
        if row is None:
            return None
        event = {
            "id": f"local-worker-job-event-{uuid4()}",
            "job_id": row["id"],
            "sequence": row["sequence"] + 1,
            "status": "claimed",
            "owner": owner.strip(),
            "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            "attempt": row["attempt"] + 1,
            "rationale": "claimed by local worker",
            "recorded_at": now_text,
        }
        connection.execute(
            """
            INSERT INTO sourcebridge_local_worker_job_event
                (id, job_id, sequence, status, owner, lease_expires_at,
                 attempt, result_digest, rationale, recorded_at)
            VALUES
                (:id, :job_id, :sequence, :status, :owner, :lease_expires_at,
                 :attempt, NULL, :rationale, :recorded_at)
            """,
            event,
        )
        return {**dict(row), "claim": event}


def record_terminal_job_event(
    database: Path,
    *,
    job_id: str,
    owner: str,
    attempt: int,
    status: str,
    rationale: str,
    result_digest: str | None = None,
) -> None:
    """Append a terminal result for an owned claimed attempt."""

    if status not in {"completed", "failed", "blocked"}:
        raise ValueError("status must be completed, failed, or blocked")
    if not all(isinstance(value, str) and value.strip() for value in (job_id, owner, rationale)):
        raise ValueError("job_id, owner, and rationale must be non-empty")
    with write_transaction(database, enable_wal=True) as connection:
        row = connection.execute(
            """
            SELECT sequence, status, owner, attempt
            FROM sourcebridge_local_worker_job_event
            WHERE job_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None or row[1] != "claimed" or row[2] != owner or row[3] != attempt:
            raise ValueError("job does not have the specified active claim")
        connection.execute(
            """
            INSERT INTO sourcebridge_local_worker_job_event
                (id, job_id, sequence, status, owner, lease_expires_at,
                 attempt, result_digest, rationale, recorded_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                f"local-worker-job-event-{uuid4()}",
                job_id,
                row[0] + 1,
                status,
                owner,
                attempt,
                result_digest,
                rationale,
                _now().isoformat(),
            ),
        )
