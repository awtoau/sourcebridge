"""Immutable project conversion profiles and rule-overlay evidence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from sourcebridge.database import read_transaction, write_transaction
from sourcebridge.pipeline import initialize


class ConversionProfileError(ValueError):
    """Raised when an immutable profile or rule operation is invalid."""


_RULE_STATES = {"draft", "active", "deprecated", "retired", "rejected"}
_PROFILE_STATES = {"draft", "candidate", "current", "retired"}
_CERTIFICATION_STATES = {"passed", "failed", "blocked"}
_RULE_DIRECTIVES = {"enable", "disable", "replace", "configure"}
_SELECTION_DIRECTIVES = {"enable", "disable", "replace"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ConversionProfileError(f"value must be JSON serializable: {error}") from error


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _id(prefix: str, value: str) -> str:
    return f"{prefix}-{_digest(value)[:24]}"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversionProfileError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


@contextmanager
def _connection(database: Path, *, write: bool = True) -> Iterator[sqlite3.Connection]:
    initialize(database)
    transaction = write_transaction if write else read_transaction
    with transaction(database) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def _latest_state(
    connection: sqlite3.Connection,
    table: str,
    foreign_key: str,
    record_id: str,
) -> str:
    row = connection.execute(
        f"""
        SELECT state
        FROM {table}
        WHERE {foreign_key} = ?
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        raise ConversionProfileError(f"{record_id}: missing lifecycle state")
    return str(row["state"])


def _record_state(
    connection: sqlite3.Connection,
    *,
    table: str,
    foreign_key: str,
    record_id: str,
    state: str,
    rationale: str,
    actor: str,
) -> None:
    sequence = connection.execute(
        f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {table} WHERE {foreign_key} = ?",
        (record_id,),
    ).fetchone()[0]
    state_id = _id("state", f"{table}:{record_id}:{sequence}:{state}")
    connection.execute(
        f"""
        INSERT INTO {table}
            (id, {foreign_key}, sequence, state, rationale, decided_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (state_id, record_id, sequence, state, rationale, actor, _now()),
    )


def _base_profile(connection: sqlite3.Connection, profile_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, profile_name, source_frontend_pack_id, corpus_pack_id,
               target_backend_pack_id, rule_layers_digest, runtime_adapters_digest,
               options_digest
        FROM sourcebridge_profile
        WHERE id = ?
        """,
        (profile_id,),
    ).fetchone()
    if row is None:
        raise ConversionProfileError(f"{profile_id}: base profile does not exist")
    return row


def _base_profile_digest(profile: sqlite3.Row) -> str:
    return _digest(
        _canonical(
            {
                "id": profile["id"],
                "profile_name": profile["profile_name"],
                "source_frontend_pack_id": profile["source_frontend_pack_id"],
                "corpus_pack_id": profile["corpus_pack_id"],
                "target_backend_pack_id": profile["target_backend_pack_id"],
                "rule_layers_digest": profile["rule_layers_digest"],
                "runtime_adapters_digest": profile["runtime_adapters_digest"],
                "options_digest": profile["options_digest"],
            }
        )
    )


