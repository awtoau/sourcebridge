# Backend packs

A backend pack emits and validates one target environment. It owns target
formatters, compiler/linter/type checks, runtime adapters, package layout, and
target-specific test contracts.

Planned packs:

- `rust/`;
- `python-qt/`;
- `crt/` for character-mode/driver-oriented output.

Backends are selected by a conversion profile. They must not assume a
particular source language.
