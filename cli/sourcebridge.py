#!/usr/bin/env python3
"""Validate Sourcebridge pack manifests and conversion profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"{path}: cannot read manifest: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a JSON object")
    return data


def require_keys(path: Path, data: dict[str, Any], required: set[str],
                 allowed: set[str]) -> None:
    missing = sorted(required - data.keys())
    unknown = sorted(data.keys() - allowed)
    if missing:
        raise ValueError(f"{path}: missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{path}: unknown field(s): {', '.join(unknown)}")


def validate_identifier(path: Path, field: str, value: Any) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{path}: `{field}` must be a lowercase hyphenated identifier")


def validate_pack(path: Path) -> None:
    data = read_json(path)
    allowed = {"schema_version", "id", "kind", "version", "provides", "requires", "entrypoints"}
    require_keys(
        path, data, {"schema_version", "id", "kind", "version", "provides"}, allowed)
    if data["schema_version"] != 1:
        raise ValueError(f"{path}: unsupported schema_version `{data['schema_version']}`")
    validate_identifier(path, "id", data["id"])
    if data["kind"] not in {"frontend", "corpus", "backend", "runtime"}:
        raise ValueError(f"{path}: unsupported pack kind `{data['kind']}`")
    if not isinstance(data["version"], str) or not VERSION.fullmatch(data["version"]):
        raise ValueError(f"{path}: `version` must be semantic version major.minor.patch")
    if not isinstance(data["provides"], dict):
        raise ValueError(f"{path}: `provides` must be an object")
    sources = data["provides"].get("source_languages")
    targets = data["provides"].get("target_environments")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        raise ValueError(f"{path}: `source_languages` must be a list of strings")
    if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
        raise ValueError(f"{path}: `target_environments` must be a list of strings")
    if data["kind"] == "frontend" and targets:
        raise ValueError(f"{path}: a frontend must not provide target environments")
    if data["kind"] == "backend" and sources:
        raise ValueError(f"{path}: a backend must not provide source languages")
    if data["kind"] == "corpus" and (sources or targets):
        raise ValueError(f"{path}: a corpus pack must not provide languages or targets")
    if set(data) - allowed:
        raise ValueError(f"{path}: contains unsupported fields")


def validate_profile(path: Path) -> None:
    data = read_json(path)
    allowed = {
        "schema_version", "id", "source_frontend", "corpus_pack",
        "target_backend", "rule_layers", "runtime_adapters",
        "component_decisions", "options",
    }
    require_keys(
        path,
        data,
        {
            "schema_version", "id", "source_frontend", "target_backend",
            "rule_layers", "runtime_adapters",
        },
        allowed,
    )
    if data["schema_version"] != 1:
        raise ValueError(f"{path}: unsupported schema_version `{data['schema_version']}`")
    for field in ("id", "source_frontend", "target_backend"):
        validate_identifier(path, field, data[field])
    if "corpus_pack" in data:
        validate_identifier(path, "corpus_pack", data["corpus_pack"])
    for field in ("rule_layers", "runtime_adapters"):
        value = data[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{path}: `{field}` must be a list of strings")
        if len(value) != len(set(value)):
            raise ValueError(f"{path}: `{field}` must not contain duplicates")
    if "options" in data and not isinstance(data["options"], dict):
        raise ValueError(f"{path}: `options` must be an object")
    decisions = data.get("component_decisions", [])
    if not isinstance(decisions, list):
        raise ValueError(f"{path}: `component_decisions` must be a list")
    dispositions = {
        "direct_translation", "retain_runtime", "runtime_replacement",
        "native_reimplementation", "external_dependency", "stubbed_boundary",
        "excluded", "blocked",
    }
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ValueError(f"{path}: component decision {index} must be an object")
        required = {"source_selector", "disposition", "rationale"}
        unknown = set(decision) - {
            "source_selector", "disposition", "target_backend",
            "runtime_adapter", "boundary_contract", "rationale",
        }
        if required - set(decision) or unknown:
            raise ValueError(f"{path}: component decision {index} has invalid fields")
        if not all(isinstance(decision[field], str) for field in required):
            raise ValueError(f"{path}: component decision {index} has non-string required fields")
        if decision["disposition"] not in dispositions:
            raise ValueError(f"{path}: component decision {index} has invalid disposition")
        if decision["disposition"] in {
                "retain_runtime", "runtime_replacement", "native_reimplementation",
                "external_dependency", "stubbed_boundary",
        } and "boundary_contract" not in decision:
            raise ValueError(
                f"{path}: component decision {index} needs a boundary_contract")
    if set(data) - allowed:
        raise ValueError(f"{path}: contains unsupported fields")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-pack", "validate-profile"):
        command = commands.add_parser(name)
        command.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-pack":
            validate_pack(args.manifest)
        else:
            validate_profile(args.manifest)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"valid {args.command.removeprefix('validate-')}: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
