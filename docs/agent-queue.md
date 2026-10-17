# Agent diagnosis queue

Sourcebridge exposes three compact JSON reports for an agent that needs to
resume bounded conversion work:

```sh
sourcebridge report-work-queue --database evidence/sourcebridge.db
sourcebridge report-work-item --database evidence/sourcebridge.db WORK_ITEM_ID
sourcebridge report-changes --database evidence/sourcebridge.db --since RUN_ID
```

`--run RUN_ID` pins all three reports to a specific completed run. Without it,
the queue and evidence reports select the newest completed run. The changes
report selects the newest completed run as its current side. Agents should use
the three canonical commands above so recorded workflows remain unambiguous.

## Contract

Each result is one JSON object with `schema_version: 1`:

- **work queue** contains the run identity and only `open`, `claimed`,
  `in_progress`, or `blocked` items. Each item has a stable key, summary,
  explicit status/disposition, ownership scope, optional owner, and compact
  evidence counts plus artifact paths.
- **work-item evidence** contains one stable item, its append-only state, the
  run's source/input/variant/profile/rule/artifact/evidence digests, source
  unit and decision provenance, and the exact diagnostic, validation, artifact,
  and immutable fragment records linked to that run.
- **changes** compares stable item snapshots and reports added, removed,
  changed, and unchanged items. It also reports which run identity digests
  changed (`input`, `variant`, `profile`, `rule`, `artifact`, and `evidence`).

Work-item state and evidence links are append-only. A worker may claim an item
by recording a new state with its owner and bounded `ownership_scope`; it must
not rewrite the old state or relink old evidence to a new run. The stable work
item ID is the claim key. The small Python API helpers
`record_run_provenance`, `record_work_item_state`, and
`link_work_item_evidence` use the same serialized writer transaction as the
rest of the evidence pipeline.

## Trust and failure behavior

Reports never silently fall back to an old successful diagnosis. The selected
run must have:

1. a terminal `passed` or `failed` execution status and `completed_at`;
2. an immutable run-provenance row with `validation_state: "complete"` and
   all required digests; and
3. an evidence link for every item returned or requested.

Missing provenance, an incomplete/stale validation state, a non-terminal run,
or missing evidence produces a non-zero CLI result and an actionable stderr
message. A cancelled or interrupted run must be recovered before it can be
used as current evidence. A prior run may have different input/profile/rule
digests; those differences are reported as changes, while a run explicitly
marked `stale` is rejected as a source of current evidence.
