"""Installed Sourcebridge command entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from cli import sourcebridge as manifest_cli
from sourcebridge.proposals import (
    DEFAULT_MODEL,
    ProposalEvidenceError,
    ProposalWorker,
    ProposalValidationError,
    installed_model,
    read_evidence,
    resolve_model,
    stage_proposal,
    validate_local_endpoint,
    write_staged_proposal,
    write_rejected_proposal,
)
from sourcebridge.reporting import (
    EvidenceReportError,
    diagnostic_groups,
    diagnostic_occurrences,
    report_packs,
)
from sourcebridge.toolchains import ToolchainReportError, report_toolchains
from sourcebridge.execution import recover_interrupted, run_approved
from sourcebridge.conversion_profiles import (
    ConversionProfileError,
    create_project_profile_revision,
    list_project_profile_revisions,
    promote_project_profile,
    record_project_profile_certification,
    register_rule_revision,
    resolve_project_profile,
    set_project_profile_state,
    set_rule_revision_state,
)
from sourcebridge.engine_tests import EngineTestError, import_catalogue, list_tests, register, run
from sourcebridge.proposal_evaluation import evaluate
from sourcebridge.pipeline import EvidencePipelineError, import_json, initialize


def _proposal_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sourcebridge propose-rule")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/proposals"))
    return parser


def _propose_rule(arguments: list[str]) -> int:
    args = _proposal_parser().parse_args(arguments)
    try:
        endpoint = validate_local_endpoint(args.endpoint)
        model = resolve_model(endpoint, args.model, args.model_digest)
        staged = stage_proposal(
            endpoint,
            model,
            read_evidence(args.evidence),
            enforce_evidence=True,
        )
        path = write_staged_proposal(args.output_dir, staged)
    except (ProposalValidationError, ProposalEvidenceError) as error:
        try:
            path = write_rejected_proposal(args.output_dir, error.record)
        except OSError as write_error:
            print(write_error, file=sys.stderr)
            return 1
        print(f"{error}; raw response staged at {path}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(path)
    return 0


def _propose_batch(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge propose-batch")
    parser.add_argument("evidence", type=Path, nargs="+")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--active-rule-digest", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--session-id")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/proposals"))
    parser.add_argument("--database", type=Path)
    args = parser.parse_args(arguments)
    try:
        worker = ProposalWorker(
            args.endpoint,
            args.model,
            args.model_digest,
            active_rule_digest=args.active_rule_digest,
            batch_id=args.batch_id,
            session_id=args.session_id,
            database=args.database,
        )
        report = worker.run_batch(
            [read_evidence(path) for path in args.evidence],
            output_directory=args.output_dir,
        )
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["summary"]["rejected"] == 0 else 1


def _model_digest(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge model-digest")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    args = parser.parse_args(arguments)
    try:
        model = installed_model(validate_local_endpoint(args.endpoint), args.model)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(model.digest)
    return 0


def _report_diagnostics(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge report-diagnostics")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--signature")
    args = parser.parse_args(arguments)
    try:
        report = (
            diagnostic_occurrences(args.database, args.signature)
            if args.signature
            else diagnostic_groups(args.database)
        )
    except EvidenceReportError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _report_packs(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge report-packs")
    parser.add_argument("roots", metavar="ROOT", type=Path, nargs="+")
    args = parser.parse_args(arguments)
    try:
        report = report_packs(args.roots)
    except (EvidenceReportError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _report_toolchains(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge report-toolchains")
    parser.add_argument("roots", metavar="ROOT", type=Path, nargs="+")
    args = parser.parse_args(arguments)
    try:
        report = report_toolchains(args.roots)
    except (ToolchainReportError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _run_approved(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge run-approved")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("run_id")
    args = parser.parse_args(arguments)
    try:
        return run_approved(args.database, args.run_id)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1


def _recover_run(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge recover-run")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("run_id")
    parser.add_argument(
        "--reason",
        default="Recovered after an interrupted run.",
    )
    args = parser.parse_args(arguments)
    try:
        result = recover_interrupted(args.database, args.run_id, reason=args.reason)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _evaluate_proposal(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge evaluate-proposal")
    parser.add_argument("proposal", type=Path)
    args = parser.parse_args(arguments)
    try:
        result = evaluate(args.proposal)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["eligible_for_human_rule_review"] else 1


def _init_db(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge init-db")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        initialize(args.database)
    except EvidencePipelineError as error:
        print(error, file=sys.stderr)
        return 1
    print(args.database)
    return 0


def _import_json(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge import-json")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append")
    parser.add_argument("--tracked-only", action="store_true")
    args = parser.parse_args(arguments)
    try:
        reports = [
            {
                "root": str(root),
                **import_json(args.database, root, tracked_only=args.tracked_only),
            }
            for root in args.root or [Path(".")]
        ]
        print(json.dumps(reports, indent=2, sort_keys=True))
    except EvidencePipelineError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _test(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge test")
    subparsers = parser.add_subparsers(dest="action", required=True)
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--database", type=Path, required=True)
    register_parser.add_argument("--pack", required=True)
    register_parser.add_argument("--adapter", required=True)
    register_parser.add_argument("--name", required=True)
    register_parser.add_argument("--fixture", type=Path, required=True)
    register_parser.add_argument("--expected", required=True)
    register_parser.add_argument("--adapter-config", required=True)
    register_parser.add_argument("--id", type=int)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--database", type=Path, required=True)
    import_parser.add_argument("catalogue", type=Path)
    import_pack_parser = subparsers.add_parser("import-pack")
    import_pack_parser.add_argument("--database", type=Path, required=True)
    import_pack_parser.add_argument("pack_manifest", type=Path)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--database", type=Path, required=True)
    list_parser.add_argument("--pack")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--database", type=Path, required=True)
    run_parser.add_argument("test_id", type=int)
    args = parser.parse_args(arguments)
    try:
        if args.action == "register":
            print(
                register(
                    args.database,
                    pack_id=args.pack,
                    adapter=args.adapter,
                    name=args.name,
                    fixture=args.fixture,
                    expected_json=args.expected,
                    adapter_config_json=args.adapter_config,
                    test_id=args.id,
                )
            )
        elif args.action == "import":
            print(json.dumps(import_catalogue(args.database, args.catalogue), indent=2, sort_keys=True))
        elif args.action == "import-pack":
            print(json.dumps(_import_pack_catalogue(args.database, args.pack_manifest), indent=2,
                             sort_keys=True))
        elif args.action == "list":
            print(json.dumps(list_tests(args.database, pack_id=args.pack), indent=2, sort_keys=True))
        else:
            result = run(args.database, args.test_id)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "passed" else 1
    except (EngineTestError, EvidencePipelineError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _profile(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge profile")
    subparsers = parser.add_subparsers(dest="action", required=True)
    register_rule_parser = subparsers.add_parser("register-rule")
    register_rule_parser.add_argument("--database", type=Path, required=True)
    register_rule_parser.add_argument("--rule-key", required=True)
    register_rule_parser.add_argument("--source-frontend", required=True)
    register_rule_parser.add_argument("--definition", type=Path, required=True)
    register_rule_parser.add_argument("--rationale", required=True)
    register_rule_parser.add_argument("--created-by", required=True)
    register_rule_parser.add_argument("--workspace")
    register_rule_parser.add_argument("--supersedes")
    rule_state_parser = subparsers.add_parser("set-rule-state")
    rule_state_parser.add_argument("--database", type=Path, required=True)
    rule_state_parser.add_argument("rule_revision_id")
    rule_state_parser.add_argument("state")
    rule_state_parser.add_argument("--rationale", required=True)
    rule_state_parser.add_argument("--decided-by", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--database", type=Path, required=True)
    create_parser.add_argument("manifest", type=Path)
    profile_state_parser = subparsers.add_parser("set-state")
    profile_state_parser.add_argument("--database", type=Path, required=True)
    profile_state_parser.add_argument("profile_revision_id")
    profile_state_parser.add_argument("state", choices=("candidate", "retired"))
    profile_state_parser.add_argument("--rationale", required=True)
    profile_state_parser.add_argument("--decided-by", required=True)
    certify_parser = subparsers.add_parser("certify")
    certify_parser.add_argument("--database", type=Path, required=True)
    certify_parser.add_argument("profile_revision_id")
    certify_parser.add_argument("--tier", required=True)
    certify_parser.add_argument("--status", choices=("passed", "failed", "blocked"), required=True)
    certify_parser.add_argument("--evidence-reference", required=True)
    certify_parser.add_argument("--evidence-digest", required=True)
    certify_parser.add_argument("--details", required=True)
    certify_parser.add_argument("--certified-by", required=True)
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--database", type=Path, required=True)
    promote_parser.add_argument("profile_revision_id")
    promote_parser.add_argument("--rationale", required=True)
    promote_parser.add_argument("--decided-by", required=True)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--database", type=Path, required=True)
    resolve_parser.add_argument("profile_revision_id")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--database", type=Path, required=True)
    list_parser.add_argument("--project")
    list_parser.add_argument("--state", choices=("draft", "candidate", "current", "retired"))
    args = parser.parse_args(arguments)
    try:
        if args.action == "register-rule":
            definition = json.loads(args.definition.read_text(encoding="utf-8"))
            result = register_rule_revision(
                args.database,
                rule_key=args.rule_key,
                source_frontend_pack_id=args.source_frontend,
                definition=definition,
                rationale=args.rationale,
                created_by=args.created_by,
                workspace_name=args.workspace,
                supersedes_revision_id=args.supersedes,
            )
        elif args.action == "set-rule-state":
            result = set_rule_revision_state(
                args.database,
                args.rule_revision_id,
                state=args.state,
                rationale=args.rationale,
                decided_by=args.decided_by,
            )
        elif args.action == "create":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ConversionProfileError("profile manifest must be a JSON object")
            result = create_project_profile_revision(
                args.database,
                project_id=manifest.get("project_id"),
                profile_name=manifest.get("profile_name"),
                base_profile_id=manifest.get("base_profile_id"),
                required_validation_tier=manifest.get("required_validation_tier"),
                rationale=manifest.get("rationale"),
                created_by=manifest.get("created_by"),
                rule_overrides=manifest.get("rule_overrides", []),
                setting_overrides=manifest.get("setting_overrides", []),
                workspace_name=manifest.get("workspace_name"),
                supersedes_profile_revision_id=manifest.get(
                    "supersedes_profile_revision_id"
                ),
            )
        elif args.action == "set-state":
            result = set_project_profile_state(
                args.database,
                args.profile_revision_id,
                state=args.state,
                rationale=args.rationale,
                decided_by=args.decided_by,
            )
        elif args.action == "certify":
            details = json.loads(args.details)
            result = record_project_profile_certification(
                args.database,
                args.profile_revision_id,
                validation_tier=args.tier,
                status=args.status,
                evidence_reference=args.evidence_reference,
                evidence_digest=args.evidence_digest,
                details=details,
                certified_by=args.certified_by,
            )
        elif args.action == "promote":
            result = promote_project_profile(
                args.database,
                args.profile_revision_id,
                rationale=args.rationale,
                decided_by=args.decided_by,
            )
        elif args.action == "resolve":
            result = resolve_project_profile(args.database, args.profile_revision_id)
        else:
            result = list_project_profile_revisions(
                args.database,
                project_id=args.project,
                state=args.state,
            )
    except (
        ConversionProfileError,
        EvidencePipelineError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _import_pack_catalogue(database: Path, pack_manifest: Path) -> dict[str, object]:
    manifest_cli.validate_pack(pack_manifest)
    manifest = manifest_cli.read_json(pack_manifest)
    entrypoints = manifest.get("entrypoints", {})
    catalogue_text = entrypoints.get("engine_tests") if isinstance(entrypoints, dict) else None
    if not isinstance(catalogue_text, str):
        raise EngineTestError(
            f"{pack_manifest}: pack must declare an `engine_tests` entrypoint"
        )
    result = import_catalogue(database, (pack_manifest.parent / catalogue_text).resolve())
    if result["pack_id"] != manifest["id"]:
        raise EngineTestError(f"{pack_manifest}: test catalogue pack ID does not match manifest")
    return result


def _dev(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge dev")
    parser.add_argument("pack_id")
    parser.add_argument("action", choices=("test",))
    parser.add_argument("test_id", type=int)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--pack-manifest", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        imported = _import_pack_catalogue(args.database, args.pack_manifest)
        if imported["pack_id"] != args.pack_id:
            raise EngineTestError(
                f"{args.pack_manifest}: declares `{imported['pack_id']}`, not `{args.pack_id}`"
            )
        result = run(args.database, args.test_id)
    except (EngineTestError, EvidencePipelineError, sqlite3.Error) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps({"catalogue": imported, "result": result}, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def _legacy_command(arguments: list[str]) -> int:
    sys.argv = [sys.argv[0], *arguments]
    return manifest_cli.main()


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "propose-rule":
        return _propose_rule(arguments[1:])
    if arguments and arguments[0] in {"propose-batch", "propose-rules", "proposal-worker"}:
        return _propose_batch(arguments[1:])
    if arguments and arguments[0] == "model-digest":
        return _model_digest(arguments[1:])
    if arguments and arguments[0] == "report-diagnostics":
        return _report_diagnostics(arguments[1:])
    if arguments and arguments[0] == "report-packs":
        return _report_packs(arguments[1:])
    if arguments and arguments[0] in {"report-toolchains", "toolchain-report"}:
        return _report_toolchains(arguments[1:])
    if arguments and arguments[0] == "run-approved":
        return _run_approved(arguments[1:])
    if arguments and arguments[0] == "recover-run":
        return _recover_run(arguments[1:])
    if arguments and arguments[0] == "evaluate-proposal":
        return _evaluate_proposal(arguments[1:])
    if arguments and arguments[0] == "init-db":
        return _init_db(arguments[1:])
    if arguments and arguments[0] == "import-json":
        return _import_json(arguments[1:])
    if arguments and arguments[0] == "test":
        return _test(arguments[1:])
    if arguments and arguments[0] == "profile":
        return _profile(arguments[1:])
    if arguments and arguments[0] == "dev":
        return _dev(arguments[1:])
    return _legacy_command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
