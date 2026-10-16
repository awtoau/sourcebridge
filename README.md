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

### First executable pack path

The initial public packs are now available:

- [`awtoau/sourcebridge-delphi`](https://github.com/awtoau/sourcebridge-delphi)
  extracts the Stage-1 Delphi scalar fixture and validates it with Free Pascal
  3.2.3 on x86_64 Linux in Delphi mode.
- [`awtoau/sourcebridge-rust`](https://github.com/awtoau/sourcebridge-rust)
  emits and compiles Rust for the corresponding verified non-overflow scalar
  addition facts.

This is deliberately a narrow end-to-end proof, not a claim that general
Delphi-to-Rust conversion or overflow behavior is validated.

Run the reproducible scalar-profile validation from this repository:

```text
python3 tools/validate_delphi_scalar_rust.py \
  --delphi-pack ../sourcebridge-delphi \
  --rust-pack ../sourcebridge-rust
```

## The C# to Rust naming exception

`sourcebridge-csharp` will become the canonical C# to Rust pack. It will
preserve the full Git history of [`awtoau/csharp2rust`](https://github.com/awtoau/csharp2rust),
including the Roslyn frontend, Rust backend, C# language mappings, corpus
adapters, and validation evidence.

The existing `csharp2rust` repository remains authoritative until the
history-preserving move and reproducibility gates succeed. Only then may it
become a compatibility placeholder that directs users to `sourcebridge-csharp`;
it must not be emptied or redirected prematurely.

## Hybrid ports are normal

A profile may convert only selected components. It has a default target backend
and explicit component decisions for source boundaries:

```text
application
├── GUI                 retain in Python+Qt
├── device/control      translate to Rust
├── database layer      retain, replace, or bridge by user decision
└── reporting tool      exclude or translate later
```

Every non-direct component must name its disposition, rationale, and—where it
crosses a runtime boundary—a versioned contract. For example, Rust control
logic can expose a service/FFI/IPC boundary to a retained Python+Qt GUI. The
boundary has its own compatibility tests; the engine never pretends this is one
monolithic generated binary.

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

The first executable proposal stage is deliberately strict:

```text
sourcebridge model-digest
sourcebridge propose-rule evidence/unmatched-cluster.json \
  --model-digest sha256:<value printed by model-digest>
```

It permits only the approved, locally installed `qwen2.5-coder:14b` model on a
loopback Ollama endpoint, pins the response to its digest, limits the supplied
JSON evidence to 256 KiB, uses fixed generation options, schema-checks the
response, and writes the result only under `evidence/proposals/`. A staged
result is always labelled `unvalidated-llm-proposal`.

See [the local Ollama operating guide](docs/local-ollama.md) for the selected
baseline, fixed safety/evidence constraints, and the hardware/workload settings
users may measure and optimize without weakening proposal validation.

## Cross-pack committed-evidence reports

`report-packs` reads committed JSON evidence only below the supplied pack roots.
It never creates a database or modifies an input. Its deterministic JSON keeps
source validation, extracted C facts, target oracle manifests, current compiler
metrics, and staged/rejected local-model proposals separate. Every artifact
includes its root-name repository label, path relative to that root, SHA-256,
recognized kind, and source schema version. Files reached through overlapping
roots are reported once.

```text
sourcebridge report-packs ../sourcebridge-c ../sourcebridge-delphi \
  ../sourcebridge-csharp .
```

`compile_baseline.json` is deliberately not a current compiler metric. It
remains in `unrecognized_artifacts` with a historical kind and provenance.
Unrecognized or malformed JSON evidence is retained there too; non-JSON source
and build files are outside this committed-evidence report.

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

## The conversion-decision workbench

The planned GUI is a review/control plane. Its main view is a source-component
tree or dependency graph, coloured by disposition and validation status. When a
user selects a component, a detail pane shows:

- source facts, variant, tests, warnings, and call/dependency relationships;
- agent-proposed target/retain/bridge/stub options with licensing, API, risk,
  cost, and evidence;
- the chosen backend/runtime adapter and boundary contract;
- target build/lint/oracle results and unresolved blockers.

The user can compare profiles such as “Rust core + Python+Qt GUI” and “all
Python retained,” then approve a decision. The GUI writes a versioned profile
record; the CLI can reproduce the same conversion without the GUI.

See [the conversion-decision workbench](docs/conversion-decision-workbench.md)
for the option cards, user actions, and AI-guidance boundary.

## Reviewable diagnostic reports

The report API groups failures by stable signature without discarding their
individual source/target evidence:

```text
sourcebridge report-diagnostics --database evidence/sourcebridge.db
sourcebridge report-diagnostics --database evidence/sourcebridge.db \
  --signature "setter:unsupported-bit-range"
```

The first command produces groups and counts. The second prints the individual
occurrences, including source and generated excerpts, coordinates, diagnostic
message, profile-side validation reference, and raw evidence path. The
workbench consumes the same records for click-through review.

See [PLAN.md](PLAN.md) for the staged implementation plan.

## SQL evidence pipeline

Operational evidence is stored in SQLite using the portable contract in
[`core/schema/evidence.sql`](core/schema/evidence.sql). Initialize a database
and import the repository's legacy JSON records with immutable path and
SHA-256 provenance:

```text
sourcebridge init-db --database evidence/sourcebridge.db
sourcebridge import-json --database evidence/sourcebridge.db --root .
```

The importer is idempotent. It retains every JSON record in SQL, projects
profiles, fact-grounded evidence, and rule proposals into typed tables where
the neutral contract applies, and keeps JSON schemas, manifests, examples, and
committed source evidence as portable audit inputs rather than deleting them.
