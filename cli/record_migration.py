#!/usr/bin/env python3
"""Record a history-preserving Sourcebridge component migration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def path_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    else:
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(child.relative_to(path).as_posix().encode())
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def history(repo: Path, source_path: str) -> list[dict[str, str]]:
    output = git(
        repo,
        "log",
        "--follow",
        "--format=%H%x1f%aI%x1f%s",
        "--",
        source_path,
    )
    if not output:
        return []
    entries = []
    for line in output.splitlines():
        commit, recorded_at, subject = line.split("\x1f", 2)
        entries.append({
            "commit": commit,
            "recorded_at": recorded_at,
            "subject": subject,
        })
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("source_paths", nargs="+")
    args = parser.parse_args()
    repo = args.source_repo.resolve()
    if not (repo / ".git").exists():
        parser.error("--source-repo must be a Git working tree")
    if git(repo, "status", "--porcelain"):
        parser.error("source repository is dirty; commit or archive its work before migration")
    records = []
    for source_path in args.source_paths:
        path = repo / source_path
        if not path.exists():
            parser.error(f"source path does not exist: {source_path}")
        records.append({
            "source_path": source_path,
            "sha256": path_digest(path),
            "history": history(repo, source_path),
        })
    remote = git(repo, "remote", "get-url", "origin")
    revision = git(repo, "rev-parse", "HEAD")
    payload = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "component": args.component,
        "destination": args.destination,
        "source": {
            "remote": remote,
            "revision": revision,
        },
        "paths": records,
    }
    output = ROOT / "migration" / "records" / f"{args.component}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {args.component} migration from {remote}@{revision}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
