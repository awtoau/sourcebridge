"""Language-neutral recording and reporting for conversion outcomes."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize


class ConversionCensusError(ValueError):
    """Raised when a conversion outcome cannot be recorded or reported."""


_SIDES = frozenset({"source", "target", "boundary", "oracle"})
_STATUSES = frozenset({"passed", "failed", "blocked", "skipped"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _id(prefix: str, value: str) -> str:
    return f"{prefix}-{_digest(value)[:24]}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def record_outcome(
    database: Path,
    *,
    decision_id: str,
    artifact_path: str,
    artifact_content: str,
    generator_digest: str,
    validations: Iterable[dict[str, Any]],
    gaps: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Persist one emitted target artifact with validation and explicit gaps."""

    if not decision_id or not artifact_path or not generator_digest:
        raise ConversionCensusError("decision ID, artifact path, and generator digest are required")
    artifact_digest = _digest(artifact_content)
    recorded_at = _now()
    artifact_id = _id("artifact", f"{decision_id}:{artifact_path}:{artifact_digest}")
    validation_rows = list(validations)
    gap_rows = list(gaps)
    _validate_validations(validation_rows)
    _validate_gaps(gap_rows)
    initialize(database)

    try:
        with write_transaction(database) as connection:
            decision = connection.execute(
                """
                SELECT decision.source_unit_id, variant.corpus_id
                FROM sourcebridge_component_decision AS decision
                JOIN sourcebridge_source_unit AS unit ON unit.id = decision.source_unit_id
                JOIN sourcebridge_variant AS variant ON variant.id = unit.variant_id
                WHERE decision.id = ?
                """,
                (decision_id,),
            ).fetchone()
            if decision is None:
                raise ConversionCensusError(f"unknown component decision `{decision_id}`")
            source_unit_id, corpus_id = decision
            _validate_gap_units(connection, gap_rows, source_unit_id)

            generated_fragment_id = _id(
                "fragment",
                f"{corpus_id}:generated:{artifact_path}:{artifact_digest}",
            )
            connection.execute(
                """
                INSERT INTO sourcebridge_evidence_fragment
                    (id, corpus_id, kind, logical_path, start_line, start_column,
                     end_line, end_column, content, content_digest, created_at)
                VALUES (?, ?, 'generated', ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    generated_fragment_id,
                    corpus_id,
                    artifact_path,
                    artifact_content,
                    artifact_digest,
                    recorded_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO sourcebridge_generated_artifact
                    (id, decision_id, artifact_path, artifact_digest, generator_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id, artifact_path, artifact_digest) DO NOTHING
                """,
                (artifact_id, decision_id, artifact_path, artifact_digest, generator_digest, recorded_at),
            )
            validation_ids: dict[str, str] = {}
            for validation in validation_rows:
                validation_id = _id(
                    "validation",
                    f"{artifact_id}:{validation['side']}:{validation['validator']}:"
                    f"{validation['evidence_digest']}",
                )
                connection.execute(
                    """
                    INSERT INTO sourcebridge_validation_result
                        (id, decision_id, side, validator, status, evidence_path,
                         evidence_digest, details_json, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        validation_id,
                        decision_id,
                        validation["side"],
                        validation["validator"],
                        validation["status"],
                        validation["evidence_path"],
                        validation["evidence_digest"],
                        _json(validation.get("details", {})),
                        recorded_at,
                    ),
                )
                validation_ids[validation["side"]] = validation_id

            if gap_rows and "target" not in validation_ids:
                raise ConversionCensusError("explicit gaps require a target validation result")
            for gap in gap_rows:
                diagnostic_id = _id(
                    "diagnostic",
                    f"{validation_ids['target']}:{gap['failure_signature']}:{gap['source_unit_id']}",
                )
                connection.execute(
                    """
                    INSERT INTO sourcebridge_diagnostic_occurrence
                        (id, validation_result_id, source_unit_id, source_fragment_id,
                         generated_artifact_id, generated_fragment_id, phase, tool_identity,
                         diagnostic_code, severity, failure_signature, message, details_json,
                         recorded_at)
                    VALUES (?, ?, ?, NULL, ?, ?, 'generation', ?, NULL, 'error', ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        diagnostic_id,
                        validation_ids["target"],
                        gap["source_unit_id"],
                        artifact_id,
                        generated_fragment_id,
                        gap["tool_identity"],
                        gap["failure_signature"],
                        gap["message"],
                        _json(gap.get("details", {})),
                        recorded_at,
                    ),
                )
    except (OSError, sqlite3.Error) as error:
        raise ConversionCensusError(f"{database}: cannot record conversion outcome: {error}") from error
    return {
        "artifact_id": artifact_id,
        "artifact_digest": artifact_digest,
        "generated_fragment_id": generated_fragment_id,
    }


def report(database: Path) -> dict[str, Any]:
    """Return a deterministic corpus-wide conversion census."""

    initialize(database)
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            outcomes = Counter(
                f"{row[0]}:{row[1]}"
                for row in connection.execute(
                    """
                    SELECT validation.side, validation.status
                    FROM sourcebridge_validation_result AS validation
                    JOIN sourcebridge_component_decision AS decision
                      ON decision.id = validation.decision_id
                    """
                )
            )
            gaps = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT failure_signature, phase, severity, COUNT(*) AS count
                    FROM sourcebridge_diagnostic_occurrence
                    GROUP BY failure_signature, phase, severity
                    ORDER BY count DESC, failure_signature
                    """
                )
            ]
            artifacts = connection.execute(
                "SELECT COUNT(*) FROM sourcebridge_generated_artifact"
            ).fetchone()[0]
            units = connection.execute("SELECT COUNT(*) FROM sourcebridge_source_unit").fetchone()[0]
    except (OSError, sqlite3.Error) as error:
        raise ConversionCensusError(f"{database}: cannot report conversion census: {error}") from error
    return {
        "schema_version": 1,
        "source_units": units,
        "generated_artifacts": artifacts,
        "validation_outcomes": dict(sorted(outcomes.items())),
        "gaps": gaps,
    }


def _validate_validations(validations: Iterable[dict[str, Any]]) -> None:
    values = list(validations)
    if not values:
        raise ConversionCensusError("at least one validation result is required")
    for value in values:
        if not isinstance(value, dict):
            raise ConversionCensusError("validation results must be objects")
        if value.get("side") not in _SIDES or value.get("status") not in _STATUSES:
            raise ConversionCensusError("validation has an unsupported side or status")
        for key in ("validator", "evidence_path", "evidence_digest"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise ConversionCensusError(f"validation requires non-empty `{key}`")


def _validate_gaps(gaps: Iterable[dict[str, Any]]) -> None:
    for gap in gaps:
        if not isinstance(gap, dict):
            raise ConversionCensusError("gaps must be objects")
        for key in ("source_unit_id", "tool_identity", "failure_signature", "message"):
            if not isinstance(gap.get(key), str) or not gap[key]:
                raise ConversionCensusError(f"gap requires non-empty `{key}`")


def _validate_gap_units(
    connection: sqlite3.Connection,
    gaps: Iterable[dict[str, Any]],
    decision_source_unit_id: str,
) -> None:
    for gap in gaps:
        source_unit_id = gap["source_unit_id"]
        if source_unit_id != decision_source_unit_id:
            exists = connection.execute(
                "SELECT 1 FROM sourcebridge_source_unit WHERE id = ?",
                (source_unit_id,),
            ).fetchone()
            if exists is None:
                raise ConversionCensusError(f"unknown gap source unit `{source_unit_id}`")
            raise ConversionCensusError(
                "gap source unit must match the emitted artifact's component decision"
            )
