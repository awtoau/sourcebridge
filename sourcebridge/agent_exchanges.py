"""Immutable transcript records for local and host agent exchanges."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sourcebridge.database import write_transaction


def record_exchange(
    database: Path, *, work_item_id: str | None, local_worker_job_id: str | None,
    role: str, provider: str, local_only: bool, request: Any, response: Any,
    status: str, endpoint: str | None = None, model_name: str | None = None,
    model_digest: str | None = None, prompt_template_version: str | None = None,
    prompt_template_digest: str | None = None, prompt_tokens: int | None = None,
    generated_tokens: int | None = None, elapsed_ms: float | None = None,
    prior_exchange_id: str | None = None, error: Any | None = None,
    artifact_path: str | None = None, artifact_bytes: bytes | None = None,
) -> str:
    """Append one complete request/response transcript with verified digests."""

    if status not in {"completed", "rejected", "failed", "blocked"}:
        raise ValueError("unsupported exchange status")
    if not (work_item_id or local_worker_job_id):
        raise ValueError("an exchange requires a work item or local worker job")
    if (artifact_path is None) != (artifact_bytes is None):
        raise ValueError("artifact path and bytes must be supplied together")
    request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
    response_json = json.dumps(response, sort_keys=True, separators=(",", ":"))
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    exchange_id = f"agent-exchange-{uuid4()}"
    with write_transaction(database, enable_wal=True) as connection:
        connection.execute(
            """
            INSERT INTO sourcebridge_agent_exchange (
                id, work_item_id, local_worker_job_id, prior_exchange_id, role,
                provider, endpoint, local_only, model_name, model_digest,
                prompt_template_version, prompt_template_digest, request_json,
                request_digest, response_json, response_digest, artifact_path,
                artifact_digest, status, prompt_tokens, generated_tokens,
                elapsed_ms, error_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                exchange_id, work_item_id, local_worker_job_id, prior_exchange_id,
                role, provider, endpoint, int(local_only), model_name, model_digest,
                prompt_template_version, prompt_template_digest, request_json,
                digest(request_json), response_json, digest(response_json), artifact_path,
                None if artifact_bytes is None else hashlib.sha256(artifact_bytes).hexdigest(), status,
                prompt_tokens, generated_tokens, elapsed_ms,
                None if error is None else json.dumps(error, sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )
    return exchange_id
