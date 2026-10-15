# Conversion-decision workbench

The workbench helps a user decide what happens to each source component and
dependency. It is not a “translate all” button and it is not an autonomous
architecture tool.

The workbench is the intended primary user interface. The command-line tools
exist for automation, code review, CI, and exact reproduction of a saved GUI
decision; they read and write the same versioned profile/decision records and
must never form a second decision model.

## Workspace layout

### Entry page: configure a conversion

The entry page guides a user through a reproducible run instead of exposing a
blank editor:

```text
1. Select source corpus and pinned revision
2. Select or create a concrete variant
   - compiler/dialect, ABI, configuration, generated inputs
3. Select source frontend
4. Select target backend and runtime adapters
5. Choose an existing profile or create a profile draft
6. Review source-side check status
7. Start analysis, not emission
```

At each choice, the page shows compatible packs, unavailable combinations,
required toolchains, existing evidence, and the consequences of the selected
target. A user can start with “retain everything except this component” as
readily as “translate the validated core.”

### Main page: project and conversion tree

The main page is a split workspace:

```text
┌ Project tree ─────────────┬ Conversion detail ────────────────┬ Evidence/options ──┐
│ Application               │ selected: DeviceControl            │ proposed choices   │
│ ├── GUI      retained     │ source facts and dependency graph  │ direct Rust        │
│ ├── Core     Rust ✓       │ selected profile/component decision│ retain/bridge GUI  │
│ ├── Database blocked      │ source ↔ target artifact/diff      │ replace adapter    │
│ └── Tests    source only  │ validation timeline and contracts  │ missing facts      │
└───────────────────────────┴───────────────────────────────────┴────────────────────┘
```

Every tree node shows compact, filterable badges:

- disposition and target backend;
- source and target validation state;
- coverage and rule-instance count;
- complexity/risk band;
- source warning, undefined-behavior, or unresolved-gap count;
- boundary/contract status.

Selecting a node never generates code implicitly. It opens its facts,
dependencies, selected decision, generated artifacts if any, and evidence.

### Reports page: find the work that matters

Reports operate over the same data as the tree and support sorting/filtering by:

- source coverage, direct-translation coverage, and explicit-disposition
  coverage;
- complexity, dependency fan-in/fan-out, and estimated migration effort;
- validation tier, failing source checks, failing target checks, and oracle
  disagreement;
- undefined/unspecified/configuration-dependent source behavior;
- rule reuse, unmatched-cluster size, and negative-match confidence;
- target backend, runtime adapter, profile, source variant, owner, or issue;
- candidate cost/risk and user decisions awaiting approval.

Selecting a report row navigates back to the exact tree node and its source,
target, rule, profile, and validation evidence.

### Failure drill-down: from a count to the actual lines

Aggregate counts are navigation aids, never the only evidence. A row such as
`254 failed bit setters` groups occurrences by a stable failure signature, but
the count is always computed from immutable diagnostic occurrences.

Selecting that row opens a review queue with one occurrence per affected source
span. The queue supports previous/next navigation and filters for variant,
profile, backend, source path, diagnostic phase, severity, and review status.
Each occurrence shows side-by-side:

```text
source file and exact lines       emitted target lines (when generation occurred)
compiler/parser fact              validator/tool message and diagnostic code
source revision + variant         profile, rule-layer, generator, and artifact digests
raw source/target/oracle logs     prior review and linked work item
```

The exact source and generated excerpts are immutable evidence fragments,
addressed by digest and source coordinates. A later source revision may add a
new occurrence but cannot rewrite what a reviewer saw.

The reviewer can mark an occurrence as a known converter gap, accepted source
behavior, target constraint, false grouping, needs investigation, or resolved,
with a rationale. The workbench writes an append-only review record and can
open or link a claimable work item. It never turns a review click into a rule
change or generated-artifact edit.

## The decision card

For a selected module, package, service, GUI area, driver, or framework
boundary, the workbench displays a decision card:

| Field | What the user sees |
|---|---|
| Source evidence | source spans, corpus variant, compiler/linter findings, tests, call/dependency graph |
| Current status | direct, retained, bridged, replaced, stubbed, excluded, blocked, or undecided |
| Candidate choices | target backends, runtime adapters, target libraries, boundary shapes, and explicit “do not convert” options |
| Consequences | APIs affected, licensing, platform/ABI constraints, estimated work, validation coverage, and unresolved risks |
| Required proof | source checks, target checks, boundary tests, oracle/trace tier, and human review conditions |
| Recorded choice | profile decision, rationale, owner, rule/runtime versions, and validation results |

The primary actions are:

1. **Translate directly** to a named backend.
2. **Keep the component** in its existing runtime.
3. **Bridge it** through FFI, IPC, a service, or an extension.
4. **Replace its runtime/library** with a named, versioned target equivalent.
5. **Reimplement a bounded capability** behind a contract.
6. **Stub, exclude, or block** it with an explicit reason.

## AI-guided, user-approved

A local model receives a bounded evidence package, never an unbounded
repository dump:

- selected component and source/dependency graph;
- corpus variant and source diagnostics;
- tests and observable behavior;
- currently selected profile and applicable rules;
- candidate target packages/adapters gathered by deterministic tooling;
- prior decisions and negative cases.

It returns schema-constrained option cards. Every option must state:

```text
choice: direct | retain | bridge | replace | reimplement | stub | exclude | block
target/backend/adapter: named identifiers or none
evidence: source facts, package/API references, compatibility constraints
benefits and risks: concrete, not generic praise
missing information: what cannot be inferred
validation plan: exact checks required before acceptance
confidence: proposal confidence, never validation confidence
```

The model may rank options and explain trade-offs. It cannot choose one, write
generated output, mark an option validated, or hide the retain/stub/block
choices. A user approval produces a versioned profile/component decision; the
normal compiler, linter, contract-test, and oracle pipeline then determines
whether that decision is viable.

## Example: retain a GUI while porting the core

For a Delphi application, the agent might identify a GUI unit and deterministic
device-control units:

| Component | Proposed choice | User decision | Required validation |
|---|---|---|---|
| GUI forms/events | retain Python+Qt or current GUI runtime | retain Python+Qt | UI/service contract tests |
| device/control | direct Rust translation | Rust | source/Rust differential tests |
| database adapter | bridge, replace, or retain | choose driver/profile | transaction/error integration tests |

The resulting profile records a `retain_runtime` GUI decision with a
`ui-service-v1` boundary contract and a `direct_translation` decision for the
Rust core. It remains possible to port the GUI later without rewriting the
validated core decision.
