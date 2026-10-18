from __future__ import annotations

import unittest

from sourcebridge.process_change import ProcessChangeError, open_issue, proposal


class ProcessChangeTests(unittest.TestCase):
    def test_builds_required_approval_proposal(self) -> None:
        title, body = proposal(
            summary="Allow fixture-specific oracle",
            current_step="Run the corpus oracle.",
            replacement="Run the fixture oracle.",
            affected_scope="Delphi profile.",
            evidence_impact="Retain both outputs.",
            validation_impact="Adds one comparison.",
            rollback_plan="Restore corpus oracle.",
        )
        self.assertEqual(title, "[port-process-change] Allow fixture-specific oracle")
        self.assertIn("Dan's approval", body)
        self.assertEqual(
            open_issue(repository="awtoau/sourcebridge", title=title, body=body, dry_run=True)["status"],
            "dry-run",
        )

    def test_rejects_missing_required_field(self) -> None:
        with self.assertRaisesRegex(ProcessChangeError, "rollback plan"):
            proposal(
                summary="x", current_step="x", replacement="x", affected_scope="x",
                evidence_impact="x", validation_impact="x", rollback_plan="",
            )
