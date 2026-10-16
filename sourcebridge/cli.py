"""Installed Sourcebridge command entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from cli import sourcebridge as manifest_cli
from sourcebridge.proposals import (
    DEFAULT_MODEL,
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
from sourcebridge.execution import run_approved
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
        staged = stage_proposal(endpoint, model, read_evidence(args.evidence))
        path = write_staged_proposal(args.output_dir, staged)
    except ProposalValidationError as error:
        path = write_rejected_proposal(args.output_dir, error.record)
        print(f"{error}; raw response staged at {path}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(path)
    return 0


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


def _run_approved(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sourcebridge run-approved")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("run_id")
    args = parser.parse_args(arguments)
    try:
        return run_approved(args.database, args.run_id)
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


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
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(arguments)
    try:
        print(json.dumps(import_json(args.database, args.root), indent=2, sort_keys=True))
    except EvidencePipelineError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _legacy_command(arguments: list[str]) -> int:
    sys.argv = [sys.argv[0], *arguments]
    return manifest_cli.main()


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "propose-rule":
        return _propose_rule(arguments[1:])
    if arguments and arguments[0] == "model-digest":
        return _model_digest(arguments[1:])
    if arguments and arguments[0] == "report-diagnostics":
        return _report_diagnostics(arguments[1:])
    if arguments and arguments[0] == "report-packs":
        return _report_packs(arguments[1:])
    if arguments and arguments[0] == "run-approved":
        return _run_approved(arguments[1:])
    if arguments and arguments[0] == "evaluate-proposal":
        return _evaluate_proposal(arguments[1:])
    if arguments and arguments[0] == "init-db":
        return _init_db(arguments[1:])
    if arguments and arguments[0] == "import-json":
        return _import_json(arguments[1:])
    return _legacy_command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
