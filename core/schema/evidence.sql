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
