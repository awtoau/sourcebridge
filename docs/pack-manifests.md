# Pack manifests and profiles

Every independently versioned Sourcebridge pack has a manifest validated
against `core/schema/pack-manifest.schema.json`.

The key separation is intentional:

- a **frontend** provides source languages and no target environment;
- a **backend** provides target environments and no source language;
- a **corpus** pack supplies neither generic language semantics nor a target;
- a **runtime** pack provides adapters selected by a profile.

A conversion profile validates against `core/schema/profile.schema.json`. It
names one source frontend, optional corpus pack, one target backend, ordered
rule layers, runtime adapters, and profile options. It is the only place that
joins a source language to a target environment.

For example, a Delphi frontend and a Python+Qt backend are independent packs:

```json
{
  "schema_version": 1,
  "id": "delphi-to-python-qt",
  "source_frontend": "sourcebridge-delphi",
  "target_backend": "sourcebridge-python-qt",
  "rule_layers": ["delphi-core"],
  "runtime_adapters": ["qt-sql"]
}
```

The same `sourcebridge-delphi` pack can appear in a `delphi-to-rust-crt`
profile with a Rust character-mode backend and different runtime adapters.
Likewise, `sourcebridge-python` can select the same Rust backend while retaining
a Python component through a declared boundary.

Resolve a profile against the available pack directory before running a
conversion:

```text
python3 cli/sourcebridge.py resolve-profile \
  examples/delphi-to-rust-crt.profile.json --packs-dir examples
```

Resolution verifies that the declared source pack is a frontend with a source
language and the declared target pack is a backend with a target environment.

## Engine-test catalogues

A pack that owns runnable fixtures declares a version-controlled catalogue with
an `engine_tests` entrypoint:

```json
{
  "schema_version": 1,
  "id": "sourcebridge-csharp",
  "kind": "frontend",
  "version": "0.1.0",
  "provides": {
    "source_languages": ["csharp"],
    "target_environments": []
  },
  "entrypoints": {
    "engine_tests": "engine-tests/catalogue.json"
  }
}
```

The catalogue declares stable numeric case IDs, pack-relative fixture paths,
the pack's adapter configuration, and expected structured results. Its source
files remain in the pack; Sourcebridge imports their hashes and run evidence
into SQL:

```text
sourcebridge test import-pack --database evidence/sourcebridge.db \
  path/to/sourcebridge-csharp.pack.json
sourcebridge test run --database evidence/sourcebridge.db 434
```
