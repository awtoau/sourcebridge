"""Read-only diagnostic evidence queries for reports and workbench clients."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


class EvidenceReportError(ValueError):
    """Raised when a diagnostic report cannot be read."""


def _engine(database: Path):
    if not database.is_file():
        raise EvidenceReportError(f"{database}: evidence database does not exist")
    return create_engine(f"sqlite+pysqlite:///{database.resolve()}")


def diagnostic_groups(database: Path) -> list[dict[str, Any]]:
    query = text(
        """
        SELECT failure_signature, phase, severity, COUNT(*) AS occurrence_count,
               MIN(recorded_at) AS first_recorded_at, MAX(recorded_at) AS last_recorded_at
        FROM sourcebridge_diagnostic_occurrence
        GROUP BY failure_signature, phase, severity
        ORDER BY occurrence_count DESC, failure_signature
        """
    )
    with _engine(database).connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def diagnostic_occurrences(database: Path, signature: str) -> list[dict[str, Any]]:
    query = text(
        """
        SELECT
            diagnostic.id,
            diagnostic.phase,
            diagnostic.tool_identity,
            diagnostic.diagnostic_code,
            diagnostic.severity,
            diagnostic.failure_signature,
            diagnostic.message,
            diagnostic.details_json,
            diagnostic.recorded_at,
            source.logical_name,
            source.source_path,
            source.source_span,
            source_fragment.logical_path AS source_fragment_path,
            source_fragment.start_line AS source_start_line,
            source_fragment.end_line AS source_end_line,
            source_fragment.content AS source_excerpt,
            artifact.artifact_path,
            generated_fragment.start_line AS generated_start_line,
            generated_fragment.end_line AS generated_end_line,
            generated_fragment.content AS generated_excerpt,
            validation.side AS validation_side,
            validation.validator,
            validation.evidence_path,
            validation.evidence_digest
        FROM sourcebridge_diagnostic_occurrence AS diagnostic
        JOIN sourcebridge_source_unit AS source ON source.id = diagnostic.source_unit_id
        JOIN sourcebridge_validation_result AS validation
            ON validation.id = diagnostic.validation_result_id
        LEFT JOIN sourcebridge_evidence_fragment AS source_fragment
            ON source_fragment.id = diagnostic.source_fragment_id
        LEFT JOIN sourcebridge_generated_artifact AS artifact
            ON artifact.id = diagnostic.generated_artifact_id
        LEFT JOIN sourcebridge_evidence_fragment AS generated_fragment
            ON generated_fragment.id = diagnostic.generated_fragment_id
        WHERE diagnostic.failure_signature = :signature
        ORDER BY source.source_path, source.source_span, diagnostic.id
        """
    )
    with _engine(database).connect() as connection:
        return [
            dict(row)
            for row in connection.execute(query, {"signature": signature}).mappings()
        ]
