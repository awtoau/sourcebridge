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

## Request contract

A proposal request contains a bounded evidence package:

- one unmatched cluster and a small set of sibling exemplars;
- source language, corpus variant, and applicability context;
- source diagnostics/root causes and existing-rule digest;
- positive/negative context and requested target backend;
- fixed prompt-template version and fixed generation options.

The client uses temperature zero, an explicit seed, and a bounded context
window. It sends the JSON schema in `core/schema/rule-proposal.schema.json` to
Ollama's structured-output interface.

## Result contract

Every response is stored as an `unvalidated-llm-proposal`. It includes proposed
positive and negative patterns, suggested tests, applicability, and open
questions. It cannot assert validation, committed status, or generated-output
ownership.

Promotion requires a separate reviewer action, rule/corpus checks, target
checks, and the strongest applicable oracle. A proposal client never writes
under generated-artifact directories.
