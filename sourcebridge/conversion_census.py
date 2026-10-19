"""Language-neutral recording and reporting for conversion outcomes."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
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


_PACK_SIDES = ("source", "target", "oracle")
_LIFECYCLES = frozenset(
    {"implemented", "measured", "validated", "partial", "blocked", "unimplemented"}
)


def _lifecycle(source: str, extraction: str, target: str, oracle: str) -> str:
    if oracle == "passed":
        return "validated"
    if target == "passed":
        return "partial"
    if extraction == "failed":
        return "unimplemented"
    return "blocked"


def _rule_status(target: str, oracle: str) -> str:
    """Rule-centric, finishable status: a rule is implemented or validated, else absent.

    A rule is a finished thing: once it emits compilable target code it is
    ``implemented``; once its behavior is oracle-proven it is ``validated``.
    If no target is emitted, no rule exists yet for that feature -- it is
    ``unimplemented`` backlog, not a broken rule.
    """

    if oracle == "passed":
        return "validated"
    if target == "passed":
        return "implemented"
    return "unimplemented"


def record_engine_test_outcome(
    database: Path,
    *,
    pack_id: str,
    profile_name: str,
    source_frontend_pack_id: str,
    target_backend_pack_id: str,
    source_language: str,
    fixture_path: str,
    fixture_digest: str,
    rule_key: str,
    test_id: int,
    logical_name: str,
    adapter_result: dict[str, Any],
    variant: dict[str, Any] | None = None,
    generated_artifact_content: str | None = None,
) -> dict[str, Any]:
    """Record one engine-test outcome as rows in the designed evidence tables.

    This writes the real source/target/oracle results to
    ``sourcebridge_validation_result``, extraction and target gaps to
    ``sourcebridge_diagnostic_occurrence``, and any generated artifact to
    ``sourcebridge_generated_artifact``.  No structured outcome is stored as a
    JSON blob; only free-form compiler diagnostic text goes in ``details_json``.
    """

    statuses = {
        side: adapter_result.get(side, {}).get("status")
        for side in ("source", "extraction", "target", "oracle")
    }
    for side, status in statuses.items():
        if status not in _STATUSES:
            raise ConversionCensusError(f"engine test {test_id}: `{side}` has no valid status")
    variant = variant or {}
    compiler = variant.get("compiler", {}) if isinstance(variant, dict) else {}
    recorded_at = _now()
    initialize(database)

    try:
        with write_transaction(database) as connection:
            corpus_id = _id("corpus", f"{pack_id}:{profile_name}")
            connection.execute(
                """
                INSERT INTO sourcebridge_corpus
                    (id, source_name, revision, source_remote, manifest_digest, created_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (corpus_id, f"{pack_id}:{profile_name}", pack_id, _digest(pack_id), recorded_at),
            )
            variant_id = _id("variant", f"{corpus_id}:{_json(variant)}")
            connection.execute(
                """
                INSERT INTO sourcebridge_variant
                    (id, corpus_id, frontend_pack_id, compiler_identity, target_abi,
                     pointer_width, byte_order, configuration_digest, generated_input_digest, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    variant_id, corpus_id, source_frontend_pack_id, _json(compiler),
                    f"{compiler.get('target_cpu', 'unknown')}-{compiler.get('target_os', 'unknown')}",
                    _digest(_json(variant)), recorded_at,
                ),
            )
            profile_id = _id("profile", f"{pack_id}:{profile_name}")
            connection.execute(
                """
                INSERT INTO sourcebridge_profile
                    (id, profile_name, source_frontend_pack_id, corpus_pack_id,
                     target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
                     options_digest, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    profile_id, profile_name, source_frontend_pack_id, target_backend_pack_id,
                    _digest("[]"), _digest("[]"), _digest(profile_name), recorded_at,
                ),
            )
            source_unit_id = _id("unit", f"{corpus_id}:{fixture_path}:{test_id}")
            connection.execute(
                """
                INSERT INTO sourcebridge_source_unit
                    (id, variant_id, logical_name, source_path, source_span, source_digest, kind)
                VALUES (?, ?, ?, ?, NULL, ?, 'function')
                ON CONFLICT(id) DO NOTHING
                """,
                (source_unit_id, variant_id, logical_name, fixture_path, fixture_digest),
            )
            disposition = "direct_translation" if statuses["target"] == "passed" else "blocked"
            decision_id = _id("decision", f"{profile_id}:{source_unit_id}")
            connection.execute(
                """
                INSERT INTO sourcebridge_component_decision
                    (id, profile_id, source_unit_id, disposition, target_backend_pack_id,
                     runtime_adapter_id, boundary_contract_id, rationale, decided_by, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    decision_id, profile_id, source_unit_id, disposition, target_backend_pack_id,
                    rule_key, f"{pack_id}-engine-test", recorded_at,
                ),
            )

            artifact_id: str | None = None
            if statuses["target"] == "passed" and generated_artifact_content:
                artifact_digest = _digest(generated_artifact_content)
                fragment_id = _id("fragment", f"{corpus_id}:generated:{fixture_path}:{artifact_digest}")
                connection.execute(
                    """
                    INSERT INTO sourcebridge_evidence_fragment
                        (id, corpus_id, kind, logical_path, start_line, start_column,
                         end_line, end_column, content, content_digest, created_at)
                    VALUES (?, ?, 'generated', ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (fragment_id, corpus_id, fixture_path, generated_artifact_content, artifact_digest, recorded_at),
                )
                artifact_id = _id("artifact", f"{decision_id}:{fixture_path}:{artifact_digest}")
                connection.execute(
                    """
                    INSERT INTO sourcebridge_generated_artifact
                        (id, decision_id, artifact_path, artifact_digest, generator_digest, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(decision_id, artifact_path, artifact_digest) DO NOTHING
                    """,
                    (artifact_id, decision_id, fixture_path, artifact_digest, _digest(rule_key), recorded_at),
                )

            validation_ids: dict[str, str] = {}
            for side in _PACK_SIDES:
                detail = adapter_result.get(side, {})
                diagnostic_text = str(detail.get("diagnostic") or detail.get("reason") or "")
                evidence_digest = _digest(_json({"side": side, "status": statuses[side], "test_id": test_id}))
                validation_id = _id("validation", f"{decision_id}:{side}:{statuses[side]}:{evidence_digest}")
                connection.execute(
                    """
                    INSERT INTO sourcebridge_validation_result
                        (id, decision_id, side, validator, status, evidence_path,
                         evidence_digest, details_json, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        validation_id, decision_id, side, f"{pack_id}-engine-test", statuses[side],
                        fixture_path, evidence_digest, _json({"diagnostic": diagnostic_text[-2000:]}),
                        recorded_at,
                    ),
                )
                validation_ids[side] = validation_id

            gaps: list[tuple[str, str, str, str]] = []
            if statuses["extraction"] == "failed":
                gaps.append((
                    "extraction", "pack-extraction-unsupported",
                    str(adapter_result.get("extraction", {}).get("diagnostic") or "extraction failed"),
                    validation_ids["source"],
                ))
            if statuses["target"] in {"blocked", "failed"} and statuses["extraction"] != "failed":
                gaps.append((
                    "target", "pack-target-unsupported",
                    str(adapter_result.get("target", {}).get("reason")
                        or adapter_result.get("target", {}).get("diagnostic") or "target not emitted"),
                    validation_ids["target"],
                ))
            for phase, signature, message, validation_result_id in gaps:
                diagnostic_id = _id("diagnostic", f"{validation_result_id}:{signature}:{source_unit_id}")
                connection.execute(
                    """
                    INSERT INTO sourcebridge_diagnostic_occurrence
                        (id, validation_result_id, source_unit_id, source_fragment_id,
                         generated_artifact_id, generated_fragment_id, phase, tool_identity,
                         diagnostic_code, severity, failure_signature, message, details_json, recorded_at)
                    VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, 'error', ?, ?, '{}', ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        diagnostic_id, validation_result_id, source_unit_id, phase,
                        f"{pack_id}-engine-test", signature, message[-2000:], recorded_at,
                    ),
                )
    except (OSError, sqlite3.Error) as error:
        raise ConversionCensusError(f"{database}: cannot record engine test outcome: {error}") from error
    return {
        "decision_id": decision_id,
        "source_unit_id": source_unit_id,
        "lifecycle": _lifecycle(
            statuses["source"], statuses["extraction"], statuses["target"], statuses["oracle"]
        ),
        "statuses": statuses,
    }


