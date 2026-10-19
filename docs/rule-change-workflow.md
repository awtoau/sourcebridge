# Rule-change workflow

This is the required process for adding, changing, enabling, or retiring a
conversion rule, output shape, semantic policy, or emitter fallback.

## Non-negotiable rules

- Do not hand-edit generated target artifacts.
- Do not claim a rule works from source inspection, a model response, or a
  single generated snippet.
- **Compilation is not completion.** A rule whose target only compiles is
  `implemented` — an incomplete, interim state, never a deliverable. A rule is
  complete only when its declared validation tier passes; for any rule with
  observable behavior that tier is an **executable oracle** that runs the
  source and the generated target and compares their output. An oracle is
  mandatory, not "nice to have."
- Do not silently replace unsupported behavior with plausible target code.
- Do not invoke a pack's internal scripts when its documented `dev.py` command
  exists.
- Do not change this workflow ad hoc.

If any step is unsuitable, incomplete, too expensive, or technically wrong,
open a GitHub issue whose title starts **`[port-process-change]`**. Apply the
`port-process-change` label when it exists. The issue must describe the
current step, the proposed replacement, affected packs/profiles, evidence
retained or lost, validation impact, and rollback plan. It requires approval
by **Dan** before the process changes.

Create a validated draft or issue through:

```text
sourcebridge process-change --dry-run --summary "..." --current-step "..." \
  --replacement "..." --affected-scope "..." --evidence-impact "..." \
  --validation-impact "..." --rollback-plan "..."
```

## Required sequence

1. **Name the work item**
   - Use one failure signature, extraction fact family, output shape, semantic
     policy, or explicit gap category.
   - Record the source corpus revision, source span/symbol, profile, current
     rule digest, and current source/target/oracle status.

2. **Reproduce with the pack entry point**
   - Use the repository or pack's documented `dev.py` command, normally:
     ```text
     python3 dev.py <command>
     ```
   - Sourcebridge agents may use only commands listed by:
     ```text
     python3 dev.py describe
     ```
   - For C# Renode compiler evidence:
     ```text
     python3 scripts/dev.py compile-check --keep -j <workers>
     ```
   - Retain the command, revision, compiler/toolchain identity, raw output,
     generated artifact digest, and failure signature.

3. **Classify the failure**
   - `rule/data`: mapping, output shape, applicability, semantic policy,
     validation tier, or explicit gap policy;
   - `generic interpreter`: parser/AST traversal, binding, target syntax,
     graph ordering, compiler/oracle execution, or evidence lifecycle;
   - `pack/corpus adapter`: source-framework/runtime semantics;
   - `unsupported`: emit only a compile-safe explicit gap and record
     `blocked` where required.
   - A rule file must not contain arbitrary executable code. A generic
     interpreter must not contain corpus-name special cases.

4. **Add proof before promotion**
   - Add a smallest reproducible source fixture or fact-level test.
   - Test both the supported output and the closest negative/unsupported form.
   - Verify emitted target syntax and required generated gap/header content.
   - **Add an executable oracle fixture** (a runnable source program plus the
     matching generated-target harness) for any rule with observable behavior.
     The oracle must run both sides and compare normalized output. A rule whose
     behavior is observable is not eligible for promotion to complete without a
     passing oracle. Only a genuinely non-observable rule may omit it, and that
     must be stated and justified in the PR.
   - A new rule without a regression proof is not eligible for promotion.

5. **Make one bounded change**
   - Modify only the relevant rule data or generic interpreter contract.
   - Preserve explicit gaps; use compile-safe target constructs such as a
     visible failing boundary only when target syntax must remain valid.
   - Never update a clean-set baseline, expected status, or census threshold
     merely to hide a failing conversion.

6. **Run validation in increasing scope**
   - focused new/changed tests;
   - target compiler/test for the affected fixture;
   - pack canonical gate through `dev.py`;
   - profile/corpus census when the rule affects a corpus;
   - **executable oracle comparison (source output vs generated-target output)
     is required for any rule with observable behavior.** A compile-only pass
     without the oracle yields `implemented`, which is incomplete; it must not
     be reported, committed, or merged as a finished rule.
   - Report `passed`, `failed`, `blocked`, and `skipped` exactly as observed.

7. **Record and review**
   - Persist emitted artifact, source/target/oracle evidence, explicit gaps,
     diagnostics, and rule/profile/toolchain digests in the conversion census.
   - Commit the rule, proof, and directly related documentation together.
   - Open a PR with the exact commands/results and known remaining gaps.
   - A full-corpus regression is a failed result, not a reason to weaken the
     ratchet or rewrite history.

## Local-model boundary

- Local models may propose a bounded candidate only.
- Preserve raw request/response, model/prompt identity, digest, timing, and
  deterministic validation.
- A model proposal never bypasses steps 1–7 and never becomes a promoted rule
  or a conversion result without the required evidence.
