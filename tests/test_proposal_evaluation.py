from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sourcebridge.proposal_evaluation import evaluate


class ProposalEvaluationTests(unittest.TestCase):
    def test_rejects_non_source_positive_pattern_and_vague_test(self) -> None:
        record = {
            "status": "unvalidated-llm-proposal",
            "evidence": {
                "source_language": "delphi",
                "source_facts": {
                    "source_path": "fixtures/scalars.pas",
                    "functions": [{"operation": "Result := Left + Right"}],
                },
                "observed_behavior": {
                    "non_overflow_examples": [{"result": 3}],
                },
            },
            "proposal": {
                "status_claim": "unvalidated-llm-proposal",
                "applicability": {"source_language": "delphi"},
                "positive_patterns": [{"snippet": "target compiler passed"}],
                "suggested_tests": [{
                    "source_input": "fixtures/scalars.pas",
                    "expected_result": "true",
                }],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proposal.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            result = evaluate(path)
        self.assertFalse(result["eligible_for_human_rule_review"])
        self.assertIn("positive pattern does not cite an observed source operation", result["reasons"])
        self.assertIn("suggested test does not state an observed concrete result", result["reasons"])
