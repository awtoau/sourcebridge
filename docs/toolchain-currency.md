# Toolchain currency reports

Toolchain currency is an evidence surface, not a conversion claim.  A pack
adapter records four separate observations:

- **installed**: tools and runtimes visible on the host;
- **declared**: versions requested by a pack manifest or project file;
- **resolved**: versions selected by the dependency resolver/lock data; and
- **latest**: versions visible from the declared package source, or an explicit
  unavailable result when that source was not queried.

The engine does not infer that an installed compiler can parse a frontend, that
an updated dependency preserves behavior, or that either can emit a target
language.  Reports therefore carry
`conversion_capability.status: "not_claimed"` and the field is validated.

## Engine report surface

Save a pack adapter's JSON output as `toolchain-report.json` (or
`<name>.toolchain.json`) and aggregate one or more pack roots:

```text
sourcebridge report-toolchains packs evidence
```

The output is deterministic for the bytes supplied by the adapters and
contains SHA-256 digests for each report.  It is safe to run without a
database; the command is read-only.

## Upgrade decisions

A newer `latest` observation is not an upgrade proposal.  If a pack proposes
an upgrade, the proposal must include:

1. compatibility evidence;
2. a validation command and its result; and
3. a deliberate `decision.status` of `adopt` or `defer`, plus a non-empty
   evidence reference.

The validator rejects proposals without all three.  A deferred upgrade remains
visible and is not silently retried or treated as an adopted dependency.

The C# pack supplies the pack-specific discovery commands and documents its
Roslyn/.NET/NuGet boundaries in its own repository.

## Current pack scope

The report contract is deliberately broader than the packs that currently have
full discovery adapters:

- **C** records the installed Clang identity.  It does not compare updates
  until a C frontend and pinned corpus variant exist.
- **Delphi** records local compiler availability only.  Dialect, compiler, and
  runtime selection remain the separate decision tracked by #97.
- **Rust** records the target/emitter toolchain version used by a backend.
- **C#** records the installed .NET SDK/runtimes and the Roslyn frontend's
  declared, resolved, and latest NuGet facts in `sourcebridge-csharp`.

An empty or unavailable observation is evidence about the current host or
pack, not evidence that a conversion path is supported.
