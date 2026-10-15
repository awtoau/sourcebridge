"""Focused tests for local-only proposal staging."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sourcebridge.proposals import (
    ModelIdentity,
    installed_model,
    resolve_model,
    stage_proposal,
    validate_local_endpoint,
    validate_proposal,
    write_staged_proposal,
)


PROPOSAL = {
    "rule_name": "lower-checked-addition",
    "family": "arithmetic",
    "applicability": {
        "source_language": "delphi",
        "version_selector": {"kind": "any"},
        "variant_selector": {"kind": "any"},
        "rationale": "uses ordinary integer expressions",
    },
    "status_claim": "unvalidated-llm-proposal",
    "description": "Candidate mapping for checked integer addition.",
    "positive_patterns": [
        {"snippet": "a + b", "why_it_matches": "integer operands"}
    ],
    "negative_patterns": [
        {"snippet": "PChar + 1", "why_it_must_not_match": "pointer arithmetic"}
    ],
    "suggested_tests": [
        {"source_input": "1 + 2", "expected_result": "3"}
    ],
    "open_questions": ["Does the selected dialect enable overflow checks?"],
}


class ProposalTests(unittest.TestCase):
    def test_only_loopback_endpoints_are_allowed(self) -> None:
        self.assertEqual(
            validate_local_endpoint("http://127.0.0.1:11434"),
            "http://127.0.0.1:11434",
        )
        with self.assertRaisesRegex(ValueError, "localhost"):
            validate_local_endpoint("https://ollama.example.test")

    def test_rejects_unvalidated_status_claim(self) -> None:
        invalid = {**PROPOSAL, "status_claim": "validated"}
        with self.assertRaisesRegex(ValueError, "unvalidated"):
            validate_proposal(invalid)

    def test_resolves_only_expected_digest(self) -> None:
        with patch(
            "sourcebridge.proposals._request_json",
            return_value={"models": [{"name": "qwen2.5-coder:14b", "digest": "sha256:ok"}]},
        ):
            model = resolve_model(
                "http://127.0.0.1:11434", "qwen2.5-coder:14b", "sha256:ok")
        self.assertEqual(model.digest, "sha256:ok")

    def test_reports_installed_model_digest(self) -> None:
        with patch(
            "sourcebridge.proposals._request_json",
            return_value={"models": [{"name": "qwen2.5-coder:14b", "digest": "sha256:ok"}]},
        ):
            model = installed_model("http://127.0.0.1:11434", "qwen2.5-coder:14b")
        self.assertEqual(model.name, "qwen2.5-coder:14b")
        self.assertEqual(model.digest, "sha256:ok")

    def test_stages_schema_checked_proposal_with_provenance(self) -> None:
        response = {"message": {"content": __import__("json").dumps(PROPOSAL)}}
        with patch("sourcebridge.proposals._request_json", return_value=response):
            staged = stage_proposal(
                "http://127.0.0.1:11434",
                ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                {"cluster": "checked-addition"},
            )
        self.assertEqual(staged["status"], "unvalidated-llm-proposal")
        self.assertEqual(staged["model"]["digest"], "sha256:ok")
        self.assertEqual(staged["evidence"], {"cluster": "checked-addition"})
        with tempfile.TemporaryDirectory() as temporary:
            output = write_staged_proposal(
                Path(temporary) / "evidence" / "proposals", staged)
            self.assertTrue(output.is_file())
