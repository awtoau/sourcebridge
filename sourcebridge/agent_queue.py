"""Compact, immutable-evidence reports for conversion agents.

The queue API is intentionally read-oriented.  Every report is built inside
one ``read_transaction`` snapshot and refuses to present a run without a
terminal lifecycle record and an immutable provenance snapshot.  The small
append-only helpers at the bottom are provided for workers that need to record
those snapshots without bypassing the database transaction policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.reporting import EvidenceReportError


AGENT_QUEUE_SCHEMA_VERSION = 1
_TERMINAL_RUN_STATUSES = {"passed", "failed"}
_BLOCKING_STATUSES = {"open", "claimed", "in_progress", "blocked"}
_ALL_WORK_ITEM_STATUSES = _BLOCKING_STATUSES | {"resolved", "closed", "stale"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def _run_row(
    connection: sqlite3.Connection,
    run_id: str | None,
) -> sqlite3.Row:
    if run_id is None:
        row = connection.execute(
            """
            SELECT run.id, run.profile_id, run.variant_id, run.action,
                   run.requested_workers, run.status, run.command_json,
                   run.requested_by, run.started_at, run.completed_at,
                   run.created_at,
                   provenance.source_revision, provenance.input_digest,
                   provenance.variant_digest, provenance.profile_digest,
                   provenance.rule_digest, provenance.validation_state,
                   provenance.artifact_digest, provenance.evidence_digest,
                   provenance.recorded_at AS provenance_recorded_at,
                   profile.profile_name, profile.source_frontend_pack_id,
                   profile.target_backend_pack_id, profile.rule_layers_digest,
                   profile.runtime_adapters_digest, profile.options_digest,
                   variant.frontend_pack_id, variant.compiler_identity,
                   variant.target_abi, variant.pointer_width,
                   variant.byte_order, variant.configuration_digest,
                   variant.generated_input_digest,
                   corpus.id AS corpus_id, corpus.source_name,
                   corpus.revision AS corpus_revision, corpus.manifest_digest
            FROM sourcebridge_execution_run AS run
            LEFT JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = run.id
            LEFT JOIN sourcebridge_profile AS profile
              ON profile.id = run.profile_id
            LEFT JOIN sourcebridge_variant AS variant
              ON variant.id = run.variant_id
            LEFT JOIN sourcebridge_corpus AS corpus
              ON corpus.id = variant.corpus_id
            WHERE run.completed_at IS NOT NULL
            ORDER BY run.completed_at DESC, run.created_at DESC, run.id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT run.id, run.profile_id, run.variant_id, run.action,
                   run.requested_workers, run.status, run.command_json,
                   run.requested_by, run.started_at, run.completed_at,
                   run.created_at,
                   provenance.source_revision, provenance.input_digest,
                   provenance.variant_digest, provenance.profile_digest,
                   provenance.rule_digest, provenance.validation_state,
                   provenance.artifact_digest, provenance.evidence_digest,
                   provenance.recorded_at AS provenance_recorded_at,
                   profile.profile_name, profile.source_frontend_pack_id,
                   profile.target_backend_pack_id, profile.rule_layers_digest,
                   profile.runtime_adapters_digest, profile.options_digest,
                   variant.frontend_pack_id, variant.compiler_identity,
                   variant.target_abi, variant.pointer_width,
                   variant.byte_order, variant.configuration_digest,
                   variant.generated_input_digest,
                   corpus.id AS corpus_id, corpus.source_name,
                   corpus.revision AS corpus_revision, corpus.manifest_digest
            FROM sourcebridge_execution_run AS run
            LEFT JOIN sourcebridge_execution_run_provenance AS provenance
              ON provenance.run_id = run.id
            LEFT JOIN sourcebridge_profile AS profile
              ON profile.id = run.profile_id
            LEFT JOIN sourcebridge_variant AS variant
              ON variant.id = run.variant_id
            LEFT JOIN sourcebridge_corpus AS corpus
              ON corpus.id = variant.corpus_id
            WHERE run.id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        if run_id is None:
            raise EvidenceReportError(
                "no completed execution run exists; record a terminal diagnosis first"
            )
        raise EvidenceReportError(f"execution run `{run_id}` does not exist")
    return row


