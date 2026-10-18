"""Create validated proposals for changing the required porting process."""

from __future__ import annotations

import subprocess


class ProcessChangeError(ValueError):
    """Raised when a process-change proposal is incomplete or cannot be opened."""


def proposal(
    *,
    summary: str,
    current_step: str,
    replacement: str,
    affected_scope: str,
    evidence_impact: str,
    validation_impact: str,
    rollback_plan: str,
) -> tuple[str, str]:
    values = {
        "summary": summary,
        "current step": current_step,
        "replacement": replacement,
        "affected scope": affected_scope,
        "evidence impact": evidence_impact,
        "validation impact": validation_impact,
        "rollback plan": rollback_plan,
    }
    for name, value in values.items():
        if not value.strip():
            raise ProcessChangeError(f"{name} is required")
    title = f"[port-process-change] {summary.strip()}"
    body = "\n".join(
        [
            "## Required approval",
            "This proposal requires Dan's approval before the porting process changes.",
            "",
            "## Current required step",
            current_step.strip(),
            "",
            "## Proposed replacement",
            replacement.strip(),
            "",
            "## Affected packs/profiles",
            affected_scope.strip(),
            "",
            "## Evidence retained or lost",
            evidence_impact.strip(),
            "",
            "## Validation impact",
            validation_impact.strip(),
            "",
            "## Rollback plan",
            rollback_plan.strip(),
        ]
    )
    return title, body


def open_issue(*, repository: str, title: str, body: str, dry_run: bool) -> dict[str, str]:
    if not repository.strip():
        raise ProcessChangeError("repository is required")
    if dry_run:
        return {"repository": repository, "title": title, "body": body, "status": "dry-run"}
    result = subprocess.run(
        ["gh", "issue", "create", "--repo", repository, "--title", title, "--body", body],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ProcessChangeError(result.stderr.strip() or "gh issue create failed")
    return {"repository": repository, "title": title, "url": result.stdout.strip(), "status": "opened"}
