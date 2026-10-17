"""Source-unit decisions and small reproducible output modes.

This module deliberately stops at the evidence boundary.  It does not render
Rust in general; callers provide generated text and Sourcebridge adds either
review markers or a provenance sidecar around that text.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from sourcebridge.conversion_profiles import resolve_project_profile
from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize
from sourcebridge.staleness import profile_provenance, staleness_report


DISPOSITION_SCHEMA_VERSION = 1
OUTPUT_SCHEMA_VERSION = 1
DISPOSITIONS = frozenset(
    {
        "direct_translation",
        "retain_runtime",
        "runtime_replacement",
        "native_reimplementation",
        "external_dependency",
        "stubbed_boundary",
        "excluded",
        "blocked",
    }
)
_BOUNDARY_DISPOSITIONS = frozenset(
    {
        "retain_runtime",
        "runtime_replacement",
        "native_reimplementation",
        "external_dependency",
        "stubbed_boundary",
    }
)
_INCOMPLETE_DISPOSITIONS = frozenset({"blocked", "stubbed_boundary"})


class DispositionError(ValueError):
    """Raised when a source-unit decision or output cannot be resolved."""


class OutputError(DispositionError):
    """Raised when a requested output mode cannot be produced safely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DispositionError(f"value must be JSON serializable: {error}") from error


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispositionError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _base_profile(
    database: Path,
    *,
    profile_id: str | None,
    profile_revision_id: str | None,
) -> tuple[str, dict[str, object] | None]:
    if (profile_id is None) == (profile_revision_id is None):
        raise DispositionError(
            "exactly one of profile_id or profile_revision_id is required"
        )
    initialize(database)
    if profile_revision_id is not None:
        try:
            resolved = resolve_project_profile(database, profile_revision_id)
        except ValueError as error:
            raise DispositionError(str(error)) from error
        return str(resolved["base_profile"]["id"]), resolved

    assert profile_id is not None
    profile_id = _text(profile_id, "profile_id")
    with read_transaction(database) as connection:
        row = connection.execute(
            "SELECT id FROM sourcebridge_profile WHERE id = ?", (profile_id,)
        ).fetchone()
    if row is None:
        raise DispositionError(f"{profile_id}: base profile does not exist")
    return profile_id, None