def _validated_run(
    connection: sqlite3.Connection,
    run_id: str | None,
) -> sqlite3.Row:
    row = _run_row(connection, run_id)
    if row["status"] not in _TERMINAL_RUN_STATUSES or row["completed_at"] is None:
        raise EvidenceReportError(
            f"execution run `{row['id']}` is incomplete "
            f"(status={row['status']}, completed_at={row['completed_at']!r}); "
            "finish or recover the run before reporting agent work"
        )
    if row["validation_state"] is None:
        raise EvidenceReportError(
            f"execution run `{row['id']}` has incomplete evidence: "
            "immutable run provenance is missing"
        )
    if row["validation_state"] != "complete":
        raise EvidenceReportError(
            f"execution run `{row['id']}` has {row['validation_state']} evidence; "
            "rerun validation before consuming the agent queue"
        )
    for field in (
        "input_digest",
        "variant_digest",
        "profile_digest",
        "rule_digest",
        "artifact_digest",
        "evidence_digest",
    ):
        if not isinstance(row[field], str) or not row[field].strip():
            raise EvidenceReportError(
                f"execution run `{row['id']}` has incomplete evidence: "
                f"{field} is missing"
            )
    return row


def _run_report(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "profile_id": row["profile_id"],
        "variant_id": row["variant_id"],
        "action": row["action"],
        "requested_workers": row["requested_workers"],
        "status": row["status"],
        "requested_by": row["requested_by"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "created_at": row["created_at"],
        "source_revision": row["source_revision"],
        "input_digest": row["input_digest"],
        "variant_digest": row["variant_digest"],
        "profile_digest": row["profile_digest"],
        "rule_digest": row["rule_digest"],
        "validation_state": row["validation_state"],
        "artifact_digest": row["artifact_digest"],
        "evidence_digest": row["evidence_digest"],
        "provenance_recorded_at": row["provenance_recorded_at"],
        "profile": {
            "id": row["profile_id"],
            "name": row["profile_name"],
            "source_frontend_pack_id": row["source_frontend_pack_id"],
            "target_backend_pack_id": row["target_backend_pack_id"],
            "rule_layers_digest": row["rule_layers_digest"],
            "runtime_adapters_digest": row["runtime_adapters_digest"],
            "options_digest": row["options_digest"],
        },
        "variant": {
            "id": row["variant_id"],
            "frontend_pack_id": row["frontend_pack_id"],
            "compiler_identity": row["compiler_identity"],
            "target_abi": row["target_abi"],
            "pointer_width": row["pointer_width"],
            "byte_order": row["byte_order"],
            "configuration_digest": row["configuration_digest"],
            "generated_input_digest": row["generated_input_digest"],
        },
        "corpus": {
            "id": row["corpus_id"],
            "source_name": row["source_name"],
            "revision": row["corpus_revision"],
            "manifest_digest": row["manifest_digest"],
        },
    }


def _state_for_item(
    connection: sqlite3.Connection,
    item_id: str,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    parameters: tuple[object, ...]
    timestamp_clause = ""
    if as_of is not None:
        timestamp_clause = "AND state.recorded_at <= ?"
        parameters = (item_id, as_of)
    else:
        parameters = (item_id,)
    row = connection.execute(
        f"""
        SELECT state.sequence, state.status, state.owner,
               state.ownership_scope, state.disposition,
               state.rationale, state.recorded_at
        FROM sourcebridge_work_item_state AS state
        WHERE state.work_item_id = ?
          {timestamp_clause}
          AND state.sequence = (
              SELECT MAX(previous.sequence)
              FROM sourcebridge_work_item_state AS previous
              WHERE previous.work_item_id = state.work_item_id
                {timestamp_clause.replace('state.', 'previous.')}
          )
        """,
        (item_id, as_of, as_of),
    ).fetchone() if as_of is not None else connection.execute(
        """
        SELECT state.sequence, state.status, state.owner,
               state.ownership_scope, state.disposition,
               state.rationale, state.recorded_at
        FROM sourcebridge_work_item_state AS state
        WHERE state.work_item_id = ?
          AND state.sequence = (
              SELECT MAX(previous.sequence)
              FROM sourcebridge_work_item_state AS previous
              WHERE previous.work_item_id = state.work_item_id
          )
        """,
        parameters,
    ).fetchone()
    if row is not None:
        return _row_dict(row) or {}
    base = connection.execute(
        """
        SELECT decision_id, status
        FROM sourcebridge_work_item
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    if base is None:
        raise EvidenceReportError(f"work item `{item_id}` does not exist")
    if base["status"] not in _ALL_WORK_ITEM_STATUSES:
        raise EvidenceReportError(
            f"work item `{item_id}` has unsupported status `{base['status']}`"
        )
    decision = base["decision_id"] or item_id
    return {
        "sequence": 0,
        "status": base["status"],
        "owner": None,
        "ownership_scope": f"decision:{decision}",
        "disposition": "unreviewed",
        "rationale": "Legacy work-item row; no append-only state record exists.",
        "recorded_at": None,
    }


def _item_base(
    connection: sqlite3.Connection,
    item_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, decision_id, category, issue_url, summary, created_at, updated_at
        FROM sourcebridge_work_item
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise EvidenceReportError(f"work item `{item_id}` does not exist")
    return row


def _link_rows(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    item_id: str | None = None,
) -> list[sqlite3.Row]:
    parameters: list[object] = [run_id]
    item_clause = ""
    if item_id is not None:
        item_clause = "AND link.work_item_id = ?"
        parameters.append(item_id)
    return connection.execute(
        f"""
        SELECT link.id, link.work_item_id, link.execution_run_id,
               link.relation, link.recorded_at,
               diagnostic.id AS diagnostic_id, diagnostic.phase AS diagnostic_phase,
               diagnostic.tool_identity, diagnostic.diagnostic_code,
               diagnostic.severity, diagnostic.failure_signature,
               diagnostic.message, diagnostic.details_json,
               diagnostic.source_unit_id AS diagnostic_source_unit_id,
               diagnostic.recorded_at AS diagnostic_recorded_at,
               validation.id AS validation_id, validation.decision_id AS validation_decision_id,
               validation.side AS validation_side, validation.validator,
               validation.status AS validation_status, validation.evidence_path,
               validation.evidence_digest AS validation_evidence_digest,
               validation.details_json AS validation_details_json,
               validation.recorded_at AS validation_recorded_at,
               artifact.id AS artifact_id, artifact.artifact_path,
               artifact.artifact_digest, artifact.generator_digest,
               artifact.created_at AS artifact_created_at,
               fragment.id AS fragment_id, fragment.kind AS fragment_kind,
               fragment.logical_path, fragment.start_line, fragment.start_column,
               fragment.end_line, fragment.end_column, fragment.content,
               fragment.content_digest
        FROM sourcebridge_work_item_evidence AS link
        LEFT JOIN sourcebridge_diagnostic_occurrence AS diagnostic
          ON diagnostic.id = link.diagnostic_occurrence_id
        LEFT JOIN sourcebridge_validation_result AS validation
          ON validation.id = link.validation_result_id
        LEFT JOIN sourcebridge_generated_artifact AS artifact
          ON artifact.id = link.generated_artifact_id
        LEFT JOIN sourcebridge_evidence_fragment AS fragment
          ON fragment.id = link.evidence_fragment_id
        WHERE link.execution_run_id = ?
          {item_clause}
        ORDER BY link.work_item_id, link.recorded_at, link.id
        """,
        parameters,
    ).fetchall()


def _compact_evidence(rows: Iterable[sqlite3.Row]) -> dict[str, Any]:
    rows = list(rows)
    artifact_paths = sorted(
        {row["artifact_path"] for row in rows if row["artifact_path"] is not None}
    )
    diagnostics = sorted(
        {row["diagnostic_id"] for row in rows if row["diagnostic_id"] is not None}
    )
    validations = sorted(
        {row["validation_id"] for row in rows if row["validation_id"] is not None}
    )
    fragments = sorted(
        {row["fragment_id"] for row in rows if row["fragment_id"] is not None}
    )
    return {
        "link_count": len(rows),
        "diagnostic_count": len(diagnostics),
        "validation_count": len(validations),
        "fragment_count": len(fragments),
        "artifact_count": len(artifact_paths),
        "artifact_paths": artifact_paths,
        "link_ids": [row["id"] for row in rows],
        "digest": _digest(
            {
                "links": [
                    {
                        "id": row["id"],
                        "relation": row["relation"],
                        "diagnostic_id": row["diagnostic_id"],
                        "validation_id": row["validation_id"],
                        "artifact_id": row["artifact_id"],
                        "fragment_id": row["fragment_id"],
                    }
                    for row in rows
                ]
            }
        ),
    }


def _require_item_evidence(
    connection: sqlite3.Connection,
    run_id: str,
    item_id: str,
) -> list[sqlite3.Row]:
    rows = _link_rows(connection, run_id, item_id=item_id)
    if not rows:
        raise EvidenceReportError(
            f"work item `{item_id}` has incomplete evidence for run `{run_id}`; "
            "no immutable evidence link was recorded"
        )
    return rows


def _item_report(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    item_id: str,
    *,
    include_evidence: bool,
    as_of: str | None = None,
) -> dict[str, Any]:
    base = _item_base(connection, item_id)
    state = _state_for_item(connection, item_id, as_of=as_of)
    if state["status"] in {"claimed", "in_progress"} and not state["owner"]:
        raise EvidenceReportError(
            f"work item `{item_id}` has incomplete ownership: "
            f"status `{state['status']}` requires an owner"
        )
    rows = _require_item_evidence(connection, str(run["id"]), item_id)
    item: dict[str, Any] = {
        "stable_key": base["id"],
        "id": base["id"],
        "decision_id": base["decision_id"],
        "category": base["category"],
        "issue_url": base["issue_url"],
        "summary": base["summary"],
        "created_at": base["created_at"],
        "updated_at": base["updated_at"],
        "status": state["status"],
        "owner": state["owner"],
        "ownership_scope": state["ownership_scope"],
        "disposition": state["disposition"],
        "state_sequence": state["sequence"],
        "state_rationale": state["rationale"],
        "state_recorded_at": state["recorded_at"],
        "evidence": _compact_evidence(rows),
    }
    if include_evidence:
        item["evidence_details"] = _detailed_evidence(rows)
    return item


def _detailed_evidence(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        evidence: dict[str, Any] = {
            "id": row["id"],
            "relation": row["relation"],
            "recorded_at": row["recorded_at"],
        }
        if row["diagnostic_id"] is not None:
            evidence["diagnostic"] = {
                "id": row["diagnostic_id"],
                "phase": row["diagnostic_phase"],
                "tool_identity": row["tool_identity"],
                "diagnostic_code": row["diagnostic_code"],
                "severity": row["severity"],
                "failure_signature": row["failure_signature"],
                "message": row["message"],
                "details": _json(row["details_json"]),
                "source_unit_id": row["diagnostic_source_unit_id"],
                "recorded_at": row["diagnostic_recorded_at"],
            }
        if row["validation_id"] is not None:
            evidence["validation"] = {
                "id": row["validation_id"],
                "decision_id": row["validation_decision_id"],
                "side": row["validation_side"],
                "validator": row["validator"],
                "status": row["validation_status"],
                "evidence_path": row["evidence_path"],
                "evidence_digest": row["validation_evidence_digest"],
                "details": _json(row["validation_details_json"]),
                "recorded_at": row["validation_recorded_at"],
            }
        if row["artifact_id"] is not None:
            evidence["artifact"] = {
                "id": row["artifact_id"],
                "path": row["artifact_path"],
                "artifact_digest": row["artifact_digest"],
                "generator_digest": row["generator_digest"],
                "created_at": row["artifact_created_at"],
            }
        if row["fragment_id"] is not None:
            evidence["fragment"] = {
                "id": row["fragment_id"],
                "kind": row["fragment_kind"],
                "logical_path": row["logical_path"],
                "start_line": row["start_line"],
                "start_column": row["start_column"],
                "end_line": row["end_line"],
                "end_column": row["end_column"],
                "content": row["content"],
                "content_digest": row["content_digest"],
            }
        result.append(evidence)
    return result


def _provenance(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    decision_id: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run": _run_report(run),
        "decision": None,
        "source_unit": None,
    }
    if decision_id is None:
        return result
    decision = connection.execute(
        """
        SELECT id, profile_id, source_unit_id, disposition,
               target_backend_pack_id, runtime_adapter_id,
               boundary_contract_id, rationale, decided_by, created_at
        FROM sourcebridge_component_decision
        WHERE id = ?
        """,
        (decision_id,),
    ).fetchone()
    if decision is None:
        raise EvidenceReportError(
            f"work item decision `{decision_id}` has incomplete provenance"
        )
    result["decision"] = _row_dict(decision)
    source_unit = connection.execute(
        """
        SELECT id, variant_id, logical_name, source_path, source_span,
               source_digest, kind
        FROM sourcebridge_source_unit
        WHERE id = ?
        """,
        (decision["source_unit_id"],),
    ).fetchone()
    if source_unit is None:
        raise EvidenceReportError(
            f"work item decision `{decision_id}` has no source-unit provenance"
        )
    result["source_unit"] = _row_dict(source_unit)
    return result


def current_work_queue(
    database: Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return the current independently claimable blocking work items."""

    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            run = _validated_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT id
                FROM sourcebridge_work_item
                ORDER BY id
                """
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                state = _state_for_item(connection, str(row["id"]))
                if state["status"] not in _BLOCKING_STATUSES:
                    continue
                items.append(
                    _item_report(
                        connection,
                        run,
                        str(row["id"]),
                        include_evidence=False,
                    )
                )
            return {
                "schema_version": AGENT_QUEUE_SCHEMA_VERSION,
                "report": "current-blocking-work-items",
                "run": _run_report(run),
                "items": items,
                "count": len(items),
            }
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(f"{database}: cannot read agent queue: {error}") from error


def work_item_evidence(
    database: Path,
    work_item_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return exact provenance and immutable evidence for one stable item."""

    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            base = _item_base(connection, work_item_id)
            selected_run = _validated_run(connection, run_id)
            item = _item_report(
                connection,
                selected_run,
                work_item_id,
                include_evidence=True,
            )
            provenance = _provenance(connection, selected_run, base["decision_id"])
            return {
                "schema_version": AGENT_QUEUE_SCHEMA_VERSION,
                "report": "work-item-evidence",
                "work_item": item,
                "provenance": provenance,
            }
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(
            f"{database}: cannot read work item evidence: {error}"
        ) from error


def _all_items_for_run(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
) -> dict[str, dict[str, Any]]:
    item_ids = connection.execute(
        """
        SELECT DISTINCT work_item_id
        FROM sourcebridge_work_item_evidence
        WHERE execution_run_id = ?
        ORDER BY work_item_id
        """,
        (run["id"],),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in item_ids:
        item = _item_report(
            connection,
            run,
            str(row["work_item_id"]),
            include_evidence=False,
            as_of=run["completed_at"],
        )
        result[str(row["work_item_id"])] = item
    return result


def changes_since(
    database: Path,
    prior_run_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Compare stable work-item snapshots and input identities across runs."""

    prior_run_id = _text(prior_run_id, "prior_run_id")
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            prior = _validated_run(connection, prior_run_id)
            current = _validated_run(connection, run_id)
            if prior["id"] == current["id"]:
                raise EvidenceReportError(
                    f"cannot compare execution run `{prior['id']}` with itself"
                )
            previous_items = _all_items_for_run(connection, prior)
            current_items = _all_items_for_run(connection, current)
            added = sorted(set(current_items) - set(previous_items))
            removed = sorted(set(previous_items) - set(current_items))
            changed: list[dict[str, Any]] = []
            unchanged = 0
            compare_fields = (
                "status",
                "owner",
                "ownership_scope",
                "disposition",
                "summary",
                "evidence",
            )
            for item_id in sorted(set(current_items) & set(previous_items)):
                before = previous_items[item_id]
                after = current_items[item_id]
                if any(before[field] != after[field] for field in compare_fields):
                    changed.append(
                        {
                            "stable_key": item_id,
                            "before": {
                                field: before[field] for field in compare_fields
                            },
                            "after": {
                                field: after[field] for field in compare_fields
                            },
                        }
                    )
                else:
                    unchanged += 1
            digest_fields = (
                "input_digest",
                "variant_digest",
                "profile_digest",
                "rule_digest",
                "artifact_digest",
                "evidence_digest",
            )
            digest_changes = {
                field: prior[field] != current[field] for field in digest_fields
            }
            return {
                "schema_version": AGENT_QUEUE_SCHEMA_VERSION,
                "report": "changes-since-run",
                "prior_run": _run_report(prior),
                "current_run": _run_report(current),
                "staleness": {
                    "prior_validation_state": prior["validation_state"],
                    "current_validation_state": current["validation_state"],
                    "prior_is_stale": prior["validation_state"] != "complete",
                    "current_is_stale": current["validation_state"] != "complete",
                    "digest_changes": digest_changes,
                    "inputs_changed": any(
                        digest_changes[field]
                        for field in (
                            "input_digest",
                            "variant_digest",
                            "profile_digest",
                            "rule_digest",
                        )
                    ),
                },
                "items": {
                    "added": [current_items[item_id] for item_id in added],
                    "removed": [previous_items[item_id] for item_id in removed],
                    "changed": changed,
                    "unchanged_count": unchanged,
                },
            }
    except EvidenceReportError:
        raise
    except (FileNotFoundError, OSError, sqlite3.Error) as error:
        raise EvidenceReportError(
            f"{database}: cannot compare agent queue runs: {error}"
        ) from error

def record_run_provenance(
    database: Path,
    run_id: str,
    *,
    input_digest: str,
    variant_digest: str,
    profile_digest: str,
    rule_digest: str,
    validation_state: str,
    artifact_digest: str,
    evidence_digest: str,
    source_revision: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append the immutable identity required before a run can be reported."""

    run_id = _text(run_id, "run_id")
    if validation_state not in {"complete", "incomplete", "stale"}:
        raise ValueError(f"unsupported validation state `{validation_state}`")
    values = {
        "input_digest": _text(input_digest, "input_digest"),
        "variant_digest": _text(variant_digest, "variant_digest"),
        "profile_digest": _text(profile_digest, "profile_digest"),
        "rule_digest": _text(rule_digest, "rule_digest"),
        "artifact_digest": _text(artifact_digest, "artifact_digest"),
        "evidence_digest": _text(evidence_digest, "evidence_digest"),
    }
    with write_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute(
            "SELECT id FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"execution run `{run_id}` does not exist")
        try:
            connection.execute(
                """
                INSERT INTO sourcebridge_execution_run_provenance
                    (run_id, source_revision, input_digest, variant_digest,
                     profile_digest, rule_digest, validation_state,
                     artifact_digest, evidence_digest, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_revision,
                    values["input_digest"],
                    values["variant_digest"],
                    values["profile_digest"],
                    values["rule_digest"],
                    validation_state,
                    values["artifact_digest"],
                    values["evidence_digest"],
                    recorded_at or _now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"execution run `{run_id}` already has immutable provenance"
            ) from error
    return {"run_id": run_id, **values, "validation_state": validation_state}


def record_work_item_state(
    database: Path,
    work_item_id: str,
    *,
    status: str,
    ownership_scope: str,
    disposition: str,
    rationale: str,
    owner: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append one explicit ownership/status decision for a work item."""

    work_item_id = _text(work_item_id, "work_item_id")
    if status not in _ALL_WORK_ITEM_STATUSES:
        raise ValueError(f"unsupported work-item status `{status}`")
    ownership_scope = _text(ownership_scope, "ownership_scope")
    disposition = _text(disposition, "disposition")
    rationale = _text(rationale, "rationale")
    if owner is not None:
        owner = _text(owner, "owner")
    if status in {"claimed", "in_progress"} and owner is None:
        raise ValueError(f"status `{status}` requires an owner")
    with write_transaction(database) as connection:
        row = connection.execute(
            "SELECT 1 FROM sourcebridge_work_item WHERE id = ?",
            (work_item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"work item `{work_item_id}` does not exist")
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM sourcebridge_work_item_state
            WHERE work_item_id = ?
            """,
            (work_item_id,),
        ).fetchone()[0]
        state_id = f"state-{uuid4()}"
        connection.execute(
            """
            INSERT INTO sourcebridge_work_item_state
                (id, work_item_id, sequence, status, owner,
                 ownership_scope, disposition, rationale, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state_id,
                work_item_id,
                sequence,
                status,
                owner,
                ownership_scope,
                disposition,
                rationale,
                recorded_at or _now(),
            ),
        )
    return {
        "id": state_id,
        "work_item_id": work_item_id,
        "sequence": sequence,
        "status": status,
        "owner": owner,
        "ownership_scope": ownership_scope,
        "disposition": disposition,
        "rationale": rationale,
    }


def link_work_item_evidence(
    database: Path,
    work_item_id: str,
    run_id: str,
    *,
    relation: str,
    diagnostic_occurrence_id: str | None = None,
    validation_result_id: str | None = None,
    generated_artifact_id: str | None = None,
    evidence_fragment_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append one immutable evidence link scoped to a producing run."""

    work_item_id = _text(work_item_id, "work_item_id")
    run_id = _text(run_id, "run_id")
    relation = _text(relation, "relation")
    evidence_ids = (
        diagnostic_occurrence_id,
        validation_result_id,
        generated_artifact_id,
        evidence_fragment_id,
    )
    if not any(evidence_ids):
        raise ValueError("at least one evidence reference is required")
    with write_transaction(database) as connection:
        if connection.execute(
            "SELECT 1 FROM sourcebridge_work_item WHERE id = ?",
            (work_item_id,),
        ).fetchone() is None:
            raise ValueError(f"work item `{work_item_id}` does not exist")
        if connection.execute(
            "SELECT 1 FROM sourcebridge_execution_run WHERE id = ?",
            (run_id,),
        ).fetchone() is None:
            raise ValueError(f"execution run `{run_id}` does not exist")
        link_id = f"evidence-link-{uuid4()}"
        try:
            connection.execute(
                """
                INSERT INTO sourcebridge_work_item_evidence
                    (id, work_item_id, execution_run_id,
                     diagnostic_occurrence_id, validation_result_id,
                     generated_artifact_id, evidence_fragment_id,
                     relation, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    work_item_id,
                    run_id,
                    diagnostic_occurrence_id,
                    validation_result_id,
                    generated_artifact_id,
                    evidence_fragment_id,
                    relation,
                    recorded_at or _now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("duplicate or invalid immutable evidence link") from error
    return {
        "id": link_id,
        "work_item_id": work_item_id,
        "execution_run_id": run_id,
        "relation": relation,
    }
