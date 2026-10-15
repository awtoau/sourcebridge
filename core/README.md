# Engine core

The core owns only language-neutral contracts and services:

- corpus, variant, source-unit, symbol, fact, profile, disposition, artifact,
  validation-result, and work-item schemas;
- rule applicability, provenance, evidence, and promotion contracts;
- profile resolution and reproducible run identity;
- evidence/report storage and machine-readable reporting;
- source-side and target-side validation orchestration contracts.

It does not parse a language or emit a target language. Packs supply those
capabilities through versioned manifests.

`schema/evidence.sql` is the first portable persistence contract. It links a
pinned corpus variant to a profile, source units, explicit component decisions,
generated artifacts, validation results, and claimable work. Database adapters
may use local SQLite or shared PostgreSQL, but they must preserve this model and
its digest/provenance fields.
