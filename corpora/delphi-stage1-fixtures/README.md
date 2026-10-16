# Delphi Stage-1 fixtures

These self-authored fixtures are a language-semantics pilot, not an attributed
application corpus. They establish a small, reproducible input set for the
Delphi frontend and local-rule-proposal stages before Sourcebridge selects a
real corpus and pins its compiler, dialect, runtime, and configuration.

The initial fixture covers signed/unsigned scalar declarations and ordinary
arithmetic. It must not be reported as source-compiler validated until the
Delphi validation adapter records the selected compiler and result.
