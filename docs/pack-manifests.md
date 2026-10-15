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
