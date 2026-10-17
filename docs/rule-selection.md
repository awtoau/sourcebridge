# Next-rule selection

The prototype exposes a read-only selector. It returns one JSON object and
never calls an LLM, creates a rule, or promotes a rule:

```sh
sourcebridge select-next-rule blocker/coverage --database evidence/sourcebridge.db
sourcebridge select-next-rule completion --database evidence/sourcebridge.db \
  --profile-id project-profile-123
sourcebridge select-next-rule basics --database evidence/sourcebridge.db \
  --pack-manifest packs/demo/sourcebridge-demo.pack.json
```

`blocker/coverage` ranks unresolved queue items by distinct impacted source
units. `completion` chooses the first source unit with the smallest explicit
gap: disposition, then current validation. `basics` reads the first ordered
entry from the pack's `entrypoints.curriculum` JSON file. Each result includes
the strategy, candidate, evidence and test references, expected profile scope,
and reason.

The selector reuses the queue, diagnostic, disposition, validation, and
provenance tables; it does not add a selection table or mutate evidence.
