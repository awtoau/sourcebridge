"""Run the supported parallel-writer SQLite benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sourcebridge.concurrency import run_contention_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--writes-per-worker", type=int, default=25)
    parser.add_argument("--hold-ms", type=float, default=1.0)
    args = parser.parse_args()
    result = run_contention_benchmark(
        args.database,
        workers=args.workers,
        writes_per_worker=args.writes_per_worker,
        hold_seconds=args.hold_ms / 1_000,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result["errors"] and result["row_count"] == result["expected_writes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