def profile_report(database: Path, *, profile_name: str) -> dict[str, Any]:
    """Build a profile capability report by querying the designed tables."""

    initialize(database)
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            decisions = connection.execute(
                """
                SELECT decision.id AS decision_id, unit.id AS source_unit_id,
                       unit.logical_name, unit.source_path, decision.rationale
                FROM sourcebridge_component_decision AS decision
                JOIN sourcebridge_profile AS profile ON profile.id = decision.profile_id
                JOIN sourcebridge_source_unit AS unit ON unit.id = decision.source_unit_id
                WHERE profile.profile_name = ?
                ORDER BY unit.source_path, decision.id
                """,
                (profile_name,),
            ).fetchall()
            files: list[dict[str, Any]] = []
            for decision in decisions:
                sides: dict[str, str] = {}
                for side in _PACK_SIDES:
                    row = connection.execute(
                        """
                        SELECT status FROM sourcebridge_validation_result
                        WHERE decision_id = ? AND side = ?
                        ORDER BY recorded_at DESC, id DESC LIMIT 1
                        """,
                        (decision["decision_id"], side),
                    ).fetchone()
                    sides[side] = row["status"] if row is not None else "skipped"
                extraction_gap = connection.execute(
                    """
                    SELECT 1 FROM sourcebridge_diagnostic_occurrence
                    WHERE source_unit_id = ? AND phase = 'extraction' LIMIT 1
                    """,
                    (decision["source_unit_id"],),
                ).fetchone()
                extraction = "failed" if extraction_gap is not None else "passed"
                blocker_row = connection.execute(
                    """
                    SELECT phase, message FROM sourcebridge_diagnostic_occurrence
                    WHERE source_unit_id = ?
                    ORDER BY recorded_at DESC, id DESC LIMIT 1
                    """,
                    (decision["source_unit_id"],),
                ).fetchone()
                rule_status = _rule_status(sides["target"], sides["oracle"])
                blocker = None
                if rule_status == "unimplemented" and blocker_row is not None:
                    missing = "frontend extraction rule" if blocker_row["phase"] == "extraction" else "backend emission rule"
                    blocker = {"missing": missing, "detail": blocker_row["message"]}
                files.append(
                    {
                        "decision_id": decision["decision_id"],
                        "source_unit_id": decision["source_unit_id"],
                        "logical_name": decision["logical_name"],
                        "source_path": decision["source_path"],
                        "rule_key": decision["rationale"],
                        "source": sides["source"],
                        "extraction": extraction,
                        "target": sides["target"],
                        "oracle": sides["oracle"],
                        "rule_status": rule_status,
                        "blocker": blocker,
                        "lifecycle": _lifecycle(sides["source"], extraction, sides["target"], sides["oracle"]),
                    }
                )
            # Aggregate per rule and join the registered rule-revision state.
            _ORDER = {"unimplemented": 0, "implemented": 1, "validated": 2}
            rules: dict[str, dict[str, Any]] = {}
            for f in files:
                key = f["rule_key"]
                entry = rules.setdefault(key, {"rule_key": key, "units": 0, "rule_status": "unimplemented"})
                entry["units"] += 1
                if _ORDER[f["rule_status"]] > _ORDER[entry["rule_status"]]:
                    entry["rule_status"] = f["rule_status"]
            for key, entry in rules.items():
                state_row = connection.execute(
                    """
                    SELECT s.state
                    FROM sourcebridge_rule_family AS fam
                    JOIN sourcebridge_rule_revision AS r ON r.family_id = fam.id
                    JOIN sourcebridge_rule_revision_state AS s ON s.rule_revision_id = r.id
                    WHERE fam.rule_key = ?
                    ORDER BY s.sequence DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                entry["registered"] = state_row is not None
                entry["rule_state"] = state_row["state"] if state_row is not None else "none"
                # A rule is only complete when its behavior is proven by an
                # executable oracle. Compilation alone (`implemented`) is never
                # equivalence, so it is explicitly NOT complete.
                entry["complete"] = (
                    entry["rule_state"] == "active" and entry["rule_status"] == "validated"
                )
                entry["needs_oracle"] = (
                    entry["rule_state"] == "active" and entry["rule_status"] == "implemented"
                )
                token_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(au.input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(au.output_tokens), 0) AS output_tokens,
                           COUNT(*) AS records
                    FROM sourcebridge_agent_usage AS au
                    JOIN sourcebridge_rule_revision AS r ON r.id = au.rule_revision_id
                    JOIN sourcebridge_rule_family AS fam ON fam.id = r.family_id
                    WHERE fam.rule_key = ?
                    """,
                    (key,),
                ).fetchone()
                entry["tokens"] = {
                    "input": token_row["input_tokens"],
                    "output": token_row["output_tokens"],
                    "total": token_row["input_tokens"] + token_row["output_tokens"],
                    "records": token_row["records"],
                    "attributed": token_row["records"] > 0,
                }
            total = len(files)
            coverage = {
                stage: {
                    "passed": sum(f[stage] == "passed" for f in files),
                    "denominator": total,
                    "percent": round(100 * sum(f[stage] == "passed" for f in files) / total, 1) if total else 0.0,
                }
                for stage in ("source", "extraction", "target", "oracle")
            }
            lifecycle_counts = Counter(f["lifecycle"] for f in files)
            rule_status_counts = Counter(f["rule_status"] for f in files)
    except (OSError, sqlite3.Error) as error:
        raise ConversionCensusError(f"{database}: cannot build profile report: {error}") from error
    return {
        "schema_version": 1,
        "profile_name": profile_name,
        "declared_files": total,
        "coverage": coverage,
        "files": files,
        "rules": sorted(rules.values(), key=lambda r: r["rule_key"]),
        "rule_status_counts": dict(sorted(rule_status_counts.items())),
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
    }


