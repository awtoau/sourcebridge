# Sourcebridge implementation plan

## Purpose

Build a reusable conversion engine that supports multiple source languages and
multiple targets. A conversion is selected by a profile, not hard-coded into an
emitter:

```text
frontend + corpus variant + source model
    + conversion profile
    + target backend
    = generated artifacts + evidence-backed dispositions
```

The first migration path will extract lessons from C# to Rust work. That work
is evidence, not an API boundary: Sourcebridge must also support paths such as
Delphi to Python+Qt without forcing either language through Rust-shaped rules.

## Principles

1. **Facts before text.** A frontend records compiler/parser facts and source
   provenance. It does not fabricate a target-language rendering.
2. **Frontends and backends are independent.** A source-language pack provides
   source semantics; a backend provides target-language rendering and runtime
   contracts. A profile chooses both.
3. **Variants are first-class.** A corpus version alone is insufficient. C
   needs compiler, target ABI, pointer width, byte order, headers and
   preprocessor configuration. C# and Delphi also need declared
   compiler/dialect/runtime and conditional-compilation variants.
4. **Rules are data, not file patches.** A rule has applicability, positive
   evidence, negative cases, provenance, and validation state.
5. **No silent semantic invention.** Source defects, undefined behavior,
   unsupported constructs, and target constraints become explicit decisions.
6. **Validation is two-sided.** Source checks establish the behavior and risks
   being ported; target checks establish output quality; an oracle establishes
   equivalence where observable.
7. **Local models propose; validation decides.** Local model output is staged,
   schema-checked, and provenance-recorded. It never lands directly in a
   generated artifact.

## Core model

The engine core owns these language-neutral entities:

| Entity | Meaning |
|---|---|
| Corpus | a complete source codebase at a pinned revision |
| Variant | one concrete build/configuration of that corpus |
| Source unit and symbol | a provenance-addressable source boundary |
| Source fact | frontend-extracted semantic fact with source span |
| Rule | a versioned, applicable conversion mapping with positive/negative evidence |
| Profile | ordered rules, runtime adapters, options, and selected backend |
| Conversion decision | direct mapping or explicit disposition for a source unit |
| Generated artifact | target output with profile/rule/source provenance |
| Validation result | source check, target check, oracle, benchmark, or review evidence |
| Work item | a claimable problem linked to diagnostics and evidence |

The core defines neutral storage and report contracts. It does not define
Roslyn operations, C macros, Delphi units, Rust ownership, or Qt widgets.

## Pack interfaces

### Frontend pack

A frontend takes a corpus variant and produces source units, symbols, facts,
and source-side diagnostics. It defines language-specific fact schemas:

- **C#**: Roslyn operations, symbols, nullability/flow facts, compiler version.
- **C**: compiler AST facts, macros, includes, layouts, linkage, preprocessor
  state, target ABI, and undefined-behavior findings.
- **Delphi**: dialect/compiler facts, units, records/classes, properties,
  managed values, exceptions, and finalization semantics.

### Corpus pack

A corpus pack supplies domain-specific rules, source fixtures, runtime
replacement contracts, and oracles. It cannot alter generic frontend facts.

### Backend pack

A backend consumes neutral decisions plus target-specific rule payloads and
emits artifacts. It owns formatting, compiler/linter integration, runtime
adapters, and target-side checks.

Initial backends:

- **Rust:** compiler, formatter, Clippy, ABI/layout checks, runtime crates,
  differential tests and trace replay.
- **Python+Qt:** formatter/linter/type-check configuration, package/runtime
  adapters, Qt contract tests, and target integration tests.

Neither backend may be selected implicitly. The profile contains a
`target_backend` identifier and a reproducible backend/options digest.

Repository boundaries follow reusable packs, not source/target pairs. For
example, `sourcebridge-delphi` provides Delphi extraction and semantics once;
profiles may target `sourcebridge-python-qt`, `sourcebridge-rust` with a Qt
adapter, or a character-mode CRT/driver backend. Target-oriented rules and
runtime adapters are profile data layered over those packs.

## Validation pipeline

