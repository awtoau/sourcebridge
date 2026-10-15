# Frontend packs

A frontend pack converts one pinned source corpus variant into neutral source
units, symbols, facts, source diagnostics, and provenance. It owns
language-specific compiler/parser integrations and fact schemas.

Planned packs:

- `csharp/` — Roslyn facts;
- `c/` — compiler AST, preprocessor, ABI, and undefined-behavior facts;
- `delphi/` — selected Delphi dialect/compiler facts.

A frontend pack does not choose a target language.
