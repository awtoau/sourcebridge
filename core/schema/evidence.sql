-- Sourcebridge neutral corpus, profile, decision, and validation evidence.
-- Database adapters own migrations and timestamps; this file defines the
-- portable logical contract, not a vendor-specific deployment.

CREATE TABLE sourcebridge_corpus (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    revision TEXT NOT NULL,
    source_remote TEXT,
    manifest_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_name, revision, manifest_digest)
);

CREATE TABLE sourcebridge_variant (
    id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL REFERENCES sourcebridge_corpus(id),
    frontend_pack_id TEXT NOT NULL,
    compiler_identity TEXT,
    target_abi TEXT,
    pointer_width INTEGER,
    byte_order TEXT,
    configuration_digest TEXT NOT NULL,
    generated_input_digest TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (corpus_id, frontend_pack_id, configuration_digest)
);

CREATE TABLE sourcebridge_profile (
    id TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    source_frontend_pack_id TEXT NOT NULL,
    corpus_pack_id TEXT,
    target_backend_pack_id TEXT NOT NULL,
    rule_layers_digest TEXT NOT NULL,
    runtime_adapters_digest TEXT NOT NULL,
    options_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        profile_name,
        source_frontend_pack_id,
        target_backend_pack_id,
        rule_layers_digest,
        runtime_adapters_digest,
        options_digest
    )
);

CREATE TABLE sourcebridge_source_unit (
    id TEXT PRIMARY KEY,
    variant_id TEXT NOT NULL REFERENCES sourcebridge_variant(id),
    logical_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_span TEXT,
    source_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    UNIQUE (variant_id, source_path, source_span, kind)
);

-- Immutable excerpts preserve the exact source, generated output, or tool
-- evidence reviewed for a result even when a later corpus revision changes it.
CREATE TABLE sourcebridge_evidence_fragment (
    id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL REFERENCES sourcebridge_corpus(id),
    kind TEXT NOT NULL CHECK (kind IN ('source', 'generated', 'tool_output')),
    logical_path TEXT NOT NULL,
    start_line INTEGER,
    start_column INTEGER,
    end_line INTEGER,
    end_column INTEGER,
    content TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        corpus_id,
        kind,
        logical_path,
        start_line,
        start_column,
        end_line,
        end_column,
        content_digest
    )
);

CREATE TABLE sourcebridge_component_decision (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES sourcebridge_profile(id),
    source_unit_id TEXT NOT NULL REFERENCES sourcebridge_source_unit(id),
    disposition TEXT NOT NULL CHECK (
        disposition IN (
            'direct_translation',
            'retain_runtime',
            'runtime_replacement',
            'native_reimplementation',
            'external_dependency',
            'stubbed_boundary',
            'excluded',
            'blocked'
        )
    ),
    target_backend_pack_id TEXT,
    runtime_adapter_id TEXT,
    boundary_contract_id TEXT,
    rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, source_unit_id)
);

CREATE TABLE sourcebridge_generated_artifact (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES sourcebridge_component_decision(id),
    artifact_path TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    generator_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (decision_id, artifact_path, artifact_digest)
);

