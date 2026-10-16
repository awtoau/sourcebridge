"""SQLite evidence initialization and legacy JSON migration."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Iterable


SCHEMA_VERSION = 2
_ROOT = Path(__file__).parents[1]
_SCHEMA = _ROOT / "core" / "schema" / "evidence.sql"


class EvidencePipelineError(ValueError):
    """Raised when an evidence database cannot be initialized or populated."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _id(prefix: str, value: str) -> str:
    return f"{prefix}-{_digest(value)[:24]}"


def initialize(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        schema = _SCHEMA.read_text(encoding="utf-8")
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            metadata_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'sourcebridge_schema_metadata'
                """
            ).fetchone()
            if metadata_exists is None:
                connection.executescript(schema)
            existing = connection.execute(
                "SELECT value FROM sourcebridge_schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO sourcebridge_schema_metadata (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            elif existing[0] == "1":
                connection.execute(
                    "ALTER TABLE sourcebridge_imported_record "
                    "ADD COLUMN source_repository TEXT NOT NULL DEFAULT 'sourcebridge'"
                )
                connection.execute(
                    "UPDATE sourcebridge_schema_metadata SET value = ? "
                    "WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif existing[0] != str(SCHEMA_VERSION):
                raise EvidencePipelineError(
                    f"{database}: unsupported schema version `{existing[0]}`"
                )
    except (OSError, sqlite3.Error) as error:
        raise EvidencePipelineError(f"{database}: cannot initialize evidence database: {error}") from error


def import_json(
    database: Path, root: Path, *, repository: str | None = None, tracked_only: bool = False
) -> dict[str, int]:
    if not root.is_dir():
        raise EvidencePipelineError(f"{root}: JSON root must be an existing directory")
    initialize(database)
    repository = repository or _repository_name(root)
    imported = projected = skipped = 0
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for path in _json_files(root, tracked_only=tracked_only):
            payload = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            try:
                record = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise EvidencePipelineError(f"{path}: invalid JSON: {error}") from error
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
            digest = _digest(payload)
            source_key = f"{repository}:{relative}"
            record_id = _id("json", source_key)
            kind = _kind(relative, record)
            result = connection.execute(
                """
                INSERT INTO sourcebridge_imported_record
                    (id, source_repository, source_path, source_sha256, kind, payload_json, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_repository = excluded.source_repository,
                    source_sha256 = excluded.source_sha256,
                    kind = excluded.kind,
                    payload_json = excluded.payload_json,
                    imported_at = excluded.imported_at
                WHERE sourcebridge_imported_record.source_sha256 != excluded.source_sha256
                """,
                (record_id, repository, source_key, digest, kind, canonical, _now()),
            )
            if result.rowcount:
                imported += 1
            else:
                skipped += 1
            if kind == "rule_proposal":
                _import_proposal(connection, record_id, record)
                projected += 1
            elif kind == "fact_grounded_evidence":
                _import_fact_evidence(connection, record_id, relative, digest, record)
                projected += 1
            elif kind == "profile":
                _import_profile(connection, record_id, record)
                projected += 1
    return {
        "repository": repository,
        "imported": imported,
        "projected": projected,
        "skipped": skipped,
    }


def _json_files(root: Path, *, tracked_only: bool) -> Iterable[Path]:
    if tracked_only:
        yield from _tracked_json_files(root)
        return
    for path in sorted(root.rglob("*.json"), key=lambda candidate: candidate.as_posix()):
        if path.is_file() and ".git" not in path.parts:
            yield path


def _tracked_json_files(root: Path) -> Iterable[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.json"],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise EvidencePipelineError(f"{root}: cannot enumerate tracked JSON: {error}") from error
    if result.returncode:
        raise EvidencePipelineError(f"{root}: tracked-only import requires a Git repository")
    for relative in sorted(
        (Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item),
        key=lambda path: path.as_posix(),
    ):
        yield root / relative


def _repository_name(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return root.name
    if result.returncode or not result.stdout.strip():
        return root.name
    return result.stdout.strip().removesuffix(".git")


def _kind(relative: str, record: Any) -> str:
    if relative.startswith("core/schema/"):
        return "json_schema"
    if relative.startswith("examples/") and isinstance(record, dict) and "source_frontend" in record:
        return "profile"
    if isinstance(record, dict) and {"proposal", "raw_response", "payload_sha256"} <= set(record):
        return "rule_proposal"
    if isinstance(record, dict) and {"source_facts", "observed_behavior"} <= set(record):
        return "fact_grounded_evidence"
    if relative.startswith("examples/"):
        return "pack_manifest"
    return "legacy_json"


def _import_profile(connection: sqlite3.Connection, record_id: str, record: dict[str, Any]) -> None:
    profile_id = _id("profile", record_id)
    options = json.dumps(record.get("options", {}), sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO sourcebridge_profile
            (id, profile_name, source_frontend_pack_id, corpus_pack_id,
             target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
             options_digest, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            profile_id,
            record["id"],
            record["source_frontend"],
            record.get("corpus_pack"),
            record["target_backend"],
            _digest(json.dumps(record.get("rule_layers", []), sort_keys=True)),
            _digest(json.dumps(record.get("runtime_adapters", []), sort_keys=True)),
            _digest(options),
            _now(),
        ),
    )


def _import_proposal(connection: sqlite3.Connection, record_id: str, record: dict[str, Any]) -> None:
    model = record.get("model", {})
    status = record.get("status")
    if status not in {"unvalidated-llm-proposal", "rejected-llm-proposal"}:
        status = "rejected-llm-proposal"
    connection.execute(
        """
        INSERT INTO sourcebridge_rule_proposal
            (id, imported_record_id, status, model_name, model_digest,
             prompt_template_version, payload_sha256, proposal_json,
             raw_response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(imported_record_id) DO UPDATE SET
            status = excluded.status, model_name = excluded.model_name,
            model_digest = excluded.model_digest,
            prompt_template_version = excluded.prompt_template_version,
            payload_sha256 = excluded.payload_sha256,
            proposal_json = excluded.proposal_json,
            raw_response_json = excluded.raw_response_json
        """,
        (
            _id("proposal", record_id),
            record_id,
            status,
            model.get("name") if isinstance(model, dict) else None,
            model.get("digest") if isinstance(model, dict) else None,
            record.get("prompt_template_version"),
            record["payload_sha256"],
            json.dumps(record.get("proposal", {}), sort_keys=True, separators=(",", ":")),
            json.dumps(record.get("raw_response", {}), sort_keys=True, separators=(",", ":")),
            _now(),
        ),
    )


def _import_fact_evidence(
    connection: sqlite3.Connection, record_id: str, relative: str, digest: str, record: dict[str, Any]
) -> None:
    cluster = str(record.get("cluster_id", record_id))
    corpus_id = _id("corpus", record_id)
    variant_id = _id("variant", record_id)
    profile_id = _id("profile", record_id)
    connection.execute(
        """
        INSERT INTO sourcebridge_corpus
            (id, source_name, revision, source_remote, manifest_digest, created_at)
        VALUES (?, ?, ?, NULL, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (corpus_id, cluster, digest, digest, _now()),
    )
    compiler = record.get("variant", {}).get("compiler", {})
    connection.execute(
        """
        INSERT INTO sourcebridge_variant
            (id, corpus_id, frontend_pack_id, compiler_identity, target_abi,
             pointer_width, byte_order, configuration_digest, generated_input_digest, created_at)
        VALUES (?, ?, 'sourcebridge-delphi', ?, ?, NULL, NULL, ?, NULL, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            variant_id,
            corpus_id,
            json.dumps(compiler, sort_keys=True, separators=(",", ":")),
            f"{compiler.get('target_cpu', 'unknown')}-{compiler.get('target_os', 'unknown')}",
            _digest(json.dumps(record.get("variant", {}), sort_keys=True)),
            _now(),
        ),
    )
    target = record.get("target_context", {})
    connection.execute(
        """
        INSERT INTO sourcebridge_profile
            (id, profile_name, source_frontend_pack_id, corpus_pack_id,
             target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
             options_digest, created_at)
        VALUES (?, ?, 'sourcebridge-delphi', NULL, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            profile_id,
            f"imported-{cluster}",
            f"sourcebridge-{target.get('backend', 'unknown')}",
            _digest(str(record.get("active_rule_digest", ""))),
            _digest("[]"),
            _digest(json.dumps(target, sort_keys=True)),
            _now(),
        ),
    )
    source_facts = record["source_facts"]
    source_path = source_facts.get("source_path", relative)
    for function in source_facts.get("functions", []):
        name = function.get("name", "unknown")
        unit_id = _id("unit", f"{record_id}:{name}")
        decision_id = _id("decision", f"{profile_id}:{unit_id}")
        connection.execute(
            """
            INSERT INTO sourcebridge_source_unit
                (id, variant_id, logical_name, source_path, source_span, source_digest, kind)
            VALUES (?, ?, ?, ?, NULL, ?, 'function')
            ON CONFLICT(id) DO NOTHING
            """,
            (unit_id, variant_id, name, source_path, digest),
        )
        connection.execute(
            """
            INSERT INTO sourcebridge_component_decision
                (id, profile_id, source_unit_id, disposition, target_backend_pack_id,
                 runtime_adapter_id, boundary_contract_id, rationale, decided_by, created_at)
            VALUES (?, ?, ?, 'direct_translation', ?, NULL, NULL, ?, 'legacy-json-import', ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                decision_id,
                profile_id,
                unit_id,
                f"sourcebridge-{target.get('backend', 'unknown')}",
                "Imported from fact-grounded legacy evidence; no rule promotion is implied.",
                _now(),
            ),
        )
        connection.execute(
            """
            INSERT INTO sourcebridge_validation_result
                (id, decision_id, side, validator, status, evidence_path,
                 evidence_digest, details_json, recorded_at)
            VALUES (?, ?, 'source', 'legacy-json-import', 'passed', ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                _id("validation", f"{decision_id}:source"),
                decision_id,
                relative,
                digest,
                json.dumps({"operation": function.get("operation")}, sort_keys=True),
                _now(),
            ),
        )
