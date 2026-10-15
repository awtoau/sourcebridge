# Conversion-decision workbench

The workbench helps a user decide what happens to each source component and
dependency. It is not a “translate all” button and it is not an autonomous
architecture tool.

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