CREATE TABLE sourcebridge_validation_result (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES sourcebridge_component_decision(id),
    side TEXT NOT NULL CHECK (side IN ('source', 'target', 'boundary', 'oracle')),
    validator TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked', 'skipped')),
    evidence_path TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    details_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

-- One adapter invocation is the immutable two-sided validation boundary.  The
-- side rows below remain compatible with the original validation-result
-- contract while this row keeps the exact decision/profile identity and raw
-- adapter response together.
CREATE TABLE sourcebridge_validation_case (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES sourcebridge_component_decision(id),
    profile_revision_id TEXT NOT NULL
        REFERENCES sourcebridge_project_profile_revision(id),
    validator TEXT NOT NULL,
    validator_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked')),
    source_classification TEXT NOT NULL CHECK (
        source_classification IN (
            'clean',
            'defect',
            'undefined-or-config-dependent',
            'unsupported'
        )
    ),
    request_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    raw_response_digest TEXT NOT NULL,
    command_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

-- Raw side evidence is append-only and intentionally not normalized into
-- compiler-specific diagnostics.  Packs retain their own evidence shape.
CREATE TABLE sourcebridge_validation_evidence (
    id TEXT PRIMARY KEY,
    validation_case_id TEXT NOT NULL
        REFERENCES sourcebridge_validation_case(id),
    validation_result_id TEXT NOT NULL
        REFERENCES sourcebridge_validation_result(id),
    side TEXT NOT NULL CHECK (side IN ('source', 'target', 'oracle')),
    evidence_path TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    raw_evidence TEXT NOT NULL,
    details_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (validation_case_id, side)
);

CREATE INDEX sourcebridge_validation_case_decision_idx
    ON sourcebridge_validation_case (decision_id, completed_at, id);

CREATE INDEX sourcebridge_validation_evidence_result_idx
    ON sourcebridge_validation_evidence (validation_result_id);

CREATE TRIGGER sourcebridge_validation_case_immutable_update
BEFORE UPDATE ON sourcebridge_validation_case
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_validation_case is append-only');
END;

CREATE TRIGGER sourcebridge_validation_case_immutable_delete
BEFORE DELETE ON sourcebridge_validation_case
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_validation_case is append-only');
END;

CREATE TRIGGER sourcebridge_validation_evidence_immutable_update
BEFORE UPDATE ON sourcebridge_validation_evidence
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_validation_evidence is append-only');
END;

CREATE TRIGGER sourcebridge_validation_evidence_immutable_delete
BEFORE DELETE ON sourcebridge_validation_evidence
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_validation_evidence is append-only');
END;

-- A report groups these rows by a stable failure signature. The occurrences,
-- not a stored counter, remain the source of truth for drill-down review.
CREATE TABLE sourcebridge_diagnostic_occurrence (
    id TEXT PRIMARY KEY,
    validation_result_id TEXT NOT NULL REFERENCES sourcebridge_validation_result(id),
    source_unit_id TEXT NOT NULL REFERENCES sourcebridge_source_unit(id),
    source_fragment_id TEXT REFERENCES sourcebridge_evidence_fragment(id),
    generated_artifact_id TEXT REFERENCES sourcebridge_generated_artifact(id),
    generated_fragment_id TEXT REFERENCES sourcebridge_evidence_fragment(id),
    phase TEXT NOT NULL CHECK (phase IN ('source', 'extraction', 'rule', 'generation', 'target', 'oracle')),
    tool_identity TEXT NOT NULL,
    diagnostic_code TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('note', 'warning', 'error')),
    failure_signature TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX sourcebridge_diagnostic_occurrence_signature_idx
    ON sourcebridge_diagnostic_occurrence (
        failure_signature,
        phase,
        severity,
        recorded_at
    );

CREATE TABLE sourcebridge_diagnostic_review (
    id TEXT PRIMARY KEY,
    diagnostic_occurrence_id TEXT NOT NULL
        REFERENCES sourcebridge_diagnostic_occurrence(id),
    disposition TEXT NOT NULL CHECK (
        disposition IN (
            'unreviewed',
            'accepted_source_behavior',
            'known_converter_gap',
            'known_target_constraint',
            'false_grouping',
            'needs_investigation',
            'resolved'
        )
    ),
    rationale TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX sourcebridge_diagnostic_review_occurrence_idx
    ON sourcebridge_diagnostic_review (diagnostic_occurrence_id, created_at, id);

CREATE TRIGGER sourcebridge_diagnostic_review_immutable_update
BEFORE UPDATE ON sourcebridge_diagnostic_review
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_diagnostic_review is append-only');
END;

CREATE TRIGGER sourcebridge_diagnostic_review_immutable_delete
BEFORE DELETE ON sourcebridge_diagnostic_review
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_diagnostic_review is append-only');
END;

CREATE TABLE sourcebridge_execution_run (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES sourcebridge_profile(id),
    variant_id TEXT NOT NULL REFERENCES sourcebridge_variant(id),
    action TEXT NOT NULL CHECK (
        action IN ('analyze', 'emit', 'validate-source', 'validate-target', 'oracle')
    ),
    requested_workers INTEGER NOT NULL CHECK (requested_workers > 0),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'stopping', 'passed', 'failed', 'cancelled')
    ),
    command_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);

-- A run is reportable only when this immutable identity and validation
-- snapshot has been recorded.  Keeping it separate from the operational run
-- row lets the scheduler update lifecycle state without rewriting historical
-- provenance.
CREATE TABLE sourcebridge_execution_run_provenance (
    run_id TEXT PRIMARY KEY REFERENCES sourcebridge_execution_run(id),
    source_revision TEXT,
    input_digest TEXT NOT NULL,
    variant_digest TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    validation_state TEXT NOT NULL CHECK (
        validation_state IN ('complete', 'incomplete', 'stale')
    ),
    artifact_digest TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE sourcebridge_execution_worker (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES sourcebridge_execution_run(id),
    worker_slot INTEGER NOT NULL CHECK (worker_slot >= 0),
    status TEXT NOT NULL CHECK (
        status IN ('idle', 'running', 'blocked', 'failed', 'stopped', 'completed')
    ),
    source_unit_id TEXT REFERENCES sourcebridge_source_unit(id),
    current_work_item_id TEXT REFERENCES sourcebridge_work_item(id),
    units_completed INTEGER NOT NULL DEFAULT 0 CHECK (units_completed >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, worker_slot)
);

CREATE TABLE sourcebridge_execution_event (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES sourcebridge_execution_run(id),
    worker_id TEXT REFERENCES sourcebridge_execution_worker(id),
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('started', 'progress', 'diagnostic', 'retry', 'blocked', 'completed', 'failed', 'stopped')
    ),
    message TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);

CREATE INDEX sourcebridge_execution_event_run_idx
    ON sourcebridge_execution_event (run_id, occurred_at);

CREATE TABLE sourcebridge_work_item (
    id TEXT PRIMARY KEY,
    decision_id TEXT REFERENCES sourcebridge_component_decision(id),
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    issue_url TEXT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Work-item lifecycle is append-only.  The row in sourcebridge_work_item is
-- the stable identity and summary; the latest state below is the claimable
-- status and ownership boundary used by agents.
CREATE TABLE sourcebridge_work_item_state (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES sourcebridge_work_item(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (
        status IN ('open', 'claimed', 'in_progress', 'blocked', 'resolved', 'closed', 'stale')
    ),
    owner TEXT,
    ownership_scope TEXT NOT NULL CHECK (length(trim(ownership_scope)) > 0),
    disposition TEXT NOT NULL CHECK (length(trim(disposition)) > 0),
    rationale TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (work_item_id, sequence)
);

CREATE INDEX sourcebridge_work_item_state_latest_idx
    ON sourcebridge_work_item_state (work_item_id, sequence DESC);

-- A link is immutable and names the run that produced the evidence.  This is
-- what prevents a current report from accidentally presenting an old
-- diagnostic as the result of a newer run.
CREATE TABLE sourcebridge_work_item_evidence (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES sourcebridge_work_item(id),
    execution_run_id TEXT NOT NULL REFERENCES sourcebridge_execution_run(id),
    diagnostic_occurrence_id TEXT REFERENCES sourcebridge_diagnostic_occurrence(id),
    validation_result_id TEXT REFERENCES sourcebridge_validation_result(id),
    generated_artifact_id TEXT REFERENCES sourcebridge_generated_artifact(id),
    evidence_fragment_id TEXT REFERENCES sourcebridge_evidence_fragment(id),
    relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
    recorded_at TEXT NOT NULL,
    CHECK (
        diagnostic_occurrence_id IS NOT NULL
        OR validation_result_id IS NOT NULL
        OR generated_artifact_id IS NOT NULL
        OR evidence_fragment_id IS NOT NULL
    ),
    UNIQUE (
        work_item_id,
        execution_run_id,
        diagnostic_occurrence_id,
        validation_result_id,
        generated_artifact_id,
        evidence_fragment_id,
        relation
    )
);

CREATE INDEX sourcebridge_work_item_evidence_run_idx
    ON sourcebridge_work_item_evidence (execution_run_id, work_item_id);

-- Local workers append lifecycle events instead of mutating job history.
CREATE TABLE sourcebridge_local_worker_job (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES sourcebridge_work_item(id),
    phase TEXT NOT NULL CHECK (phase IN ('generator', 'critic', 'repair', 'validation')),
    input_digest TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    prompt_template_digest TEXT NOT NULL,
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    created_at TEXT NOT NULL,
    UNIQUE (work_item_id, phase, input_digest, model_digest, prompt_template_digest)
);

CREATE TABLE sourcebridge_local_worker_job_dependency (
    job_id TEXT NOT NULL REFERENCES sourcebridge_local_worker_job(id),
    prerequisite_job_id TEXT NOT NULL REFERENCES sourcebridge_local_worker_job(id),
    PRIMARY KEY (job_id, prerequisite_job_id),
    CHECK (job_id != prerequisite_job_id)
);

CREATE TABLE sourcebridge_local_worker_job_event (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES sourcebridge_local_worker_job(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'claimed', 'completed', 'failed', 'blocked')
    ),
    owner TEXT,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    result_digest TEXT,
    rationale TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (job_id, sequence)
);

CREATE INDEX sourcebridge_local_worker_job_event_latest_idx
    ON sourcebridge_local_worker_job_event (job_id, sequence DESC);

CREATE TABLE sourcebridge_agent_exchange (
    id TEXT PRIMARY KEY,
    work_item_id TEXT REFERENCES sourcebridge_work_item(id),
    local_worker_job_id TEXT REFERENCES sourcebridge_local_worker_job(id),
    prior_exchange_id TEXT REFERENCES sourcebridge_agent_exchange(id),
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint TEXT,
    local_only INTEGER NOT NULL CHECK (local_only IN (0, 1)),
    model_name TEXT,
    model_digest TEXT,
    prompt_template_version TEXT,
    prompt_template_digest TEXT,
    request_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    artifact_path TEXT,
    artifact_digest TEXT,
    status TEXT NOT NULL CHECK (status IN ('completed', 'rejected', 'failed', 'blocked')),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    generated_tokens INTEGER CHECK (generated_tokens IS NULL OR generated_tokens >= 0),
    elapsed_ms REAL CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    error_json TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (work_item_id IS NOT NULL OR local_worker_job_id IS NOT NULL)
);

CREATE INDEX sourcebridge_agent_exchange_scope_idx
    ON sourcebridge_agent_exchange (work_item_id, local_worker_job_id, recorded_at);

CREATE TRIGGER sourcebridge_execution_run_provenance_immutable_update
BEFORE UPDATE ON sourcebridge_execution_run_provenance
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_execution_run_provenance is append-only');
END;

CREATE TRIGGER sourcebridge_execution_run_provenance_immutable_delete
BEFORE DELETE ON sourcebridge_execution_run_provenance
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_execution_run_provenance is append-only');
END;

CREATE TRIGGER sourcebridge_work_item_state_immutable_update
BEFORE UPDATE ON sourcebridge_work_item_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_work_item_state is append-only');
END;

CREATE TRIGGER sourcebridge_work_item_state_immutable_delete
BEFORE DELETE ON sourcebridge_work_item_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_work_item_state is append-only');
END;

CREATE TRIGGER sourcebridge_work_item_evidence_immutable_update
BEFORE UPDATE ON sourcebridge_work_item_evidence
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_work_item_evidence is append-only');
END;

CREATE TRIGGER sourcebridge_work_item_evidence_immutable_delete
BEFORE DELETE ON sourcebridge_work_item_evidence
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_work_item_evidence is append-only');
END;

-- Schema adapters record their version before importing operational evidence.
CREATE TABLE sourcebridge_schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Engine tests are a language-neutral registry for invoking pack-owned or
-- upstream-suite fixtures. Source code and golden artifacts remain normal
-- versioned files; this table records their identity, expected result, and
-- reproducible execution evidence.
CREATE TABLE sourcebridge_engine_test (
    id INTEGER PRIMARY KEY,
    pack_id TEXT NOT NULL,
    adapter TEXT NOT NULL,
    name TEXT NOT NULL,
    fixture_path TEXT NOT NULL,
    fixture_digest TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    adapter_config_json TEXT NOT NULL,
    catalogue_path TEXT,
    catalogue_digest TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (pack_id, name)
);

CREATE TABLE sourcebridge_engine_test_run (
    id TEXT PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES sourcebridge_engine_test(id),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked')),
    command_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE INDEX sourcebridge_engine_test_run_test_idx
    ON sourcebridge_engine_test_run (test_id, completed_at);

-- Every imported legacy JSON record retains its original relative path, exact
-- bytes, and digest. Typed tables may project facts from this immutable source.
CREATE TABLE sourcebridge_imported_record (
    id TEXT PRIMARY KEY,
    source_repository TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    source_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE sourcebridge_rule_proposal (
    id TEXT PRIMARY KEY,
    imported_record_id TEXT NOT NULL REFERENCES sourcebridge_imported_record(id),
    status TEXT NOT NULL CHECK (
        status IN ('unvalidated-llm-proposal', 'rejected-llm-proposal')
    ),
    model_name TEXT,
    model_digest TEXT,
    prompt_template_version TEXT,
    payload_sha256 TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (imported_record_id)
);

-- A pack-owned Ollama inventory is supplemental planning evidence. It is kept
-- separate from execution results and rule proposals because it cannot prove
-- target behavior or promote a rule.
CREATE TABLE sourcebridge_ollama_test_inventory (
    id TEXT PRIMARY KEY,
    imported_record_id TEXT NOT NULL UNIQUE
        REFERENCES sourcebridge_imported_record(id),
    pack_id TEXT NOT NULL,
    test_id INTEGER NOT NULL REFERENCES sourcebridge_engine_test(id),
    fixture_path TEXT NOT NULL,
    fixture_digest TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    prompt_template_digest TEXT NOT NULL,
    prompt_json TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    raw_response_json TEXT NOT NULL,
    inventory_json TEXT NOT NULL,
    statuses_json TEXT NOT NULL,
    status_source TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX sourcebridge_ollama_test_inventory_test_idx
    ON sourcebridge_ollama_test_inventory (pack_id, test_id, recorded_at);

-- Agent usage is imported only from host-provided counters. It never derives
-- token values from prompts, text length, or model estimates.
CREATE TABLE sourcebridge_agent_usage (
    id TEXT PRIMARY KEY,
    imported_record_id TEXT NOT NULL UNIQUE
        REFERENCES sourcebridge_imported_record(id),
    provider TEXT NOT NULL,
    session_id TEXT,
    model_name TEXT,
    model_digest TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cache_read_tokens INTEGER CHECK (
        cache_read_tokens IS NULL OR cache_read_tokens >= 0
    ),
    cache_write_tokens INTEGER CHECK (
        cache_write_tokens IS NULL OR cache_write_tokens >= 0
    ),
    duration_ms REAL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    local_only INTEGER NOT NULL CHECK (local_only IN (0, 1)),
    proposal_worker_request_id TEXT
        REFERENCES sourcebridge_proposal_worker_request(id),
    rule_revision_id TEXT REFERENCES sourcebridge_rule_revision(id),
    work_item_id TEXT REFERENCES sourcebridge_work_item(id),
    recorded_at TEXT NOT NULL,
    CHECK (
        input_tokens IS NOT NULL
        OR output_tokens IS NOT NULL
        OR cache_read_tokens IS NOT NULL
        OR cache_write_tokens IS NOT NULL
    ),
    CHECK (
        proposal_worker_request_id IS NOT NULL
        OR rule_revision_id IS NOT NULL
        OR work_item_id IS NOT NULL
    )
);

CREATE INDEX sourcebridge_agent_usage_scope_idx
    ON sourcebridge_agent_usage
        (proposal_worker_request_id, rule_revision_id, work_item_id, recorded_at);

CREATE TRIGGER sourcebridge_agent_usage_immutable_update
BEFORE UPDATE ON sourcebridge_agent_usage
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_agent_usage is append-only');
END;

CREATE TRIGGER sourcebridge_agent_usage_immutable_delete
BEFORE DELETE ON sourcebridge_agent_usage
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_agent_usage is append-only');
END;

-- Rule definitions are immutable. Lifecycle changes are recorded as append-only
-- state events so historical conversions always retain the rule content and
-- state that they used.
CREATE TABLE sourcebridge_rule_family (
    id TEXT PRIMARY KEY,
    rule_key TEXT NOT NULL,
    source_frontend_pack_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_frontend_pack_id, rule_key)
);

CREATE TABLE sourcebridge_rule_revision (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES sourcebridge_rule_family(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    definition_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    workspace_name TEXT,
    supersedes_revision_id TEXT REFERENCES sourcebridge_rule_revision(id),
    rationale TEXT NOT NULL,
    created_by TEXT NOT NULL,
    proposal_id TEXT REFERENCES sourcebridge_rule_proposal(id),
    model_name TEXT,
    model_digest TEXT,
    human_reviewed_by TEXT,
    human_reviewed_at TEXT,
    implementation_pack_id TEXT,
    implementation_path TEXT,
    implementation_digest TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (family_id, revision),
    UNIQUE (family_id, content_digest)
);

CREATE TABLE sourcebridge_rule_revision_state (
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

CREATE INDEX sourcebridge_rule_revision_state_latest_idx
    ON sourcebridge_rule_revision_state (rule_revision_id, sequence DESC);

-- A project or corpus profile is an immutable overlay over a generic base
-- profile. The overlay records exactly which rules/settings differ for that
-- project, without mutating the generic language profile.
CREATE TABLE sourcebridge_project_profile_revision (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    base_profile_id TEXT NOT NULL REFERENCES sourcebridge_profile(id),
    base_profile_digest TEXT NOT NULL,
    required_validation_tier TEXT NOT NULL,
    toolchain_digest TEXT,
    workspace_name TEXT,
    supersedes_profile_revision_id TEXT
        REFERENCES sourcebridge_project_profile_revision(id),
    resolved_digest TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, profile_name, revision)
);

CREATE TABLE sourcebridge_project_profile_state (
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

CREATE INDEX sourcebridge_project_profile_state_latest_idx
    ON sourcebridge_project_profile_state (profile_revision_id, sequence DESC);

CREATE TABLE sourcebridge_project_profile_rule_override (
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

CREATE INDEX sourcebridge_project_profile_rule_override_profile_idx
    ON sourcebridge_project_profile_rule_override (profile_revision_id, rule_family_id);

CREATE TABLE sourcebridge_project_profile_setting_override (
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

CREATE TABLE sourcebridge_project_profile_certification (
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

CREATE INDEX sourcebridge_project_profile_certification_profile_idx
    ON sourcebridge_project_profile_certification (profile_revision_id, created_at);

-- A pack adapter run is the executable counterpart of a resolved project
-- profile. The request and digests are retained with the run so an adapter
-- result cannot be mistaken for output from a different profile or source.
CREATE TABLE sourcebridge_rule_runtime_run (
    id TEXT PRIMARY KEY,
    profile_revision_id TEXT NOT NULL
        REFERENCES sourcebridge_project_profile_revision(id),
    status TEXT NOT NULL CHECK (
        status IN ('running', 'passed', 'failed', 'blocked', 'rejected')
    ),
    adapter_id TEXT NOT NULL,
    adapter_digest TEXT NOT NULL,
    resolved_profile_digest TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    implementation_digest TEXT NOT NULL,
    settings_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    command_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX sourcebridge_rule_runtime_run_profile_idx
    ON sourcebridge_rule_runtime_run (profile_revision_id, completed_at, id);

-- Conversion, runtime, and validation records retain the exact resolved
-- profile/rule/toolchain identity that produced them.  Staleness is derived
-- by comparing these immutable links with a selected current profile; no
-- historical row is rewritten.
CREATE TABLE sourcebridge_conversion_provenance (
    id TEXT PRIMARY KEY,
    execution_run_id TEXT REFERENCES sourcebridge_execution_run(id),
    runtime_run_id TEXT REFERENCES sourcebridge_rule_runtime_run(id),
    generated_artifact_id TEXT REFERENCES sourcebridge_generated_artifact(id),
    validation_result_id TEXT REFERENCES sourcebridge_validation_result(id),
    profile_revision_id TEXT NOT NULL
        REFERENCES sourcebridge_project_profile_revision(id),
    resolved_profile_digest TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    toolchain_digest TEXT NOT NULL,
    validation_tier TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (
        (execution_run_id IS NOT NULL)
        + (runtime_run_id IS NOT NULL)
        + (generated_artifact_id IS NOT NULL)
        + (validation_result_id IS NOT NULL) = 1
    )
);

CREATE UNIQUE INDEX sourcebridge_conversion_provenance_execution_idx
    ON sourcebridge_conversion_provenance (execution_run_id)
    WHERE execution_run_id IS NOT NULL;

CREATE UNIQUE INDEX sourcebridge_conversion_provenance_runtime_idx
    ON sourcebridge_conversion_provenance (runtime_run_id)
    WHERE runtime_run_id IS NOT NULL;

CREATE UNIQUE INDEX sourcebridge_conversion_provenance_artifact_idx
    ON sourcebridge_conversion_provenance (generated_artifact_id)
    WHERE generated_artifact_id IS NOT NULL;

CREATE UNIQUE INDEX sourcebridge_conversion_provenance_validation_idx
    ON sourcebridge_conversion_provenance (validation_result_id)
    WHERE validation_result_id IS NOT NULL;

CREATE INDEX sourcebridge_conversion_provenance_profile_idx
    ON sourcebridge_conversion_provenance (profile_revision_id, recorded_at, id);

CREATE TRIGGER sourcebridge_rule_runtime_run_provenance_immutable_update
BEFORE UPDATE ON sourcebridge_rule_runtime_run
WHEN NEW.profile_revision_id != OLD.profile_revision_id
  OR NEW.adapter_id != OLD.adapter_id
  OR NEW.adapter_digest != OLD.adapter_digest
  OR NEW.resolved_profile_digest != OLD.resolved_profile_digest
  OR NEW.rule_digest != OLD.rule_digest
  OR NEW.implementation_digest != OLD.implementation_digest
  OR NEW.settings_digest != OLD.settings_digest
  OR NEW.request_json != OLD.request_json
  OR NEW.command_json != OLD.command_json
  OR NEW.requested_by != OLD.requested_by
  OR NEW.started_at != OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_runtime_run provenance is append-only');
END;

CREATE TRIGGER sourcebridge_rule_runtime_run_immutable_delete
BEFORE DELETE ON sourcebridge_rule_runtime_run
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_runtime_run is append-only');
END;

CREATE TRIGGER sourcebridge_conversion_provenance_immutable_update
BEFORE UPDATE ON sourcebridge_conversion_provenance
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_conversion_provenance is append-only');
END;

CREATE TRIGGER sourcebridge_conversion_provenance_immutable_delete
BEFORE DELETE ON sourcebridge_conversion_provenance
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_conversion_provenance is append-only');
END;

-- A worker session is a durable contract for one serial, local-only batch.
-- Requests retain raw model output and deterministic acceptance/rejection data.
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
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    summary_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE sourcebridge_proposal_worker_request (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sourcebridge_proposal_worker_session(id),
    request_index INTEGER NOT NULL CHECK (request_index >= 0),
    evidence_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'rejected', 'failed')),
    elapsed_ms REAL NOT NULL CHECK (elapsed_ms >= 0),
    load_duration_ms REAL NOT NULL CHECK (load_duration_ms >= 0),
    generation_ms REAL NOT NULL CHECK (generation_ms >= 0),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    generated_tokens INTEGER,
    rejection_reasons_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX sourcebridge_proposal_worker_request_session_idx
    ON sourcebridge_proposal_worker_request (session_id, request_index);

CREATE TABLE sourcebridge_proposal_worker_session_event (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sourcebridge_proposal_worker_session(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    summary_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE INDEX sourcebridge_proposal_worker_session_event_idx
    ON sourcebridge_proposal_worker_session_event (session_id, sequence);

CREATE TRIGGER sourcebridge_proposal_worker_session_immutable_update
BEFORE UPDATE ON sourcebridge_proposal_worker_session
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session is append-only');
END;

CREATE TRIGGER sourcebridge_proposal_worker_session_immutable_delete
BEFORE DELETE ON sourcebridge_proposal_worker_session
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session is append-only');
END;

CREATE TRIGGER sourcebridge_proposal_worker_request_immutable_update
BEFORE UPDATE ON sourcebridge_proposal_worker_request
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_request is append-only');
END;

CREATE TRIGGER sourcebridge_proposal_worker_request_immutable_delete
BEFORE DELETE ON sourcebridge_proposal_worker_request
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_request is append-only');
END;

CREATE TRIGGER sourcebridge_proposal_worker_session_event_immutable_update
BEFORE UPDATE ON sourcebridge_proposal_worker_session_event
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session_event is append-only');
END;

CREATE TRIGGER sourcebridge_proposal_worker_session_event_immutable_delete
BEFORE DELETE ON sourcebridge_proposal_worker_session_event
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_proposal_worker_session_event is append-only');
END;

CREATE TRIGGER sourcebridge_rule_family_immutable_update
BEFORE UPDATE ON sourcebridge_rule_family
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_family is append-only');
END;

CREATE TRIGGER sourcebridge_rule_family_immutable_delete
BEFORE DELETE ON sourcebridge_rule_family
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_family is append-only');
END;

CREATE TRIGGER sourcebridge_model_authored_rule_requires_human_review
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
    SELECT RAISE(ABORT, 'model-authored rule requires persisted matching proposal and human review');
END;

CREATE TRIGGER sourcebridge_rule_revision_immutable_update
BEFORE UPDATE ON sourcebridge_rule_revision
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_revision is append-only');
END;

CREATE TRIGGER sourcebridge_rule_revision_immutable_delete
BEFORE DELETE ON sourcebridge_rule_revision
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_revision is append-only');
END;

CREATE TRIGGER sourcebridge_rule_revision_state_immutable_update
BEFORE UPDATE ON sourcebridge_rule_revision_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_revision_state is append-only');
END;

CREATE TRIGGER sourcebridge_rule_revision_state_immutable_delete
BEFORE DELETE ON sourcebridge_rule_revision_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_rule_revision_state is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_revision_immutable_update
BEFORE UPDATE ON sourcebridge_project_profile_revision
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_revision is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_revision_immutable_delete
BEFORE DELETE ON sourcebridge_project_profile_revision
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_revision is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_state_immutable_update
BEFORE UPDATE ON sourcebridge_project_profile_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_state is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_state_immutable_delete
BEFORE DELETE ON sourcebridge_project_profile_state
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_state is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_rule_override_immutable_update
BEFORE UPDATE ON sourcebridge_project_profile_rule_override
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_rule_override is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_rule_override_immutable_delete
BEFORE DELETE ON sourcebridge_project_profile_rule_override
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_rule_override is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_setting_override_immutable_update
BEFORE UPDATE ON sourcebridge_project_profile_setting_override
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_setting_override is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_setting_override_immutable_delete
BEFORE DELETE ON sourcebridge_project_profile_setting_override
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_setting_override is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_certification_immutable_update
BEFORE UPDATE ON sourcebridge_project_profile_certification
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_certification is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_certification_immutable_delete
BEFORE DELETE ON sourcebridge_project_profile_certification
BEGIN
    SELECT RAISE(ABORT, 'sourcebridge_project_profile_certification is append-only');
END;

CREATE TRIGGER sourcebridge_project_profile_state_current_requires_certification
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