def register_rule_revision(
    database: Path,
    *,
    rule_key: str,
    source_frontend_pack_id: str,
    definition: Any,
    rationale: str,
    created_by: str,
    workspace_name: str | None = None,
    supersedes_revision_id: str | None = None,
    proposal_id: str | None = None,
    model_name: str | None = None,
    model_digest: str | None = None,
    human_reviewed_by: str | None = None,
    human_reviewed_at: str | None = None,
) -> dict[str, object]:
    """Create an immutable draft revision for a generic or project-specific rule."""

    rule_key = _text(rule_key, "rule_key")
    source_frontend_pack_id = _text(source_frontend_pack_id, "source_frontend_pack_id")
    rationale = _text(rationale, "rationale")
    created_by = _text(created_by, "created_by")
    workspace_name = _optional_text(workspace_name, "workspace_name")
    proposal_id = _optional_text(proposal_id, "proposal_id")
    model_name = _optional_text(model_name, "model_name")
    model_digest = _optional_text(model_digest, "model_digest")
    human_reviewed_by = _optional_text(human_reviewed_by, "human_reviewed_by")
    human_reviewed_at = _optional_text(human_reviewed_at, "human_reviewed_at")
    if model_name is not None and (
        model_digest is None
        or proposal_id is None
        or human_reviewed_by is None
        or human_reviewed_at is None
    ):
        raise ConversionProfileError(
            "model-authored rule requires proposal provenance and human review"
        )
    definition_json = _canonical(definition)
    content_digest = _digest(definition_json)

    with _connection(database) as connection:
        family = connection.execute(
            """
            SELECT id
            FROM sourcebridge_rule_family
            WHERE source_frontend_pack_id = ? AND rule_key = ?
            """,
            (source_frontend_pack_id, rule_key),
        ).fetchone()
        if family is None:
            family_id = _id("rule-family", f"{source_frontend_pack_id}:{rule_key}")
            connection.execute(
                """
                INSERT INTO sourcebridge_rule_family
                    (id, rule_key, source_frontend_pack_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (family_id, rule_key, source_frontend_pack_id, _now()),
            )
        else:
            family_id = str(family["id"])

        if supersedes_revision_id is not None:
            superseded = connection.execute(
                """
                SELECT family_id
                FROM sourcebridge_rule_revision
                WHERE id = ?
                """,
                (supersedes_revision_id,),
            ).fetchone()
            if superseded is None:
                raise ConversionProfileError(
                    f"{supersedes_revision_id}: superseded rule revision does not exist"
                )
            if superseded["family_id"] != family_id:
                raise ConversionProfileError(
                    "a rule revision may only supersede a revision in the same rule family"
                )

        existing = connection.execute(
            """
            SELECT id
            FROM sourcebridge_rule_revision
            WHERE family_id = ? AND content_digest = ?
            """,
            (family_id, content_digest),
        ).fetchone()
        if existing is not None:
            raise ConversionProfileError(
                f"{rule_key}: identical rule content already exists as {existing['id']}"
            )

        revision = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM sourcebridge_rule_revision "
            "WHERE family_id = ?",
            (family_id,),
        ).fetchone()[0]
        revision_id = _id("rule-revision", f"{family_id}:{revision}:{content_digest}")
        connection.execute(
            """
            INSERT INTO sourcebridge_rule_revision
                (id, family_id, revision, definition_json, content_digest,
                 workspace_name, supersedes_revision_id, rationale, created_by,
                 proposal_id, model_name, model_digest, human_reviewed_by,
                 human_reviewed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                family_id,
                revision,
                definition_json,
                content_digest,
                workspace_name,
                supersedes_revision_id,
                rationale,
                created_by,
                proposal_id,
                model_name,
                model_digest,
                human_reviewed_by,
                human_reviewed_at,
                _now(),
            ),
        )
        _record_state(
            connection,
            table="sourcebridge_rule_revision_state",
            foreign_key="rule_revision_id",
            record_id=revision_id,
            state="draft",
            rationale="Created as an uncommitted rule revision.",
            actor=created_by,
        )

    return {
        "rule_revision_id": revision_id,
        "rule_key": rule_key,
        "revision": revision,
        "content_digest": content_digest,
        "state": "draft",
        "proposal_id": proposal_id,
        "model_name": model_name,
        "model_digest": model_digest,
    }


