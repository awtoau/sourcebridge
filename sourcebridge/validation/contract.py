"""Pack-owned two-sided validation contract.

The contract is deliberately an orchestration boundary.  A language pack
owns its compiler, formatter, linter, analysis, and oracle commands; this
module validates the small result envelope and records it against one
immutable Sourcebridge decision and one resolved project profile.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from cli import sourcebridge as manifest_cli
from sourcebridge.conversion_profiles import resolve_project_profile
from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize


SOURCE_CLASSIFICATIONS = frozenset(
    {
        "clean",
        "defect",
        "undefined-or-config-dependent",
        "unsupported",
    }
)
VALIDATION_SIDES = ("source", "target", "oracle")
VALIDATION_STATUSES = frozenset({"passed", "failed", "blocked", "skipped"})


class ValidationError(ValueError):
    """Raised when a pack validation contract cannot be executed or recorded."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"value must be JSON serializable: {error}") from error


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _load_contract(pack_manifest: Path) -> tuple[dict[str, Any], Path, str]:
    try:
        manifest_cli.validate_pack(pack_manifest)
        manifest = manifest_cli.read_json(pack_manifest)
    except (OSError, ValueError) as error:
        raise ValidationError(str(error)) from error
    entrypoints = manifest.get("entrypoints", {})
    contract = entrypoints.get("validation") if isinstance(entrypoints, dict) else None
    if not isinstance(contract, dict):
        raise ValidationError(f"{pack_manifest}: pack has no validation entrypoint")
    required = {"id", "command", "source"}
    if set(contract) - required - {"timeout_seconds"} or required - set(contract):
        raise ValidationError(
            f"{pack_manifest}: validation entrypoint must declare only id, command, source, "
            "and optional timeout_seconds"
        )
    adapter_id = _text(contract["id"], "validation entrypoint id")
    command = contract["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) and value for value in command)
    ):
        raise ValidationError("validation command must be a non-empty string list")
    source = _text(contract["source"], "validation entrypoint source")
    source_path = (pack_manifest.parent / source).resolve()
    try:
        source_path.relative_to(pack_manifest.parent.resolve())
    except ValueError as error:
        raise ValidationError("validation entrypoint source must stay below the pack") from error
    if not source_path.is_file():
        raise ValidationError(f"{source_path}: validation entrypoint source does not exist")
    timeout = contract.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValidationError("validation timeout_seconds must be a positive integer")
    normalized = dict(contract)
    normalized["id"] = adapter_id
    normalized["source"] = source
    normalized["command"] = list(command)
    normalized["timeout_seconds"] = timeout
    contract_digest = _sha256(
        source_path.read_bytes() + b"\0" + _canonical(normalized).encode("utf-8")
    )
    return normalized, source_path, contract_digest


def _profile_identity(
    database: Path, profile_revision_id: str
) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        resolved = resolve_project_profile(database, profile_revision_id)
    except ValueError as error:
        raise ValidationError(str(error)) from error
    toolchain_digest = resolved.get("toolchain_digest")
    if not isinstance(toolchain_digest, str) or not toolchain_digest.strip():
        raise ValidationError(
            f"{profile_revision_id}: a declared toolchain_digest is required for validation"
        )
    rules = resolved.get("rule_overrides")
    if not isinstance(rules, list):
        raise ValidationError("resolved profile rule_overrides must be a list")
    rule_digest = _sha256(
        _canonical(
            [
                {
                    "rule_key": rule["rule_key"],
                    "directive": rule["directive"],
                    "rule_revision_id": rule["rule_revision_id"],
                    "selected_rule_digest": rule["selected_rule_digest"],
                }
                for rule in rules
            ]
        )
    )
    identity = {
        "profile_revision_id": profile_revision_id,
        "base_profile_id": str(resolved["base_profile"]["id"]),
        "resolved_profile_digest": _text(
            resolved["resolved_digest"], "resolved profile digest"
        ),
        "rule_digest": rule_digest,
        "toolchain_digest": toolchain_digest.strip(),
        "validation_tier": _text(
            resolved["required_validation_tier"], "required validation tier"
        ),
    }
    return identity, resolved


