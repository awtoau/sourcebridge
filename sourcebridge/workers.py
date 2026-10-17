"""Canonical worker policy and audit for supported Sourcebridge operations."""

from __future__ import annotations

import importlib.util
import inspect
import json
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_WORKERS = 24
"""The default for a supported operation that has a parallel stage."""

AUDIT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WorkerPolicy:
    """Worker behavior for one user-facing Sourcebridge operation."""

    operation: str
    command: str
    parallel_stage: bool
    default_workers: int | None
    override: str | None
    exception: str | None
    reason: str
    revisit_evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _parallel(
    operation: str,
    command: str,
    *,
    override: str = "--workers",
) -> WorkerPolicy:
    return WorkerPolicy(
        operation=operation,
        command=command,
        parallel_stage=True,
        default_workers=DEFAULT_WORKERS,
        override=override,
        exception=None,
        reason="The operation has independent writer work that is safe to run in parallel.",
        revisit_evidence=(
            "Keep one-worker and 24-worker results equivalent for rows, errors, "
            "and the reported success count."
        ),
    )


def _serial(
    operation: str,
    command: str,
    *,
    exception: str,
    reason: str,
    revisit_evidence: str,
) -> WorkerPolicy:
    return WorkerPolicy(
        operation=operation,
        command=command,
        parallel_stage=False,
        default_workers=None,
        override=None,
        exception=exception,
        reason=reason,
        revisit_evidence=revisit_evidence,
    )


_NO_PARALLEL_STAGE = dict(
    exception="no-parallel-stage",
    reason=(
        "The current engine stage is deliberately serial; this command does not "
        "process independent work items in parallel."
    ),
    revisit_evidence=(
        "Add an independently schedulable stage, then compare canonical one-worker "
        "and 24-worker outputs before changing this exception."
    ),
)
_SINGLE_OWNER = dict(
    exception="single-owner-execution",
    reason=(
        "The operation claims or recovers one execution run, and the current "
        "database contract requires a single owner."
    ),
    revisit_evidence=(
        "Provide scheduler, claim, recovery, and deterministic multi-run evidence; "
        "do not multiply workers inside one run."
    ),
)
_SINGLE_MODEL = dict(
    exception="single-model-request",
    reason=(
        "The operation makes one local model request or inspects one model "
        "identity; it has no independent request batch."
    ),
    revisit_evidence=(
        "Add a bounded request batch with ordered aggregation and one-worker versus "
        "24-worker equivalence evidence."
    ),
)
_SINGLE_PROCESS = dict(
    exception="single-process-validation",
    reason=(
        "The validator is a dependency-ordered fixture pipeline with one generated "
        "artifact and one target execution."
    ),
    revisit_evidence=(
        "Partition independent fixtures and prove deterministic generated artifacts "
        "and oracle results at one and 24 workers."
    ),
)
_LEGACY_VALIDATION = dict(
    exception="legacy-manifest-validation",
    reason=(
        "Legacy pack and profile commands validate small manifests and do not own "
        "a parallel conversion stage."
    ),
    revisit_evidence=(
        "Replace the legacy command with a measured multi-item validation stage and "
        "add deterministic one-worker versus 24-worker evidence."
    ),
)


