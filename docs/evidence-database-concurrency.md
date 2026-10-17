# Evidence database concurrency policy

**Decision record — 2026-10-17:** retain SQLite for the supported local
workflow. The repeatable contention result is retained at
`evidence/concurrency/sqlite-contention-2026-10-17.json`. It exercised eight
independent writer processes and 200 mutations; all 200 rows committed, with
no exhausted busy retries. The measured p95 writer transaction time was
12.342 ms and p99 was 937.17 ms. This is evidence for the current local
workload, not a claim that SQLite is suitable for a shared network filesystem
or an unbounded service.

## Supported location and journal policy

- The database is the path supplied to `--database`; the repository default is
  `evidence/sourcebridge.db`. It must be on a local filesystem. A database
  opened through NFS, SMB, or another multi-host filesystem is unsupported.
- `sourcebridge init-db` enables SQLite WAL mode, `synchronous=NORMAL`, and a
  1,000-page automatic checkpoint threshold. WAL allows report readers to
  continue while the single writer commits.
- Automatic checkpoints are sufficient for normal operation. Operators may
  run a passive checkpoint during maintenance with
  `sourcebridge.database.checkpoint(path, mode="PASSIVE")`; passive is
  preferred because it does not wait for readers. Do not delete `-wal` or
  `-shm` files while a process is using the database. Copy all three files
  (or use SQLite backup) when taking a live backup.
- The database and its WAL sidecars are one unit. An interrupted writer is
  rolled back by SQLite; it does not leave a committed half-transaction.

## Transaction ownership and serialization

`sourcebridge.database.write_transaction()` is the only supported mutation
boundary. It:

1. sets foreign-key enforcement and a 5,000 ms SQLite busy timeout;
2. acquires `BEGIN IMMEDIATE` before yielding the connection, making SQLite's
   one-writer rule explicit; and
3. retries a busy writer lease four additional times with bounded exponential
   backoff (25, 50, 100, and 200 ms) before raising an actionable
   `SQLiteBusyError`.

The transaction owns every related row for one logical operation and is kept
short. External commands and model/tool work run before their result is
inserted; they are never run while a database writer lease is held. A busy
failure is safe to retry because the lease is acquired before the mutation.
If the bounded retries are exhausted, wait for the active writer to exit and
rerun the command. A traceback or CLI error containing the database path and
`SQLite writer remained busy` is an operational failure, not evidence to
import.

Supported commands that may be invoked concurrently are serialized by the
same policy:

| Command/workflow | Database behavior |
| --- | --- |
| `init-db` and first-use schema migration | serialized schema initialization |
| `import-json` | one atomic import transaction per root |
| `test register`, `test import`, and test result recording | serialized registry/result writes |
| `profile register-rule`, state changes, certification, create, and promote | serialized append-only lifecycle writes |
| `run-approved` event and final-status writes | one short transaction per event; only one process may claim a queued run |
| `launch`, `status`, and `activity` | launch claims one queued run; status/activity use read-only snapshots |
| `stop` | serialized transition to `stopping`/`cancelled`, stop event, and process-group signal |
| `recover-run` | serialized cancellation and stop event |
| `report-diagnostics`, profile resolve/list, and execution activity | read-only snapshot; never a writer |

Proposal generation and pack reporting write files, not the evidence
database. Direct ad-hoc SQLite writes are outside the supported model.

## Reader semantics

`read_transaction()` opens a read-only connection, sets `query_only=ON`, and
starts one explicit deferred transaction. Every report query therefore sees a
single committed SQLite snapshot and cannot accidentally create or mutate the
database. Historical execution activity reports include only `passed`, `failed`,
or `cancelled` runs with a non-null `completed_at`. The operational
`sourcebridge activity` command explicitly opts into queued/running/stopping
runs and reads the same durable events for reconnectability; it does not turn
active work into completed evidence. A terminal run without a completion
timestamp (or a non-terminal run with one) is rejected as an ambiguous state
instead of being reported.

`run-approved` claims a queued run with a conditional update. A second process
cannot execute the same run: it receives an actionable single-owner error.
If a process is killed after claiming a run, use:

```sh
sourcebridge recover-run --database evidence/sourcebridge.db RUN_ID \
  --reason "worker was interrupted"
```

Recovery marks the run `cancelled`, stops active workers, and records a
stopped event. The run then has a complete terminal record and is visible as
cancelled activity; it must not be treated as a successful validation.

## Repeatable contention check

Run the retained benchmark on an isolated database:

```sh
PYTHONPATH=. python tools/sqlite_contention_benchmark.py \
  --database evidence/sqlite-contention-benchmark.db \
  --workers 8 --writes-per-worker 25 --hold-ms 1
```

The benchmark creates and removes a probe table, starts independent processes
at the same time, records per-transaction wait time, and verifies that the
committed row count equals the expected mutation count. Its JSON output should
be retained with the run evidence when used for an escalation decision. The
tracked 2026-10-17 run is the baseline for this policy.

The explicit `--workers 8` preserves that historical evidence run. The routine
default is now 24; run `sourcebridge audit-workers` to verify the Python API
and CLI defaults. Worker count is an input override, not a license to change
the rows, errors, or success disposition.

## PostgreSQL escalation criteria

Do not migrate merely because multiple contributors exist. Escalate only when
at least one objective criterion below is met and reproduced in **three**
representative 15-minute measurements (unless the multi-host criterion
applies):

1. **Deployment topology:** a supported workflow must write from more than one
   host, or the database must live on NFS/SMB/shared storage. This is an
   immediate escalation; SQLite remains local-only.
2. **Lock contention:** more than 1% of mutation attempts still fail after the
   configured timeout and four retries, or p95 writer-lease acquisition
   exceeds 500 ms or p99 exceeds 2,000 ms.
3. **Write rate:** sustained committed mutation throughput exceeds 200
   transactions/second for 15 minutes, or the writer queue remains above eight
   concurrent agents for that period.
4. **Retention/query envelope:** the database exceeds 10 GiB, execution events
   exceed 10 million retained rows, or a complete execution report has p95
   latency above 2 seconds after `ANALYZE` and a passive checkpoint.

The retained baseline meets none of these criteria: it is local-only, has zero
failed mutations, p95/p99 below the limits, and 123.3 measured transactions
per second (200 / 1.622 seconds).

If a criterion is met, the adoption record must include the measured evidence
and a migration plan before changing the default:

1. stop new mutations and take a SQLite backup including WAL state;
2. verify row counts, foreign keys, schema version, and all content digests;
3. map every table and check constraint to a versioned PostgreSQL migration,
   preserving IDs, timestamps, append-only history, and evidence digests;
4. load into an isolated PostgreSQL database and compare deterministic reports
   and per-table counts;
5. run a dual-read/canary period while SQLite remains the rollback source;
6. document `DATABASE_URL`, role/credential ownership, backups, retention,
   lock/transaction behavior, and rollback steps; then
7. switch writers only after parity checks pass and retain the immutable
   pre-migration SQLite backup.
