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
