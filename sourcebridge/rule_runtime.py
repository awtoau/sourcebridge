"""Execute digest-pinned project rules through a pack-declared adapter."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from cli import sourcebridge as manifest_cli
from sourcebridge.conversion_profiles import resolve_project_profile
from sourcebridge.database import write_transaction
from sourcebridge.pipeline import initialize


class RuleRuntimeError(ValueError):
    """Raised when a pack adapter cannot execute a resolved rule profile."""


_DIRECTIVES = {"enable", "disable", "replace", "configure"}
_CONFIGURATION_TYPES = {"array", "boolean", "integer", "number", "object", "string"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RuleRuntimeError(f"value must be JSON serializable: {error}") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleRuntimeError(f"{field} must be a non-empty string")
    return value.strip()


def _relative_pack_path(root: Path, value: object, field: str) -> Path:
    relative = Path(_text(value, field))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuleRuntimeError(f"{field} must stay below the pack manifest")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuleRuntimeError(f"{field} must stay below the pack manifest") from error
    return path


def _load_contract(pack_manifest: Path) -> tuple[dict[str, Any], Path, dict[str, Any], str]:
    try:
        manifest_cli.validate_pack(pack_manifest)
        manifest = manifest_cli.read_json(pack_manifest)
    except (OSError, ValueError) as error:
        raise RuleRuntimeError(f"{pack_manifest}: invalid pack manifest: {error}") from error
    entrypoints = manifest.get("entrypoints", {})
    contract = entrypoints.get("rule_runtime") if isinstance(entrypoints, dict) else None
    required = {
        "id",
        "command",
        "source",
        "supported_directives",
        "supported_settings",
        "configuration_schema",
    }
    if (
        not isinstance(contract, dict)
        or set(contract) - required - {"timeout_seconds"}
        or required - set(contract)
    ):
        raise RuleRuntimeError(
            f"{pack_manifest}: rule_runtime entrypoint must declare exactly "
            "id, command, source, supported_directives, supported_settings, "
            "and configuration_schema"
        )
    adapter_id = _text(contract["id"], "rule_runtime adapter id")
    command = contract["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) and value for value in command)
    ):
        raise RuleRuntimeError("rule_runtime command must be a non-empty string list")
    source = _relative_pack_path(
        pack_manifest.parent.resolve(), contract["source"], "adapter source"
    )
    if not source.is_file():
        raise RuleRuntimeError(f"{source}: adapter source does not exist")

    directives = contract["supported_directives"]
    if (
        not isinstance(directives, list)
        or not all(isinstance(value, str) for value in directives)
        or len(directives) != len(set(directives))
        or not set(directives) <= _DIRECTIVES
    ):
        raise RuleRuntimeError("rule_runtime supported_directives must be unique known directives")
    settings = contract["supported_settings"]
    if (
        not isinstance(settings, list)
        or not all(isinstance(value, str) and value.strip() for value in settings)
        or len(settings) != len(set(settings))
    ):
        raise RuleRuntimeError("rule_runtime supported_settings must be unique non-empty strings")
    schema = contract["configuration_schema"]
    if not isinstance(schema, dict):
        raise RuleRuntimeError("rule_runtime configuration_schema must be an object")
    schema_required = {"schema_version", "type", "properties", "required", "additionalProperties"}
    if set(schema) - schema_required or schema_required - set(schema):
        raise RuleRuntimeError(
            "rule_runtime configuration_schema must declare schema_version, type, "
            "properties, required, and additionalProperties"
        )
    if schema["schema_version"] != 1 or schema["type"] != "object":
        raise RuleRuntimeError("rule_runtime supports only configuration schema version 1 objects")
    if not isinstance(schema["properties"], dict) or not isinstance(schema["required"], list):
        raise RuleRuntimeError(
            "rule_runtime configuration_schema properties and required are invalid"
        )
    if not isinstance(schema["additionalProperties"], bool):
        raise RuleRuntimeError(
            "rule_runtime configuration_schema additionalProperties must be boolean"
        )
    for key, property_schema in schema["properties"].items():
        if (
            not isinstance(key, str)
            or not isinstance(property_schema, dict)
            or property_schema.get("type") not in _CONFIGURATION_TYPES
        ):
            raise RuleRuntimeError(
                f"rule_runtime configuration schema has invalid property `{key}`"
            )
    if not all(
        isinstance(value, str) and value in schema["properties"]
        for value in schema["required"]
    ):
        raise RuleRuntimeError("rule_runtime configuration schema has an invalid required property")

    root = pack_manifest.parent.resolve()
    try:
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as error:
        raise RuleRuntimeError(f"{source}: cannot digest adapter source: {error}") from error
    adapter_digest = _digest({"contract": contract, "source_digest": source_digest})
    return manifest, root, contract, adapter_digest


def _matches_type(value: object, expected: str) -> bool:
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in {int, float} and type(value) is not bool
    return {
        "array": list,
        "object": dict,
        "string": str,
    }.get(expected, object) is type(value)


def _validate_configuration(
    rule_key: str,
    configuration: object,
    schema: Mapping[str, Any],
) -> None:
    if not isinstance(configuration, dict):
        raise RuleRuntimeError(f"{rule_key}: adapter configuration must be an object")
    properties = schema["properties"]
    unknown = sorted(set(configuration) - set(properties))
    if unknown and not schema["additionalProperties"]:
        raise RuleRuntimeError(
            f"{rule_key}: adapter rejects unsupported configuration key(s): {', '.join(unknown)}"
        )
    missing = sorted(set(schema["required"]) - set(configuration))
    if missing:
        raise RuleRuntimeError(
            f"{rule_key}: adapter configuration is missing required key(s): {', '.join(missing)}"
        )
    for key, value in configuration.items():
        property_schema = properties.get(key)
        if property_schema is not None and not _matches_type(value, property_schema["type"]):
            raise RuleRuntimeError(
                f"{rule_key}: adapter rejects configuration `{key}` with unsupported type"
            )


def _resolve_request(
    profile: Mapping[str, Any],
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
    root: Path,
    input_payload: Any,
) -> tuple[dict[str, Any], str, str, str]:
    supported_directives = set(contract["supported_directives"])
    supported_settings = set(contract["supported_settings"])
    schema = contract["configuration_schema"]
    selected: dict[str, Mapping[str, Any]] = {}
    for override in profile["rule_overrides"]:
        directive = str(override["directive"])
        if directive in {"enable", "replace"}:
            selected[str(override["rule_key"])] = override

    rules: list[dict[str, Any]] = []
    implementation_refs: list[dict[str, str]] = []
    for override in profile["rule_overrides"]:
        rule_key = str(override["rule_key"])
        directive = str(override["directive"])
        if directive not in supported_directives:
            raise RuleRuntimeError(
                f"{rule_key}: adapter `{contract['id']}` rejects unsupported directive "
                f"`{directive}`"
            )
        configuration = override["configuration"]
        if configuration:
            _validate_configuration(rule_key, configuration, schema)
        implementation = override.get("implementation")
        if directive in {"enable", "replace"}:
            if not isinstance(implementation, Mapping):
                raise RuleRuntimeError(
                    f"{rule_key}: `{directive}` requires a digest-pinned pack implementation"
                )
        elif directive == "configure" and implementation is None:
            selected_override = selected.get(rule_key)
            implementation = (
                None if selected_override is None else selected_override.get("implementation")
            )
        if implementation is not None:
            if not isinstance(implementation, Mapping) or set(implementation) != {
                "pack_id",
                "path",
                "digest",
            }:
                raise RuleRuntimeError(f"{rule_key}: invalid implementation reference")
            if implementation["pack_id"] != manifest["id"]:
                raise RuleRuntimeError(
                    f"{rule_key}: implementation belongs to pack `{implementation['pack_id']}`, "
                    f"not adapter pack `{manifest['id']}`"
                )
            implementation_path = _relative_pack_path(
                root, implementation["path"], "implementation path"
            )
            if not implementation_path.is_file():
                raise RuleRuntimeError(f"{implementation_path}: rule implementation does not exist")
            actual_digest = hashlib.sha256(implementation_path.read_bytes()).hexdigest()
            if actual_digest != implementation["digest"]:
                raise RuleRuntimeError(
                    f"{implementation_path}: implementation digest does not match "
                    f"{implementation['digest']}"
                )
            reference = {
                "pack_id": str(implementation["pack_id"]),
                "path": str(implementation["path"]),
                "digest": str(implementation["digest"]),
            }
            implementation_refs.append(reference)
        else:
            implementation = None
        rules.append(
            {
                "rule_key": rule_key,
                "directive": directive,
                "rule_revision_id": override["rule_revision_id"],
                "selected_rule_digest": override["selected_rule_digest"],
                "implementation": implementation,
                "resolution": override["resolution"],
                "configuration": configuration,
            }
        )

    settings = profile["setting_overrides"]
    for setting in settings:
        key = str(setting["key"])
        if key not in supported_settings:
            raise RuleRuntimeError(
                f"{key}: adapter `{contract['id']}` rejects unsupported profile setting"
            )
    normalized_refs = sorted(
        {json.dumps(reference, sort_keys=True) for reference in implementation_refs}
    )
    implementation_refs = [json.loads(reference) for reference in normalized_refs]
    rule_digest = _digest(
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
    implementation_digest = (
        implementation_refs[0]["digest"]
        if len(implementation_refs) == 1
        else _digest(implementation_refs)
    )
    settings_digest = _digest(settings)
    request = {
        "schema_version": 1,
        "profile": dict(profile),
        "rules": rules,
        "settings": settings,
        "input": input_payload,
    }
    _canonical(request)
    return request, rule_digest, implementation_digest, settings_digest


def _insert_run(
    database: Path,
    *,
    run_id: str,
    profile_revision_id: str,
    status: str,
    adapter_id: str,
    adapter_digest: str,
    resolved_profile_digest: str,
    rule_digest: str,
    implementation_digest: str,
    settings_digest: str,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    command: list[str],
    requested_by: str,
    completed_at: str | None = None,
) -> None:
    with write_transaction(database) as connection:
        connection.execute(
            """
            INSERT INTO sourcebridge_rule_runtime_run
                (id, profile_revision_id, status, adapter_id, adapter_digest,
                 resolved_profile_digest, rule_digest, implementation_digest,
                 settings_digest, request_json, result_json, command_json,
                 requested_by, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                profile_revision_id,
                status,
                adapter_id,
                adapter_digest,
                resolved_profile_digest,
                rule_digest,
                implementation_digest,
                settings_digest,
                _canonical(request),
                _canonical(result),
                _canonical(command),
                requested_by,
                _now(),
                completed_at,
            ),
        )