def set_rule_revision_state(
    database: Path,
    rule_revision_id: str,
    *,
    state: str,
    rationale: str,
    decided_by: str,
) -> dict[str, str]:
    """Append a lifecycle event; activating a replacement deprecates its predecessor."""

    state = _text(state, "state")
    rationale = _text(rationale, "rationale")
    decided_by = _text(decided_by, "decided_by")
    if state not in _RULE_STATES:
        raise ConversionProfileError(f"unsupported rule state `{state}`")

    with _connection(database) as connection:
        revision = connection.execute(
            """
            SELECT supersedes_revision_id
            FROM sourcebridge_rule_revision
            WHERE id = ?
            """,
            (rule_revision_id,),
        ).fetchone()
        if revision is None:
            raise ConversionProfileError(f"{rule_revision_id}: rule revision does not exist")
        previous = _latest_state(
            connection,
            "sourcebridge_rule_revision_state",
            "rule_revision_id",
            rule_revision_id,
        )
        allowed = {
            "draft": {"active", "rejected", "retired"},
            "active": {"deprecated", "retired"},
            "deprecated": {"retired"},
            "retired": set(),
            "rejected": set(),
        }
        if state not in allowed[previous]:
            raise ConversionProfileError(
                f"{rule_revision_id}: cannot transition rule state {previous} -> {state}"
            )
        _record_state(
            connection,
            table="sourcebridge_rule_revision_state",
            foreign_key="rule_revision_id",
            record_id=rule_revision_id,
            state=state,
            rationale=rationale,
            actor=decided_by,
        )
        supersedes = revision["supersedes_revision_id"]
        if state == "active" and supersedes is not None:
            superseded_state = _latest_state(
                connection,
                "sourcebridge_rule_revision_state",
                "rule_revision_id",
                str(supersedes),
            )
            if superseded_state == "active":
                _record_state(
                    connection,
                    table="sourcebridge_rule_revision_state",
                    foreign_key="rule_revision_id",
                    record_id=str(supersedes),
                    state="deprecated",
                    rationale=f"Superseded by {rule_revision_id}.",
                    actor=decided_by,
                )

    return {"rule_revision_id": rule_revision_id, "state": state}


def _rule_override(
    connection: sqlite3.Connection,
    override: Mapping[str, Any],
    *,
    source_frontend_pack_id: str,
) -> dict[str, object]:
    rule_key = _text(override.get("rule_key"), "rule override rule_key")
    directive = _text(override.get("directive"), f"{rule_key} directive")
    rationale = _text(override.get("rationale"), f"{rule_key} rationale")
    if directive not in _RULE_DIRECTIVES:
        raise ConversionProfileError(f"{rule_key}: unsupported override directive `{directive}`")
    family = connection.execute(
        """
        SELECT id
        FROM sourcebridge_rule_family
        WHERE source_frontend_pack_id = ? AND rule_key = ?
        """,
        (source_frontend_pack_id, rule_key),
    ).fetchone()
    if family is None:
        raise ConversionProfileError(
            f"{rule_key}: no rule family exists for `{source_frontend_pack_id}`"
        )
    revision_id = override.get("rule_revision_id")
    if directive in {"enable", "replace"}:
        revision_id = _text(revision_id, f"{rule_key} rule_revision_id")
        revision = connection.execute(
            """
            SELECT id, family_id, content_digest
            FROM sourcebridge_rule_revision
            WHERE id = ?
            """,
            (revision_id,),
        ).fetchone()
        if revision is None:
            raise ConversionProfileError(f"{revision_id}: selected rule revision does not exist")
        if revision["family_id"] != family["id"]:
            raise ConversionProfileError(
                f"{rule_key}: selected revision belongs to a different rule family"
            )
        selected_digest = str(revision["content_digest"])
    else:
        if revision_id is not None:
            raise ConversionProfileError(
                f"{rule_key}: `{directive}` must not select a rule revision"
            )
        revision_id = None
        selected_digest = None

    configuration = override.get("configuration", {})
    resolution = override.get("resolution", {})
    if directive == "disable":
        if "resolution" not in override or not isinstance(resolution, Mapping) or not resolution:
            raise ConversionProfileError(f"{rule_key}: disabled rules require a resolution object")
        resolution_kind = _text(resolution.get("kind"), f"{rule_key} resolution kind")
        if resolution_kind not in {
            "replacement",
            "source_unit_disposition",
            "runtime_adapter",
            "not_applicable",
            "blocked",
        }:
            raise ConversionProfileError(
                f"{rule_key}: unsupported disable resolution kind `{resolution_kind}`"
            )
        _text(resolution.get("scope"), f"{rule_key} resolution scope")
        _text(resolution.get("reference"), f"{rule_key} resolution reference")
    elif "resolution" in override:
        raise ConversionProfileError(f"{rule_key}: only `disable` may set a resolution")
    resolution_json = _canonical(dict(resolution))
    if directive == "configure":
        if not isinstance(configuration, Mapping):
            raise ConversionProfileError(f"{rule_key}: configuration must be a JSON object")
        if not configuration:
            raise ConversionProfileError(f"{rule_key}: configuration must not be empty")
    elif "configuration" in override and configuration:
        raise ConversionProfileError(f"{rule_key}: only `configure` may set configuration")
    configuration_json = _canonical(dict(configuration))
    return {
        "rule_key": rule_key,
        "family_id": str(family["id"]),
        "directive": directive,
        "rule_revision_id": revision_id,
        "selected_rule_digest": selected_digest,
        "resolution": json.loads(resolution_json),
        "resolution_json": resolution_json,
        "configuration": json.loads(configuration_json),
        "configuration_json": configuration_json,
        "configuration_digest": _digest(configuration_json),
        "rationale": rationale,
    }