For every selected source unit:

1. run source compiler, linter, and static-analysis checks for the exact
   variant;
2. classify findings as source defect, undefined/unspecified behavior,
   unsupported construct, or clean input;
3. extract facts and apply the profile;
4. generate target artifacts and provenance;
5. run target formatter, compiler, linter, type/layout/ABI checks and tests;
6. run a differential oracle or trace replay when behavior is observable;
7. record the strongest validation tier and the resulting disposition.

Source behavior that is undefined or configuration-dependent cannot be
“faithfully copied” without a declared policy. The engine must block, mark, or
record a justified target deviation.

## Local Ollama proposal stage

Use local Ollama for bounded, high-volume proposal work:

1. package an unmatched cluster with exemplars, sibling instances, source
   variant, active-rule digest, failures, and negative context;
2. request schema-constrained candidate rules, negative patterns, test ideas,
   and open questions;
3. record model digest, prompt-template version, fixed options, payload hash,
   raw response, and schema result;
4. require a human or policy-controlled review to promote a candidate;
5. run normal corpus, compiler, and oracle validation before a rule is
   committed.

The engine must reject remote/cloud model routes unless a profile explicitly
allows external processing. Local-only is the default.

## Staged delivery

### Stage 0: contracts and repository foundation

- Define versioned schemas for the core model and pack manifests.
- Add a CLI that validates manifests and prints a machine-readable registry.
- Define reproducibility, artifact, and report locations.
- Publish contributor and database/concurrency guidance.

### Stage 1: import proven generic capabilities

- Extract neutral corpus/variant/profile/evidence/rule/report contracts from
  existing C# conversion work.
- Preserve provenance and current reproducible evidence.
- Add an explicit source-unit disposition table and report.
- Keep existing language-specific schemas inside their packs.

### Stage 2: C# to Rust reference pack

- Adapt the Roslyn frontend, C# language rules, Rust backend, and their
  conformance tests behind Sourcebridge pack interfaces.
- Separate C# language mappings from Renode corpus rules/runtime/oracles.
- Prove output equivalence against the established C# reference cases.

### Stage 3: Delphi bootstrap with local model proposals

- Select an attributed Stage-1 Delphi corpus and pin dialect/compiler/runtime.
- Build a Delphi frontend with small basic-feature fixtures.
- Use schema-constrained local Ollama proposals for candidate mappings.
- Validate each promoted rule through Delphi-side cases and chosen target
  backend checks.
- Demonstrate at least one Delphi profile targeting Rust and one profile
  targeting Python+Qt, even if their coverage is intentionally small.

### Stage 4: C to Rust with concrete variants

- Import C compiler/preprocessor/ABI facts and concrete variants.
- Establish fixed-width integer, control-flow, pointer/string, aggregate, and
  macro stages before corpus idioms.
- Add undefined-behavior and sanitizer evidence to conversion decisions.
- Integrate corpus-specific packs such as Linux only after language basics are
  independently measurable.

### Stage 5: backend expansion and cross-target reporting

- Add target-backend conformance contracts and profile comparison reports.
- Support one source corpus through multiple target profiles without
  duplicating frontend work.
- Track source coverage, dispositions, validation tier, and backend-specific
  blockers separately.

## Repository layout

```text
sourcebridge/
  core/
    schema/
    corpus/
    rules/
    profiles/
    evidence/
    validation/
    reporting/
  cli/
  packs/
    csharp/
    c/
    delphi/
    rust/
    python-qt/
  docs/
```

Packs may later become separate repositories. Do that only after their
manifests and stable core interfaces are proven; premature repository moves
make changes harder to test and do not create a generic engine.

## Success measures

- A run is reproducible from pinned corpus variant, pack versions, profile,
  rule digest, backend digest, and recorded toolchain versions.
- No generated artifact lacks source/profile/rule provenance.
- Every source unit has a disposition.
- Source and target diagnostics are linked to the same conversion decision.
- Local model proposals are auditable and cannot bypass validation.
- Adding a new frontend or backend does not require changing another pack’s
  semantic implementation.
