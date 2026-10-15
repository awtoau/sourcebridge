# Sourcebridge

**Evidence-backed, language-neutral source-porting engine.**

<p align="center">
  <img src="assets/sourcebridge-logo.svg" alt="Sourcebridge logo: linked source and target nodes over an evidence path" width="560">
</p>

Sourcebridge helps migrate a source codebase to a chosen target ecosystem
without pretending every source file has one mechanical translation. It records
the source corpus and variant, extracts semantic facts, applies reviewable
rules, emits through a selected backend, and keeps validation evidence and
explicit boundary decisions.

The engine is intentionally not “C# to Rust” or “Delphi to Python”:

```text
source corpus + concrete variant
  -> frontend extracts language facts
  -> normalized source model and conversion decisions
  -> profile selects rules, runtime adapters, and a target backend
  -> generated artifacts plus provenance
  -> source-side and target-side validation evidence
```

## Independent packs

| Pack type | Responsibility | Examples |
|---|---|---|
| Engine core | corpus/variant identity, normalized facts, rules, profiles, dispositions, evidence, reports | shared by every conversion |
| Frontend | parser/compiler integration and source-language semantic facts | C#, C, Delphi |
| Corpus pack | framework/DSL knowledge, source fixtures, and source oracle | Renode, Linux, a Delphi application |
| Backend | target-language emission, runtime adapters, formatting/linting, target validation | Rust, Python+Qt |

A frontend is independent of a backend. The same Delphi facts may be evaluated
by a Rust profile or by a Python+Qt profile; neither target is an implicit
default. A profile declares the chosen target backend, rule overlays, runtime
adapters, and options, then records their resolved digests for reproduction.

This avoids a repository for every source/target pair. The intended reusable
shape is:

```text
sourcebridge-delphi     Delphi frontend and semantic pack
sourcebridge-rust       Rust backend pack
sourcebridge-python-qt  Python and Qt backend pack
sourcebridge-crt        character-mode/driver backend pack

profile: delphi-to-python-qt
profile: delphi-to-rust-qt
profile: delphi-to-rust-crt
```

Each profile may choose different runtime adapters and target-oriented rules,
but all three begin from the same pinned Delphi corpus facts. The same applies
to C#, C, and future source languages.

## Honest conversion decisions

Every source unit receives a recorded disposition:

- direct translation;
- validated runtime replacement;
- native reimplementation behind a declared contract;
- external dependency;
- stubbed boundary;
- excluded; or
- blocked.

“Complete” means every unit has an evidence-backed disposition. It does not
mean every line was emitted mechanically.

## Validation is two-sided

The pipeline checks the source before conversion and the target after emission.
Compiler warnings, linter findings, static-analysis results, target compiler
diagnostics, ABI/layout checks, tests, and differential oracles are all
evidence. Undefined, unspecified, or configuration-dependent source behavior
must be recorded under a concrete source variant; Sourcebridge must not invent
target semantics for it.

## Local model assistance

Local models may generate high-volume candidate rules, negative cases, fixtures,
and isolated target drafts. They never directly modify generated output, mark a
rule validated, or promote a proposal. Sourcebridge records model/prompt/input
provenance and lets normal validation and review decide what lands.

## Dependencies are user-controlled decisions

Sourcebridge does not automatically convert every library or silently replace
it with a convenient target package. A profile inventories each dependency,
framework integration, and native boundary, then records a user-approved
disposition:

- direct conversion;
- retain the original library/runtime;
- retain it through an extension, FFI, or service boundary;
- map it to a named target-language equivalent;
- reimplement a small capability behind a contract;
- declare an external dependency;
- stub, exclude, or block it.

An agent can gather APIs, lockfile versions, licensing, transitive use, test
coverage, target-library candidates, and compatibility risks. It cannot
silently choose a replacement. A future conversion-decision workbench will
present those options and their evidence; the accepted result remains a
versioned profile record usable without the UI.

For example, a Python profile may retain a PyPI package or map it to a Rust
crate; a Delphi profile may retain, replace, or stub a database layer; a C
profile may preserve a C ABI boundary instead of translating a driver.

See [PLAN.md](PLAN.md) for the staged implementation plan.
