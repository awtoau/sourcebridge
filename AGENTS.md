# Agent contract (read this first)

Read this one file before changing anything. It exists so you do not re-derive
the architecture or invent parallel mechanisms. Doing so wastes tokens and
breaks the design.

## Invariants (non-negotiable)

1. **All data lives in the database** (`core/schema/evidence.sql`). Never store
   structured, queryable data as a JSON blob column. JSON is allowed only for
   versioned *rule/profile definitions* (git files that diff as commits) and raw
   model I/O. If data fits in columns/rows, it is columns/rows.
2. **Most behavior is data, not code.** Versioned rule/profile data selects a
   named policy or output shape; a deterministic interpreter applies it. Do not
   hardcode language policy, and do not add corpus-specific `if` branches to the
   engine or a pack emitter.
3. **Profiles are the only source→target join** (`examples/*.profile.json`,
   `core/schema/profile.schema.json`).
4. **Complex steps are isolated scripts with a clear API** — the `json-command`
   adapter envelope `{source, extraction, target, oracle}`. A pack provides an
   adapter and its data; it does not generate its own reports.
5. **Reporting is an engine capability.** Use the engine commands below. Never
   write a pack-specific HTML/JSON report.
6. **Evidence only, and compilation is not completion.**
   `passed/failed/blocked/skipped` are first-class. A rule's lifecycle is
   `unimplemented → implemented → validated`. **`implemented` (target compiles)
   is an incomplete interim state, never a deliverable** — compilation never
   proves equivalence. A rule with observable behavior is complete only when an
   **executable oracle** (runs source and generated target, compares output)
   passes, i.e. `validated`. A file may be partial.
7. **Local models (Ollama) are a non-authoritative proposal worker — never a
   rule author, approver, or oracle.** Read
   [docs/local-ollama.md](docs/local-ollama.md) before *any* model use; do not
   re-derive this. The hard rules (all have been litigated and tested):
   - **Two surfaces, both non-authoritative.** (a) Supplemental *annotation /
     planning* (`tools/inventory_engine_tests.py`) returns only fixture
     feature labels and **cannot propose or author rules**. (b) *Proposal
     drafting* (`sourcebridge propose-rule` / `propose-batch`) emits
     `unvalidated-llm-proposal`s under a ground-or-abstain, evidence-anchored
     contract. **No command in either path writes a rule revision, generated
     output, or promoted profile.** Promotion is a separate human lifecycle
     decision; the DB trigger rejects model-authored rule rows without review.
   - **The model drafts; the orchestrator does not.** Do **not** hand-author a
     rule's `output_shape`/semantics and pass it off as model work, and do
     **not** call the model API ad hoc. Use the sanctioned tools with the
     versioned prompt templates and the schema
     `core/schema/rule-proposal.schema.json`.
   - **Approved model: `qwen3-coder:30b`** (provisional default);
     `qwen2.5-coder:14b` is the approved lower-cost fallback. Resolve the tag to
     the exact observed local digest — **never guess it**. Reject `:cloud` /
     cloud-routed tags; source/corpus content stays on the local host.
   - **No model-size escalation, no unbounded self-play.** The measured trial
     found larger/deeper models did *not* reliably improve field fidelity, so
     the answer is deterministic field validation, not swapping to a bigger
     model. The only loop is: deterministic evidence → draft → evidence-citing
     critic → **at most one** repair → deterministic compiler/schema/oracle gate
     → human lifecycle decision. Fixed generation: temperature 0, explicit seed,
     8,192-token context, 1,024-token output ceiling, 5-minute local ceiling.
   - **Retain full provenance** per request: resolved model digest,
     prompt-template version/digest, evidence digest, raw response, and the
     Ollama counters `prompt_tokens`/`generated_tokens` (never estimated).
     Orchestrator usage is a separate `agent_usage` record.

## Add or change a rule (the only sanctioned path)

