"""Read-only diagnostic and committed-evidence reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Sequence

from sourcebridge.database import read_transaction


class EvidenceReportError(ValueError):
    """Raised when a diagnostic report cannot be read."""


def _rows(
    database: Path,
    query: str,
    parameters: dict[str, object] | tuple[object, ...] = (),
) -> list[dict[str, Any]]:
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
    except FileNotFoundError as error:
        raise EvidenceReportError(str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise EvidenceReportError(f"{database}: cannot read evidence database: {error}") from error
    return [dict(zip(row.keys(), row)) for row in rows]


def diagnostic_groups(database: Path) -> list[dict[str, Any]]:
    query = """
        SELECT failure_signature, phase, severity, COUNT(*) AS occurrence_count,
               MIN(recorded_at) AS first_recorded_at, MAX(recorded_at) AS last_recorded_at
        FROM sourcebridge_diagnostic_occurrence
        GROUP BY failure_signature, phase, severity
        ORDER BY occurrence_count DESC, failure_signature
        """
    return _rows(database, query)


_DIAGNOSTIC_OCCURRENCE_QUERY = """
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
        source.id AS source_unit_id,
        source.logical_name,
        source.source_path,
        source.source_span,
        source.source_digest,
        source.kind AS source_kind,
        corpus.revision AS corpus_revision,
        variant.id AS variant_id,
        variant.frontend_pack_id AS variant_frontend_pack_id,
        variant.compiler_identity,
        variant.target_abi,
        variant.pointer_width,
        variant.byte_order,
        variant.configuration_digest AS variant_configuration_digest,
        variant.generated_input_digest,
        decision.id AS decision_id,
        profile.id AS profile_id,
        profile.profile_name,
        profile.source_frontend_pack_id,
        profile.target_backend_pack_id,
        profile.rule_layers_digest,
        profile.runtime_adapters_digest,
        profile.options_digest,
        source_fragment.id AS source_fragment_id,
        source_fragment.logical_path AS source_fragment_path,
        source_fragment.start_line AS source_start_line,
        source_fragment.start_column AS source_start_column,
        source_fragment.end_line AS source_end_line,
        source_fragment.end_column AS source_end_column,
        source_fragment.content AS source_excerpt,
        source_fragment.content_digest AS source_excerpt_digest,
        artifact.id AS generated_artifact_id,
        artifact.artifact_path,
        artifact.artifact_digest,
        artifact.generator_digest,
        generated_fragment.id AS generated_fragment_id,
        generated_fragment.logical_path AS generated_fragment_path,
        generated_fragment.start_line AS generated_start_line,
        generated_fragment.start_column AS generated_start_column,
        generated_fragment.end_line AS generated_end_line,
        generated_fragment.end_column AS generated_end_column,
        generated_fragment.content AS generated_excerpt,
        generated_fragment.content_digest AS generated_excerpt_digest,
        validation.id AS validation_result_id,
        validation.side AS validation_side,
        validation.validator,
        validation.status AS validation_status,
        validation.evidence_path,
        validation.evidence_digest,
        validation.details_json AS validation_details_json,
        validation.recorded_at AS validation_recorded_at,
        (
            SELECT provenance.run_id
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_run_id,
        (
            SELECT provenance.source_revision
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_source_revision,
        (
            SELECT provenance.variant_digest
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_variant_digest,
        (
            SELECT provenance.profile_digest
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_profile_digest,
        (
            SELECT provenance.rule_digest
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_rule_digest,
        (
            SELECT provenance.artifact_digest
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_artifact_digest,
        (
            SELECT provenance.evidence_digest
            FROM sourcebridge_work_item_evidence AS link
            JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = link.execution_run_id
            WHERE link.diagnostic_occurrence_id = diagnostic.id
            ORDER BY link.recorded_at DESC, link.id DESC
            LIMIT 1
        ) AS provenance_evidence_digest
    FROM sourcebridge_diagnostic_occurrence AS diagnostic
    JOIN sourcebridge_source_unit AS source ON source.id = diagnostic.source_unit_id
    JOIN sourcebridge_validation_result AS validation
        ON validation.id = diagnostic.validation_result_id
    JOIN sourcebridge_component_decision AS decision
        ON decision.id = validation.decision_id
    JOIN sourcebridge_profile AS profile
        ON profile.id = decision.profile_id
    JOIN sourcebridge_variant AS variant
        ON variant.id = source.variant_id
    JOIN sourcebridge_corpus AS corpus
        ON corpus.id = variant.corpus_id
    LEFT JOIN sourcebridge_evidence_fragment AS source_fragment
        ON source_fragment.id = diagnostic.source_fragment_id
    LEFT JOIN sourcebridge_generated_artifact AS artifact
        ON artifact.id = diagnostic.generated_artifact_id
    LEFT JOIN sourcebridge_evidence_fragment AS generated_fragment
        ON generated_fragment.id = diagnostic.generated_fragment_id
    WHERE diagnostic.failure_signature = :signature
    ORDER BY source.source_path, source.source_span, diagnostic.id
    """


def _json_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _diagnostic_reviews(
    connection: sqlite3.Connection,
    occurrence_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, disposition, rationale, reviewed_by, created_at
        FROM sourcebridge_diagnostic_review
        WHERE diagnostic_occurrence_id = ?
        ORDER BY created_at, id
        """,
        (occurrence_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _diagnostic_occurrence_report(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    reviews = _diagnostic_reviews(connection, str(row["id"]))
    source_revision = row["provenance_source_revision"] or row["corpus_revision"]
    variant_digest = (
        row["provenance_variant_digest"] or row["variant_configuration_digest"]
    )
    report: dict[str, Any] = {
        # These flat fields preserve the original compact report contract.
        "id": row["id"],
        "phase": row["phase"],
        "tool_identity": row["tool_identity"],
        "diagnostic_code": row["diagnostic_code"],
        "severity": row["severity"],
        "failure_signature": row["failure_signature"],
        "message": row["message"],
        "details_json": row["details_json"],
        "details": _json_value(row["details_json"]),
        "recorded_at": row["recorded_at"],
        "logical_name": row["logical_name"],
        "source_path": row["source_path"],
        "source_span": row["source_span"],
        "source_fragment_path": row["source_fragment_path"],
        "source_start_line": row["source_start_line"],
        "source_end_line": row["source_end_line"],
        "source_excerpt": row["source_excerpt"],
        "artifact_path": row["artifact_path"],
        "generated_start_line": row["generated_start_line"],
        "generated_end_line": row["generated_end_line"],
        "generated_excerpt": row["generated_excerpt"],
        "validation_side": row["validation_side"],
        "validator": row["validator"],
        "evidence_path": row["evidence_path"],
        "evidence_digest": row["evidence_digest"],
        "source_revision": source_revision,
        "variant_digest": variant_digest,
        "profile_digest": row["provenance_profile_digest"],
        "generator_digest": row["generator_digest"],
        "raw_validation_evidence": {
            "path": row["evidence_path"],
            "digest": row["evidence_digest"],
            "details_json": row["validation_details_json"],
        },
        # Nested fields make the drill-down self-describing without requiring
        # another query for provenance or the exact reviewed excerpts.
        "source": {
            "unit_id": row["source_unit_id"],
            "logical_name": row["logical_name"],
            "path": row["source_path"],
            "span": row["source_span"],
            "digest": row["source_digest"],
            "kind": row["source_kind"],
            "excerpt": (
                {
                    "id": row["source_fragment_id"],
                    "path": row["source_fragment_path"],
                    "start_line": row["source_start_line"],
                    "start_column": row["source_start_column"],
                    "end_line": row["source_end_line"],
                    "end_column": row["source_end_column"],
                    "content": row["source_excerpt"],
                    "content_digest": row["source_excerpt_digest"],
                }
                if row["source_fragment_id"] is not None
                else None
            ),
        },
        "generated": (
            {
                "artifact": {
                    "id": row["generated_artifact_id"],
                    "path": row["artifact_path"],
                    "artifact_digest": row["artifact_digest"],
                    "generator_digest": row["generator_digest"],
                },
                "excerpt": (
                    {
                        "id": row["generated_fragment_id"],
                        "path": row["generated_fragment_path"],
                        "start_line": row["generated_start_line"],
                        "start_column": row["generated_start_column"],
                        "end_line": row["generated_end_line"],
                        "end_column": row["generated_end_column"],
                        "content": row["generated_excerpt"],
                        "content_digest": row["generated_excerpt_digest"],
                    }
                    if row["generated_fragment_id"] is not None
                    else None
                ),
            }
            if row["generated_artifact_id"] is not None
            else None
        ),
        "validation": {
            "id": row["validation_result_id"],
            "side": row["validation_side"],
            "validator": row["validator"],
            "status": row["validation_status"],
            "evidence_path": row["evidence_path"],
            "evidence_digest": row["evidence_digest"],
            "details_json": row["validation_details_json"],
            "details": _json_value(row["validation_details_json"]),
            "recorded_at": row["validation_recorded_at"],
        },
        "provenance": {
            "source_revision": source_revision,
            "variant": {
                "id": row["variant_id"],
                "frontend_pack_id": row["variant_frontend_pack_id"],
                "compiler_identity": row["compiler_identity"],
                "target_abi": row["target_abi"],
                "pointer_width": row["pointer_width"],
                "byte_order": row["byte_order"],
                "configuration_digest": row["variant_configuration_digest"],
                "generated_input_digest": row["generated_input_digest"],
                "variant_digest": variant_digest,
            },
            "profile": {
                "id": row["profile_id"],
                "name": row["profile_name"],
                "source_frontend_pack_id": row["source_frontend_pack_id"],
                "target_backend_pack_id": row["target_backend_pack_id"],
                "rule_layers_digest": row["rule_layers_digest"],
                "runtime_adapters_digest": row["runtime_adapters_digest"],
                "options_digest": row["options_digest"],
                "profile_digest": row["provenance_profile_digest"],
            },
            "generator_digest": row["generator_digest"],
            "run": (
                {
                    "id": row["provenance_run_id"],
                    "source_revision": row["provenance_source_revision"],
                    "variant_digest": row["provenance_variant_digest"],
                    "profile_digest": row["provenance_profile_digest"],
                    "rule_digest": row["provenance_rule_digest"],
                    "artifact_digest": row["provenance_artifact_digest"],
                    "evidence_digest": row["provenance_evidence_digest"],
                }
                if row["provenance_run_id"] is not None
                else None
            ),
        },
        "reviews": reviews,
        "review_status": reviews[-1]["disposition"] if reviews else "unreviewed",
    }
    return report


def _diagnostic_rows(
    connection: sqlite3.Connection,
    signature: str,
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        _DIAGNOSTIC_OCCURRENCE_QUERY,
        {"signature": signature},
    ).fetchall()
    return [_diagnostic_occurrence_report(connection, row) for row in rows]


def diagnostic_occurrences(database: Path, signature: str) -> list[dict[str, Any]]:
    signature = signature.strip() if isinstance(signature, str) else ""
    if not signature:
        raise EvidenceReportError("failure signature must be a non-empty string")
    try:
        with read_transaction(database) as connection:
            return _diagnostic_rows(connection, signature)
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(
            f"{database}: cannot read diagnostic occurrences: {error}"
        ) from error


def diagnostic_drilldown(database: Path, signature: str) -> dict[str, Any]:
    """Return one compact aggregate-to-occurrence diagnostic report.

    Unknown signatures are a valid empty result.  This keeps report consumers
    able to probe a queue without turning an absent failure into a transport
    error.
    """

    signature = signature.strip() if isinstance(signature, str) else ""
    if not signature:
        raise EvidenceReportError("failure signature must be a non-empty string")
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            groups = connection.execute(
                """
                SELECT failure_signature, phase, severity, COUNT(*) AS occurrence_count,
                       MIN(recorded_at) AS first_recorded_at,
                       MAX(recorded_at) AS last_recorded_at
                FROM sourcebridge_diagnostic_occurrence
                WHERE failure_signature = ?
                GROUP BY failure_signature, phase, severity
                ORDER BY phase, severity
                """,
                (signature,),
            ).fetchall()
            occurrences = _diagnostic_rows(connection, signature)
            summary = {
                "failure_signature": signature,
                "occurrence_count": len(occurrences),
                "phases": sorted({str(row["phase"]) for row in groups}),
                "severities": sorted({str(row["severity"]) for row in groups}),
                "first_recorded_at": min(
                    (row["first_recorded_at"] for row in groups), default=None
                ),
                "last_recorded_at": max(
                    (row["last_recorded_at"] for row in groups), default=None
                ),
                "groups": [dict(row) for row in groups],
            }
            return {
                "schema_version": 1,
                "report": "diagnostic-drilldown",
                "failure_signature": signature,
                "summary": summary,
                "occurrences": occurrences,
            }
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(
            f"{database}: cannot read diagnostic drill-down: {error}"
        ) from error


def diagnostic_review_history(
    database: Path,
    occurrence_id: str,
) -> list[dict[str, Any]]:
    """Return append-only review classifications for one occurrence."""

    occurrence_id = occurrence_id.strip() if isinstance(occurrence_id, str) else ""
    if not occurrence_id:
        raise EvidenceReportError("diagnostic occurrence ID must be a non-empty string")
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute(
                "SELECT 1 FROM sourcebridge_diagnostic_occurrence WHERE id = ?",
                (occurrence_id,),
            ).fetchone() is None:
                raise EvidenceReportError(
                    f"diagnostic occurrence `{occurrence_id}` does not exist"
                )
            return _diagnostic_reviews(connection, occurrence_id)
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(
            f"{database}: cannot read diagnostic review history: {error}"
        ) from error


def execution_activity(database: Path) -> list[dict[str, Any]]:
    query = """
        WITH completed_runs AS (
            SELECT *
            FROM sourcebridge_execution_run
            WHERE status IN ('passed', 'failed', 'cancelled')
              AND completed_at IS NOT NULL
        ),
        activity AS (
            SELECT run.id AS run_id, run.action, run.status AS run_status,
                   run.requested_workers, worker.worker_slot, worker.status AS worker_status,
                   worker.units_completed, source.source_path, event.occurred_at,
                   event.kind AS event_kind, event.message, event.metrics_json
            FROM completed_runs AS run
            JOIN sourcebridge_execution_event AS event ON event.run_id = run.id
            LEFT JOIN sourcebridge_execution_worker AS worker ON worker.id = event.worker_id
            LEFT JOIN sourcebridge_source_unit AS source ON source.id = worker.source_unit_id

            UNION ALL

            SELECT run.id AS run_id, run.action, run.status AS run_status,
                   run.requested_workers, worker.worker_slot, worker.status AS worker_status,
                   worker.units_completed, source.source_path, NULL AS occurred_at,
                   NULL AS event_kind, NULL AS message, NULL AS metrics_json
            FROM completed_runs AS run
            JOIN sourcebridge_execution_worker AS worker ON worker.run_id = run.id
            LEFT JOIN sourcebridge_source_unit AS source ON source.id = worker.source_unit_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM sourcebridge_execution_event AS event
                WHERE event.worker_id = worker.id
            )
        )
        SELECT *
        FROM activity
        ORDER BY run_id DESC, worker_slot, occurred_at DESC
        """
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            ambiguous = connection.execute(
                """
                SELECT id, status, completed_at
                FROM sourcebridge_execution_run
                WHERE (status IN ('passed', 'failed', 'cancelled') AND completed_at IS NULL)
                   OR (status IN ('queued', 'running', 'stopping') AND completed_at IS NOT NULL)
                   OR (
                        status IN ('passed', 'failed', 'cancelled')
                        AND EXISTS (
                            SELECT 1
                            FROM sourcebridge_execution_worker AS worker
                            WHERE worker.run_id = sourcebridge_execution_run.id
                              AND worker.status IN ('idle', 'running')
                        )
                   )
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if ambiguous is not None:
                raise EvidenceReportError(
                    "execution activity contains an ambiguous run state "
                    f"({ambiguous['id']}: status={ambiguous['status']}, "
                    f"completed_at={ambiguous['completed_at']!r}); "
                    "recover interrupted work before reporting it"
                )
            rows = connection.execute(query).fetchall()
    except EvidenceReportError:
        raise
    except FileNotFoundError as error:
        raise EvidenceReportError(str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise EvidenceReportError(f"{database}: cannot read evidence database: {error}") from error
    return [dict(zip(row.keys(), row)) for row in rows]


def current_work_queue(
    database: Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return the compact current blocking queue for an agent."""

    from sourcebridge.agent_queue import current_work_queue as _current_work_queue

    return _current_work_queue(database, run_id=run_id)


def work_item_evidence(
    database: Path,
    work_item_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return immutable provenance and evidence for one stable work item."""

    from sourcebridge.agent_queue import work_item_evidence as _work_item_evidence

    return _work_item_evidence(database, work_item_id, run_id=run_id)


def changes_since(
    database: Path,
    prior_run_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return work-item and run-identity changes since a prior run."""

    from sourcebridge.agent_queue import changes_since as _changes_since

    return _changes_since(database, prior_run_id, run_id=run_id)


def staleness_report(
    database: Path,
    *,
    profile_revision_id: str | None = None,
    project_id: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Return the compact digest comparison for one current profile."""

    from sourcebridge.staleness import staleness_report as _staleness_report

    try:
        return _staleness_report(
            database,
            profile_revision_id=profile_revision_id,
            project_id=project_id,
            profile_name=profile_name,
        )
    except ValueError as error:
        raise EvidenceReportError(str(error)) from error


def record_run_provenance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Append immutable run provenance through the agent queue API."""

    from sourcebridge.agent_queue import record_run_provenance as _record

    return _record(*args, **kwargs)


def record_work_item_state(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Append immutable work-item ownership/status through the queue API."""

    from sourcebridge.agent_queue import record_work_item_state as _record

    return _record(*args, **kwargs)


def link_work_item_evidence(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Append a run-scoped immutable work-item evidence link."""

    from sourcebridge.agent_queue import link_work_item_evidence as _record

    return _record(*args, **kwargs)


def record_diagnostic_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Append a diagnostic classification and optionally queue its evidence."""

    from sourcebridge.agent_queue import record_diagnostic_review as _record

    return _record(*args, **kwargs)


def record_source_unit_disposition(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Record one immutable per-source-unit component decision."""

    from sourcebridge.dispositions import (
        record_source_unit_disposition as _record,
    )

    return _record(*args, **kwargs)


def query_source_unit_dispositions(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Return source-unit decisions with their evidence and provenance."""

    from sourcebridge.dispositions import (
        query_source_unit_dispositions as _query,
    )

    return _query(*args, **kwargs)


def source_unit_dispositions(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Compatibility name for the source-unit disposition query."""

    return query_source_unit_dispositions(*args, **kwargs)


def render_annotated_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Render deterministic annotated review output."""

    from sourcebridge.dispositions import (
        render_annotated_review as _render,
    )

    return _render(*args, **kwargs)


def annotated_review_output(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility name for annotated review output."""

    return render_annotated_review(*args, **kwargs)


def render_clean_release(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Render clean output with a mandatory provenance sidecar report."""

    from sourcebridge.dispositions import render_clean_release as _render

    return _render(*args, **kwargs)


def clean_release_output(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility name for clean release output."""

    return render_clean_release(*args, **kwargs)


PACK_REPORT_SCHEMA_VERSION = 1
_PACK_REPORT_SECTIONS = (
    "source_validation",
    "fact_extraction",
    "target_emission_oracle",
    "compiler_metrics",
    "ollama_proposals",
    "unrecognized_artifacts",
)


def report_packs(roots: Sequence[Path]) -> dict[str, Any]:
    """Report recognized committed JSON evidence below the supplied pack roots.

    This deliberately has no database or input-file write path. Only JSON files
    physically contained by a supplied root are read; a file reached through
    overlapping roots is reported once.
    """
    normalized_roots = _normalize_pack_roots(roots)
    sections: dict[str, list[dict[str, Any]]] = {
        section: [] for section in _PACK_REPORT_SECTIONS
    }
    files: dict[Path, tuple[Path, str]] = {}
    for root in normalized_roots:
        for path in _json_files_below(root):
            resolved = path.resolve()
            relative = resolved.relative_to(root)
            candidate = (root, relative.as_posix())
            previous = files.get(resolved)
            if previous is None or _root_sort_key(candidate) < _root_sort_key(previous):
                files[resolved] = candidate

    for path, (root, relative_path) in sorted(
            files.items(), key=lambda item: (*_root_sort_key(item[1]), str(item[0]))):
        artifact = _read_pack_artifact(path, root, relative_path)
        sections[_pack_report_section(artifact)].append(artifact)

    for artifacts in sections.values():
        artifacts.sort(key=lambda artifact: (
            artifact["repository"], artifact["path"], artifact["sha256"]))
    return {
        "schema_version": PACK_REPORT_SCHEMA_VERSION,
        "roots": [
            {"repository": root.name, "path": str(root)}
            for root in normalized_roots
        ],
        **sections,
    }


def _normalize_pack_roots(roots: Sequence[Path]) -> list[Path]:
    if not roots:
        raise EvidenceReportError("at least one pack root is required")
    normalized: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise EvidenceReportError(f"{root}: pack root must be an existing directory")
        normalized.add(resolved)
    return sorted(normalized, key=lambda root: str(root))


def _json_files_below(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.json"), key=lambda candidate: candidate.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        yield resolved


def _root_sort_key(candidate: tuple[Path, str]) -> tuple[int, str, str]:
    root, relative_path = candidate
    return (len(root.parts), str(root), relative_path)


def _read_pack_artifact(path: Path, root: Path, relative_path: str) -> dict[str, Any]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    try:
        record: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return {
            "repository": root.name,
            "path": relative_path,
            "sha256": digest,
            "kind": "unrecognized_json",
            "schema_version": None,
            "parse_error": str(error),
        }

    kind = _artifact_kind(relative_path, record)
    return {
        "repository": root.name,
        "path": relative_path,
        "sha256": digest,
        "kind": kind,
        "schema_version": record.get("schema_version") if isinstance(record, dict) else None,
        "record": record,
    }


def _artifact_kind(relative_path: str, record: Any) -> str:
    if not isinstance(record, dict):
        return "unrecognized_json"
    path = Path(relative_path)
    parts = path.parts
    if path.name.endswith(".facts.json") and isinstance(record.get("fact_schema"), str) and (
            record["fact_schema"].startswith("sourcebridge.c.")):
        return "c_facts"
    if path.name == "fpc-compiler-tests-quick-run.json" and isinstance(record.get("run"), dict):
        return "delphi_fpc_quick_run"
    if path.name == "fpc-compiler-tests-manifest.json" and isinstance(
            record.get("source_name"), str):
        return "delphi_fpc_manifest"
    if path.name == "compile_clean_set.json" and isinstance(record.get("modules"), list) and (
            isinstance(record.get("min_modules"), int)):
        return "csharp_compile_clean_set"
    if path.name == "compile_baseline.json":
        return "historical_compile_baseline"
    if len(parts) >= 3 and parts[-3:-1] == ("oracle", "traces") and path.name == "manifest.json" and (
            isinstance(record.get("peripherals"), dict)):
        return "oracle_trace_manifest"
    if len(parts) >= 3 and parts[:2] == ("evidence", "proposals") and _is_proposal_record(record):
        return "ollama_rejected_proposal" if "rejected" in parts[2:-1] else "ollama_staged_proposal"
    return "unrecognized_json"


def _is_proposal_record(record: dict[str, Any]) -> bool:
    return all(key in record for key in (
        "payload_sha256", "proposal", "raw_response", "model", "generation_options"))


def _pack_report_section(artifact: dict[str, Any]) -> str:
    kind = artifact["kind"]
    if kind in {"delphi_fpc_quick_run", "delphi_fpc_manifest"}:
        return "source_validation"
    if kind == "c_facts":
        return "fact_extraction"
    if kind == "oracle_trace_manifest":
        return "target_emission_oracle"
    if kind == "csharp_compile_clean_set":
        return "compiler_metrics"
    if kind in {"ollama_staged_proposal", "ollama_rejected_proposal"}:
        return "ollama_proposals"
    return "unrecognized_artifacts"
