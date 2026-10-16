# Delphi scalar fact-grounded Ollama pilot

## Scope

This pilot evaluates one local proposal for the pinned Free Pascal 3.2.3,
x86_64 Linux, Delphi-mode scalar fixture. It does not evaluate a converted
application and it does not promote a conversion rule.

The input is
`corpora/delphi-stage1-fixtures/scalar-addition-fact-grounded-evidence.json`.
The model is `qwen2.5-coder:14b` pinned to digest
`9ec8897f747e246e970bc5cfdda85d22f1123dc2e3d34978a010a75968716849`.

## Results

| Attempt | Outcome | Evidence |
|---|---|---|
| Fact-grounded request with the original 120-second ceiling | client timeout | No response record: the request exceeded the client ceiling. |
| 768-token response cap | rejected | `proposals/rejected/598c06cadee5b26a174d1a00fb679b61ecc12c39027eebee1ae8b79bfc7c9fb9.json` |
| Concise prompt with a 1,024-token cap | schema-valid but not rule-ready | `proposals/d136c1f0df455b1cc6260c8b5dd788e216103ce0cba8b06426f450bbab775328.json` |

The rejected 768-token response stopped with `done_reason: "length"` after
768 generated tokens. The concise response finished normally with 519 generated
tokens in 107.511 seconds total and 99.116 seconds evaluation time, or about
5.2 generated tokens per second.

## Readiness evaluation

`sourcebridge evaluate-proposal` rejected the schema-valid response for human
rule review:

1. its positive pattern cited target-validation prose instead of the observed
   source operation `Result := Left + Right`;
2. its proposed test expected `"true"` instead of an observed concrete result
   such as `3`.

The proposal correctly retained the FPC variant and exclusions, but those were
provided by the input evidence. It did not supply a sufficiently grounded
positive match or executable test contract.

## Conclusion

The local workflow is operational: local-only routing, digest pinning,
structured output, rejection retention, provenance, and schema validation all
work. It is not semantically effective enough to promote a Delphi conversion
rule from this sample.

The next measured iteration must require a literal observed source operation
for the positive pattern and an observed concrete result for the suggested
test, then compare the readiness-gate pass rate across a fixed fixture set.
