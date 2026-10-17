"""SQLite evidence initialization and legacy JSON migration."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Iterable

from sourcebridge.database import (
    write_transaction,
)


SCHEMA_VERSION = 8
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


def _run_atomic_script(connection: sqlite3.Connection, script: str) -> None:
    """Run DDL and its metadata update as one explicit SQLite transaction.

    ``sqlite3.Connection.executescript`` commits an existing transaction
    before running its input.  Splitting the script into complete statements
    keeps the transaction and the writer lease acquired by ``write_transaction``
    intact, so schema inspection cannot become stale before the DDL runs.
    """

    if not connection.in_transaction:
        raise sqlite3.ProgrammingError(
            "SQLite migration scripts require an active writer transaction"
        )
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            pending.clear()
    if "".join(pending).strip():
        raise sqlite3.ProgrammingError("incomplete SQLite migration script")


def _run_atomic_migration(
    connection: sqlite3.Connection, ddl: str, target_version: str
) -> None:
    _run_atomic_script(
        connection,
        f"""
        {ddl}
        UPDATE sourcebridge_schema_metadata
        SET value = '{target_version}'
        WHERE key = 'schema_version';
        """,
    )


def _create_worker_audit_objects(connection: sqlite3.Connection) -> None:
    """Create proposal-worker audit objects without leaving the writer transaction."""

    for statement in (
        """
        CREATE TABLE IF NOT EXISTS sourcebridge_proposal_worker_session (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_digest TEXT NOT NULL,
            generation_options_json TEXT NOT NULL,
            prompt_template_version TEXT NOT NULL,
            prompt_template_digest TEXT NOT NULL,
            active_rule_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            summary_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sourcebridge_proposal_worker_request (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
                REFERENCES sourcebridge_proposal_worker_session(id),
            request_index INTEGER NOT NULL CHECK (request_index >= 0),
            evidence_sha256 TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('staged', 'rejected', 'failed')),
            elapsed_ms REAL NOT NULL CHECK (elapsed_ms >= 0),
            load_duration_ms REAL NOT NULL CHECK (load_duration_ms >= 0),
            generation_ms REAL NOT NULL CHECK (generation_ms >= 0),
            generated_tokens INTEGER,
            rejection_reasons_json TEXT NOT NULL,
            proposal_json TEXT NOT NULL,
            raw_response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS sourcebridge_proposal_worker_request_session_idx
            ON sourcebridge_proposal_worker_request (session_id, request_index)
        """,
        """
        CREATE TABLE IF NOT EXISTS sourcebridge_proposal_worker_session_event (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
                REFERENCES sourcebridge_proposal_worker_session(id),
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            summary_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE (session_id, sequence)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS sourcebridge_proposal_worker_session_event_idx
            ON sourcebridge_proposal_worker_session_event (session_id, sequence)
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_session_immutable_update
        BEFORE UPDATE ON sourcebridge_proposal_worker_session
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_session_immutable_delete
        BEFORE DELETE ON sourcebridge_proposal_worker_session
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_request_immutable_update
        BEFORE UPDATE ON sourcebridge_proposal_worker_request
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_request is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_request_immutable_delete
        BEFORE DELETE ON sourcebridge_proposal_worker_request
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_request is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_session_event_immutable_update
        BEFORE UPDATE ON sourcebridge_proposal_worker_session_event
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session_event is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_proposal_worker_session_event_immutable_delete
        BEFORE DELETE ON sourcebridge_proposal_worker_session_event
        BEGIN
            SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session_event is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sourcebridge_model_authored_rule_requires_human_review
        BEFORE INSERT ON sourcebridge_rule_revision
        WHEN NEW.model_name IS NOT NULL
         AND (
            NEW.model_digest IS NULL
            OR NEW.proposal_id IS NULL
            OR NEW.human_reviewed_by IS NULL
            OR NEW.human_reviewed_at IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM sourcebridge_rule_proposal AS proposal
                WHERE proposal.id = NEW.proposal_id
                  AND proposal.model_name = NEW.model_name
                  AND proposal.model_digest = NEW.model_digest
            )
         )
        BEGIN
            SELECT RAISE(ABORT,
                'model-authored rule requires persisted matching proposal and human review');
        END
        """,
    ):
        connection.execute(statement)


def initialize(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        schema = _SCHEMA.read_text(encoding="utf-8")
        with write_transaction(database, enable_wal=True) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            metadata_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'sourcebridge_schema_metadata'
                """
            ).fetchone()
            if metadata_exists is None:
                _run_atomic_script(
                    connection,
                    f"""
                    {schema}
                    INSERT INTO sourcebridge_schema_metadata (key, value)
                    VALUES ('schema_version', '{SCHEMA_VERSION}');
                    """,
                )
            existing = connection.execute(
                "SELECT value FROM sourcebridge_schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO sourcebridge_schema_metadata (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            version = existing[0] if existing is not None else str(SCHEMA_VERSION)
            if version == "1":
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_imported_record)"
                    )
                }
                ddl = ""
                if "source_repository" not in columns:
                    ddl = (
                        "ALTER TABLE sourcebridge_imported_record "
                        "ADD COLUMN source_repository TEXT NOT NULL DEFAULT 'sourcebridge';"
                    )
                _run_atomic_migration(connection, ddl, "2")
                version = "2"
            if version == "2":
                _run_atomic_migration(
                    connection,
                    """
                    CREATE TABLE IF NOT EXISTS sourcebridge_engine_test (
                        id INTEGER PRIMARY KEY,
                        pack_id TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        name TEXT NOT NULL,
                        fixture_path TEXT NOT NULL,
                        fixture_digest TEXT NOT NULL,
                        expected_json TEXT NOT NULL,
                        adapter_config_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (pack_id, name)
                    );
                    CREATE TABLE IF NOT EXISTS sourcebridge_engine_test_run (
                        id TEXT PRIMARY KEY,
                        test_id INTEGER NOT NULL REFERENCES sourcebridge_engine_test(id),
                        status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked')),
                        command_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS sourcebridge_engine_test_run_test_idx
                        ON sourcebridge_engine_test_run (test_id, completed_at);
                    """,
                    "3",
                )
                version = "3"
            if version == "3":
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_engine_test)"
                    )
                }
                ddl = "\n".join(
                    statement
                    for column, statement in (
                        (
                            "catalogue_path",
                            "ALTER TABLE sourcebridge_engine_test "
                            "ADD COLUMN catalogue_path TEXT;",
                        ),
                        (
                            "catalogue_digest",
                            "ALTER TABLE sourcebridge_engine_test "
                            "ADD COLUMN catalogue_digest TEXT;",
                        ),
                    )
                    if column not in columns
                )
                _run_atomic_migration(connection, ddl, "4")
                version = "4"
            if version == "4":
                _run_atomic_migration(
                    connection,
                    """
                    CREATE TABLE IF NOT EXISTS sourcebridge_rule_family (
                        id TEXT PRIMARY KEY,
                        rule_key TEXT NOT NULL,
                        source_frontend_pack_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (source_frontend_pack_id, rule_key)
                    );
                    CREATE TABLE IF NOT EXISTS sourcebridge_rule_revision (
                        id TEXT PRIMARY KEY,
                        family_id TEXT NOT NULL REFERENCES sourcebridge_rule_family(id),
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        definition_json TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        workspace_name TEXT,
                        supersedes_revision_id TEXT REFERENCES sourcebridge_rule_revision(id),
                        rationale TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (family_id, revision),
                        UNIQUE (family_id, content_digest)
                    );
                    CREATE TABLE IF NOT EXISTS sourcebridge_rule_revision_state (
                        id TEXT PRIMARY KEY,
                        rule_revision_id TEXT NOT NULL REFERENCES sourcebridge_rule_revision(id),
                        sequence INTEGER NOT NULL CHECK (sequence > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('draft', 'active', 'deprecated', 'retired', 'rejected')
                        ),
                        rationale TEXT NOT NULL,
                        decided_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (rule_revision_id, sequence)
                    );
                    CREATE INDEX IF NOT EXISTS sourcebridge_rule_revision_state_latest_idx
                        ON sourcebridge_rule_revision_state (rule_revision_id, sequence DESC);
                    CREATE TABLE IF NOT EXISTS sourcebridge_project_profile_revision (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        profile_name TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        base_profile_id TEXT NOT NULL REFERENCES sourcebridge_profile(id),
                        base_profile_digest TEXT NOT NULL,
                        required_validation_tier TEXT NOT NULL,
                        workspace_name TEXT,
                        supersedes_profile_revision_id TEXT
                            REFERENCES sourcebridge_project_profile_revision(id),
                        resolved_digest TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (project_id, profile_name, revision)
                    );
                    CREATE TABLE IF NOT EXISTS sourcebridge_project_profile_state (
                        id TEXT PRIMARY KEY,
                        profile_revision_id TEXT NOT NULL
                            REFERENCES sourcebridge_project_profile_revision(id),
                        sequence INTEGER NOT NULL CHECK (sequence > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('draft', 'candidate', 'current', 'retired')
                        ),
                        rationale TEXT NOT NULL,
                        decided_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (profile_revision_id, sequence)
                    );
                    CREATE INDEX IF NOT EXISTS sourcebridge_project_profile_state_latest_idx
                        ON sourcebridge_project_profile_state (profile_revision_id, sequence DESC);
                    CREATE TABLE IF NOT EXISTS sourcebridge_project_profile_rule_override (
                        id TEXT PRIMARY KEY,
                        profile_revision_id TEXT NOT NULL
                            REFERENCES sourcebridge_project_profile_revision(id),
                        rule_family_id TEXT NOT NULL REFERENCES sourcebridge_rule_family(id),
                        directive TEXT NOT NULL CHECK (
                            directive IN ('enable', 'disable', 'replace', 'configure')
                        ),
                        selected_rule_revision_id TEXT
                            REFERENCES sourcebridge_rule_revision(id),
                        resolution_json TEXT NOT NULL,
                        configuration_json TEXT NOT NULL,
                        configuration_digest TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (profile_revision_id, rule_family_id, directive)
                    );
                    CREATE INDEX IF NOT EXISTS sourcebridge_project_profile_rule_override_profile_idx
                        ON sourcebridge_project_profile_rule_override
                            (profile_revision_id, rule_family_id);
                    CREATE TABLE IF NOT EXISTS sourcebridge_project_profile_setting_override (
                        id TEXT PRIMARY KEY,
                        profile_revision_id TEXT NOT NULL
                            REFERENCES sourcebridge_project_profile_revision(id),
                        setting_key TEXT NOT NULL,
                        schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                        value_json TEXT NOT NULL,
                        value_digest TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (profile_revision_id, setting_key)
                    );
                    CREATE TABLE IF NOT EXISTS sourcebridge_project_profile_certification (
                        id TEXT PRIMARY KEY,
                        profile_revision_id TEXT NOT NULL
                            REFERENCES sourcebridge_project_profile_revision(id),
                        validation_tier TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked')),
                        evidence_reference TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL,
                        details_json TEXT NOT NULL,
                        certified_by TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS sourcebridge_project_profile_certification_profile_idx
                        ON sourcebridge_project_profile_certification
                            (profile_revision_id, created_at);
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_family_immutable_update
                    BEFORE UPDATE ON sourcebridge_rule_family
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_family is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_family_immutable_delete
                    BEFORE DELETE ON sourcebridge_rule_family
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_family is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_revision_immutable_update
                    BEFORE UPDATE ON sourcebridge_rule_revision
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_revision is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_revision_immutable_delete
                    BEFORE DELETE ON sourcebridge_rule_revision
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_revision is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_revision_state_immutable_update
                    BEFORE UPDATE ON sourcebridge_rule_revision_state
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_revision_state is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_rule_revision_state_immutable_delete
                    BEFORE DELETE ON sourcebridge_rule_revision_state
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_rule_revision_state is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_revision_immutable_update
                    BEFORE UPDATE ON sourcebridge_project_profile_revision
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_revision is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_revision_immutable_delete
                    BEFORE DELETE ON sourcebridge_project_profile_revision
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_revision is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_state_immutable_update
                    BEFORE UPDATE ON sourcebridge_project_profile_state
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_state is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_state_immutable_delete
                    BEFORE DELETE ON sourcebridge_project_profile_state
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_state is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_rule_override_immutable_update
                    BEFORE UPDATE ON sourcebridge_project_profile_rule_override
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_rule_override is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_rule_override_immutable_delete
                    BEFORE DELETE ON sourcebridge_project_profile_rule_override
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_rule_override is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_setting_override_immutable_update
                    BEFORE UPDATE ON sourcebridge_project_profile_setting_override
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_setting_override is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_setting_override_immutable_delete
                    BEFORE DELETE ON sourcebridge_project_profile_setting_override
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_setting_override is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_certification_immutable_update
                    BEFORE UPDATE ON sourcebridge_project_profile_certification
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_certification is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_certification_immutable_delete
                    BEFORE DELETE ON sourcebridge_project_profile_certification
                    BEGIN
                        SELECT RAISE(ABORT, 'sourcebridge_project_profile_certification is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS sourcebridge_project_profile_state_current_requires_certification
                    BEFORE INSERT ON sourcebridge_project_profile_state
                    WHEN NEW.state = 'current'
                     AND NOT EXISTS (
                        SELECT 1
                        FROM sourcebridge_project_profile_revision AS profile
                        JOIN sourcebridge_project_profile_certification AS certification
                          ON certification.profile_revision_id = profile.id
                        WHERE profile.id = NEW.profile_revision_id
                          AND certification.validation_tier = profile.required_validation_tier
                          AND certification.status = 'passed'
                     )
                    BEGIN
                        SELECT RAISE(ABORT, 'current profile requires passed required-tier certification');
                    END;
                    """,
                    "5",
                )
                version = "5"
            if version == "5":
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_project_profile_rule_override)"
                    )
                }
                ddl = ""
                if "resolution_json" not in columns:
                    ddl = (
                        "ALTER TABLE sourcebridge_project_profile_rule_override "
                        "ADD COLUMN resolution_json TEXT NOT NULL DEFAULT '{}';"
                    )
                _run_atomic_migration(connection, ddl, "6")
                version = "6"
            if version == "6":
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_rule_revision)"
                    )
                }
                for column, definition in (
                    ("proposal_id", "TEXT"),
                    ("model_name", "TEXT"),
                    ("model_digest", "TEXT"),
                    ("human_reviewed_by", "TEXT"),
                    ("human_reviewed_at", "TEXT"),
                ):
                    if column not in columns:
                        connection.execute(
                            f"ALTER TABLE sourcebridge_rule_revision ADD COLUMN {column} {definition}"
                        )
                worker_session_exists = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'sourcebridge_proposal_worker_session'
                    """
                ).fetchone()
                if worker_session_exists is not None:
                    worker_columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(sourcebridge_proposal_worker_session)"
                        )
                    }
                    if "prompt_template_digest" not in worker_columns:
                        connection.execute(
                            """
                            ALTER TABLE sourcebridge_proposal_worker_session
                            ADD COLUMN prompt_template_digest TEXT NOT NULL DEFAULT 'unknown'
                            """
                        )
                connection.execute(
                    "DROP TRIGGER IF EXISTS "
                    "sourcebridge_model_authored_rule_requires_human_review"
                )
                _create_worker_audit_objects(connection)
                _run_atomic_migration(connection, "", "7")
                version = "7"
            if version == "7":
                connection.execute(
                    "DROP TRIGGER IF EXISTS "
                    "sourcebridge_model_authored_rule_requires_human_review"
                )
                worker_session_exists = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'sourcebridge_proposal_worker_session'
                    """
                ).fetchone()
                if worker_session_exists is None:
                    connection.execute(
                        """
                        CREATE TABLE sourcebridge_proposal_worker_session (
                            id TEXT PRIMARY KEY,
                            batch_id TEXT NOT NULL,
                            endpoint TEXT NOT NULL,
                            model_name TEXT NOT NULL,
                            model_digest TEXT NOT NULL,
                            generation_options_json TEXT NOT NULL,
                            prompt_template_version TEXT NOT NULL,
                            prompt_template_digest TEXT NOT NULL,
                            active_rule_digest TEXT NOT NULL,
                            status TEXT NOT NULL
                                CHECK (status IN ('running', 'completed', 'failed')),
                            summary_json TEXT NOT NULL,
                            started_at TEXT NOT NULL,
                            completed_at TEXT
                        )
                        """
                    )
                session_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(sourcebridge_proposal_worker_session)"
                    )
                }
                if "prompt_template_digest" not in session_columns:
                    connection.execute(
                        """
                        ALTER TABLE sourcebridge_proposal_worker_session
                        ADD COLUMN prompt_template_digest TEXT NOT NULL DEFAULT 'unknown'
                        """
                    )

                request_table = connection.execute(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'sourcebridge_proposal_worker_request'
                    """
                ).fetchone()
                request_sql = request_table[0] if request_table is not None else ""
                if request_table is None or "'failed'" not in request_sql or (
                    "UNIQUE (session_id, request_index)" in request_sql
                ):
                    connection.execute(
                        "DROP TRIGGER IF EXISTS "
                        "sourcebridge_proposal_worker_request_immutable_update"
                    )
                    connection.execute(
                        "DROP TRIGGER IF EXISTS "
                        "sourcebridge_proposal_worker_request_immutable_delete"
                    )
                    if request_table is not None:
                        connection.execute(
                            "ALTER TABLE sourcebridge_proposal_worker_request "
                            "RENAME TO sourcebridge_proposal_worker_request_legacy"
                        )
                    connection.execute(
                        """
                        CREATE TABLE sourcebridge_proposal_worker_request (
                            id TEXT PRIMARY KEY,
                            session_id TEXT NOT NULL
                                REFERENCES sourcebridge_proposal_worker_session(id),
                            request_index INTEGER NOT NULL CHECK (request_index >= 0),
                            evidence_sha256 TEXT NOT NULL,
                            payload_sha256 TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN ('staged', 'rejected', 'failed')
                            ),
                            elapsed_ms REAL NOT NULL CHECK (elapsed_ms >= 0),
                            load_duration_ms REAL NOT NULL CHECK (load_duration_ms >= 0),
                            generation_ms REAL NOT NULL CHECK (generation_ms >= 0),
                            generated_tokens INTEGER,
                            rejection_reasons_json TEXT NOT NULL,
                            proposal_json TEXT NOT NULL,
                            raw_response_json TEXT NOT NULL,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    if request_table is not None:
                        connection.execute(
                            """
                            INSERT INTO sourcebridge_proposal_worker_request
                                (id, session_id, request_index, evidence_sha256,
                                 payload_sha256, status, elapsed_ms, load_duration_ms,
                                 generation_ms, generated_tokens,
                                 rejection_reasons_json, proposal_json,
                                 raw_response_json, created_at)
                            SELECT id, session_id, request_index, evidence_sha256,
                                   payload_sha256, status, elapsed_ms, load_duration_ms,
                                   generation_ms, generated_tokens,
                                   rejection_reasons_json, proposal_json,
                                   raw_response_json, created_at
                            FROM sourcebridge_proposal_worker_request_legacy
                            """
                        )
                        connection.execute(
                            "DROP TABLE sourcebridge_proposal_worker_request_legacy"
                        )
                _create_worker_audit_objects(connection)
                _run_atomic_migration(connection, "", "8")
                version = "8"
            if version != str(SCHEMA_VERSION):
                raise EvidencePipelineError(
                    f"{database}: unsupported schema version `{version}`"
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
    with write_transaction(database) as connection:
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