Do **not** edit an emitter in Python to add a mapping. The database governs
emission: only rule revisions whose latest state is `active` are resolved and
passed to the adapter. The deterministic *mechanics* below are how approved
rule data enters the DB and governs emission — they are **not** a licence for
you to invent the rule's semantics by hand. Non-trivial `output_shape` content
is drafted by the local-model **proposal worker** (invariant 7,
[docs/local-ollama.md](docs/local-ollama.md)), gated deterministically, and
promoted by a human decision. Then:

1. Scaffold the rule definition **data** and register it as a draft in one step:
   ```
   sourcebridge scaffold-rule --database DB --rule-key KEY \
     --source-frontend PACK --fact-kind FACT --rules-dir PACK/rules \
     --body "{op0}...{op1}" --rationale "..." --created-by you
   ```
   (Or hand-write `rules/KEY.json` and `profile register-rule`.) The interpreter
   applies `output_shape`; it has no hardcoded policy.
2. Add a fixture and its expected engine-test result (data).
3. **Add an executable oracle** for any rule with observable behavior: a
   runnable source program fixture and the matching generated-target harness
   that compares normalized output. This is mandatory, not optional — a rule
   stuck at `implemented` is unfinished work, not a result.
4. Activate it (append-only state event; this is what makes it govern emission):
   ```
   sourcebridge profile set-rule-state REVISION_ID active --database DB \
     --rationale "..." --decided-by you
   ```
5. Record and read evidence through the engine (never a pack report):
   ```
   sourcebridge record-catalogue-census --database DB --profile PROFILE.json \
     --catalogue PACK/engine-tests.json --curriculum PACK/rule-curriculum.json
   ```
   The report marks a rule `complete` only when `validated`; `needs_oracle:true`
   flags an active rule that compiles but is not yet behavior-proven.

See [docs/rule-change-workflow.md](docs/rule-change-workflow.md). Process
deviations need a `[port-process-change]` issue and Dan's approval.

## Token-cheap commands (prefer these; avoid dumping large reports)

```
sourcebridge report-rules --database DB --profile-name NAME        # compact per-rule status (~1 KB)
sourcebridge report-conversion-census --database DB                # aggregate census
sourcebridge report-diagnostics --database DB [--signature SIG]    # grouped gaps
```

Do not paste or regenerate full multi-hundred-line reports to check status; use
`report-rules`. A "report" is a read-only query over the database, not a file.

## Authoritative design docs (read before touching the relevant area)

Durable decisions live in version-controlled docs, **not** in session
checkpoints or merged-PR descriptions. If a decision is not written in one of
these, it is at risk of being lost and re-derived incorrectly — add it here.

- [docs/local-ollama.md](docs/local-ollama.md) — local-model proposal workflow,
  approved models/digests, ground-or-abstain contract (see invariant 7).
- [docs/rule-change-workflow.md](docs/rule-change-workflow.md) — required
  process to add/change/retire a rule; "compilation is not completion."
- [docs/rule-selection.md](docs/rule-selection.md) — `select-next-rule`
  strategies (`blocker/coverage`, `completion`, `basics`).
- [docs/pack-manifests.md](docs/pack-manifests.md) — pack manifest contract,
  entrypoints, and the profile source→target join.
- [docs/worker-defaults.md](docs/worker-defaults.md) — canonical 24-worker
  default and the named parallelism exceptions.
- [docs/evidence-database-concurrency.md](docs/evidence-database-concurrency.md)
  — SQLite single-host concurrency/transaction policy.
- [docs/staleness.md](docs/staleness.md) and
  [docs/toolchain-currency.md](docs/toolchain-currency.md) — stale-evidence and
  toolchain-currency policy.
- [docs/dispositions-output.md](docs/dispositions-output.md),
  [docs/conversion-decision-workbench.md](docs/conversion-decision-workbench.md),
  [docs/agent-queue.md](docs/agent-queue.md) — disposition classes, workbench,
  and agent queue.
