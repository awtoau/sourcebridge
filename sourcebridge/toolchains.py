"""Validation and aggregation for Sourcebridge toolchain currency reports.

Pack adapters own discovery because an installed compiler, a package
declaration, and a resolved dependency graph are all pack-specific facts.  The
engine owns the report contract and the upgrade-decision guardrails.  In
particular, a newer version being visible in a report is not an upgrade
proposal and never implies conversion support.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


TOOLCHAIN_REPORT_SCHEMA_VERSION = 1
TOOLCHAIN_REPORT_TYPE = "sourcebridge.toolchain_currency"
TOOLCHAIN_INDEX_TYPE = "sourcebridge.toolchain_currency_index"
_REPORT_NAMES = {"toolchain-report.json", "roslyn-report.json"}


class ToolchainReportError(ValueError):
    """Raised when a toolchain report is missing evidence or is malformed."""


def validate_toolchain_report(
    report: Any,
    *,
    source: Path | None = None,
) -> dict[str, Any]:
    """Validate and return one pack-owned currency report.

    The validator intentionally does not interpret version compatibility.  It
    only guarantees that observations are separated from decisions and that a
    proposed upgrade has compatibility, validation, and adopt/defer evidence.
    """

    label = str(source) if source is not None else "toolchain report"
    if not isinstance(report, dict):
        raise ToolchainReportError(f"{label}: report must be a JSON object")
    required = {
        "schema_version",
        "report_type",
        "pack",
        "observations",
        "conversion_capability",
        "upgrade_proposals",
    }
    missing = sorted(required - report.keys())
    if missing:
        raise ToolchainReportError(
            f"{label}: missing required field(s): {', '.join(missing)}"
        )
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != TOOLCHAIN_REPORT_SCHEMA_VERSION
    ):
        raise ToolchainReportError(f"{label}: unsupported schema_version")
    if report["report_type"] != TOOLCHAIN_REPORT_TYPE:
        raise ToolchainReportError(f"{label}: unsupported report_type")

    pack = report["pack"]
    if not isinstance(pack, dict) or not isinstance(pack.get("id"), str) or not pack["id"]:
        raise ToolchainReportError(f"{label}: pack.id must be a non-empty string")
    if not isinstance(pack.get("kind"), str) or not pack["kind"]:
        raise ToolchainReportError(f"{label}: pack.kind must be a non-empty string")

    observations = report["observations"]
    if not isinstance(observations, dict):
        raise ToolchainReportError(f"{label}: observations must be an object")
    observation_fields = {"installed", "declared", "resolved", "latest"}
    missing_observations = sorted(observation_fields - observations.keys())
    if missing_observations:
        raise ToolchainReportError(
            f"{label}: observations missing field(s): {', '.join(missing_observations)}"
        )
    if any(not isinstance(observations[field], dict) for field in observation_fields):
        raise ToolchainReportError(f"{label}: observations fields must be objects")

    capability = report["conversion_capability"]
    if not isinstance(capability, dict) or capability.get("status") != "not_claimed":
        raise ToolchainReportError(
            f"{label}: conversion_capability.status must be `not_claimed`"
        )

    proposals = report["upgrade_proposals"]
    if not isinstance(proposals, list):
        raise ToolchainReportError(f"{label}: upgrade_proposals must be a list")
    for index, proposal in enumerate(proposals):
        _validate_upgrade_proposal(proposal, f"{label}: upgrade_proposals[{index}]")
    return report


def _validate_upgrade_proposal(proposal: Any, label: str) -> None:
    if not isinstance(proposal, dict):
        raise ToolchainReportError(f"{label}: proposal must be an object")
    required = {
        "id",
        "component",
        "from",
        "to",
        "compatibility_evidence",
        "validation",
        "decision",
    }
    missing = sorted(required - proposal.keys())
    if missing:
        raise ToolchainReportError(
            f"{label}: missing required field(s): {', '.join(missing)}"
        )
    for field in ("id", "component", "from", "to"):
        if not isinstance(proposal[field], str) or not proposal[field]:
            raise ToolchainReportError(f"{label}: `{field}` must be a non-empty string")

    compatibility = proposal["compatibility_evidence"]
    if not isinstance(compatibility, list) or not compatibility:
        raise ToolchainReportError(
            f"{label}: compatibility_evidence must contain at least one item"
        )
    if any(not isinstance(item, dict) or not item for item in compatibility):
        raise ToolchainReportError(
            f"{label}: compatibility_evidence items must be non-empty objects"
        )

    validation = proposal["validation"]
    if not isinstance(validation, dict):
        raise ToolchainReportError(f"{label}: validation must be an object")
    if not isinstance(validation.get("command"), str) or not validation["command"]:
        raise ToolchainReportError(f"{label}: validation.command is required")
    if not isinstance(validation.get("result"), str) or not validation["result"]:
        raise ToolchainReportError(f"{label}: validation.result is required")

    decision = proposal["decision"]
    if not isinstance(decision, dict):
        raise ToolchainReportError(f"{label}: decision must be an object")
    if decision.get("status") not in {"adopt", "defer"}:
        raise ToolchainReportError(
            f"{label}: decision.status must be `adopt` or `defer`"
        )
    if not isinstance(decision.get("evidence"), str) or not decision["evidence"]:
        raise ToolchainReportError(
            f"{label}: decision.evidence is required for adopt/defer"
        )


def read_toolchain_report(path: Path) -> dict[str, Any]:
    """Read and validate a pack report without changing it."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ToolchainReportError(f"{path}: cannot read report: {error}") from error
    except json.JSONDecodeError as error:
        raise ToolchainReportError(f"{path}: invalid report JSON: {error}") from error
    return validate_toolchain_report(payload, source=path)


