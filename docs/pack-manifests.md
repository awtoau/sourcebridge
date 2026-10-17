# Pack manifests and profiles

Every independently versioned Sourcebridge pack has a manifest validated
against `core/schema/pack-manifest.schema.json`.

The key separation is intentional:

- a **frontend** provides source languages and no target environment;
- a **backend** provides target environments and no source language;
- a **corpus** pack supplies neither generic language semantics nor a target;
- a **runtime** pack provides adapters selected by a profile.

A conversion profile validates against `core/schema/profile.schema.json`. It
names one source frontend, optional corpus pack, one target backend, ordered
rule layers, runtime adapters, and profile options. It is the only place that
joins a source language to a target environment.

For example, a Delphi frontend and a Python+Qt backend are independent packs:

```json
{
  "schema_version": 1,
  "id": "delphi-to-python-qt",
  "source_frontend": "sourcebridge-delphi",
  "target_backend": "sourcebridge-python-qt",
  "rule_layers": ["delphi-core"],
  "runtime_adapters": ["qt-sql"]
}
```

The same `sourcebridge-delphi` pack can appear in a `delphi-to-rust-crt`
profile with a Rust character-mode backend and different runtime adapters.
Likewise, `sourcebridge-python` can select the same Rust backend while retaining
a Python component through a declared boundary.

Resolve a profile against the available pack directory before running a
conversion:

```text
python3 cli/sourcebridge.py resolve-profile \
  examples/delphi-to-rust-crt.profile.json --packs-dir examples
```

Resolution verifies that the declared source pack is a frontend with a source
language and the declared target pack is a backend with a target environment.

## Immutable project and corpus overlays

The generic base profile is not edited for a project-specific conversion such
as Renode. Instead, create a version-controlled manifest for an immutable
project profile revision. It names the imported base-profile ID and records
only the project/corpus differences:

```json
{
  "project_id": "renode",
  "profile_name": "csharp-to-rust",
  "base_profile_id": "csharp-to-rust-base",
  "required_validation_tier": "renode-full",
  "workspace_name": "renode-logging",
  "rationale": "Use the Renode logging and runtime contracts.",
  "created_by": "dan",
  "rule_overrides": [
    {
      "rule_key": "logging",
      "directive": "replace",
      "rule_revision_id": "rule-revision-...",
      "rationale": "Renode owns this API surface."
    },
    {
      "rule_key": "logging",
      "directive": "configure",
      "configuration": {"receiver": "machine"},
      "rationale": "The receiver is part of the Renode contract."
    }
  ],
  "setting_overrides": [
    {
      "key": "runtime_adapter",
      "schema_version": 1,
      "value": {"id": "renode-runtime-v1"},
      "rationale": "Select the Renode runtime adapter."
    }
  ]
}
```

Rule-overrides are explicit:

- `enable` selects an additional rule revision;
- `disable` removes an inherited rule for this project profile only and must
  include a structured `resolution` with `kind`, `scope`, and `reference`;
- `replace` selects a project/corpus rule revision instead of the inherited
  behavior;
- `configure` supplies a schema-versioned JSON object to the selected rule.

An overlay cannot select two replacements for the same rule family or configure
a disabled rule. It must reference rule families from the base profile's
frontend, preventing a Renode/C# policy from being applied to another source
language accidentally.

Register a rule definition as an immutable draft first. The definition file is
JSON and remains version controlled alongside the pack:

```text
sourcebridge profile register-rule \
  --database evidence/sourcebridge.db \
  --rule-key logging \
  --source-frontend sourcebridge-csharp \
  --definition packs/renode/rules/logging.json \
  --workspace renode-logging \
  --rationale "Lower Renode logging calls." \
  --created-by agent
```

The command returns a revision ID for the overlay manifest. A new rule
revision starts as `draft`; it becomes `active` only through an append-only
state event:

```text
sourcebridge profile set-rule-state \
  --database evidence/sourcebridge.db rule-revision-... active \
  --rationale "Rule regression suite passed." --decided-by reviewer
```

Create and inspect the project profile:

```text
sourcebridge profile create --database evidence/sourcebridge.db renode-profile.json
sourcebridge profile resolve --database evidence/sourcebridge.db project-profile-...
sourcebridge profile list --database evidence/sourcebridge.db --project renode
```

The new profile remains `draft` or `candidate` and may select draft rules for
development runs, but it cannot become `current`. Promotion requires an
append-only `passed` certification at its declared full validation tier and
only active selected rules:

```text
sourcebridge profile set-state --database evidence/sourcebridge.db \
  project-profile-... candidate \
  --rationale "Ready for full validation." --decided-by agent
sourcebridge profile certify --database evidence/sourcebridge.db \
  project-profile-... --tier renode-full --status passed \
  --evidence-reference engine-test-run:renode-full-001 \
  --evidence-digest SHA256 --details '{"tests":[434,435,436]}' \
  --certified-by ci
sourcebridge profile promote --database evidence/sourcebridge.db \
  project-profile-... --rationale "Full validation passed." --decided-by reviewer
```

Promotion retires the former current revision for that project/profile. No
rule, profile, overlay, setting, certification, or lifecycle event can be
updated or deleted in place; historical conversion runs retain their original
rule/profile digest.

## Engine-test catalogues

A pack that owns runnable fixtures declares a version-controlled catalogue with
an `engine_tests` entrypoint:

```json
{
  "schema_version": 1,
  "id": "sourcebridge-csharp",
  "kind": "frontend",
  "version": "0.1.0",
  "provides": {
    "source_languages": ["csharp"],
    "target_environments": []
  },
  "entrypoints": {
    "engine_tests": "engine-tests/catalogue.json"
  }
}
```

The catalogue declares stable numeric case IDs, pack-relative fixture paths,
the pack's adapter configuration, and expected structured results. Its source
files remain in the pack; Sourcebridge imports their hashes and run evidence
into SQL:

```text
sourcebridge test import-pack --database evidence/sourcebridge.db \
  path/to/sourcebridge-csharp.pack.json
sourcebridge test run --database evidence/sourcebridge.db 434
```