def _setting_override(setting: Mapping[str, Any]) -> dict[str, object]:
    key = _text(setting.get("key"), "setting key")
    schema_version = setting.get("schema_version", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version <= 0:
        raise ConversionProfileError(f"{key}: schema_version must be a positive integer")
    if "value" not in setting:
        raise ConversionProfileError(f"{key}: setting value is required")
    value_json = _canonical(setting["value"])
    return {
        "key": key,
        "schema_version": schema_version,
        "value": json.loads(value_json),
        "value_json": value_json,
        "value_digest": _digest(value_json),
        "rationale": _text(setting.get("rationale"), f"{key} rationale"),
    }


def _profile_payload(
    base_profile: sqlite3.Row,
    *,
    rule_overrides: Sequence[Mapping[str, object]],
    setting_overrides: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "base_profile": {
            "id": base_profile["id"],
            "digest": _base_profile_digest(base_profile),
        },
        "rule_overrides": [
            {
                "rule_key": override["rule_key"],
                "directive": override["directive"],
                "rule_revision_id": override["rule_revision_id"],
                "selected_rule_digest": override["selected_rule_digest"],
                "resolution": override["resolution"],
                "configuration": override["configuration"],
            }
            for override in sorted(
                rule_overrides,
                key=lambda item: (str(item["rule_key"]), str(item["directive"])),
            )
        ],
        "setting_overrides": [
            {
                "key": setting["key"],
                "schema_version": setting["schema_version"],
                "value": setting["value"],
            }
            for setting in sorted(setting_overrides, key=lambda item: str(item["key"]))
        ],
    }


def create_project_profile_revision(
    database: Path,
    *,
    project_id: str,
    profile_name: str,
    base_profile_id: str,
    required_validation_tier: str,
    rationale: str,
    created_by: str,
    rule_overrides: Sequence[Mapping[str, Any]] = (),
    setting_overrides: Sequence[Mapping[str, Any]] = (),
    workspace_name: str | None = None,
    supersedes_profile_revision_id: str | None = None,
) -> dict[str, object]:
    """Create an immutable draft project/corpus overlay over a base profile."""

    project_id = _text(project_id, "project_id")
    profile_name = _text(profile_name, "profile_name")
    base_profile_id = _text(base_profile_id, "base_profile_id")
    required_validation_tier = _text(required_validation_tier, "required_validation_tier")
    rationale = _text(rationale, "rationale")
    created_by = _text(created_by, "created_by")
    workspace_name = _optional_text(workspace_name, "workspace_name")
    if isinstance(rule_overrides, (str, bytes)) or isinstance(setting_overrides, (str, bytes)):
        raise ConversionProfileError("overrides must be JSON arrays")

    with _connection(database) as connection:
        base_profile = _base_profile(connection, base_profile_id)
        normalized_rules = []
        for override in rule_overrides:
            if not isinstance(override, Mapping):
                raise ConversionProfileError("each rule override must be a JSON object")
            normalized_rules.append(
                _rule_override(
                    connection,
                    override,
                    source_frontend_pack_id=str(base_profile["source_frontend_pack_id"]),
                )
            )
        normalized_settings = []
        for setting in setting_overrides:
            if not isinstance(setting, Mapping):
                raise ConversionProfileError("each setting override must be a JSON object")
            normalized_settings.append(_setting_override(setting))

        seen_selection: set[str] = set()
        seen_directives: set[tuple[str, str]] = set()
        for override in normalized_rules:
            key = str(override["rule_key"])
            directive = str(override["directive"])
            if (key, directive) in seen_directives:
                raise ConversionProfileError(f"{key}: duplicate `{directive}` override")
            seen_directives.add((key, directive))
            if directive in _SELECTION_DIRECTIVES:
                if key in seen_selection:
                    raise ConversionProfileError(
                        f"{key}: profile may select, replace, or disable a rule only once"
                    )
                seen_selection.add(key)
        disabled = {
            str(override["rule_key"])
            for override in normalized_rules
            if override["directive"] == "disable"
        }
        if disabled & {
            str(override["rule_key"])
            for override in normalized_rules
            if override["directive"] == "configure"
        }:
            raise ConversionProfileError("a disabled rule must not have a configuration override")

        seen_settings: set[str] = set()
        for setting in normalized_settings:
            key = str(setting["key"])
            if key in seen_settings:
                raise ConversionProfileError(f"{key}: duplicate setting override")
            seen_settings.add(key)

        previous = connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) AS revision
            FROM sourcebridge_project_profile_revision
            WHERE project_id = ? AND profile_name = ?
            """,
            (project_id, profile_name),
        ).fetchone()
        revision = int(previous["revision"]) + 1
        if supersedes_profile_revision_id is not None:
            superseded = connection.execute(
                """
                SELECT project_id, profile_name
                FROM sourcebridge_project_profile_revision
                WHERE id = ?
                """,
                (supersedes_profile_revision_id,),
            ).fetchone()
            if superseded is None:
                raise ConversionProfileError(
                    f"{supersedes_profile_revision_id}: superseded profile revision does not exist"
                )
            if (
                superseded["project_id"] != project_id
                or superseded["profile_name"] != profile_name
            ):
                raise ConversionProfileError(
                    "a project profile revision may only supersede the same project/profile"
                )
        elif revision > 1:
            raise ConversionProfileError(
                "a later project profile revision must identify the revision it supersedes"
            )

        resolved_payload = _profile_payload(
            base_profile,
            rule_overrides=normalized_rules,
            setting_overrides=normalized_settings,
        )
        resolved_digest = _digest(_canonical(resolved_payload))
        profile_revision_id = _id(
            "project-profile",
            f"{project_id}:{profile_name}:{revision}:{resolved_digest}",
        )
        connection.execute(
            """
            INSERT INTO sourcebridge_project_profile_revision
                (id, project_id, profile_name, revision, base_profile_id,
                 base_profile_digest, required_validation_tier, workspace_name,
                 supersedes_profile_revision_id, resolved_digest, rationale,
                 created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_revision_id,
                project_id,
                profile_name,
                revision,
                base_profile_id,
                _base_profile_digest(base_profile),
                required_validation_tier,
                workspace_name,
                supersedes_profile_revision_id,
                resolved_digest,
                rationale,
                created_by,
                _now(),
            ),
        )
        for override in normalized_rules:
            override_id = _id(
                "profile-rule",
                f"{profile_revision_id}:{override['family_id']}:{override['directive']}"
                f":{override['configuration_digest']}:{override['rule_revision_id']}",
            )
            connection.execute(
                """
                INSERT INTO sourcebridge_project_profile_rule_override
                    (id, profile_revision_id, rule_family_id, directive,
                     selected_rule_revision_id, resolution_json, configuration_json,
                     configuration_digest, rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    override_id,
                    profile_revision_id,
                    override["family_id"],
                    override["directive"],
                    override["rule_revision_id"],
                    override["resolution_json"],
                    override["configuration_json"],
                    override["configuration_digest"],
                    override["rationale"],
                    _now(),
                ),
            )
        for setting in normalized_settings:
            setting_id = _id(
                "profile-setting",
                f"{profile_revision_id}:{setting['key']}:{setting['value_digest']}",
            )
            connection.execute(
                """
                INSERT INTO sourcebridge_project_profile_setting_override
                    (id, profile_revision_id, setting_key, schema_version,
                     value_json, value_digest, rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    setting_id,
                    profile_revision_id,
                    setting["key"],
                    setting["schema_version"],
                    setting["value_json"],
                    setting["value_digest"],
                    setting["rationale"],
                    _now(),
                ),
            )
        _record_state(
            connection,
            table="sourcebridge_project_profile_state",
            foreign_key="profile_revision_id",
            record_id=profile_revision_id,
            state="draft",
            rationale="Created as a development profile revision.",
            actor=created_by,
        )

    return {
        "profile_revision_id": profile_revision_id,
        "project_id": project_id,
        "profile_name": profile_name,
        "revision": revision,
        "resolved_digest": resolved_digest,
        "state": "draft",
    }