def report_toolchains(paths: Sequence[Path]) -> dict[str, Any]:
    """Aggregate committed pack reports in deterministic path order.

    A pack command may print a report to stdout for an ephemeral observation or
    save the same bytes as ``toolchain-report.json`` for evidence collection.
    This function only reads the latter; it never invents a latest version or
    an upgrade decision.
    """

    report_paths = _report_paths(paths)
    reports: list[dict[str, Any]] = []
    for path in report_paths:
        payload = path.read_bytes()
        report = read_toolchain_report(path)
        root = _display_root(path, paths)
        reports.append(
            {
                "repository": root.name,
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "report": report,
            }
        )
    reports.sort(
        key=lambda item: (
            item["report"]["pack"]["id"],
            item["repository"],
            item["path"],
            item["sha256"],
        )
    )
    proposals = [
        proposal
        for item in reports
        for proposal in item["report"]["upgrade_proposals"]
    ]
    return {
        "schema_version": TOOLCHAIN_REPORT_SCHEMA_VERSION,
        "report_type": TOOLCHAIN_INDEX_TYPE,
        "reports": reports,
        "upgrade_proposals": proposals,
    }


def _report_paths(paths: Sequence[Path]) -> list[Path]:
    if not paths:
        raise ToolchainReportError("at least one report root is required")
    found: set[Path] = set()
    for supplied in paths:
        path = supplied.resolve()
        if path.is_file():
            if _is_report_path(path):
                found.add(path)
            continue
        if not path.is_dir():
            raise ToolchainReportError(f"{supplied}: report root does not exist")
        for candidate in path.rglob("*.json"):
            if candidate.is_file() and not candidate.is_symlink() and _is_report_path(candidate):
                found.add(candidate.resolve())
    return sorted(found, key=lambda item: item.as_posix())


def _is_report_path(path: Path) -> bool:
    return path.name in _REPORT_NAMES or path.name.endswith(".toolchain.json")


def _display_root(path: Path, supplied: Sequence[Path]) -> Path:
    directories = [item.resolve() for item in supplied if item.resolve().is_dir()]
    containing = [
        directory
        for directory in directories
        if path == directory or directory in path.parents
    ]
    if containing:
        return max(containing, key=lambda item: len(item.parts))
    return path.parent
