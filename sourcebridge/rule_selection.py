"""Deterministic next-rule selection for the Sourcebridge prototype.

This module is deliberately a selector, not a rule engine: it only reads
existing evidence and returns one bounded candidate.  In particular it never
calls a model or writes/promotes a rule.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from sourcebridge.database import read_transaction


STRATEGIES = ("blocker/coverage", "completion", "basics")


class RuleSelectionError(ValueError):
    """The requested strategy or its version-controlled inputs are invalid."""


def _candidate(
    strategy: str,
    *,
    evidence: list[str],
    tests: list[str],
    scope: dict[str, Any],
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "candidate": extra.pop("candidate", None),
        "evidence_references": evidence,
        "test_references": tests,
        "expected_profile_scope": scope,
        "reason": reason,
        **extra,
    }


def _blocker(database: Path) -> dict[str, Any]:
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT wi.id, wi.summary, wi.category, wi.status, wi.decision_id,
                   wi.updated_at,
                   COUNT(DISTINCT d.source_unit_id) AS impacted_scope,
                   COUNT(DISTINCT d.id) AS diagnostic_count
            FROM sourcebridge_work_item AS wi
            LEFT JOIN sourcebridge_work_item_evidence AS link
              ON link.work_item_id = wi.id
            LEFT JOIN sourcebridge_diagnostic_occurrence AS d
              ON d.id = link.diagnostic_occurrence_id
            WHERE wi.status IN ('open', 'claimed', 'in_progress', 'blocked')
            GROUP BY wi.id
            ORDER BY impacted_scope DESC, diagnostic_count DESC,
                     wi.updated_at ASC, wi.id ASC
            """
        ).fetchall()
        if not rows:
            return _candidate(
                "blocker/coverage", evidence=[], tests=[],
                scope={"source_units": 0}, reason="No unresolved queue item remains.",
            )
        row = rows[0]
        links = connection.execute(
            """
            SELECT diagnostic_occurrence_id, validation_result_id
            FROM sourcebridge_work_item_evidence
            WHERE work_item_id = ? ORDER BY recorded_at, rowid
            """,
            (row["id"],),
        ).fetchall()
    evidence = [r["diagnostic_occurrence_id"] for r in links if r["diagnostic_occurrence_id"]]
    tests = [r["validation_result_id"] for r in links if r["validation_result_id"]]
    return _candidate(
        "blocker/coverage", candidate=row["id"], evidence=evidence, tests=tests,
        scope={"source_units": row["impacted_scope"], "category": row["category"]},
        reason=f"Unresolved {row['category']} item impacts the largest evidence scope.",
        work_item_id=row["id"], summary=row["summary"],
    )


def _completion(database: Path, profile_id: str | None) -> dict[str, Any]:
    with read_transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        query = """
            SELECT su.id, su.logical_name, su.source_path, d.id AS decision_id,
                   d.disposition
            FROM sourcebridge_source_unit AS su
            LEFT JOIN sourcebridge_component_decision AS d
              ON d.source_unit_id = su.id AND (? IS NULL OR d.profile_id = ?)
            ORDER BY su.id
        """
        units = connection.execute(query, (profile_id, profile_id)).fetchall()
        for unit in units:
            if unit["decision_id"] is None:
                return _candidate(
                    "completion", candidate=unit["id"], evidence=[], tests=[],
                    scope={"profile_id": profile_id, "source_unit_id": unit["id"]},
                    reason="The smallest source unit has no explicit disposition.",
                    gap="disposition", source_unit=unit["logical_name"],
                )
            validation = connection.execute(
                """
                SELECT id, status FROM sourcebridge_validation_result
                WHERE decision_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1
                """, (unit["decision_id"],)
            ).fetchone()
            if validation is None or validation["status"] not in ("passed", "complete", "current"):
                return _candidate(
                    "completion", candidate=unit["id"], evidence=[],
                    tests=[validation["id"]] if validation else [],
                    scope={"profile_id": profile_id, "source_unit_id": unit["id"]},
                    reason="The source unit has a disposition but lacks current validation.",
                    gap="validation", source_unit=unit["logical_name"],
                )
    return _candidate(
        "completion", evidence=[], tests=[], scope={"profile_id": profile_id},
        reason="All source units have explicit dispositions and current validation.",
    )


def _basics(pack_manifest: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(pack_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuleSelectionError(f"{pack_manifest}: cannot read pack manifest") from error
    entrypoint = manifest.get("entrypoints", {}).get("rule_curriculum")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise RuleSelectionError(f"{pack_manifest}: missing entrypoints.rule_curriculum")
    curriculum_path = (pack_manifest.parent / entrypoint).resolve()
    try:
        payload = json.loads(curriculum_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuleSelectionError(f"{curriculum_path}: invalid curriculum") from error
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not entries:
        raise RuleSelectionError(f"{curriculum_path}: curriculum entries must be non-empty")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise RuleSelectionError(f"{curriculum_path}: curriculum entry must be an object")
    return _candidate(
        "basics", candidate=entry.get("id"), evidence=entry.get("evidence_references", []),
        tests=entry.get("test_references", []),
        scope=entry.get("expected_profile_scope", {}),
        reason="Selected the first ordered entry from the version-controlled pack curriculum.",
        curriculum_entry=entry,
    )


def select_next_rule(
    database: Path,
    strategy: str,
    *,
    profile_id: str | None = None,
    pack_manifest: Path | None = None,
) -> dict[str, Any]:
    """Return exactly one machine-readable candidate without mutating state."""
    if strategy not in STRATEGIES:
        raise RuleSelectionError(f"strategy must be one of: {', '.join(STRATEGIES)}")
    if strategy == "blocker/coverage":
        return _blocker(database)
    if strategy == "completion":
        return _completion(database, profile_id)
    if pack_manifest is None:
        raise RuleSelectionError("basics requires --pack-manifest")
    return _basics(pack_manifest)
