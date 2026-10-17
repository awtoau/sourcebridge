# Worker-default policy

Sourcebridge has one canonical worker audit:

```sh
sourcebridge audit-workers
```

The output is stable, machine-readable JSON. It contains the complete supported
operation inventory, the effective routine default, every deliberate exception,
and checks for the defaults implemented by the parallel benchmark API and CLI.
`sourcebridge worker-audit` is a compatibility alias.

## Default

An operation with a parallel stage defaults to **24 workers**. A caller may use
the operation's documented worker override, such as:

```sh
python3 tools/sqlite_contention_benchmark.py \
  --database evidence/sourcebridge.db \
  --workers 1
```

The override is explicit; it does not change the canonical default. Worker
count must not change the result: one worker and 24 workers must produce the
same records, errors, ordering, and success disposition. Timing and contention
metrics are measurements and are allowed to differ.

The only currently supported operation with a parallel stage is
`sqlite-contention-benchmark`, implemented by
`sourcebridge.concurrency.run_contention_benchmark` and exposed through
`tools/sqlite_contention_benchmark.py`. Both the Python API and its `--workers`
argument are checked by the audit.

## Deliberate exceptions

An exception is not an implicit worker default. It is named in the audit with
the reason it exists and the evidence required before parallelism can be
introduced.

| Exception | Operations | Reason |
| --- | --- | --- |
| `no-parallel-stage` | `sourcebridge report-diagnostics`, `report-packs`, `report-toolchains`, `evaluate-proposal`, `init-db`, `import-json`, all `sourcebridge profile` commands, and `sourcebridge test register/import/import-pack/list` | The current engine stage is serial and has no independently schedulable work items. |
| `single-model-request` | `sourcebridge propose-rule`, `sourcebridge model-digest` | Each operation makes one local model request or inspects one model identity. |
| `single-owner-execution` | `sourcebridge run-approved`, `launch`, `stop`, `recover-run`, `test run`, `dev test`, and `sourcebridge-workbench` | The current execution contract has one owner for a run or one external action. |
| `legacy-manifest-validation` | `python3 cli/sourcebridge.py validate-pack`, `validate-profile`, and `resolve-profile` | These legacy commands validate small manifests and do not own a conversion stage. |
| `single-process-validation` | `python3 tools/validate_delphi_scalar_rust.py` | The fixture validator is dependency-ordered and produces one generated artifact and one target execution. |

For each exception, the audit's `revisit_evidence` field describes the
required measured change. In particular, do not add speculative parallelism to
a serial engine stage merely to make its worker count 24.

## Verification

The canonical audit is the lightweight regression check for this policy:

```sh
PYTHONPATH=. python -m sourcebridge.cli audit-workers
```

It fails if the benchmark API or CLI regresses to an implicit value other than
24. It also reports all exceptions rather than silently treating them as
defaults. The full suite checks the audit output, both public defaults, and the
one-worker/default success contract.