def record_source_unit_disposition(
    database: Path,
    *,
    source_unit_id: str,
    disposition: str,
    rationale: str,
    decided_by: str,
    profile_id: str | None = None,
    profile_revision_id: str | None = None,
    target_backend_pack_id: str | None = None,
    runtime_adapter_id: str | None = None,
    boundary_contract_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    """Record one immutable disposition for one source unit.

    Project profile revisions resolve to their existing base
    ``sourcebridge_component_decision`` profile.  This keeps the prototype
    compatible with the core schema while making the selected project profile
    explicit in the returned record and in output provenance.
    """

    source_unit_id = _text(source_unit_id, "source_unit_id")
    disposition = _text(disposition, "disposition")
    rationale = _text(rationale, "rationale")
    decided_by = _text(decided_by, "decided_by")
    if disposition not in DISPOSITIONS:
        raise DispositionError(f"unsupported disposition `{disposition}`")
    if disposition in _BOUNDARY_DISPOSITIONS and boundary_contract_id is None:
        raise DispositionError(
            f"{disposition}: a boundary_contract_id is required for an explicit boundary"
        )
    target_backend_pack_id = _optional_text(
        target_backend_pack_id, "target_backend_pack_id"
    )
    runtime_adapter_id = _optional_text(runtime_adapter_id, "runtime_adapter_id")
    boundary_contract_id = _optional_text(boundary_contract_id, "boundary_contract_id")
    base_profile_id, resolved = _base_profile(
        database,
        profile_id=profile_id,
        profile_revision_id=profile_revision_id,
    )
    timestamp = created_at or _now()

    with write_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            """
            SELECT source.id, profile.target_backend_pack_id
            FROM sourcebridge_source_unit AS source
            JOIN sourcebridge_variant AS variant ON variant.id = source.variant_id
            JOIN sourcebridge_corpus AS corpus ON corpus.id = variant.corpus_id
            JOIN sourcebridge_profile AS profile ON profile.id = ?
            WHERE source.id = ?
            """,
            (base_profile_id, source_unit_id),
        ).fetchone()
        if source is None:
            raise DispositionError(f"{source_unit_id}: source unit does not exist")
        if target_backend_pack_id is None and disposition == "direct_translation":
            target_backend_pack_id = str(source["target_backend_pack_id"])

        existing = connection.execute(
            """
            SELECT id, profile_id, source_unit_id, disposition,
                   target_backend_pack_id, runtime_adapter_id,
                   boundary_contract_id, rationale, decided_by, created_at
            FROM sourcebridge_component_decision
            WHERE profile_id = ? AND source_unit_id = ?
            """,
            (base_profile_id, source_unit_id),
        ).fetchone()
        if existing is not None:
            expected = {
                "profile_id": base_profile_id,
                "source_unit_id": source_unit_id,
                "disposition": disposition,
                "target_backend_pack_id": target_backend_pack_id,
                "runtime_adapter_id": runtime_adapter_id,
                "boundary_contract_id": boundary_contract_id,
                "rationale": rationale,
                "decided_by": decided_by,
            }
            actual = {
                key: existing[key]
                for key in expected
            }
            if actual != expected:
                raise DispositionError(
                    f"{source_unit_id}: disposition already recorded for profile "
                    f"{base_profile_id}; decisions are immutable"
                )
            result = dict(existing)
            result["created"] = False
        else:
            decision_id = f"decision-{_digest(f'{base_profile_id}:{source_unit_id}')[:24]}"
            connection.execute(
                """
                INSERT INTO sourcebridge_component_decision
                    (id, profile_id, source_unit_id, disposition,
                     target_backend_pack_id, runtime_adapter_id,
                     boundary_contract_id, rationale, decided_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    base_profile_id,
                    source_unit_id,
                    disposition,
                    target_backend_pack_id,
                    runtime_adapter_id,
                    boundary_contract_id,
                    rationale,
                    decided_by,
                    timestamp,
                ),
            )
            result = {
                "id": decision_id,
                "profile_id": base_profile_id,
                "source_unit_id": source_unit_id,
                "disposition": disposition,
                "target_backend_pack_id": target_backend_pack_id,
                "runtime_adapter_id": runtime_adapter_id,
                "boundary_contract_id": boundary_contract_id,
                "rationale": rationale,
                "decided_by": decided_by,
                "created_at": timestamp,
                "created": True,
            }

    result["profile_revision_id"] = (
        None if resolved is None else str(resolved["id"])
    )
    return result


def _profile_context(
    database: Path,
    *,
    base_profile_id: str,
    resolved: dict[str, object] | None,
) -> dict[str, object]:
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, profile_name, source_frontend_pack_id, corpus_pack_id,
                   target_backend_pack_id, rule_layers_digest,
                   runtime_adapters_digest, options_digest
            FROM sourcebridge_profile
            WHERE id = ?
            """,
            (base_profile_id,),
        ).fetchone()
    if row is None:
        raise DispositionError(f"{base_profile_id}: base profile does not exist")
    profile: dict[str, object] = {
        "id": str(row["id"]),
        "profile_name": str(row["profile_name"]),
        "source_frontend_pack_id": str(row["source_frontend_pack_id"]),
        "corpus_pack_id": row["corpus_pack_id"],
        "target_backend_pack_id": str(row["target_backend_pack_id"]),
        "rule_layers_digest": str(row["rule_layers_digest"]),
        "runtime_adapters_digest": str(row["runtime_adapters_digest"]),
        "options_digest": str(row["options_digest"]),
    }
    if resolved is not None:
        profile.update(
            {
                "profile_revision_id": str(resolved["id"]),
                "project_id": str(resolved["project_id"]),
                "project_profile_name": str(resolved["profile_name"]),
                "revision": int(resolved["revision"]),
                "state": str(resolved["state"]),
                "resolved_profile_digest": str(resolved["resolved_digest"]),
                "rule_digest": _digest(
                    _canonical(
                        [
                            {
                                "rule_key": rule["rule_key"],
                                "directive": rule["directive"],
                                "rule_revision_id": rule["rule_revision_id"],
                                "selected_rule_digest": rule["selected_rule_digest"],
                            }
                            for rule in resolved["rule_overrides"]  # type: ignore[index]
                        ]
                    )
                ),
                "toolchain_digest": resolved["toolchain_digest"],
                "rules": [
                    {
                        "rule_key": rule["rule_key"],
                        "rule_revision_id": rule["rule_revision_id"],
                        "selected_rule_digest": rule["selected_rule_digest"],
                        "directive": rule["directive"],
                    }
                    for rule in resolved["rule_overrides"]  # type: ignore[index]
                ],
            }
        )
    return profile