def resolve_active_rules(database: Path, source_frontend_pack_id: str) -> list[dict[str, Any]]:
    """Return the definitions of all active rule revisions for a source frontend.

    The database is the authority: only rule revisions whose latest lifecycle
    state is ``active`` govern emission.
    """

    initialize(database)
    try:
        with read_transaction(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT r.definition_json
                FROM sourcebridge_rule_family AS fam
                JOIN sourcebridge_rule_revision AS r ON r.family_id = fam.id
                JOIN (
                    SELECT rule_revision_id, state,
                           ROW_NUMBER() OVER (
                               PARTITION BY rule_revision_id ORDER BY sequence DESC
                           ) AS rn
                    FROM sourcebridge_rule_revision_state
                ) AS latest ON latest.rule_revision_id = r.id AND latest.rn = 1
                WHERE fam.source_frontend_pack_id = ? AND latest.state = 'active'
                ORDER BY fam.rule_key, r.revision DESC
                """,
                (source_frontend_pack_id,),
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        raise ConversionCensusError(f"{database}: cannot resolve active rules: {error}") from error
    definitions: list[dict[str, Any]] = []
    for row in rows:
        definition = json.loads(row["definition_json"])
        if isinstance(definition, dict):
            definitions.append(definition)
    return definitions


def record_catalogue_census(
    database: Path,
    *,
    profile_manifest: Path,
    catalogue: Path,
    curriculum: Path | None = None,
) -> dict[str, Any]:
    """Run a pack engine-test catalogue and record its census against a profile.

    The profile manifest is the only place that joins the source frontend to the
    target backend, per the pack-manifest design. The catalogue and curriculum
    are versioned pack data files. All evidence lands in the designed tables;
    the report is then available through ``report-conversion-census``.
    """

    from sourcebridge.engine_tests import import_catalogue, list_tests
    from sourcebridge.engine_tests import run as run_engine_test

    try:
        profile = json.loads(profile_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConversionCensusError(f"{profile_manifest}: cannot read profile manifest: {error}") from error
    for field in ("id", "source_frontend", "target_backend"):
        if not isinstance(profile.get(field), str) or not profile[field]:
            raise ConversionCensusError(f"{profile_manifest}: profile must declare `{field}`")

    rules: dict[int, str] = {}
    if curriculum is not None:
        try:
            data = json.loads(curriculum.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConversionCensusError(f"{curriculum}: cannot read rule curriculum: {error}") from error
        rules = {
            entry["test_id"]: entry["rule"]
            for entry in data.get("entries", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("test_id"), int)
            and isinstance(entry.get("rule"), str)
        }

    imported = import_catalogue(database, catalogue)
    pack_id = imported["pack_id"]
    source_language = (
        profile.get("options", {}).get("source_language")
        or profile["source_frontend"].replace("sourcebridge-", "")
    )
    # The database governs emission: resolve the active rule revisions for this
    # frontend and hand them to the adapter through an isolated file+env path.
    active_rules = resolve_active_rules(database, profile["source_frontend"])
    recorded: list[dict[str, Any]] = []
    previous_env = os.environ.get("SOURCEBRIDGE_RESOLVED_RULES")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="sourcebridge-rules-", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(active_rules))
        rules_path = handle.name
    os.environ["SOURCEBRIDGE_RESOLVED_RULES"] = rules_path
    try:
        for test in list_tests(database, pack_id=pack_id):
            run_result = run_engine_test(database, test["id"])
            adapter_result = run_result.get("actual", {}).get("adapter_result")
            if not isinstance(adapter_result, dict):
                raise ConversionCensusError(
                    f"engine test {test['id']}: adapter produced no structured result"
                )
            variant = {"compiler": adapter_result.get("extraction", {}).get("variant", {})}
            outcome = record_engine_test_outcome(
                database,
                pack_id=pack_id,
                profile_name=profile["id"],
                source_frontend_pack_id=profile["source_frontend"],
                target_backend_pack_id=profile["target_backend"],
                source_language=source_language,
                fixture_path=f"fixtures/{Path(test['fixture_path']).name}",
                fixture_digest=test["fixture_digest"],
                rule_key=rules.get(test["id"], "unclassified"),
                test_id=test["id"],
                logical_name=test["name"],
                adapter_result=adapter_result,
                variant=variant,
                generated_artifact_content=adapter_result.get("target", {}).get("artifact_content"),
            )
            recorded.append({"test_id": test["id"], "lifecycle": outcome["lifecycle"]})
    finally:
        if previous_env is None:
            os.environ.pop("SOURCEBRIDGE_RESOLVED_RULES", None)
        else:
            os.environ["SOURCEBRIDGE_RESOLVED_RULES"] = previous_env
        os.unlink(rules_path)
    return {
        "profile": profile["id"],
        "pack_id": pack_id,
        "active_rules": [r.get("rule_key") for r in active_rules],
        "recorded": len(recorded),
        "report": profile_report(database, profile_name=profile["id"]),
    }