# Keep this list explicit.  It is the inventory audited by the canonical command,
# rather than a claim inferred from whichever command happened to be run recently.
WORKER_POLICIES: tuple[WorkerPolicy, ...] = (
    _parallel(
        "sqlite-contention-benchmark",
        "python3 tools/sqlite_contention_benchmark.py",
    ),
    _serial(
        "sourcebridge propose-rule",
        "sourcebridge propose-rule EVIDENCE --model-digest DIGEST",
        **_SINGLE_MODEL,
    ),
    _serial(
        "sourcebridge model-digest",
        "sourcebridge model-digest",
        **_SINGLE_MODEL,
    ),
    _serial(
        "sourcebridge report-diagnostics",
        "sourcebridge report-diagnostics --database DATABASE",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge report-packs",
        "sourcebridge report-packs ROOT [ROOT ...]",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge report-toolchains",
        "sourcebridge report-toolchains ROOT [ROOT ...]",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge run-approved",
        "sourcebridge run-approved --database DATABASE RUN_ID",
        **_SINGLE_OWNER,
    ),
    _serial(
        "sourcebridge recover-run",
        "sourcebridge recover-run --database DATABASE RUN_ID",
        **_SINGLE_OWNER,
    ),
    _serial(
        "sourcebridge evaluate-proposal",
        "sourcebridge evaluate-proposal PROPOSAL",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge init-db",
        "sourcebridge init-db --database DATABASE",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge import-json",
        "sourcebridge import-json --database DATABASE [--root ROOT]",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge test register",
        "sourcebridge test register --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge test import",
        "sourcebridge test import --database DATABASE CATALOGUE",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge test import-pack",
        "sourcebridge test import-pack --database DATABASE PACK_MANIFEST",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge test list",
        "sourcebridge test list --database DATABASE",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge test run",
        "sourcebridge test run --database DATABASE TEST_ID",
        **_SINGLE_OWNER,
    ),
    _serial(
        "sourcebridge profile register-rule",
        "sourcebridge profile register-rule --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile set-rule-state",
        "sourcebridge profile set-rule-state --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile create",
        "sourcebridge profile create --database DATABASE MANIFEST",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile set-state",
        "sourcebridge profile set-state --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile certify",
        "sourcebridge profile certify --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile promote",
        "sourcebridge profile promote --database DATABASE ...",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile resolve",
        "sourcebridge profile resolve --database DATABASE PROFILE_REVISION_ID",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge profile list",
        "sourcebridge profile list --database DATABASE",
        **_NO_PARALLEL_STAGE,
    ),
    _serial(
        "sourcebridge dev test",
        "sourcebridge dev PACK_ID test TEST_ID --database DATABASE --pack-manifest MANIFEST",
        **_SINGLE_OWNER,
    ),
    _serial(
        "sourcebridge-workbench",
        "sourcebridge-workbench",
        **_SINGLE_OWNER,
    ),
    _serial(
        "cli/sourcebridge.py validate-pack",
        "python3 cli/sourcebridge.py validate-pack MANIFEST",
        **_LEGACY_VALIDATION,
    ),
    _serial(
        "cli/sourcebridge.py validate-profile",
        "python3 cli/sourcebridge.py validate-profile PROFILE",
        **_LEGACY_VALIDATION,
    ),
    _serial(
        "cli/sourcebridge.py resolve-profile",
        "python3 cli/sourcebridge.py resolve-profile PROFILE --packs-dir PACKS_DIR",
        **_LEGACY_VALIDATION,
    ),
    _serial(
        "tools/validate_delphi_scalar_rust.py",
        "python3 tools/validate_delphi_scalar_rust.py --delphi-pack PACK --rust-pack PACK",
        **_SINGLE_PROCESS,
    ),
)


def _benchmark_default_checks() -> list[dict[str, object]]:
    """Check both public benchmark defaults without duplicating their values."""

    from sourcebridge.concurrency import run_contention_benchmark

    api_default = inspect.signature(run_contention_benchmark).parameters["workers"].default
    checks: list[dict[str, object]] = [
        {
            "name": "sourcebridge.concurrency.run_contention_benchmark",
            "expected": DEFAULT_WORKERS,
            "actual": api_default,
            "status": "passed" if api_default == DEFAULT_WORKERS else "failed",
        }
    ]

    tool_path = Path(__file__).parents[1] / "tools" / "sqlite_contention_benchmark.py"
    if not tool_path.is_file():
        checks.append(
            {
                "name": "tools/sqlite_contention_benchmark.py --workers",
                "expected": DEFAULT_WORKERS,
                "actual": None,
                "status": "skipped",
            }
        )
        return checks

    spec = importlib.util.spec_from_file_location("_sourcebridge_worker_audit_tool", tool_path)
    if spec is None or spec.loader is None:
        checks.append(
            {
                "name": "tools/sqlite_contention_benchmark.py --workers",
                "expected": DEFAULT_WORKERS,
                "actual": None,
                "status": "failed",
            }
        )
        return checks
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.build_parser()
    workers_action = next(
        action for action in parser._actions if "--workers" in action.option_strings
    )
    cli_default = workers_action.default
    checks.append(
        {
            "name": "tools/sqlite_contention_benchmark.py --workers",
            "expected": DEFAULT_WORKERS,
            "actual": cli_default,
            "status": "passed" if cli_default == DEFAULT_WORKERS else "failed",
        }
    )
    return checks


def audit_worker_defaults() -> dict[str, object]:
    """Return the stable, machine-readable worker-default audit."""

    operations = [policy.as_dict() for policy in WORKER_POLICIES]
    exceptions = [
        {
            "operation": policy.operation,
            "exception": policy.exception,
            "reason": policy.reason,
            "revisit_evidence": policy.revisit_evidence,
        }
        for policy in WORKER_POLICIES
        if policy.exception is not None
    ]
    checks = _benchmark_default_checks()
    failed_checks = [check for check in checks if check["status"] == "failed"]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "default_workers": DEFAULT_WORKERS,
        "status": "failed" if failed_checks else "passed",
        "checks": checks,
        "operations": operations,
        "exceptions": exceptions,
    }


# Short aliases make the policy useful to callers without making the CLI name
# part of the Python API.
worker_audit = audit_worker_defaults
audit_workers = audit_worker_defaults


def audit_json() -> str:
    """Serialize the canonical audit without platform-dependent formatting."""

    return json.dumps(audit_worker_defaults(), indent=2, sort_keys=True)
