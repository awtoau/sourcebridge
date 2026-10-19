"""Focused tests for local-only proposal staging."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import json
import tempfile
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from sourcebridge.cli import _propose_batch
from sourcebridge.proposals import (
    ModelIdentity,
    ProposalWorker,
    ProposalWorkerError,
    installed_model,
    resolve_model,
    stage_proposal,
    validate_local_endpoint,
    validate_proposal,
    evidence_gate,
    write_staged_proposal,
)
from sourcebridge.pipeline import initialize


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

    def test_resolves_provisional_default_model(self) -> None:
        with patch(
            "sourcebridge.proposals._request_json",
            return_value={"models": [{"name": "qwen3-coder:30b", "digest": "sha256:best"}]},
        ):
            model = resolve_model(
                "http://127.0.0.1:11434", "qwen3-coder:30b", "sha256:best"
            )
        self.assertEqual(model.name, "qwen3-coder:30b")

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

    def test_reuses_one_pinned_worker_for_serial_batch(self) -> None:
        evidence = {
            "cluster_id": "checked-addition",
            "source_language": "delphi",
            "variant": {"compiler": {"family": "free-pascal", "mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
            "active_rule_digest": "rules-v1",
            "target_context": {"backend": "rust"},
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_family": "free-pascal", "compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left + Right",
                "why_it_matches": "observed operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
            }],
        }
        response = {"message": {"content": __import__("json").dumps(proposal)}}
        with patch(
            "sourcebridge.proposals._request_json",
            side_effect=[
                {"models": [{"name": "qwen3-coder:30b", "digest": "sha256:ok"}]},
                response,
                response,
            ],
        ) as request:
            worker = ProposalWorker(
                "http://127.0.0.1:11434",
                expected_digest="sha256:ok",
                active_rule_digest="rules-v1",
                batch_id="batch-1",
                session_id="session-1",
            )
            with tempfile.TemporaryDirectory() as temporary:
                report = worker.run_batch(
                    [evidence, evidence],
                    output_directory=Path(temporary) / "evidence" / "proposals",
                )
        self.assertEqual(request.call_count, 3)
        self.assertEqual(report["summary"]["accepted"], 2)
        self.assertEqual(report["summary"]["rejected"], 0)
        self.assertEqual(report["session"]["session_id"], "session-1")
        self.assertEqual(report["session"]["active_rule_digest"], "rules-v1")
        self.assertEqual(report["summary"]["request_count"], 2)
        self.assertIn("request_elapsed_ms", report["requests"][0]["timing"])

    def test_weak_evidence_is_rejected_and_raw_response_is_retained(self) -> None:
        evidence = {
            "cluster_id": "weak",
            "source_language": "delphi",
            "variant": {"status": "fixture-only"},
            "source_facts": {"source_path": "fixtures/scalars.pas", "functions": []},
        }
        response = {"message": {"content": __import__("json").dumps(PROPOSAL)}}
        with patch("sourcebridge.proposals._request_json", return_value=response):
            worker = ProposalWorker(
                "http://127.0.0.1:11434",
                ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                active_rule_digest="rules-v1",
                session_id="session-weak",
            )
            with tempfile.TemporaryDirectory() as temporary:
                output_directory = Path(temporary) / "evidence" / "proposals"
                report = worker.run_batch([evidence], output_directory=output_directory)
                rejected = list((output_directory / "rejected").glob("*.json"))
                record = __import__("json").loads(rejected[0].read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["rejected"], 1)
        self.assertEqual(record["status"], "rejected-llm-proposal")
        self.assertIn("weak evidence must abstain from positive patterns", record["rejection_reasons"])
        self.assertEqual(record["worker_session"]["session_id"], "session-weak")

    def test_worker_audit_records_session_and_raw_request(self) -> None:
        evidence = {
            "source_language": "delphi",
            "variant": {"compiler": {"mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left + Right",
                "why_it_matches": "observed operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            initialize(database)
            with patch(
                "sourcebridge.proposals._request_json",
                return_value={
                    "message": {"content": __import__("json").dumps(proposal)},
                    "prompt_eval_count": 17,
                    "eval_count": 9,
                },
            ):
                report = ProposalWorker(
                    "http://127.0.0.1:11434",
                    ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                    active_rule_digest="rules-v1",
                    session_id="session-audit",
                    database=database,
                ).run_batch([evidence])
            with sqlite3.connect(database) as connection:
                session = connection.execute(
                    "SELECT status, active_rule_digest FROM sourcebridge_proposal_worker_session "
                    "WHERE id = 'session-audit'"
                ).fetchone()
                events = connection.execute(
                    "SELECT status FROM sourcebridge_proposal_worker_session_event "
                    "WHERE session_id = 'session-audit' ORDER BY sequence"
                ).fetchall()
                request = connection.execute(
                    "SELECT status, prompt_tokens, generated_tokens, raw_response_json "
                    "FROM sourcebridge_proposal_worker_request "
                    "WHERE session_id = 'session-audit'"
                ).fetchone()
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(session, ("running", "rules-v1"))
        self.assertEqual([event[0] for event in events], ["running", "completed"])
        self.assertEqual(request[0], "staged")
        self.assertEqual(request[1:3], (17, 9))
        self.assertIn("message", request[3])

    def test_evidence_gate_rejects_mismatched_executable_fields(self) -> None:
        evidence = {
            "source_language": "delphi",
            "variant": {"compiler": {"mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left + Right",
                "why_it_matches": "observed operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
                "symbol": "NotAdd",
            }],
        }
        reasons, _ = evidence_gate(proposal, evidence)
        self.assertIn("suggested test `symbol` differs from evidence", reasons)

    def test_evidence_gate_associates_positive_operation_with_behavioral_symbol(self) -> None:
        evidence = {
            "source_language": "delphi",
            "variant": {"compiler": {"mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [
                    {"name": "Add", "operation": "Result := Left + Right"},
                    {"name": "Subtract", "operation": "Result := Left - Right"},
                ],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left - Right",
                "why_it_matches": "wrong function operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
            }],
        }
        reasons, _ = evidence_gate(proposal, evidence)
        self.assertIn(
            "positive pattern does not cite the behavioral example's source operation",
            reasons,
        )

    def test_repeated_identical_packages_get_distinct_request_files(self) -> None:
        evidence = {
            "source_language": "delphi",
            "variant": {"compiler": {"mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left + Right",
                "why_it_matches": "observed operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
            }],
        }
        response = {"message": {"content": __import__("json").dumps(proposal)}}
        with patch(
            "sourcebridge.proposals._request_json",
            side_effect=[response, response],
        ):
            worker = ProposalWorker(
                "http://127.0.0.1:11434",
                ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                active_rule_digest="rules-v1",
                session_id="session-files",
            )
            with tempfile.TemporaryDirectory() as temporary:
                output_directory = Path(temporary) / "evidence" / "proposals"
                report = worker.run_batch(
                    [evidence, evidence], output_directory=output_directory
                )
                paths = [Path(item["path"]) for item in report["requests"]]
                self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0].name, paths[1].name)
        self.assertEqual(
            report["requests"][0]["record"]["payload_sha256"],
            report["requests"][1]["record"]["payload_sha256"],
        )

    def test_reused_session_id_is_rejected_before_new_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            with patch(
                "sourcebridge.proposals._request_json",
                return_value={"models": [{"name": "qwen3-coder:30b", "digest": "sha256:ok"}]},
            ):
                first = ProposalWorker(
                    "http://127.0.0.1:11434",
                    expected_digest="sha256:ok",
                    active_rule_digest="rules-v1",
                    session_id="session-reused",
                    database=database,
                )
                first.run_batch([])
                with self.assertRaisesRegex(ProposalWorkerError, "already been finalized"):
                    first.run_batch([])
                second = ProposalWorker(
                    "http://127.0.0.1:11434",
                    ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                    active_rule_digest="rules-v1",
                    session_id="session-reused",
                    database=database,
                )
                with self.assertRaisesRegex(ProposalWorkerError, "already exists"):
                    second.start()

    def test_operational_failure_finalizes_session_and_retains_raw_record(self) -> None:
        evidence = {
            "source_language": "delphi",
            "variant": {"compiler": {"mode": "delphi"}},
            "source_facts": {
                "source_path": "fixtures/scalars.pas",
                "functions": [{"name": "Add", "operation": "Result := Left + Right"}],
            },
            "observed_behavior": {
                "non_overflow_examples": [
                    {"function": "Add", "input": [1, 2], "result": 3}
                ]
            },
        }
        proposal = {
            **PROPOSAL,
            "applicability": {
                **PROPOSAL["applicability"],
                "variant_selector": {"compiler_mode": "delphi"},
            },
            "positive_patterns": [{
                "snippet": "Result := Left + Right",
                "why_it_matches": "observed operation",
            }],
            "suggested_tests": [{
                "source_input": "fixtures/scalars.pas",
                "expected_result": "3",
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.db"
            with patch(
                "sourcebridge.proposals._request_json",
                return_value={"message": {"content": __import__("json").dumps(proposal)}},
            ), patch(
                "sourcebridge.proposals.write_staged_proposal",
                side_effect=OSError("output unavailable"),
            ):
                worker = ProposalWorker(
                    "http://127.0.0.1:11434",
                    ModelIdentity("qwen2.5-coder:14b", "sha256:ok"),
                    active_rule_digest="rules-v1",
                    session_id="session-failed",
                    database=database,
                )
                with self.assertRaisesRegex(ProposalWorkerError, "output unavailable"):
                    worker.run_batch([evidence], output_directory=Path(temporary) / "evidence" / "proposals")
            with sqlite3.connect(database) as connection:
                events = connection.execute(
                    "SELECT status FROM sourcebridge_proposal_worker_session_event "
                    "WHERE session_id = 'session-failed' ORDER BY sequence"
                ).fetchall()
                request = connection.execute(
                    "SELECT status, raw_response_json "
                    "FROM sourcebridge_proposal_worker_request "
                    "WHERE session_id = 'session-failed'"
                ).fetchone()
        self.assertEqual([event[0] for event in events], ["running", "failed"])
        self.assertEqual(request[0], "failed")
        self.assertIn("message", request[1])

    def test_cli_reports_ollama_failure_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_path = Path(temporary) / "evidence.json"
            evidence_path.write_text(
                json.dumps({"source_language": "delphi"}), encoding="utf-8"
            )
            stderr = StringIO()
            with patch(
                "sourcebridge.proposals._request_json",
                side_effect=[
                    {"models": [{"name": "qwen3-coder:30b", "digest": "sha256:ok"}]},
                    ValueError("Ollama unavailable"),
                ],
            ), redirect_stderr(stderr):
                result = _propose_batch(
                    [
                        str(evidence_path),
                        "--model-digest",
                        "sha256:ok",
                        "--active-rule-digest",
                        "rules-v1",
                        "--session-id",
                        "session-cli-failed",
                        "--database",
                        str(root / "evidence.db"),
                        "--output-dir",
                        str(root / "evidence" / "proposals"),
                    ]
                )
            with sqlite3.connect(root / "evidence.db") as connection:
                event = connection.execute(
                    """
                    SELECT status
                    FROM sourcebridge_proposal_worker_session_event
                    WHERE session_id = 'session-cli-failed'
                    ORDER BY sequence DESC
                    LIMIT 1
                    """
                ).fetchone()
        self.assertEqual(result, 1)
        self.assertIn("Ollama unavailable", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(event[0], "failed")


class PromptTemplateVersioningTests(unittest.TestCase):
    """The proposal prompt is an external, versioned template, not hardcoded."""

    def test_v1_template_is_external_and_digest_is_stable(self) -> None:
        from sourcebridge import proposals

        repo_root = Path(proposals.__file__).resolve().parent.parent
        template = repo_root / "prompts" / "local-rule-proposal-v1.txt"
        self.assertTrue(template.is_file(), "v1 prompt must live in prompts/")
        # Moving the text from code to file must not change the recorded digest.
        self.assertEqual(
            proposals._prompt_template_digest("local-rule-proposal-v1"),
            "18656b98767c0c7aa6f34d84fd5145ca9c3ff46f2f1c40422b7d99c54b604ad0",
        )

    def test_missing_version_raises_worker_error(self) -> None:
        from sourcebridge import proposals

        with self.assertRaises(ProposalWorkerError):
            proposals.load_prompt_template("no-such-template")

    def test_explicit_template_path_overrides_version_lookup(self) -> None:
        from sourcebridge import proposals

        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "tuned.txt"
            custom.write_text("Tuned proposal prompt v2.\n", encoding="utf-8")
            self.assertEqual(
                proposals.load_prompt_template("v2-tuned", custom),
                "Tuned proposal prompt v2.",
            )
            # The system message and digest both follow the selected template.
            model = ModelIdentity(name="qwen3-coder:30b", digest="sha256:ok")
            payload = proposals._proposal_payload(
                model, {"a": 1}, prompt_template_version="v2-tuned", prompt_template_path=custom
            )
            self.assertEqual(
                payload["messages"][0]["content"], "Tuned proposal prompt v2."
            )
            self.assertEqual(
                proposals._prompt_template_digest("v2-tuned", custom),
                proposals._prompt_template_digest("v2-tuned", custom),
            )
            self.assertNotEqual(
                proposals._prompt_template_digest("v2-tuned", custom),
                proposals._prompt_template_digest("local-rule-proposal-v1"),
            )


if __name__ == "__main__":
    unittest.main()
