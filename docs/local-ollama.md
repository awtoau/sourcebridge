# Local Ollama proposal stage

Sourcebridge uses local Ollama for high-volume candidate exploration, not for
automatic conversion approval.

## Allowed model

The initial approved model is `qwen2.5-coder:14b`, selected because it fits a
16 GiB local GPU with context headroom and demonstrated schema-conforming JSON
under fixed options. The engine resolves the model tag to a local digest before
each proposal and stores that digest with the result.

Cloud-routed tags, including any `:cloud` suffix, are rejected. Source or
corpus content must not leave the local host unless a future profile explicitly
records a different privacy policy.

## Chosen operating model

The model is a proposal worker, never an authority. It handles a batch of
similar evidence packages in one long-lived local session so the model load is
amortized. Each request remains independently reproducible: it records the
resolved digest, prompt-template version, evidence digest, generation options,
and raw response.

The worker must use a ground-or-abstain contract:

- weak evidence produces no positive pattern and no executable test;
- compiler-grounded evidence may produce a narrowly scoped candidate;
- source paths, symbols, inputs, observed results, compiler fields, and target
  claims must be copied exactly from the evidence;
- executable-test fields are reconstructed and checked deterministically, not
  trusted as free-form model text.

This policy is necessary because a model can produce schema-valid JSON while
still inventing a fixture path, test input, expected result, source variant, or
target semantic claim.

## What users may optimize

Users may optimize a local deployment for their own hardware, but must measure
quality and retain provenance before changing the selected configuration.

| Setting | Default or policy | Permitted optimization |
|---|---|---|
| Model | `qwen2.5-coder:14b` is the current approved baseline | Compare a candidate only with digest pinning, local-only transport, and the evaluation matrix below. |
| Worker lifetime | One loaded worker processes a related batch | Keep the process/model loaded when cold-start cost is unimportant. Record session load time separately from request time. |
| Reasoning level | Use the model's practical, measured setting | Do not select a deeper setting merely because it is available. It must improve evidence fidelity or readiness acceptance. |
| Context and output limits | 8,192 input tokens and 1,024 output tokens | Increase only for evidence packages that demonstrate truncation; preserve fixed limits for a measured comparison. |
| Parallelism | One proposal stream per loaded model | Prefer serial related requests over competing memory-heavy model loads. |
| Heavyweight model | Optional adjudicator only | Use only when it demonstrates a material quality gain over the primary worker; paging-bound throughput alone is not a reason to select it. |

The current local trial found that a grounding prompt improved abstention, but
neither larger models nor deeper reasoning reliably preserved all free-form
test fields. That result reinforces deterministic field validation rather than
model-size escalation.

## Prompt optimization is part of conversion

Prompt templates are versioned conversion inputs, not operator preferences.
Each template targets one task shape and source language, such as Delphi
compiler-test triage, C AST-gap analysis, or C# Roslyn-gap analysis. A worker
processes one such queue serially, with 10 requests as the normal batch size
and 20 as the maximum, then unloads before a queue for another language starts.

Every batch record must include the prompt-template version and digest, model
digest, fixed generation options, evidence digest, queue/session identifier,
per-request elapsed time, total batch time, and the deterministic validation
outcome. Retain raw responses for both staged and rejected records.

Tune a prompt only against a fixed, language-specific evaluation queue. Compare
it with the existing template using:

- literal evidence-reference fidelity and abstention precision;
- rate of deterministic validation pass and rejection;
- downstream proposal-readiness acceptance;
- request and batch wall-clock time, generated-token count, and local GPU
  time; and
- the resulting cost per evidence-grounded, ready proposal.

A new template may replace the current one only when it does not reduce
grounding fidelity or abstention quality and produces a measured improvement
for the declared task. It must not rely on corpus-specific invented facts,
weaken the validators, or bypass target/oracle validation. This makes prompt
optimization a cost-reduction mechanism for every conversion stage without
turning model output into an authority.

## Required evaluation before changing a model

Evaluate every candidate with the same pinned cases:

1. a weak-evidence case, where correct behavior is abstention;
2. a compiler-grounded case, where every literal reference can be checked; and
3. a repeated related-task batch, where session warm-up and per-request timing
   are recorded separately.

Record schema validity, abstention precision, literal-reference fidelity,
deterministic field-validation pass rate, proposal-readiness acceptance,
prompt-evaluation time, generation time, and generated-token count. Select a
primary worker from those measures rather than parameter count, apparent
fluency, or raw throughput.

## Request contract

A proposal request contains a bounded evidence package:

- one unmatched cluster and a small set of sibling exemplars;
- source language, corpus variant, and applicability context;
- source diagnostics/root causes and existing-rule digest;
- positive/negative context and requested target backend;
- fixed prompt-template version and fixed generation options.

The client uses temperature zero, an explicit seed, an 8,192-token context
window, a 1,024-token output ceiling, and a five-minute local request ceiling.
It sends the JSON schema in `core/schema/rule-proposal.schema.json` to Ollama's
structured-output interface.

## Result contract

Every response is stored as an `unvalidated-llm-proposal`. It includes proposed
positive and negative patterns, suggested tests, applicability, and open
questions. It cannot assert validation, committed status, or generated-output
ownership.

Promotion requires a separate reviewer action, rule/corpus checks, target
checks, and the strongest applicable oracle. A proposal client never writes
under generated-artifact directories.

The active optimization work is tracked in
[#12](https://github.com/awtoau/sourcebridge/issues/12). No inference may be
run merely to tune a configuration: every request must be an explicitly
authorized, local-only evaluation or proposal run.
