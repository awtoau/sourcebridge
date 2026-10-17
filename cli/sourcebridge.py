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
    entrypoints = data.get("entrypoints", {})
    if not isinstance(entrypoints, dict):
        raise ValueError(f"{path}: `entrypoints` must be an object")
    rule_runtime = entrypoints.get("rule_runtime")
    if rule_runtime is not None:
        required = {
            "id",
            "command",
            "source",
            "supported_directives",
            "supported_settings",
            "configuration_schema",
        }
        if (
            not isinstance(rule_runtime, dict)
            or set(rule_runtime) - required - {"timeout_seconds"}
            or required - set(rule_runtime)
        ):
            raise ValueError(
                f"{path}: `rule_runtime` must declare its adapter contract fields"
            )
        validate_identifier(path, "entrypoints.rule_runtime.id", rule_runtime["id"])
        if (
            not isinstance(rule_runtime["command"], list)
            or not rule_runtime["command"]
            or not all(isinstance(value, str) and value for value in rule_runtime["command"])
        ):
            raise ValueError(f"{path}: `rule_runtime.command` must be a non-empty string list")
        if not isinstance(rule_runtime["source"], str) or not rule_runtime["source"].strip():
            raise ValueError(f"{path}: `rule_runtime.source` must be a non-empty string")
        for field in ("supported_directives", "supported_settings"):
            values = rule_runtime[field]
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value.strip() for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{path}: `rule_runtime.{field}` must be unique strings")
        if not set(rule_runtime["supported_directives"]) <= {
            "enable", "disable", "replace", "configure",
        }:
            raise ValueError(f"{path}: `rule_runtime` declares an unknown directive")
        schema = rule_runtime["configuration_schema"]
        schema_keys = {
            "schema_version", "type", "properties", "required", "additionalProperties",
        }
        if (
            not isinstance(schema, dict)
            or set(schema) != schema_keys
            or schema["schema_version"] != 1
            or schema["type"] != "object"
            or not isinstance(schema["properties"], dict)
            or not isinstance(schema["required"], list)
            or not isinstance(schema["additionalProperties"], bool)
        ):
            raise ValueError(f"{path}: invalid `rule_runtime.configuration_schema`")
        if "timeout_seconds" in rule_runtime and (
            not isinstance(rule_runtime["timeout_seconds"], int)
            or isinstance(rule_runtime["timeout_seconds"], bool)
            or rule_runtime["timeout_seconds"] <= 0
        ):
            raise ValueError(f"{path}: `rule_runtime.timeout_seconds` must be positive")
    validation = entrypoints.get("validation")
    if validation is not None:
        required = {"id", "command", "source"}
        if (
            not isinstance(validation, dict)
            or set(validation) - required - {"timeout_seconds"}
            or required - set(validation)
        ):
            raise ValueError(
                f"{path}: `validation` must declare id, command, source, "
                "and optional timeout_seconds"
            )
        validate_identifier(path, "entrypoints.validation.id", validation["id"])
        if (
            not isinstance(validation["command"], list)
            or not validation["command"]
            or not all(
                isinstance(value, str) and value for value in validation["command"]
            )
        ):
            raise ValueError(
                f"{path}: `validation.command` must be a non-empty string list"
            )
        if not isinstance(validation["source"], str) or not validation["source"].strip():
            raise ValueError(f"{path}: `validation.source` must be a non-empty string")
        if "timeout_seconds" in validation and (
            not isinstance(validation["timeout_seconds"], int)
            or isinstance(validation["timeout_seconds"], bool)
            or validation["timeout_seconds"] <= 0
        ):
            raise ValueError(f"{path}: `validation.timeout_seconds` must be positive")
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


def load_packs(directory: Path) -> dict[str, dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.pack.json")):
        validate_pack(path)
        pack = read_json(path)
        pack_id = pack["id"]
        if pack_id in packs:
            raise ValueError(f"{directory}: duplicate pack id `{pack_id}`")
        packs[pack_id] = pack
    if not packs:
        raise ValueError(f"{directory}: contains no *.pack.json manifests")
    return packs


def resolve_profile(profile_path: Path, packs_directory: Path) -> None:
    validate_profile(profile_path)
    profile = read_json(profile_path)
    packs = load_packs(packs_directory)
    frontend = packs.get(profile["source_frontend"])
    backend = packs.get(profile["target_backend"])
    if frontend is None:
        raise ValueError(
            f"{profile_path}: source_frontend `{profile['source_frontend']}` "
            "does not name an available pack")
    if frontend["kind"] != "frontend":
        raise ValueError(f"{profile_path}: source_frontend must name a frontend pack")
    if backend is None:
        raise ValueError(
            f"{profile_path}: target_backend `{profile['target_backend']}` "
            "does not name an available pack")
    if backend["kind"] != "backend":
        raise ValueError(f"{profile_path}: target_backend must name a backend pack")
    if not frontend["provides"]["source_languages"]:
        raise ValueError(f"{profile_path}: source frontend provides no source language")
    if not backend["provides"]["target_environments"]:
        raise ValueError(f"{profile_path}: target backend provides no target environment")
    if "corpus_pack" in profile:
        corpus = packs.get(profile["corpus_pack"])
        if corpus is None or corpus["kind"] != "corpus":
            raise ValueError(f"{profile_path}: corpus_pack must name a corpus pack")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-pack", "validate-profile"):
        command = commands.add_parser(name)
        command.add_argument("manifest", type=Path)
    resolve = commands.add_parser("resolve-profile")
    resolve.add_argument("profile", type=Path)
    resolve.add_argument("--packs-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-pack":
            validate_pack(args.manifest)
        elif args.command == "validate-profile":
            validate_profile(args.manifest)
        else:
            resolve_profile(args.profile, args.packs_dir)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    manifest = args.profile if args.command == "resolve-profile" else args.manifest
    print(f"valid {args.command.removeprefix('validate-')}: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
