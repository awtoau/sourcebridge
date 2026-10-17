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
  -> generated target artifacts with provenance and explicit gaps
  -> source-side, target-side, and oracle validation evidence
  -> corpus census, diagnostics, and regression report
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
  emits and compiles Rust for the corresponding verified scalar addition facts.

This is deliberately a narrow end-to-end proof, not a claim that general
Delphi-to-Rust conversion or overflow behavior is validated.

Run the reproducible scalar-profile validation from this repository:

```text
python3 tools/validate_delphi_scalar_rust.py \
  --delphi-pack ../sourcebridge-delphi \
  --rust-pack ../sourcebridge-rust
```

### Arithmetic policy is visible and overridable

The same source `+` expression can mean different things in a real
application. Sourcebridge does not silently treat all arithmetic as either
legacy-compatible wrapping or modern safe arithmetic. A profile selects and
records the policy for each applicable rule family.

For the bounded FPC scalar fixture, measured source behavior is modular
32-bit addition. A compatibility profile may therefore emit:

```rust
pub fn add_signed(left: i32, right: i32) -> i32 {
    left.wrapping_add(right)
}
```

That preserves the measured source result at the tested overflow boundary, but
it is not necessarily a suitable business-domain policy. A project may instead
choose an explicit risk-accepting modernization override:

```text
arithmetic_policy: plain-rust-add
```

```rust
pub fn add_signed(left: i32, right: i32) -> i32 {
    left + right
}
```

This intentionally does **not** guarantee overflow equivalence: Rust `+`
depends on the selected overflow-check build settings. The generated artifact
and its conversion report must record that risk, the affected source span, and
the selected profile digest. Other explicit policies can emit `checked_add`
with an error or source-mapped panic, `saturating_add`, or a domain numeric
type.

Rules can use source facts to flag arithmetic for review. For example, a
declared Delphi `Currency` value, a money/domain type selected by a project,
or a database decimal field is a strong candidate for a decimal/domain-number
rule; it should not inherit an integer wrapping rule. A bounded counter,
bitmask, checksum, or protocol field may instead be a candidate for
compatibility wrapping or an explicitly sized integer.

Candidate detection is not intent inference. Variable names such as `total` or
`counter` alone never change generated semantics. Until a type, source
contract, project policy, or validated rule supports a choice, Sourcebridge
reports `modernization-risk: arithmetic-intent-unresolved` and blocks
automatic semantic rewriting.

## C# to Rust pack

