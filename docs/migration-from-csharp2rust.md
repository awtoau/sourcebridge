# Migration from the C# to Rust implementation

The existing C# to Rust repository is a working reference implementation, not
a directory tree to copy wholesale. Migration is compatibility-gated: each
extraction must preserve a known command/result contract before the next
component moves.

## Move order

1. **Neutral core contracts**
   - Start from the generic portions of `scripts/core/evidence_db.py`,
     `scripts/core/rule_store.py`, status reporting, variant/profile metadata,
     and work-item evidence.
   - Remove the current C# and Renode defaults before treating any module as
     core.
   - Add the missing explicit disposition schema.

2. **C# frontend pack**
   - Move `frontend/RenodeIngest/` behind a C# frontend manifest.
   - Keep its Roslyn operation schema pack-owned.
   - Preserve an ingest comparison against the current corpus database.

3. **Rust backend pack**
   - Move generic target formatting, compilation, linting, and artifact
     provenance contracts first.
   - Move C#-specific Rust emission only with the C# language pack, not as a
     generic Rust backend rule.

4. **C# language pack**
   - Move C# semantic rules, emitter language handlers, runtime mappings, and
     conformance harness.

5. **Renode corpus pack**
   - Move register DSL rules, Renode plugins, runtime crates, and trace oracle
     last. Renode must remain optional for a different C# corpus.

## Required migration checks

For every moved component:

- source facts and rule/profile digests remain reproducible;
- generated artifacts retain source/profile/rule provenance;
- `-j1` and routine parallel execution remain byte-identical where applicable;
- source and target validation reports retain their linked diagnostics;
- the legacy reference command and the Sourcebridge path agree on the
  committed fixture/corpus result.

No generated Rust file is moved or edited as a substitute for moving its
frontend/rule/backend behavior.
