"""Check that configured layers do not contain forbidden project knowledge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def check(root: Path, configuration: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    layers = configuration.get("layers")
    if not isinstance(layers, list):
        raise ValueError("configuration must contain a `layers` array")
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("each layer must be an object")
        paths = layer.get("paths")
        forbidden = layer.get("forbidden_terms")
        name = layer.get("name")
        if not isinstance(name, str) or not isinstance(paths, list) or not isinstance(forbidden, list):
            raise ValueError("each layer needs name, paths, and forbidden_terms")
        if not all(isinstance(item, str) for item in paths + forbidden):
            raise ValueError("layer paths and forbidden_terms must be strings")
        for pattern in paths:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                for term in forbidden:
                    if term.casefold() in text.casefold():
                        failures.append(f"{name}: {path.relative_to(root)} contains `{term}`")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        data = json.loads(args.configuration.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        failures = check(args.root.resolve(), data)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