[`sourcebridge-csharp`](https://github.com/awtoau/sourcebridge-csharp) preserves
the established C# to Rust system from
[`awtoau/csharp2rust`](https://github.com/awtoau/csharp2rust), including its
Roslyn frontend, Rust emitter/runtime, language and Renode DSL rules, corpus
adapters, generated artifacts, compiler census, and oracle assets.

Sourcebridge does not replace that system with fixture-only claims. Its pack
adapter invokes and persists the real corpus pipeline using the shared
structured validation contract. Current compiler or oracle gaps remain visible
as failed or skipped validation sides; they are never rewritten as success.

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

## Emission is mandatory; gaps are explicit

A source-only classification is not a conversion outcome. For every source unit
selected for conversion, a pack must attempt to emit a real target artifact.
The artifact must be attributable to its corpus revision, source unit,
resolved profile, rule revisions, generator version, and toolchain.

Supported semantics emit executable target code. Unsupported semantics must
be visible in a standard generated-source header and in the evidence database:

```rust
// SOURCEBRIDGE INPUT:
// - corpus revision: <revision>
// - source: <path>:<span>
//
// SOURCEBRIDGE RULES:
// - <rule revision and digest>
//
// SOURCEBRIDGE GAPS:
// - <category>: <source span>: <reason>
//
// SOURCEBRIDGE VALIDATION:
// - source: passed | failed | blocked | skipped
// - target: passed | failed | blocked | skipped
// - oracle: passed | failed | blocked | skipped
```

The exact comment syntax may follow the target language, but the semantic
fields are pack-independent. A gap must never silently delete source behavior,
and a placeholder must never claim equivalent behavior. If withholding a
required symbol prevents a valid target program, the result is
`blocked-on-target`, not an invented implementation.

## Data-driven rules, reusable interpreters

Rules own language mappings, output shapes, semantic policies, applicability,
required evidence, and validation tiers. For example, a generic C# rule may
declare that an explicit-interface member name is emitted with the
`declared-member-segment-snake-case` shape. A Delphi rule may select a
source-overflow policy or a Rust runtime helper.

The engine owns reusable algorithms: compiler/AST traversal, symbol binding,
safe target syntax construction, graph ordering, compilation, execution,
oracle replay, artifact hashing, and report generation. Sourcebridge must not
replace these algorithms with arbitrary code embedded in JSON, nor accumulate
corpus-specific `if` branches in the engine. The intended boundary is:

```text
versioned rule/profile data
  -> selects a named semantic policy or output shape
deterministic frontend/backend interpreter
  -> applies that policy to compiler-grounded facts
validation and corpus census
  -> prove, reject, or record the resulting behavior
```

## Full-corpus conversion census

Every conversion profile must support a corpus-level report. It is not a count
of files that looked translatable; it is an auditable inventory of what was
actually attempted:

- source units discovered, extracted, emitted, and withheld;
- generated artifact and header digests;
- source, target, and oracle outcomes;
- explicit gaps grouped by stable category, source span, rule/profile, and
  blocked dependency;
- compiler diagnostics grouped by stable failure signature while retaining
  individual occurrences and excerpts;
- source/target observable-output comparisons where both programs run; and
- differences from the prior report, so regressions cannot hide in totals.

`passed` means the declared validation tier passed. `failed`, `blocked`, and
`skipped` are first-class results, not omissions. Compilation proves
well-formed target syntax; it never by itself proves behavioral equivalence.

The core recorder persists the emitted artifact digest and body, each
source/target/boundary/oracle validation result, and explicit generated-code
gaps. Query its deterministic current database view with:

```text
sourcebridge report-conversion-census --database evidence/sourcebridge.db
```

Current pack status is intentionally narrow and truthful:

- the Delphi/FPC path has measured scalar arithmetic and Boolean-branch
  extraction/emission scopes; its source-only candidate discovery is not a
  conversion result;
- the preserved C# Renode pipeline runs against its pinned corpus and records
  its structured result, but the latest compiler census is 677 of 687 clean
  modules with 14 errors across 10 modules; and
- neither status is a claim of general Delphi-to-Rust conversion or complete
  C# Renode equivalence.

## Agent-native, versioned conversion engine

Sourcebridge is an interactive conversion engine operated primarily through
bounded agent work: one profile, source unit, rule family, diagnostic, test ID,
or evidence-backed work item at a time. Agents do not repair generated output
by hand. They propose or change rules, profiles, overlays, dispositions, and
tests; the engine regenerates artifacts and records the resulting evidence.

Every conversion-relevant decision uses this system: source-unit disposition,
profile selection, rule precedence, runtime adapter, diagnostic classification,
test/oracle verdict, and agent authority. A decision records its scope,
rationale, actor, evidence/test IDs, resolved profile and rule digests,
validation state, and supersession lineage. Conversation itself is not
conversion evidence, but a decision that changes behavior, certification, or
agent authority must be recorded.

### Immutable rules and historical conversions

A rule revision is never edited or deleted in place. Replacing it creates a new
immutable revision with an explicit supersession link; the earlier revision is
retired from future profile resolution but remains available for every prior
conversion, test, and diagnostic that used it. Storage retention is deliberate:
content digests and deduplicated artifacts may reduce duplication, but do not
erase history.

Profiles resolve an ordered, digest-pinned rule set. Customer or corpus policy
(for example Renode, Linux, or a comment-emission convention) is a versioned
overlay with declared precedence, not an untracked edit to stock language
rules. This makes a local override reviewable and prevents it from silently
changing unrelated conversions.

When a rule, overlay, profile, or toolchain changes, historical artifacts stay
reproducible under their original digest but can become
`stale_for_current_profile`. Sourcebridge must identify the affected scope and
require regeneration plus the declared full validation tier before calling the
new profile current. A focused test helps diagnose a change; it does not
recertify an affected corpus by itself.

The compact `sourcebridge report-staleness` report compares immutable
conversion/runtime/validation provenance links with a selected current profile.
It reports the resolved profile, rule, and declared toolchain digests, each
linked result's current/stale status, and the full validation tier required
before certification. It derives this view without rewriting historical
evidence; see [`docs/staleness.md`](docs/staleness.md).

Per-source-unit decisions can be queried from the same component-decision
records.  The prototype also provides deterministic annotated-review and
clean-release output helpers; clean output refuses unresolved boundaries and
writes a `.provenance.json` sidecar.  See
[`docs/dispositions-output.md`](docs/dispositions-output.md).

### Efficient, constrained agent operations

Agent-facing tooling must expose stable structured APIs and compact CLI output
for one work item, test, diagnostic, run, profile diff, rule impact, or
staleness query. This reduces token/context cost and makes interrupted work
resumable. When an agent finds a missing capability or expensive workflow, it
must report a bounded improvement proposal to the operator, including the
expected context/token reduction; it must not silently broaden scope or begin
authoring output outside the evidence/rule workflow. See the permanent
[agent-native operations tracker](https://github.com/awtoau/sourcebridge/issues/31)
and [immutable rule-history tracker](https://github.com/awtoau/sourcebridge/issues/32).

## Validation is two-sided

The pipeline checks the source before conversion and the target after emission.
Compiler warnings, linter findings, static-analysis results, target compiler
diagnostics, ABI/layout checks, tests, and differential oracles are all
evidence. Undefined, unspecified, or configuration-dependent source behavior
must be recorded under a concrete source variant; Sourcebridge must not invent
target semantics for it.

## Local model assistance

Local models may help prioritize gaps, generate negative cases, or draft a
bounded rule proposal. They never directly modify generated output, mark a
rule validated, or promote a proposal. Model classifications alone are not
conversion outcomes: an emitted target artifact and its deterministic
source/target/oracle validation remain required.

The first executable proposal stage is deliberately strict:

```text
sourcebridge model-digest
sourcebridge propose-rule evidence/unmatched-cluster.json \
  --model-digest sha256:<value printed by model-digest>
```

It permits only approved, locally installed models—defaulting to
`qwen3-coder:30b` with `qwen2.5-coder:14b` as a lower-cost fallback—on a
loopback Ollama endpoint, pins the response to its digest, limits the supplied
JSON evidence to 256 KiB, uses fixed generation options, schema-checks the
response, and writes the result only under `evidence/proposals/`. A staged
result is always labelled `unvalidated-llm-proposal`.

For related evidence tasks, keep one pinned worker session alive and process
the packages serially:

```text
sourcebridge propose-batch evidence/cluster-a.json evidence/cluster-b.json \
  --model-digest sha256:<value printed by model-digest> \
  --active-rule-digest <resolved-rule-digest> \
  --database evidence/sourcebridge.db
```

The batch report separates the first-request model load/warm-up measurement
from each request's elapsed and generation time. A weak or mismatched package
is retained below `evidence/proposals/rejected/`; rejected output never becomes
a rule. A successful package is only an evidence-gated,
`unvalidated-llm-proposal`, not a validated rule or generated artifact.

See [the local Ollama operating guide](docs/local-ollama.md) for the selected
baseline, fixed safety/evidence constraints, and the hardware/workload settings
users may measure and optimize without weakening proposal validation.

## Worker defaults and audit

Supported parallel work defaults to 24 workers, with an explicit `--workers`
override where the operation supports it. Run the canonical audit to see the
complete command inventory, the effective default, and every named exception
for an operation that is deliberately serial:

```sh
sourcebridge audit-workers
```

See [the worker-default policy](docs/worker-defaults.md) for the deterministic
one-worker/default contract and the evidence required before introducing
parallelism to an exception.

## Approved run control

The prototype operational control layer acts only on an existing queued
execution run and its persisted command contract. It does not accept an
arbitrary command or provide a scheduler:

```sh
sourcebridge launch --database evidence/sourcebridge.db RUN_ID
sourcebridge status --database evidence/sourcebridge.db RUN_ID
sourcebridge activity --database evidence/sourcebridge.db --run RUN_ID
sourcebridge stop --database evidence/sourcebridge.db RUN_ID \
  --reason "operator requested stop"
```

`status` exposes the selected corpus variant and observed worker-slot/source
unit boundaries. `activity` reads durable events and can be called again after
disconnecting; use `--since` with the last event timestamp for a compact
reconnect. A controlled stop moves a live run through `stopping`, signals the
approved process group, and finalizes it as `cancelled` without downgrading
workers that already reached a terminal state. This is CLI/API plumbing only;
the GUI remains a separate planned surface.

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

## Agent diagnosis queue

Agents can consume the current blocking queue, one item's immutable
provenance, and changes since a prior run without reading raw logs:

```text
sourcebridge report-work-queue --database evidence/sourcebridge.db
sourcebridge report-work-item --database evidence/sourcebridge.db WORK_ITEM_ID
sourcebridge report-changes --database evidence/sourcebridge.db --since RUN_ID
```

Reports require a completed run with complete immutable provenance and
run-scoped evidence links; stale or incomplete evidence fails clearly instead
of being presented as current. See
[docs/agent-queue.md](docs/agent-queue.md) for the compact JSON contract,
ownership/status rules, and append-only history guarantees.

## SQL evidence pipeline

Operational evidence is stored in SQLite using the portable contract in
[`core/schema/evidence.sql`](core/schema/evidence.sql). Initialize a database
and import the repository's legacy JSON records with immutable path and
SHA-256 provenance:

```text
sourcebridge init-db --database evidence/sourcebridge.db
sourcebridge import-json --database evidence/sourcebridge.db --root . --tracked-only
```

SQLite concurrency, transaction ownership, reader consistency, recovery, the
repeatable contention benchmark, and objective PostgreSQL escalation criteria
are documented in
[`docs/evidence-database-concurrency.md`](docs/evidence-database-concurrency.md).
An interrupted approved run can be made explicitly terminal with:

```text
sourcebridge recover-run --database evidence/sourcebridge.db RUN_ID \
  --reason "worker was interrupted"
```

## Engine-test orchestration

Sourcebridge runs language-pack fixtures and upstream suites through a common,
database-backed registry. The fixture source remains a normal versioned file;
SQL stores its stable numeric ID, expected structured result, hash, adapter
configuration, and every execution result.

Packs declare a version-controlled test catalogue. Import it before running:

```text
sourcebridge test import-pack --database evidence/sourcebridge.db \
  packs/sourcebridge-csharp.pack.json
sourcebridge test run --database evidence/sourcebridge.db 434
```

The catalogue stores pack-relative fixture paths, one pack-wide adapter
configuration, and each test's numeric ID, name, and expected structured
result. Import records the catalogue and fixture SHA-256 digests so its
operational SQL rows can be rebuilt on another checkout.

Run a case through its pack in one command:

```text
sourcebridge dev sourcebridge-csharp test 434 \
  --database evidence/sourcebridge.db \
  --pack-manifest ../sourcebridge-csharp/sourcebridge-csharp.pack.json
```

The `dev` command imports the declared catalogue before resolving and running
the requested stable test ID.

The command adapter does not invoke a shell. Pack adapters may wrap existing
C#, C, Delphi, Python, or Rust compiler suites by supplying their executable
argument vector. Add a dedicated adapter only when its reusable result
normalization needs exceed this contract.

Use `json-command` when a pack adapter emits a structured result envelope:

```json
{
  "schema_version": 1,
  "source": {"status": "passed"},
  "extraction": {"status": "blocked"},
  "target": {"status": "blocked"},
  "oracle": {"status": "skipped"}
}
```

The catalogue's expected result declares the same validation sides. A run
passes only when those side statuses match; its stored evidence always retains
the complete adapter envelope, so a blocked target cannot be mistaken for a
completed equivalence test.

The importer is idempotent. It retains every JSON record in SQL, projects
profiles, fact-grounded evidence, and rule proposals into typed tables where
the neutral contract applies, and keeps JSON schemas, manifests, examples, and
committed source evidence as portable audit inputs rather than deleting them.
Repeat `--root` to consolidate several Git pack repositories; `--tracked-only`
imports only committed JSON and records the Git remote as source provenance.
For example, the current Delphi and C# conversion evidence can be consolidated
without ingesting build products or uncommitted work:

```text
sourcebridge import-json --database evidence/sourcebridge.db --tracked-only \
  --root . \
  --root ../sourcebridge-delphi \
  --root ../sourcebridge-rust \
  --root ../../csharp2rust \
  --root ../sourcebridge-csharp
```