def set_project_profile_state(
    database: Path,
    profile_revision_id: str,
    *,
    state: str,
    rationale: str,
    decided_by: str,
) -> dict[str, str]:
    """Move a draft to candidate or retire a non-current project profile revision."""

    state = _text(state, "state")
    rationale = _text(rationale, "rationale")
    decided_by = _text(decided_by, "decided_by")
    if state not in {"candidate", "retired"}:
        raise ConversionProfileError(
            "project profile state changes must be `candidate` or `retired`; "
            "use promote_project_profile for `current`"
        )
    with _connection(database) as connection:
        current = _latest_state(
            connection,
            "sourcebridge_project_profile_state",
            "profile_revision_id",
            profile_revision_id,
        )
        if state == "candidate" and current != "draft":
            raise ConversionProfileError(
                f"{profile_revision_id}: only a draft profile may become a candidate"
            )
        if state == "retired" and current == "retired":
            raise ConversionProfileError(f"{profile_revision_id}: profile is already retired")
        _record_state(
            connection,
            table="sourcebridge_project_profile_state",
            foreign_key="profile_revision_id",
            record_id=profile_revision_id,
            state=state,
            rationale=rationale,
            actor=decided_by,
        )
    return {"profile_revision_id": profile_revision_id, "state": state}


def record_project_profile_certification(
    database: Path,
    profile_revision_id: str,
    *,
    validation_tier: str,
    status: str,
    evidence_reference: str,
    evidence_digest: str,
    details: Mapping[str, Any],
    certified_by: str,
) -> dict[str, str]:
    """Record validation evidence without changing the profile revision."""

    validation_tier = _text(validation_tier, "validation_tier")
    status = _text(status, "status")
    evidence_reference = _text(evidence_reference, "evidence_reference")
    evidence_digest = _text(evidence_digest, "evidence_digest")
    certified_by = _text(certified_by, "certified_by")
    if status not in _CERTIFICATION_STATES:
        raise ConversionProfileError(f"unsupported certification status `{status}`")
    if not isinstance(details, Mapping):
        raise ConversionProfileError("certification details must be a JSON object")
    details_json = _canonical(dict(details))

    with _connection(database) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sourcebridge_project_profile_revision WHERE id = ?",
            (profile_revision_id,),
        ).fetchone()
        if exists is None:
            raise ConversionProfileError(f"{profile_revision_id}: profile revision does not exist")
        certification_id = _id(
            "profile-certification",
            f"{profile_revision_id}:{validation_tier}:{status}:{evidence_digest}:{_now()}",
        )
        connection.execute(
            """
            INSERT INTO sourcebridge_project_profile_certification
                (id, profile_revision_id, validation_tier, status,
                 evidence_reference, evidence_digest, details_json,
                 certified_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                certification_id,
                profile_revision_id,
                validation_tier,
                status,
                evidence_reference,
                evidence_digest,
                details_json,
                certified_by,
                _now(),
            ),
        )
    return {"certification_id": certification_id, "status": status}


def promote_project_profile(
    database: Path,
    profile_revision_id: str,
    *,
    rationale: str,
    decided_by: str,
) -> dict[str, object]:
    """Promote a fully certified profile and retire the prior current revision."""

    rationale = _text(rationale, "rationale")
    decided_by = _text(decided_by, "decided_by")
    with _connection(database) as connection:
        profile = connection.execute(
            """
            SELECT project_id, profile_name, required_validation_tier
            FROM sourcebridge_project_profile_revision
            WHERE id = ?
            """,
            (profile_revision_id,),
        ).fetchone()
        if profile is None:
            raise ConversionProfileError(f"{profile_revision_id}: profile revision does not exist")
        current_state = _latest_state(
            connection,
            "sourcebridge_project_profile_state",
            "profile_revision_id",
            profile_revision_id,
        )
        if current_state not in {"draft", "candidate"}:
            raise ConversionProfileError(
                f"{profile_revision_id}: only draft or candidate profiles may be promoted"
            )
        certified = connection.execute(
            """
            SELECT 1
            FROM sourcebridge_project_profile_certification
            WHERE profile_revision_id = ? AND validation_tier = ? AND status = 'passed'
            LIMIT 1
            """,
            (profile_revision_id, profile["required_validation_tier"]),
        ).fetchone()
        if certified is None:
            raise ConversionProfileError(
                f"{profile_revision_id}: a passed `{profile['required_validation_tier']}` "
                "certification is required before promotion"
            )
        selected_rules = connection.execute(
            """
            SELECT selected_rule_revision_id
            FROM sourcebridge_project_profile_rule_override
            WHERE profile_revision_id = ? AND directive IN ('enable', 'replace')
            """,
            (profile_revision_id,),
        ).fetchall()
        for selected in selected_rules:
            selected_id = str(selected["selected_rule_revision_id"])
            state = _latest_state(
                connection,
                "sourcebridge_rule_revision_state",
                "rule_revision_id",
                selected_id,
            )
            if state != "active":
                raise ConversionProfileError(
                    f"{selected_id}: a current profile may select only active rule revisions"
                )

        prior_current = connection.execute(
            """
            SELECT profile.id
            FROM sourcebridge_project_profile_revision AS profile
            JOIN sourcebridge_project_profile_state AS state
              ON state.profile_revision_id = profile.id
            WHERE profile.project_id = ? AND profile.profile_name = ?
              AND state.sequence = (
                  SELECT MAX(latest.sequence)
                  FROM sourcebridge_project_profile_state AS latest
                  WHERE latest.profile_revision_id = profile.id
              )
              AND state.state = 'current'
            """,
            (profile["project_id"], profile["profile_name"]),
        ).fetchall()
        for prior in prior_current:
            prior_id = str(prior["id"])
            if prior_id != profile_revision_id:
                _record_state(
                    connection,
                    table="sourcebridge_project_profile_state",
                    foreign_key="profile_revision_id",
                    record_id=prior_id,
                    state="retired",
                    rationale=f"Superseded by {profile_revision_id}.",
                    actor=decided_by,
                )
        _record_state(
            connection,
            table="sourcebridge_project_profile_state",
            foreign_key="profile_revision_id",
            record_id=profile_revision_id,
            state="current",
            rationale=rationale,
            actor=decided_by,
        )

    return resolve_project_profile(database, profile_revision_id)


def resolve_project_profile(database: Path, profile_revision_id: str) -> dict[str, object]:
    """Return the exact base profile, project overlays, and digest for a revision."""

    with _connection(database, write=False) as connection:
        profile = connection.execute(
            """
            SELECT id, project_id, profile_name, revision, base_profile_id,
                   base_profile_digest, required_validation_tier, workspace_name,
                   supersedes_profile_revision_id, resolved_digest, rationale,
                   created_by, created_at
            FROM sourcebridge_project_profile_revision
            WHERE id = ?
            """,
            (profile_revision_id,),
        ).fetchone()
        if profile is None:
            raise ConversionProfileError(f"{profile_revision_id}: profile revision does not exist")
        base_profile = _base_profile(connection, str(profile["base_profile_id"]))
        if profile["base_profile_digest"] != _base_profile_digest(base_profile):
            raise ConversionProfileError(
                f"{profile_revision_id}: base profile digest no longer matches its stored identity"
            )
        rules = connection.execute(
            """
            SELECT family.rule_key, override.directive, override.selected_rule_revision_id,
                   revision.content_digest AS selected_rule_digest,
                   override.resolution_json, override.configuration_json, override.rationale
            FROM sourcebridge_project_profile_rule_override AS override
            JOIN sourcebridge_rule_family AS family ON family.id = override.rule_family_id
            LEFT JOIN sourcebridge_rule_revision AS revision
              ON revision.id = override.selected_rule_revision_id
            WHERE override.profile_revision_id = ?
            ORDER BY family.rule_key, override.directive
            """,
            (profile_revision_id,),
        ).fetchall()
        normalized_rules = [
            {
                "rule_key": str(rule["rule_key"]),
                "directive": str(rule["directive"]),
                "rule_revision_id": rule["selected_rule_revision_id"],
                "selected_rule_digest": rule["selected_rule_digest"],
                "resolution": json.loads(str(rule["resolution_json"])),
                "configuration": json.loads(str(rule["configuration_json"])),
                "rationale": str(rule["rationale"]),
            }
            for rule in rules
        ]
        settings = connection.execute(
            """
            SELECT setting_key, schema_version, value_json, rationale
            FROM sourcebridge_project_profile_setting_override
            WHERE profile_revision_id = ?
            ORDER BY setting_key
            """,
            (profile_revision_id,),
        ).fetchall()
        normalized_settings = [
            {
                "key": str(setting["setting_key"]),
                "schema_version": int(setting["schema_version"]),
                "value": json.loads(str(setting["value_json"])),
                "rationale": str(setting["rationale"]),
            }
            for setting in settings
        ]
        resolved_digest = _digest(
            _canonical(
                _profile_payload(
                    base_profile,
                    rule_overrides=normalized_rules,
                    setting_overrides=normalized_settings,
                )
            )
        )
        if resolved_digest != profile["resolved_digest"]:
            raise ConversionProfileError(
                f"{profile_revision_id}: resolved profile digest does not match stored evidence"
            )
        state = _latest_state(
            connection,
            "sourcebridge_project_profile_state",
            "profile_revision_id",
            profile_revision_id,
        )

    return {
        "id": profile_revision_id,
        "project_id": profile["project_id"],
        "profile_name": profile["profile_name"],
        "revision": profile["revision"],
        "state": state,
        "base_profile": {
            "id": base_profile["id"],
            "profile_name": base_profile["profile_name"],
            "source_frontend_pack_id": base_profile["source_frontend_pack_id"],
            "target_backend_pack_id": base_profile["target_backend_pack_id"],
            "digest": profile["base_profile_digest"],
        },
        "required_validation_tier": profile["required_validation_tier"],
        "workspace_name": profile["workspace_name"],
        "supersedes_profile_revision_id": profile["supersedes_profile_revision_id"],
        "resolved_digest": profile["resolved_digest"],
        "rule_overrides": normalized_rules,
        "setting_overrides": normalized_settings,
    }


def list_project_profile_revisions(
    database: Path,
    *,
    project_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, object]]:
    """List compact profile identities and their latest lifecycle state."""

    if project_id is not None:
        project_id = _text(project_id, "project_id")
    if state is not None and state not in _PROFILE_STATES:
        raise ConversionProfileError(f"unsupported profile state `{state}`")
    with _connection(database, write=False) as connection:
        query = """
            SELECT profile.id, profile.project_id, profile.profile_name, profile.revision,
                   profile.resolved_digest, profile.required_validation_tier, state.state
            FROM sourcebridge_project_profile_revision AS profile
            JOIN sourcebridge_project_profile_state AS state
              ON state.profile_revision_id = profile.id
            WHERE state.sequence = (
                SELECT MAX(latest.sequence)
                FROM sourcebridge_project_profile_state AS latest
                WHERE latest.profile_revision_id = profile.id
            )
        """
        parameters: list[str] = []
        if project_id is not None:
            query += " AND profile.project_id = ?"
            parameters.append(project_id)
        if state is not None:
            query += " AND state.state = ?"
            parameters.append(state)
        query += " ORDER BY profile.project_id, profile.profile_name, profile.revision DESC"
        rows = connection.execute(query, parameters).fetchall()
    return [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "profile_name": row["profile_name"],
            "revision": row["revision"],
            "state": row["state"],
            "required_validation_tier": row["required_validation_tier"],
            "resolved_digest": row["resolved_digest"],
        }
        for row in rows
    ]
