"""Evaluate whether an unvalidated proposal has enough evidence for human rule review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sourcebridge.proposals import evidence_gate


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def evaluate(proposal_path: Path) -> dict[str, Any]:
    record = _read(proposal_path)
    proposal = record.get("proposal")
    evidence = record.get("evidence")
    if not isinstance(proposal, dict) or not isinstance(evidence, dict):
        raise ValueError(f"{proposal_path}: missing proposal or evidence")
    reasons: list[str] = []
    if record.get("status") != "unvalidated-llm-proposal":
        reasons.append("record is not an unvalidated proposal")
    if proposal.get("status_claim") != "unvalidated-llm-proposal":
        reasons.append("proposal status claim is invalid")
    if proposal.get("applicability", {}).get("source_language") != evidence.get("source_language"):
        reasons.append("proposal source language does not match evidence")

    source_facts = evidence.get("source_facts", {})
    source_path = source_facts.get("source_path")
    operations = {
        function.get("operation")
        for function in source_facts.get("functions", [])
        if isinstance(function, dict) and isinstance(function.get("operation"), str)
    }
    positive = proposal.get("positive_patterns", [])
    if not positive:
        reasons.append("proposal has no positive pattern")
    elif not any(item.get("snippet") in operations for item in positive if isinstance(item, dict)):
        reasons.append("positive pattern does not cite an observed source operation")

    tests = proposal.get("suggested_tests", [])
    if not tests:
        reasons.append("proposal has no suggested test")
    else:
        test = tests[0] if isinstance(tests[0], dict) else {}
        if test.get("source_input") != source_path:
            reasons.append("suggested test does not reference the observed source fixture")
        expected = test.get("expected_result")
        observed_results = {
            str(example.get("result"))
            for example in evidence.get("observed_behavior", {}).get("non_overflow_examples", [])
            if isinstance(example, dict) and "result" in example
        }
        if expected not in observed_results:
            reasons.append("suggested test does not state an observed concrete result")

    gate_reasons, gate = evidence_gate(proposal, evidence)
    for reason in gate_reasons:
        if reason not in reasons:
            reasons.append(reason)

    return {
        "proposal_path": proposal_path.as_posix(),
        "eligible_for_human_rule_review": not reasons,
        "reasons": reasons,
        "evidence_gate": gate,
    }