def query_source_unit_dispositions(
    database: Path,
    *,
    profile_id: str | None = None,
    profile_revision_id: str | None = None,
    source_unit_id: str | None = None,
) -> list[dict[str, object]]:
    """Return decisions with their source, variant, validation, and artifacts."""

    base_profile_id, resolved = _base_profile(
        database,
        profile_id=profile_id,
        profile_revision_id=profile_revision_id,
    )
    source_unit_id = (
        None if source_unit_id is None else _text(source_unit_id, "source_unit_id")
    )
    query = """
        SELECT decision.id, decision.profile_id, decision.source_unit_id,
               decision.disposition, decision.target_backend_pack_id,
               decision.runtime_adapter_id, decision.boundary_contract_id,
               decision.rationale, decision.decided_by, decision.created_at,
               source.variant_id, source.logical_name, source.source_path,
               source.source_span, source.source_digest, source.kind,
               variant.corpus_id, variant.frontend_pack_id, variant.compiler_identity,
               variant.target_abi, variant.pointer_width, variant.byte_order,
               variant.configuration_digest, variant.generated_input_digest,
               corpus.source_name, corpus.revision AS corpus_revision,
               corpus.manifest_digest
        FROM sourcebridge_component_decision AS decision
        JOIN sourcebridge_source_unit AS source ON source.id = decision.source_unit_id
        JOIN sourcebridge_variant AS variant ON variant.id = source.variant_id
        JOIN sourcebridge_corpus AS corpus ON corpus.id = variant.corpus_id
        WHERE decision.profile_id = ?
    """
    parameters: list[str] = [base_profile_id]
    if source_unit_id is not None:
        query += " AND decision.source_unit_id = ?"
        parameters.append(source_unit_id)
    query += " ORDER BY source.source_path, source.source_span, source.id"

    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, parameters).fetchall()
        profile = _profile_context(
            database, base_profile_id=base_profile_id, resolved=resolved
        )
        result: list[dict[str, object]] = []
        for row in rows:
            decision = {
                key: row[key]
                for key in (
                    "id",
                    "profile_id",
                    "source_unit_id",
                    "disposition",
                    "target_backend_pack_id",
                    "runtime_adapter_id",
                    "boundary_contract_id",
                    "rationale",
                    "decided_by",
                    "created_at",
                )
            }
            validations = connection.execute(
                """
                SELECT id, side, validator, status, evidence_path,
                       evidence_digest, details_json, recorded_at
                FROM sourcebridge_validation_result
                WHERE decision_id = ?
                ORDER BY id
                """,
                (row["id"],),
            ).fetchall()
            artifacts = connection.execute(
                """
                SELECT artifact.id, artifact.artifact_path, artifact.artifact_digest,
                       artifact.generator_digest, artifact.created_at,
                       provenance.id AS provenance_id,
                       provenance.profile_revision_id,
                       provenance.resolved_profile_digest,
                       provenance.rule_digest, provenance.toolchain_digest,
                       provenance.validation_tier, provenance.recorded_at
                FROM sourcebridge_generated_artifact AS artifact
                LEFT JOIN sourcebridge_conversion_provenance AS provenance
                  ON provenance.generated_artifact_id = artifact.id
                WHERE artifact.decision_id = ?
                ORDER BY artifact.artifact_path, artifact.id
                """,
                (row["id"],),
            ).fetchall()
            result.append(
                {
                    **decision,
                    "profile": profile,
                    "source_unit": {
                        "id": row["source_unit_id"],
                        "variant_id": row["variant_id"],
                        "logical_name": row["logical_name"],
                        "source_path": row["source_path"],
                        "source_span": row["source_span"],
                        "source_digest": row["source_digest"],
                        "kind": row["kind"],
                    },
                    "variant": {
                        "id": row["variant_id"],
                        "corpus_id": row["corpus_id"],
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
                    "validation": [dict(validation) for validation in validations],
                    "artifacts": [dict(artifact) for artifact in artifacts],
                }
            )
    return result


def source_unit_dispositions(
    database: Path,
    **kwargs: object,
) -> list[dict[str, object]]:
    """Compatibility name for querying explicit source-unit decisions."""

    return query_source_unit_dispositions(database, **kwargs)


def _normalize_artifacts(
    artifacts: Sequence[Mapping[str, object]] | Mapping[str, object],
) -> list[dict[str, object]]:
    if isinstance(artifacts, Mapping):
        artifacts = artifacts.get("artifacts", [])  # type: ignore[assignment]
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise OutputError("artifacts must be a JSON array of generated text records")
    normalized: list[dict[str, object]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise OutputError(f"artifact {index} must be an object")
        try:
            source_unit_id = _text(artifact.get("source_unit_id"), "source_unit_id")
            artifact_path = _text(artifact.get("artifact_path"), "artifact_path")
        except DispositionError as error:
            raise OutputError(f"artifact {index}: {error}") from error
        content = artifact.get("content")
        if not isinstance(content, str):
            raise OutputError(f"artifact {index}: content must be a string")
        normalized.append(
            {
                "source_unit_id": source_unit_id,
                "artifact_path": artifact_path,
                "content": content,
                "rule_id": _optional_text(
                    artifact.get("rule_id") or artifact.get("rule_key"), "rule_id"
                ),
                "generated_artifact_id": _optional_text(
                    artifact.get("generated_artifact_id"), "generated_artifact_id"
                ),
                "incomplete": bool(artifact.get("incomplete", False)),
            }
        )
    if not normalized:
        raise OutputError("at least one generated artifact is required")
    return sorted(
        normalized,
        key=lambda item: (
            str(item["artifact_path"]),
            str(item["source_unit_id"]),
            str(item["generated_artifact_id"] or ""),
        ),
    )


def _artifact_link(
    database: Path,
    *,
    artifact_id: str,
    source_unit_id: str,
) -> dict[str, object] | None:
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT artifact.id, artifact.decision_id, decision.source_unit_id,
                   artifact.artifact_path, artifact.artifact_digest,
                   artifact.generator_digest,
                   provenance.id AS provenance_id,
                   provenance.profile_revision_id,
                   provenance.resolved_profile_digest,
                   provenance.rule_digest, provenance.toolchain_digest,
                   provenance.validation_tier, provenance.recorded_at
            FROM sourcebridge_generated_artifact AS artifact
            JOIN sourcebridge_component_decision AS decision
              ON decision.id = artifact.decision_id
            LEFT JOIN sourcebridge_conversion_provenance AS provenance
              ON provenance.generated_artifact_id = artifact.id
            WHERE artifact.id = ?
            """,
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise OutputError(f"{artifact_id}: generated artifact does not exist")
    if row["source_unit_id"] != source_unit_id:
        raise OutputError(
            f"{artifact_id}: generated artifact belongs to source unit "
            f"{row['source_unit_id']}, not {source_unit_id}"
        )
    return dict(row)


def _unresolved_reason(
    decision: Mapping[str, object],
    *,
    declared_incomplete: bool,
) -> str | None:
    if declared_incomplete:
        return "declared_incomplete"
    disposition = str(decision["disposition"])
    if disposition in _INCOMPLETE_DISPOSITIONS:
        return disposition
    return None


def _render(
    database: Path,
    *,
    profile_revision_id: str,
    artifacts: Sequence[Mapping[str, object]] | Mapping[str, object],
    mode: str,
) -> dict[str, object]:
    if mode not in {"annotated-review", "clean-release"}:
        raise OutputError(f"unsupported output mode `{mode}`")
    profile_revision_id = _text(profile_revision_id, "profile_revision_id")
    normalized = _normalize_artifacts(artifacts)
    try:
        profile_report = staleness_report(
            database, profile_revision_id=profile_revision_id
        )
    except ValueError as error:
        raise OutputError(str(error)) from error
    decisions = query_source_unit_dispositions(
        database,
        profile_revision_id=profile_revision_id,
    )
    by_unit = {str(item["source_unit_id"]): item for item in decisions}
    profile = profile_provenance(database, profile_revision_id)
    rendered: list[str] = []
    reports: list[dict[str, object]] = []
    incomplete_by_unit: dict[str, dict[str, object]] = {}
    for decision in decisions:
        reason = _unresolved_reason(decision, declared_incomplete=False)
        if reason is None:
            continue
        source = decision["source_unit"]
        assert isinstance(source, Mapping)
        incomplete_by_unit[str(decision["source_unit_id"])] = {
            "source_unit_id": decision["source_unit_id"],
            "source_path": source["source_path"],
            "source_span": source["source_span"],
            "disposition": decision["disposition"],
            "reason": reason,
        }
    for item in normalized:
        source_unit_id = str(item["source_unit_id"])
        decision = by_unit.get(source_unit_id)
        if decision is None:
            raise OutputError(
                f"{source_unit_id}: no disposition recorded for profile "
                f"{profile_revision_id}"
            )
        source_unit = decision["source_unit"]
        assert isinstance(source_unit, Mapping)
        content = str(item["content"])
        content_digest = _digest(content)
        linked = None
        artifact_id = item["generated_artifact_id"]
        if artifact_id is not None:
            linked = _artifact_link(
                database,
                artifact_id=str(artifact_id),
                source_unit_id=source_unit_id,
            )
            assert linked is not None
            if linked["artifact_path"] != item["artifact_path"]:
                raise OutputError(
                    f"{artifact_id}: artifact_path does not match supplied output"
                )
            recorded_digest = str(linked["artifact_digest"])
            if recorded_digest != content_digest:
                raise OutputError(
                    f"{artifact_id}: supplied content digest {content_digest} does "
                    f"not match recorded artifact digest {recorded_digest}"
                )
        rule_id = item["rule_id"] or f"resolved:{profile['rule_digest']}"
        rule = {
            "id": rule_id,
            "profile_rule_digest": profile["rule_digest"],
        }
        reason = _unresolved_reason(
            decision,
            declared_incomplete=bool(item["incomplete"]),
        )
        provenance: dict[str, object] = {
            "source_unit_id": source_unit_id,
            "source_digest": source_unit["source_digest"],
            "artifact_digest": content_digest,
            "generated_artifact_id": artifact_id,
            "profile_revision_id": profile["profile_revision_id"],
            "resolved_profile_digest": profile["resolved_profile_digest"],
            "rule_digest": profile["rule_digest"],
            "toolchain_digest": profile["toolchain_digest"],
            "staleness": "not_linked",
        }
        if linked is not None:
            provenance["staleness"] = (
                "current"
                if (
                    linked["profile_revision_id"] == profile["profile_revision_id"]
                    and linked["resolved_profile_digest"]
                    == profile["resolved_profile_digest"]
                    and linked["rule_digest"] == profile["rule_digest"]
                    and linked["toolchain_digest"] == profile["toolchain_digest"]
                )
                else "stale_for_current_profile"
            )
            provenance["staleness_link_id"] = linked["provenance_id"]
        report = {
            "artifact_path": item["artifact_path"],
            "source": dict(source_unit),
            "disposition": {
                "id": decision["id"],
                "kind": decision["disposition"],
                "rationale": decision["rationale"],
                "boundary_contract_id": decision["boundary_contract_id"],
                "incomplete": reason is not None,
            },
            "rule": rule,
            "profile": profile,
            "provenance": provenance,
        }
        reports.append(report)
        if reason is not None:
            incomplete_by_unit[source_unit_id] = {
                "source_unit_id": source_unit_id,
                "source_path": source_unit["source_path"],
                "source_span": source_unit["source_span"],
                "disposition": decision["disposition"],
                "reason": reason,
            }
        if mode == "annotated-review":
            rendered.extend(
                [
                    "// SOURCEBRIDGE REVIEW OUTPUT",
                    f"// sourcebridge source: {_canonical(dict(source_unit))}",
                    f"// sourcebridge rule: {_canonical(rule)}",
                    f"// sourcebridge profile: {_canonical(profile)}",
                    f"// sourcebridge provenance: {_canonical(provenance)}",
                ]
            )
            if reason is not None:
                rendered.append(
                    "// SOURCEBRIDGE INCOMPLETE boundary: "
                    + _canonical(
                        {
                            "source_unit_id": source_unit_id,
                            "source_path": source_unit["source_path"],
                            "source_span": source_unit["source_span"],
                            "disposition": decision["disposition"],
                            "reason": reason,
                        }
                    )
                )
            rendered.append(content.rstrip())
            rendered.append("")
        else:
            rendered.extend([content.rstrip(), ""])

    incomplete = [
        incomplete_by_unit[source_unit_id]
        for source_unit_id in sorted(incomplete_by_unit)
    ]
    disposition_report = []
    for decision in decisions:
        source = decision["source_unit"]
        assert isinstance(source, Mapping)
        disposition_report.append(
            {
                "decision_id": decision["id"],
                "source": dict(source),
                "disposition": decision["disposition"],
                "rationale": decision["rationale"],
                "target_backend_pack_id": decision["target_backend_pack_id"],
                "runtime_adapter_id": decision["runtime_adapter_id"],
                "boundary_contract_id": decision["boundary_contract_id"],
                "validation": decision["validation"],
                "artifacts": decision["artifacts"],
                "profile": profile,
                "rule": {
                    "id": f"resolved:{profile['rule_digest']}",
                    "profile_rule_digest": profile["rule_digest"],
                },
            }
        )
    if mode == "annotated-review":
        rendered_source_units = {
            str(report["provenance"]["source_unit_id"]) for report in reports
        }
        for boundary in incomplete:
            if str(boundary["source_unit_id"]) in rendered_source_units:
                continue
            rendered.extend(
                [
                    "// SOURCEBRIDGE INCOMPLETE boundary: "
                    + _canonical(boundary),
                    "",
                ]
            )
    if mode == "clean-release" and incomplete:
        details = ", ".join(
            f"{item['source_unit_id']}={item['reason']}" for item in incomplete
        )
        raise OutputError(
            "clean-release refuses unresolved incomplete/blocked dispositions: "
            + details
        )

    output = "\n".join(rendered)
    report = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "mode": mode,
        "profile": profile,
        "validation": {
            "required_tier": profile_report["required_validation_tier"],
            "certification": profile_report["certification"],
            "staleness_summary": profile_report["summary"],
        },
        "artifacts": reports,
        "dispositions": disposition_report,
        "incomplete_boundaries": incomplete,
        "summary": {
            "artifact_count": len(reports),
            "incomplete_boundary_count": len(incomplete),
            "non_direct_count": sum(
                item["disposition"] != "direct_translation"
                for item in disposition_report
            ),
        },
        "output_digest": _digest(output),
    }
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "mode": mode,
        "output": output,
        "output_digest": report["output_digest"],
        "provenance": report,
        "sidecar": report if mode == "clean-release" else None,
    }


def render_annotated_review(
    database: Path,
    *,
    profile_revision_id: str,
    artifacts: Sequence[Mapping[str, object]] | Mapping[str, object],
) -> dict[str, object]:
    """Produce deterministic review text with source and incomplete markers."""

    return _render(
        database,
        profile_revision_id=profile_revision_id,
        artifacts=artifacts,
        mode="annotated-review",
    )


def render_clean_release(
    database: Path,
    *,
    profile_revision_id: str,
    artifacts: Sequence[Mapping[str, object]] | Mapping[str, object],
) -> dict[str, object]:
    """Produce clean text and a sidecar, refusing unresolved boundaries."""

    return _render(
        database,
        profile_revision_id=profile_revision_id,
        artifacts=artifacts,
        mode="clean-release",
    )


def annotated_review_output(
    database: Path,
    **kwargs: object,
) -> dict[str, object]:
    """Compatibility name for annotated review output."""

    return render_annotated_review(database, **kwargs)  # type: ignore[arg-type]


def clean_release_output(
    database: Path,
    **kwargs: object,
) -> dict[str, object]:
    """Compatibility name for clean release output."""

    return render_clean_release(database, **kwargs)  # type: ignore[arg-type]


def write_conversion_output(
    database: Path,
    *,
    profile_revision_id: str,
    artifacts: Sequence[Mapping[str, object]] | Mapping[str, object],
    mode: str,
    output_path: Path,
) -> dict[str, object]:
    """Write one output and, for clean mode, ``.provenance.json`` beside it."""

    output_path = Path(output_path)
    result = _render(
        database,
        profile_revision_id=profile_revision_id,
        artifacts=artifacts,
        mode=mode,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(str(result["output"]), encoding="utf-8")
    sidecar_path: Path | None = None
    if mode == "clean-release":
        sidecar_path = Path(f"{output_path}.provenance.json")
        sidecar = result["sidecar"]
        assert isinstance(sidecar, Mapping)
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "mode": mode,
        "output_path": str(output_path),
        "sidecar_path": None if sidecar_path is None else str(sidecar_path),
        "output_digest": result["output_digest"],
        "provenance": result["provenance"],
    }


record_disposition = record_source_unit_disposition
query_dispositions = query_source_unit_dispositions
annotated_output = render_annotated_review
clean_output = render_clean_release
