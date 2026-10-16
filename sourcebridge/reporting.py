"""Read-only diagnostic and committed-evidence reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

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


def execution_activity(database: Path) -> list[dict[str, Any]]:
    query = text(
        """
        SELECT run.id AS run_id, run.action, run.status AS run_status,
               run.requested_workers, worker.worker_slot, worker.status AS worker_status,
               worker.units_completed, source.source_path, event.occurred_at,
               event.kind AS event_kind, event.message, event.metrics_json
        FROM sourcebridge_execution_run AS run
        LEFT JOIN sourcebridge_execution_worker AS worker ON worker.run_id = run.id
        LEFT JOIN sourcebridge_source_unit AS source ON source.id = worker.source_unit_id
        LEFT JOIN sourcebridge_execution_event AS event ON event.worker_id = worker.id
        ORDER BY run.created_at DESC, worker.worker_slot, event.occurred_at DESC
        """
    )
    with _engine(database).connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


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
