#!/usr/bin/env python3
"""Run the bounded FPC Delphi scalar fixture through the Rust backend pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=capture, text=True)
    if result.returncode:
        detail = (result.stdout or "") + (result.stderr or "")
        raise ValueError(f"`{' '.join(command)}` failed:\n{detail.strip()}")
    return result.stdout if capture else ""


def validate(delphi_pack: Path, rust_pack: Path) -> dict[str, str]:
    for pack in (delphi_pack, rust_pack):
        if not pack.is_dir():
            raise ValueError(f"pack directory does not exist: {pack}")
    run([sys.executable, "tools/validate_fpc_fixture.py"], cwd=delphi_pack)
    run([sys.executable, "tools/validate_fpc_scalar_behavior.py"], cwd=delphi_pack)
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        facts_path = temporary_path / "scalars.fpc.facts.json"
        facts_path.write_text(
            run(
                [sys.executable, "tools/extract_fpc_fixture.py"],
                cwd=delphi_pack,
                capture=True,
            ),
            encoding="utf-8",
        )
        generated_path = temporary_path / "generated.rs"
        generated = run(
            [
                "cargo",
                "run",
                "--quiet",
                "--bin",
                "sourcebridge-rust-emit",
                "--",
                str(facts_path),
            ],
            cwd=rust_pack,
            capture=True,
        )
        generated_path.write_text(
            generated
            + "\nfn main() {\n"
            "    assert_eq!(add_signed(1, 2), 3);\n"
            "    assert_eq!(add_unsigned(1, 2), 3);\n"
            "    assert_eq!(add_signed(i32::MAX, 1), i32::MIN);\n"
            "    assert_eq!(add_unsigned(u32::MAX, 1), 0);\n"
            "}\n",
            encoding="utf-8",
        )
        executable_path = temporary_path / "generated"
        run(
            ["rustc", str(generated_path), "-o", str(executable_path)],
            cwd=rust_pack,
        )
        run([str(executable_path)], cwd=rust_pack)
    return {
        "profile": "delphi-fpc-scalar-to-rust",
        "source_validation": "passed",
        "target_validation": "passed",
        "oracle_scope": "ordinary and measured 32-bit addition boundaries",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delphi-pack", type=Path, required=True)
    parser.add_argument("--rust-pack", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.delphi_pack.resolve(), args.rust_pack.resolve()), indent=2))
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