def _finish_run(
    database: Path,
    run_id: str,
    *,
    status: str,
    result: Mapping[str, Any],
) -> None:
    with write_transaction(database) as connection:
        connection.execute(
            """
            UPDATE sourcebridge_rule_runtime_run
            SET status = ?, result_json = ?, completed_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, _canonical(result), _now(), run_id),
        )


def execute(
    database: Path,
    profile_revision_id: str,
    pack_manifest: Path,
    *,
    input_payload: Any,
    requested_by: str = "sourcebridge",
) -> dict[str, Any]:
    """Resolve a project profile, invoke its pack adapter, and record the run."""

    requested_by = _text(requested_by, "requested_by")
    initialize(database)
    manifest, root, contract, adapter_digest = _load_contract(pack_manifest.resolve())
    try:
        profile = resolve_project_profile(database, profile_revision_id)
    except ValueError as error:
        raise RuleRuntimeError(str(error)) from error
    try:
        request, rule_digest, implementation_digest, settings_digest = _resolve_request(
            profile, manifest, contract, root, input_payload
        )
    except RuleRuntimeError as error:
        rejected_request = {
            "schema_version": 1,
            "profile": dict(profile),
            "rules": profile["rule_overrides"],
            "settings": profile["setting_overrides"],
            "input": input_payload,
        }
        rejected_rule_digest = _digest(profile["rule_overrides"])
        rejected_settings_digest = _digest(profile["setting_overrides"])
        rejected_implementation_digest = _digest(
            [
                rule["implementation"]
                for rule in profile["rule_overrides"]
                if rule.get("implementation") is not None
            ]
        )
        _insert_run(
            database,
            run_id=f"rule-runtime-run-{uuid4()}",
            profile_revision_id=profile_revision_id,
            status="rejected",
            adapter_id=str(contract["id"]),
            adapter_digest=adapter_digest,
            resolved_profile_digest=str(profile["resolved_digest"]),
            rule_digest=rejected_rule_digest,
            implementation_digest=rejected_implementation_digest,
            settings_digest=rejected_settings_digest,
            request=rejected_request,
            result={"status": "rejected", "error": str(error)},
            command=[],
            requested_by=requested_by,
            completed_at=_now(),
        )
        raise
    command_values = {
        "adapter_source": str(root / str(contract["source"])),
        "pack_root": str(root),
    }
    try:
        command = [value.format(**command_values) for value in contract["command"]]
    except KeyError as error:
        raise RuleRuntimeError(
            f"rule_runtime command uses unsupported placeholder `{error.args[0]}`"
        ) from error
    run_id = f"rule-runtime-run-{uuid4()}"
    _insert_run(
        database,
        run_id=run_id,
        profile_revision_id=profile_revision_id,
        status="running",
        adapter_id=str(contract["id"]),
        adapter_digest=adapter_digest,
        resolved_profile_digest=str(profile["resolved_digest"]),
        rule_digest=rule_digest,
        implementation_digest=implementation_digest,
        settings_digest=settings_digest,
        request=request,
        result={"status": "running"},
        command=command,
        requested_by=requested_by,
    )
    timeout = contract.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or timeout <= 0:
        result = {"status": "rejected", "error": "adapter timeout_seconds must be positive"}
        _finish_run(database, run_id, status="rejected", result=result)
        raise RuleRuntimeError(result["error"])
    try:
        process = subprocess.run(
            command,
            cwd=root,
            input=_canonical(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {"status": "failed", "error": str(error)}
        _finish_run(database, run_id, status="failed", result=result)
        raise RuleRuntimeError(f"adapter `{contract['id']}` failed: {error}") from error
    if process.returncode != 0:
        result = {
            "status": "failed",
            "exit_code": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        _finish_run(database, run_id, status="failed", result=result)
        raise RuleRuntimeError(
            f"adapter `{contract['id']}` exited with {process.returncode}: {process.stderr.strip()}"
        )
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        failed = {"status": "failed", "error": f"adapter emitted invalid JSON: {error}"}
        _finish_run(database, run_id, status="failed", result=failed)
        raise RuleRuntimeError(failed["error"]) from error
    if not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "blocked"}:
        failed = {
            "status": "failed",
            "error": "adapter result must be an object with status passed, failed, or blocked",
        }
        _finish_run(database, run_id, status="failed", result=failed)
        raise RuleRuntimeError(failed["error"])
    _finish_run(database, run_id, status=str(result["status"]), result=result)
    return {
        "run_id": run_id,
        "status": result["status"],
        "result": result,
        "request": request,
        "provenance": {
            "adapter_id": contract["id"],
            "adapter_digest": adapter_digest,
            "resolved_profile_id": profile_revision_id,
            "resolved_profile_digest": profile["resolved_digest"],
            "rule_digest": rule_digest,
            "implementation_digest": implementation_digest,
            "settings_digest": settings_digest,
        },
    }


run = execute
