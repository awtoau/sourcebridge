"""Digest-pinned conversion evidence and compact staleness reports.

The table owned here is deliberately an append-only link layer.  Existing
artifact, validation, runtime, and queue rows keep their historical shape;
this layer records the resolved project profile identity that produced each
result.  A report compares those immutable links with one selected profile and
derives staleness without rewriting historical evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from sourcebridge.conversion_profiles import resolve_project_profile
from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize


STALENESS_SCHEMA_VERSION = 1
_TARGET_COLUMNS = (
    "execution_run_id",
    "runtime_run_id",
    "generated_artifact_id",
    "validation_result_id",
)


class StalenessError(ValueError):
    """Raised when digest-pinned evidence cannot be recorded or reported."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StalenessError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _rule_digest(profile: dict[str, object]) -> str:
    rules = profile["rule_overrides"]
    if not isinstance(rules, list):
        raise StalenessError("resolved profile rule_overrides must be a list")
    return _digest(
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


def _profile_identity(
    database: Path,
    profile_revision_id: str,
    *,
    toolchain_digest: str | None = None,
) -> dict[str, str]:
    try:
        resolved = resolve_project_profile(database, profile_revision_id)
    except ValueError as error:
        raise StalenessError(str(error)) from error
    declared_toolchain = resolved.get("toolchain_digest")
    if not isinstance(declared_toolchain, str) or not declared_toolchain.strip():
        raise StalenessError(
            f"{profile_revision_id}: a declared toolchain_digest is required "
            "before linking conversion evidence"
        )
    declared_toolchain = declared_toolchain.strip()
    if toolchain_digest is not None and _text(toolchain_digest, "toolchain_digest") != declared_toolchain:
        raise StalenessError(
            f"{profile_revision_id}: toolchain_digest does not match the resolved profile"
        )
    return {
        "profile_revision_id": profile_revision_id,
        "base_profile_id": str(resolved["base_profile"]["id"]),
        "resolved_profile_digest": _text(
            resolved["resolved_digest"], "resolved profile digest"
        ),
        "rule_digest": _rule_digest(resolved),
        "toolchain_digest": declared_toolchain,
        "validation_tier": _text(
            resolved["required_validation_tier"], "required validation tier"
        ),
    }


def record_conversion_provenance(
    database: Path,
    *,
    profile_revision_id: str,
    execution_run_id: str | None = None,
    runtime_run_id: str | None = None,
    generated_artifact_id: str | None = None,
    validation_result_id: str | None = None,
    toolchain_digest: str | None = None,
    validation_tier: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, str]:
    """Link exactly one result to the profile/rule/toolchain that produced it."""

    profile_revision_id = _text(profile_revision_id, "profile_revision_id")
    targets = {
        "execution_run_id": execution_run_id,
        "runtime_run_id": runtime_run_id,
        "generated_artifact_id": generated_artifact_id,
        "validation_result_id": validation_result_id,
    }
    present = {
        key: _text(value, key)
        for key, value in targets.items()
        if value is not None
    }
    if len(present) != 1:
        raise StalenessError(
            "exactly one execution, runtime, generated-artifact, or validation "
            "evidence reference is required"
        )
    identity = _profile_identity(
        database,
        profile_revision_id,
        toolchain_digest=toolchain_digest,
    )
    validation_tier = (
        identity["validation_tier"]
        if validation_tier is None
        else _text(validation_tier, "validation_tier")
    )
    initialize(database)
    with write_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        target_column, target_id = next(iter(present.items()))
        target = connection.execute(
            f"SELECT 1 FROM sourcebridge_{_target_table(target_column)} WHERE id = ?",
            (target_id,),
        ).fetchone()
        if target is None:
            raise StalenessError(f"{target_id}: {target_column} does not exist")
        if target_column == "runtime_run_id":
            target_profile = connection.execute(
                "SELECT profile_revision_id FROM sourcebridge_rule_runtime_run WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target_profile["profile_revision_id"] != profile_revision_id:
                raise StalenessError(
                    f"{target_id}: runtime evidence belongs to a different profile revision"
                )
        elif target_column == "execution_run_id":
            target_profile = connection.execute(
                "SELECT profile_id FROM sourcebridge_execution_run WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target_profile["profile_id"] != identity["base_profile_id"]:
                raise StalenessError(
                    f"{target_id}: execution evidence belongs to a different base profile"
                )
        else:
            target_decision = connection.execute(
                f"""
                SELECT decision.profile_id
                FROM sourcebridge_{_target_table(target_column)} AS evidence
                JOIN sourcebridge_component_decision AS decision
                  ON decision.id = evidence.decision_id
                WHERE evidence.id = ?
                """,
                (target_id,),
            ).fetchone()
            if target_decision["profile_id"] != identity["base_profile_id"]:
                raise StalenessError(
                    f"{target_id}: conversion evidence belongs to a different base profile"
                )
        link_id = f"conversion-provenance-{uuid4()}"
        try:
            connection.execute(
                """
                INSERT INTO sourcebridge_conversion_provenance
                    (id, execution_run_id, runtime_run_id, generated_artifact_id,
                     validation_result_id, profile_revision_id,
                     resolved_profile_digest, rule_digest, toolchain_digest,
                     validation_tier, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    present.get("execution_run_id"),
                    present.get("runtime_run_id"),
                    present.get("generated_artifact_id"),
                    present.get("validation_result_id"),
                    identity["profile_revision_id"],
                    identity["resolved_profile_digest"],
                    identity["rule_digest"],
                    identity["toolchain_digest"],
                    validation_tier,
                    recorded_at or _now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StalenessError(
                f"{target_id}: evidence already has an immutable provenance link"
            ) from error
    return {
        "id": link_id,
        **present,
        "profile_revision_id": identity["profile_revision_id"],
        "resolved_profile_digest": identity["resolved_profile_digest"],
        "rule_digest": identity["rule_digest"],
        "toolchain_digest": identity["toolchain_digest"],
        "validation_tier": validation_tier,
    }


def _target_table(target_column: str) -> str:
    return {
        "execution_run_id": "execution_run",
        "runtime_run_id": "rule_runtime_run",
        "generated_artifact_id": "generated_artifact",
        "validation_result_id": "validation_result",
    }[target_column]


def link_runtime_evidence(
    database: Path,
    runtime_run_id: str,
    *,
    profile_revision_id: str,
    toolchain_digest: str | None = None,
    validation_tier: str | None = None,
) -> dict[str, str]:
    """Link one #35 rule-runtime result to its exact resolved identity."""

    return record_conversion_provenance(
        database,
        profile_revision_id=profile_revision_id,
        runtime_run_id=runtime_run_id,
        toolchain_digest=toolchain_digest,
        validation_tier=validation_tier,
    )


def link_execution_evidence(
    database: Path,
    execution_run_id: str,
    *,
    profile_revision_id: str,
    toolchain_digest: str | None = None,
    validation_tier: str | None = None,
) -> dict[str, str]:
    """Link one #21 queue execution to its exact resolved identity."""

    return record_conversion_provenance(
        database,
        profile_revision_id=profile_revision_id,
        execution_run_id=execution_run_id,
        toolchain_digest=toolchain_digest,
        validation_tier=validation_tier,
    )


def link_conversion_artifact(
    database: Path,
    generated_artifact_id: str,
    *,
    profile_revision_id: str,
    toolchain_digest: str | None = None,
    validation_tier: str | None = None,
) -> dict[str, str]:
    """Link one generated artifact to its exact resolved identity."""

    return record_conversion_provenance(
        database,
        profile_revision_id=profile_revision_id,
        generated_artifact_id=generated_artifact_id,
        toolchain_digest=toolchain_digest,
        validation_tier=validation_tier,
    )


def link_validation_evidence(
    database: Path,
    validation_result_id: str,
    *,
    profile_revision_id: str,
    toolchain_digest: str | None = None,
    validation_tier: str | None = None,
) -> dict[str, str]:
    """Link one validation result to its exact resolved identity."""

    return record_conversion_provenance(
        database,
        profile_revision_id=profile_revision_id,
        validation_result_id=validation_result_id,
        toolchain_digest=toolchain_digest,
        validation_tier=validation_tier,
    )


def profile_provenance(
    database: Path,
    profile_revision_id: str,
) -> dict[str, Any]:
    """Return the digest-pinned identity used by reproducible output.

    Unlike :func:`staleness_report`, this is only the selected profile
    identity.  It is useful to emit provenance for a new artifact before a
    conversion-provenance link has been recorded for that artifact.
    """

    profile_revision_id = _text(profile_revision_id, "profile_revision_id")
    initialize(database)
    try:
        resolved = resolve_project_profile(database, profile_revision_id)
    except ValueError as error:
        raise StalenessError(str(error)) from error
    identity = _profile_identity(database, profile_revision_id)
    return {
        "profile_revision_id": profile_revision_id,
        "project_id": resolved["project_id"],
        "profile_name": resolved["profile_name"],
        "revision": resolved["revision"],
        "state": resolved["state"],
        "base_profile_id": resolved["base_profile"]["id"],
        "resolved_profile_digest": identity["resolved_profile_digest"],
        "rule_digest": identity["rule_digest"],
        "toolchain_digest": identity["toolchain_digest"],
        "required_validation_tier": identity["validation_tier"],
        "rules": [
            {
                "rule_key": rule["rule_key"],
                "directive": rule["directive"],
                "rule_revision_id": rule["rule_revision_id"],
                "selected_rule_digest": rule["selected_rule_digest"],
            }
            for rule in resolved["rule_overrides"]
        ],
    }


def _target_profile_id(
    connection: sqlite3.Connection,
    *,
    profile_revision_id: str | None,
    project_id: str | None,
    profile_name: str | None,
) -> str:
    if profile_revision_id is not None:
        row = connection.execute(
            "SELECT id FROM sourcebridge_project_profile_revision WHERE id = ?",
            (profile_revision_id,),
        ).fetchone()
    else:
        if project_id is None or profile_name is None:
            raise StalenessError(
                "project_id and profile_name are required when profile_revision_id is omitted"
            )
        row = connection.execute(
            """
            SELECT profile.id
            FROM sourcebridge_project_profile_revision AS profile
            JOIN sourcebridge_project_profile_state AS state
              ON state.profile_revision_id = profile.id
            WHERE profile.project_id = ? AND profile.profile_name = ?
              AND state.sequence = (
                  SELECT MAX(latest.sequence)
                  FROM sourcebridge_project_profile_state AS latest
                  WHERE latest.profile_revision_id = profile.id
              )
              AND state.state = 'current'
            ORDER BY profile.revision DESC
            LIMIT 1
            """,
            (project_id, profile_name),
        ).fetchone()
    if row is None:
        label = profile_revision_id or f"{project_id}/{profile_name}"
        raise StalenessError(f"{label}: current profile does not exist")
    return str(row["id"])


def staleness_report(
    database: Path,
    *,
    profile_revision_id: str | None = None,
    project_id: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Return a compact, derived stale/current report for one profile identity."""

    initialize(database)
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        target_id = _target_profile_id(
            connection,
            profile_revision_id=profile_revision_id,
            project_id=project_id,
            profile_name=profile_name,
        )

    identity = _profile_identity(database, target_id)
    resolved = resolve_project_profile(database, target_id)
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        profile = connection.execute(
            """
            SELECT profile.id, profile.project_id, profile.profile_name,
                   profile.revision, profile.required_validation_tier,
                   profile.toolchain_digest, state.state
            FROM sourcebridge_project_profile_revision AS profile
            JOIN sourcebridge_project_profile_state AS state
              ON state.profile_revision_id = profile.id
            WHERE profile.id = ?
              AND state.sequence = (
                  SELECT MAX(latest.sequence)
                  FROM sourcebridge_project_profile_state AS latest
                  WHERE latest.profile_revision_id = profile.id
              )
            """,
            (target_id,),
        ).fetchone()
        assert profile is not None
        certification = connection.execute(
            """
            SELECT validation_tier, status, evidence_reference, evidence_digest,
                   created_at
            FROM sourcebridge_project_profile_certification
            WHERE profile_revision_id = ?
              AND validation_tier = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (target_id, identity["validation_tier"]),
        ).fetchone()
        links = connection.execute(
            """
            SELECT provenance.id, provenance.profile_revision_id,
                   provenance.execution_run_id, provenance.runtime_run_id,
                   provenance.generated_artifact_id,
                   provenance.validation_result_id,
                   provenance.resolved_profile_digest,
                   provenance.rule_digest, provenance.toolchain_digest,
                   provenance.validation_tier, provenance.recorded_at,
                   historical.project_id, historical.profile_name,
                   historical.revision AS profile_revision
            FROM sourcebridge_conversion_provenance AS provenance
            JOIN sourcebridge_project_profile_revision AS historical
              ON historical.id = provenance.profile_revision_id
            WHERE historical.project_id = ? AND historical.profile_name = ?
            ORDER BY provenance.recorded_at, provenance.id
            """,
            (profile["project_id"], profile["profile_name"]),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for link in links:
        mismatches: list[str] = []
        if link["resolved_profile_digest"] != identity["resolved_profile_digest"]:
            mismatches.append("resolved_profile_digest")
        if link["rule_digest"] != identity["rule_digest"]:
            mismatches.append("rule_digest")
        if link["toolchain_digest"] != identity["toolchain_digest"]:
            mismatches.append("toolchain_digest")
        target = next(
            key
            for key in _TARGET_COLUMNS
            if link[key] is not None
        )
        results.append(
            {
                "id": link["id"],
                "target": {"kind": target.removesuffix("_id"), "id": link[target]},
                "historical_profile": {
                    "id": link["profile_revision_id"],
                    "project_id": link["project_id"],
                    "profile_name": link["profile_name"],
                    "revision": link["profile_revision"],
                },
                "digests": {
                    "resolved_profile": link["resolved_profile_digest"],
                    "rule": link["rule_digest"],
                    "toolchain": link["toolchain_digest"],
                },
                "validation_tier": link["validation_tier"],
                "recorded_at": link["recorded_at"],
                "status": "stale_for_current_profile" if mismatches else "current",
                **({"mismatches": mismatches} if mismatches else {}),
            }
        )

    stale_count = sum(
        result["status"] == "stale_for_current_profile" for result in results
    )
    certification_status = "required" if certification is None else certification["status"]
    return {
        "schema_version": STALENESS_SCHEMA_VERSION,
        "profile": {
            "id": target_id,
            "project_id": profile["project_id"],
            "profile_name": profile["profile_name"],
            "revision": profile["revision"],
            "state": profile["state"],
            "resolved_profile_digest": resolved["resolved_digest"],
            "rule_digest": identity["rule_digest"],
            "toolchain_digest": identity["toolchain_digest"],
        },
        "required_validation_tier": identity["validation_tier"],
        "certification": {
            "validation_tier": identity["validation_tier"],
            "status": certification_status,
            "evidence_reference": (
                None if certification is None else certification["evidence_reference"]
            ),
        },
        "summary": {
            "total": len(results),
            "current": len(results) - stale_count,
            "stale": stale_count,
        },
        "results": results,
    }


current_profile_staleness = staleness_report
stale_for_current_profile = staleness_report