def _decision_context(
    database: Path, decision_id: str, profile_identity: Mapping[str, str]
) -> dict[str, Any]:
    initialize(database)
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT decision.id, decision.profile_id, decision.source_unit_id,
                   decision.disposition, decision.runtime_adapter_id,
                   decision.target_backend_pack_id, source.variant_id,
                   source.source_path, source.source_digest, variant.corpus_id,
                   profile.target_backend_pack_id AS profile_target_backend_pack_id
            FROM sourcebridge_component_decision AS decision
            JOIN sourcebridge_source_unit AS source
              ON source.id = decision.source_unit_id
            JOIN sourcebridge_variant AS variant
              ON variant.id = source.variant_id
            JOIN sourcebridge_profile AS profile
              ON profile.id = decision.profile_id
            WHERE decision.id = ?
            """,
            (decision_id,),
        ).fetchone()
    if row is None:
        raise ValidationError(f"{decision_id}: conversion decision does not exist")
    if row["profile_id"] != profile_identity["base_profile_id"]:
        raise ValidationError(
            f"{decision_id}: decision belongs to base profile `{row['profile_id']}`, "
            f"not `{profile_identity['base_profile_id']}`"
        )
    if row["target_backend_pack_id"] not in {
        None,
        row["profile_target_backend_pack_id"],
    }:
        raise ValidationError(
            f"{decision_id}: decision target backend does not match its base profile"
        )
    return dict(row)


def _format_command(
    command: list[str],
    *,
    case_id: str,
    decision_id: str,
    profile_revision_id: str,
    manifest_dir: Path,
) -> list[str]:
    values = {
        "case_id": case_id,
        "decision_id": decision_id,
        "profile_revision_id": profile_revision_id,
        "manifest_dir": str(manifest_dir),
    }
    formatted: list[str] = []
    for value in command:
        try:
            formatted.append(value.format(**values))
        except KeyError as error:
            raise ValidationError(
                f"validation command uses unsupported placeholder `{error.args[0]}`"
            ) from error
    return formatted


def _validate_envelope(envelope: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(envelope, dict):
        raise ValidationError("validation adapter result must be a JSON object")
    required = {"schema_version", "source", "target"}
    allowed = required | {"oracle"}
    if set(envelope) - allowed or required - set(envelope):
        raise ValidationError(
            "validation adapter result must contain schema_version, source, and target; "
            "oracle is optional"
        )
    if envelope["schema_version"] != 1:
        raise ValidationError("unsupported validation adapter result schema")
    result: dict[str, dict[str, Any]] = {}
    for side in VALIDATION_SIDES:
        if side not in envelope:
            continue
        value = envelope[side]
        if not isinstance(value, dict):
            raise ValidationError(f"validation `{side}` result must be an object")
        if set(value) - {"status", "classification", "evidence", "details"}:
            raise ValidationError(f"validation `{side}` result has unsupported fields")
        status = value.get("status")
        if status not in VALIDATION_STATUSES:
            raise ValidationError(f"validation `{side}` has invalid status `{status}`")
        evidence = value.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) - {"path", "raw"}:
            raise ValidationError(
                f"validation `{side}` evidence must contain only path and raw"
            )
        path = evidence.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValidationError(f"validation `{side}` evidence path must be non-empty")
        if "raw" not in evidence:
            raise ValidationError(f"validation `{side}` evidence must preserve raw content")
        if side == "source":
            classification = value.get("classification")
            if classification not in SOURCE_CLASSIFICATIONS:
                raise ValidationError(
                    "validation source classification must be one of "
                    + ", ".join(sorted(SOURCE_CLASSIFICATIONS))
                )
        elif "classification" in value:
            raise ValidationError(f"validation `{side}` does not support classification")
        details = value.get("details", {})
        if not isinstance(details, dict):
            raise ValidationError(f"validation `{side}` details must be an object")
        result[side] = dict(value)
    return result


def _raw_text(value: Any) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _aggregate_status(source: Mapping[str, Any], sides: Mapping[str, Mapping[str, Any]]) -> str:
    source_classification = str(source["classification"])
    if source_classification in {"undefined-or-config-dependent", "unsupported"}:
        return "blocked"
    statuses = [str(side["status"]) for side in sides.values()]
    if any(status in {"blocked", "skipped"} for status in statuses):
        return "blocked"
    if source_classification == "defect" or any(status == "failed" for status in statuses):
        return "failed"
    return "passed"


def run_validation(
    database: Path,
    *,
    profile_revision_id: str,
    decision_id: str,
    pack_manifest: Path,
    input_payload: Mapping[str, Any],
    requested_by: str = "sourcebridge",
    toolchain_digest: str | None = None,
) -> dict[str, Any]:
    """Run a pack adapter and persist one source/target/oracle validation case."""

    profile_revision_id = _text(profile_revision_id, "profile_revision_id")
    decision_id = _text(decision_id, "decision_id")
    requested_by = _text(requested_by, "requested_by")
    if not isinstance(input_payload, Mapping):
        raise ValidationError("input_payload must be a JSON object")
    profile_identity, resolved_profile = _profile_identity(database, profile_revision_id)
    if toolchain_digest is not None and _text(toolchain_digest, "toolchain_digest") != profile_identity[
        "toolchain_digest"
    ]:
        raise ValidationError(
            f"{profile_revision_id}: toolchain_digest does not match the resolved profile"
        )
    _decision_context(database, decision_id, profile_identity)
    contract, source_path, contract_digest = _load_contract(pack_manifest)
    case_id = f"validation-case-{uuid4()}"
    command = _format_command(
        contract["command"],
        case_id=case_id,
        decision_id=decision_id,
        profile_revision_id=profile_revision_id,
        manifest_dir=pack_manifest.parent.resolve(),
    )
    request = {
        "schema_version": 1,
        "case_id": case_id,
        "decision_id": decision_id,
        "profile_revision_id": profile_revision_id,
        "validator": contract["id"],
        "input": dict(input_payload),
        "profile": {
            "base_profile_id": profile_identity["base_profile_id"],
            "resolved_profile_digest": profile_identity["resolved_profile_digest"],
            "rule_digest": profile_identity["rule_digest"],
            "toolchain_digest": profile_identity["toolchain_digest"],
            "validation_tier": profile_identity["validation_tier"],
            "profile": resolved_profile,
        },
    }
    request_text = _canonical(request)
    started_at = _now()
    try:
        process = subprocess.run(
            command,
            cwd=pack_manifest.parent,
            input=request_text,
            capture_output=True,
            text=True,
            timeout=contract["timeout_seconds"],
            check=False,
        )
    except OSError as error:
        raise ValidationError(f"validation adapter cannot execute: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ValidationError(
            f"validation adapter timed out after {contract['timeout_seconds']} seconds"
        ) from error
    completed_at = _now()
    if process.returncode != 0:
        raise ValidationError(
            f"validation adapter exited with {process.returncode}: {process.stderr.strip()}"
        )
    raw_response = process.stdout
    try:
        envelope = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise ValidationError(f"validation adapter emitted invalid JSON: {error}") from error
    sides = _validate_envelope(envelope)
    source = sides["source"]
    status = _aggregate_status(source, sides)
    recorded_at = completed_at
    raw_response_digest = _sha256(raw_response)

    initialize(database)
    with write_transaction(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        decision = connection.execute(
            "SELECT profile_id FROM sourcebridge_component_decision WHERE id = ?",
            (decision_id,),
        ).fetchone()
        if decision is None:
            raise ValidationError(f"{decision_id}: conversion decision does not exist")
        case_values = (
            case_id,
            decision_id,
            profile_revision_id,
            contract["id"],
            contract_digest,
            status,
            source["classification"],
            request_text,
            raw_response,
            raw_response_digest,
            _canonical(command),
            requested_by,
            started_at,
            completed_at,
        )
        connection.execute(
            """
            INSERT INTO sourcebridge_validation_case
                (id, decision_id, profile_revision_id, validator, validator_digest,
                 status, source_classification, request_json, raw_response,
                 raw_response_digest, command_json, requested_by, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            case_values,
        )
        side_records: list[dict[str, Any]] = []
        for side, result in sides.items():
            validation_id = f"validation-{uuid4()}"
            raw_evidence = _raw_text(result["evidence"]["raw"])
            evidence_digest = _sha256(raw_evidence)
            details = {
                "schema_version": 1,
                "validation_case_id": case_id,
                "validator": contract["id"],
                "status": result["status"],
                "raw_evidence": raw_evidence,
                **(
                    {"classification": result["classification"]}
                    if side == "source"
                    else {}
                ),
                "details": result.get("details", {}),
            }
            details_text = _canonical(details)
            connection.execute(
                """
                INSERT INTO sourcebridge_validation_result
                    (id, decision_id, side, validator, status, evidence_path,
                     evidence_digest, details_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id,
                    decision_id,
                    side,
                    contract["id"],
                    result["status"],
                    result["evidence"]["path"],
                    evidence_digest,
                    details_text,
                    recorded_at,
                ),
            )
            evidence_id = f"validation-evidence-{uuid4()}"
            connection.execute(
                """
                INSERT INTO sourcebridge_validation_evidence
                    (id, validation_case_id, validation_result_id, side,
                     evidence_path, evidence_digest, raw_evidence, details_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    case_id,
                    validation_id,
                    side,
                    result["evidence"]["path"],
                    evidence_digest,
                    raw_evidence,
                    details_text,
                    recorded_at,
                ),
            )
            provenance_id = f"conversion-provenance-{uuid4()}"
            connection.execute(
                """
                INSERT INTO sourcebridge_conversion_provenance
                    (id, validation_result_id, profile_revision_id,
                     resolved_profile_digest, rule_digest, toolchain_digest,
                     validation_tier, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provenance_id,
                    validation_id,
                    profile_revision_id,
                    profile_identity["resolved_profile_digest"],
                    profile_identity["rule_digest"],
                    profile_identity["toolchain_digest"],
                    profile_identity["validation_tier"],
                    recorded_at,
                ),
            )
            side_records.append(
                {
                    "id": validation_id,
                    "side": side,
                    "status": result["status"],
                    "classification": result.get("classification"),
                    "evidence_id": evidence_id,
                    "evidence_path": result["evidence"]["path"],
                    "evidence_digest": evidence_digest,
                    "raw_evidence": raw_evidence,
                    "details": result.get("details", {}),
                    "provenance_id": provenance_id,
                }
            )

    return {
        "id": case_id,
        "decision_id": decision_id,
        "profile_revision_id": profile_revision_id,
        "validator": contract["id"],
        "validator_digest": contract_digest,
        "status": status,
        "source_classification": source["classification"],
        "request": request,
        "raw_response": raw_response,
        "raw_response_digest": raw_response_digest,
        "command": command,
        "requested_by": requested_by,
        "started_at": started_at,
        "completed_at": completed_at,
        "provenance": profile_identity,
        "evidence": side_records,
    }


def get_validation_case(database: Path, case_id: str) -> dict[str, Any]:
    """Return a recorded case with raw side evidence and provenance."""

    case_id = _text(case_id, "case_id")
    initialize(database)
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        case = connection.execute(
            """
            SELECT id, decision_id, profile_revision_id, validator, validator_digest,
                   status, source_classification, request_json, raw_response,
                   raw_response_digest, command_json, requested_by, started_at, completed_at
            FROM sourcebridge_validation_case
            WHERE id = ?
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            raise ValidationError(f"{case_id}: validation case does not exist")
        rows = connection.execute(
            """
            SELECT evidence.id, evidence.validation_result_id, evidence.side,
                   result.status, result.validator, evidence.evidence_path,
                   evidence.evidence_digest, evidence.raw_evidence,
                   evidence.details_json, provenance.id AS provenance_id,
                   provenance.profile_revision_id, provenance.resolved_profile_digest,
                   provenance.rule_digest, provenance.toolchain_digest,
                   provenance.validation_tier, evidence.recorded_at
            FROM sourcebridge_validation_evidence AS evidence
            JOIN sourcebridge_validation_result AS result
              ON result.id = evidence.validation_result_id
            JOIN sourcebridge_conversion_provenance AS provenance
              ON provenance.validation_result_id = result.id
            WHERE evidence.validation_case_id = ?
            ORDER BY CASE evidence.side
                WHEN 'source' THEN 1
                WHEN 'target' THEN 2
                WHEN 'oracle' THEN 3
            END
            """,
            (case_id,),
        ).fetchall()
    return {
        "id": case["id"],
        "decision_id": case["decision_id"],
        "profile_revision_id": case["profile_revision_id"],
        "validator": case["validator"],
        "validator_digest": case["validator_digest"],
        "status": case["status"],
        "source_classification": case["source_classification"],
        "request": json.loads(case["request_json"]),
        "raw_response": case["raw_response"],
        "raw_response_digest": case["raw_response_digest"],
        "command": json.loads(case["command_json"]),
        "requested_by": case["requested_by"],
        "started_at": case["started_at"],
        "completed_at": case["completed_at"],
        "evidence": [
            {
                "id": row["id"],
                "validation_result_id": row["validation_result_id"],
                "side": row["side"],
                "status": row["status"],
                "validator": row["validator"],
                "evidence_path": row["evidence_path"],
                "evidence_digest": row["evidence_digest"],
                "raw_evidence": row["raw_evidence"],
                "details": json.loads(row["details_json"]),
                "provenance": {
                    "id": row["provenance_id"],
                    "profile_revision_id": row["profile_revision_id"],
                    "resolved_profile_digest": row["resolved_profile_digest"],
                    "rule_digest": row["rule_digest"],
                    "toolchain_digest": row["toolchain_digest"],
                    "validation_tier": row["validation_tier"],
                },
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ],
    }


def list_validation_cases(
    database: Path,
    *,
    decision_id: str | None = None,
    profile_revision_id: str | None = None,
) -> list[dict[str, Any]]:
    """List compact validation identities without discarding raw evidence on read."""

    initialize(database)
    clauses: list[str] = []
    parameters: list[str] = []
    if decision_id is not None:
        clauses.append("decision_id = ?")
        parameters.append(_text(decision_id, "decision_id"))
    if profile_revision_id is not None:
        clauses.append("profile_revision_id = ?")
        parameters.append(_text(profile_revision_id, "profile_revision_id"))
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    with read_transaction(database) as connection:
        rows = connection.execute(
            """
            SELECT id, decision_id, profile_revision_id, validator, status,
                   source_classification, validator_digest, raw_response_digest,
                   requested_by, started_at, completed_at
            FROM sourcebridge_validation_case
            """
            + where
            + " ORDER BY completed_at, id",
            parameters,
        ).fetchall()
    return [
        {
            "id": row[0],
            "decision_id": row[1],
            "profile_revision_id": row[2],
            "validator": row[3],
            "status": row[4],
            "source_classification": row[5],
            "validator_digest": row[6],
            "raw_response_digest": row[7],
            "requested_by": row[8],
            "started_at": row[9],
            "completed_at": row[10],
        }
        for row in rows
    ]


execute = run_validation
run = run_validation
record = run_validation
