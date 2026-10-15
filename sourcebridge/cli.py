"""Installed Sourcebridge command entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from cli import sourcebridge as manifest_cli
from sourcebridge.proposals import (
    DEFAULT_MODEL,
    installed_model,
    read_evidence,
    resolve_model,
    stage_proposal,
    validate_local_endpoint,
    write_staged_proposal,
)


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


def _legacy_command(arguments: list[str]) -> int:
    sys.argv = [sys.argv[0], *arguments]
    return manifest_cli.main()


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "propose-rule":
        return _propose_rule(arguments[1:])
    if arguments and arguments[0] == "model-digest":
        return _model_digest(arguments[1:])
    return _legacy_command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
