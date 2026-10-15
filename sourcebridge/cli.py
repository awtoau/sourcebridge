"""Installed Sourcebridge command entry point."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    script = Path(__file__).resolve().parents[1] / "cli" / "sourcebridge.py"
    return subprocess.call([sys.executable, str(script), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
