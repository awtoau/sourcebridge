"""Local-Ollama rule proposal staging with reproducible provenance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


DEFAULT_MODEL = "qwen3-coder:30b"
APPROVED_MODELS = frozenset({DEFAULT_MODEL, "qwen2.5-coder:14b"})
PROMPT_TEMPLATE_VERSION = "local-rule-proposal-v1"
PROMPT_TEMPLATE_TEXT = (
    "Return only one JSON object matching the supplied schema. "
    "Propose one concise rule candidate; never claim validation, commit a rule, "
    "or write generated code. "
    "Use exact source snippets, symbols, compiler fields, and committed fixture paths "
    "from the evidence, not prose summaries. "
    "If the evidence is insufficient, abstain: return empty positive_patterns, "
    "empty suggested_tests, and explain the gap in open_questions. "
    "Never invent overflow, compiler, source, target, or fixture claims. "
    "Suggested tests are only evidence references; their executable fields are "
    "resolved and checked deterministically by Sourcebridge. "
    "Keep every explanation concise."
)
GENERATION_OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 1024}
REQUEST_TIMEOUT_SECONDS = 300
MAX_BATCH_SIZE = 20
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_REQUIRED_PROPOSAL_FIELDS = {
    "rule_name",
    "family",
    "applicability",
    "status_claim",
    "description",
    "positive_patterns",
    "negative_patterns",
    "suggested_tests",
    "open_questions",
}
_OPTIONAL_PROPOSAL_FIELDS = {"candidate_template", "evidence_references"}


def _prompt_template_digest(version: str) -> str:
    return hashlib.sha256(f"{version}\n{PROMPT_TEMPLATE_TEXT}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelIdentity:
    name: str
    digest: str


@dataclass(frozen=True)
class ProposalWorkerSession:
    """Immutable identity and policy contract for one loaded proposal worker."""

    session_id: str
    batch_id: str
    endpoint: str
    model: ModelIdentity
    generation_options: dict[str, int]
    prompt_template_version: str
    prompt_template_digest: str
    active_rule_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "batch_id": self.batch_id,
            "endpoint": self.endpoint,
            "model": asdict(self.model),
            "generation_options": dict(self.generation_options),
            "prompt_template_version": self.prompt_template_version,
            "prompt_template_digest": self.prompt_template_digest,
            "active_rule_digest": self.active_rule_digest,
        }


class ProposalEvidenceError(ValueError):
    """A proposal response that failed deterministic evidence gates."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class ProposalWorkerError(ValueError):
    """A controlled operational failure from the durable proposal worker."""


class ProposalValidationError(ValueError):
    """A model response that must be retained for prompt/schema review."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


def validate_local_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama endpoint must use http on localhost, 127.0.0.1, or ::1")
    if parsed.path not in ("", "/"):
        raise ValueError("Ollama endpoint must not include a path")
    return endpoint.rstrip("/")


def validate_model_name(model: str) -> None:
    if ":cloud" in model:
        raise ValueError("cloud-routed Ollama models are not allowed")
    if model not in APPROVED_MODELS:
        raise ValueError(
            "model must be an approved local model: "
            + ", ".join(sorted(APPROVED_MODELS))
        )


def _request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except OSError as error:
        raise ValueError(f"Ollama request failed: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Ollama returned invalid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError("Ollama response must be a JSON object")
    return decoded


def resolve_model(endpoint: str, model: str, expected_digest: str) -> ModelIdentity:
    validate_model_name(model)
    if not expected_digest:
        raise ValueError("an expected local model digest is required")
    identity = installed_model(endpoint, model)
    if identity.digest != expected_digest:
        raise ValueError(
            f"Ollama model digest differs: expected `{expected_digest}`, got `{identity.digest}`")
    return identity


def installed_model(endpoint: str, model: str) -> ModelIdentity:
    validate_model_name(model)
    response = _request_json(f"{endpoint}/api/tags")
    models = response.get("models")
    if not isinstance(models, list):
        raise ValueError("Ollama model list does not contain `models`")
    for item in models:
        if not isinstance(item, dict) or item.get("name") != model:
            continue
        digest = item.get("digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError(f"Ollama model `{model}` has no digest")
        return ModelIdentity(name=model, digest=digest)
    raise ValueError(f"approved local model `{model}` is not installed")


def read_evidence(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"{path}: cannot read evidence: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: evidence must be valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: evidence must be a JSON object")
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 262_144:
        raise ValueError(f"{path}: evidence exceeds the 256 KiB proposal limit")
    return data


def _proposal_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": sorted(_REQUIRED_PROPOSAL_FIELDS),
        "properties": {
            "rule_name": {"type": "string"},
            "family": {"type": "string"},
            "applicability": {
                "type": "object",
                "required": [
                    "source_language",
                    "version_selector",
                    "variant_selector",
                    "rationale",
                ],
                "properties": {
                    "source_language": {"type": "string"},
                    "version_selector": {"type": "object"},
                    "variant_selector": {"type": "object"},
                    "rationale": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "status_claim": {"const": "unvalidated-llm-proposal"},
            "description": {"type": "string"},
            "positive_patterns": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "required": ["snippet", "why_it_matches"],
                    "properties": {
                        "snippet": {"type": "string"},
                        "symbol": {"type": "string"},
                        "why_it_matches": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "negative_patterns": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": ["snippet", "why_it_must_not_match"],
                    "properties": {"snippet": {"type": "string"}, "why_it_must_not_match": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "suggested_tests": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "required": ["source_input", "expected_result"],
                    "properties": {
                        "source_input": {"type": "string"},
                        "expected_result": {"type": "string"},
                        "source_path": {"type": "string"},
                        "symbol": {"type": "string"},
                        "observed_input": {},
                        "variant": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
            },
            "open_questions": {"type": "array", "maxItems": 1, "items": {"type": "string"}},
            "candidate_template": {
                "type": "object",
                "required": ["target_backend", "template"],
                "properties": {
                    "target_backend": {"type": "string"},
                    "template": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "evidence_references": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    }


def validate_proposal(proposal: Any) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object")
    if not _REQUIRED_PROPOSAL_FIELDS.issubset(proposal) or (
        set(proposal) - _REQUIRED_PROPOSAL_FIELDS - _OPTIONAL_PROPOSAL_FIELDS
    ):
        raise ValueError("proposal does not match the required rule-proposal fields")
    if proposal["status_claim"] != "unvalidated-llm-proposal":
        raise ValueError("proposal must be an unvalidated-llm-proposal")
    if not all(isinstance(proposal[key], str) for key in ("rule_name", "family", "description")):
        raise ValueError("proposal identity and description fields must be strings")
    applicability = proposal["applicability"]
    if not isinstance(applicability, dict) or set(applicability) != {
        "source_language", "version_selector", "variant_selector", "rationale"
    }:
        raise ValueError("proposal applicability does not match the required fields")
    if not isinstance(applicability["source_language"], str) or not isinstance(
            applicability["rationale"], str):
        raise ValueError("proposal applicability language and rationale must be strings")
    for field in ("version_selector", "variant_selector"):
        if not isinstance(applicability[field], dict):
            raise ValueError(f"proposal applicability `{field}` must be an object")
    for field in ("positive_patterns", "negative_patterns", "suggested_tests", "open_questions"):
        if not isinstance(proposal[field], list):
            raise ValueError(f"proposal `{field}` must be an array")
    limits = {
        "positive_patterns": 1,
        "negative_patterns": 2,
        "suggested_tests": 1,
        "open_questions": 1,
    }
    for field, limit in limits.items():
        if len(proposal[field]) > limit:
            raise ValueError(f"proposal `{field}` exceeds the maximum of {limit} item(s)")
    _validate_pattern_list(
        proposal["positive_patterns"], "positive_patterns", "why_it_matches")
    _validate_pattern_list(
        proposal["negative_patterns"], "negative_patterns", "why_it_must_not_match")
    for item in proposal["suggested_tests"]:
        if not isinstance(item, dict) or not {"source_input", "expected_result"}.issubset(item):
            raise ValueError("proposal suggested_tests entries must describe source and expected result")
        if set(item) - {
            "source_input", "expected_result", "source_path", "symbol", "observed_input", "variant"
        }:
            raise ValueError("proposal suggested_tests contains unsupported fields")
        if not isinstance(item["source_input"], str) or not isinstance(item["expected_result"], str):
            raise ValueError("proposal suggested_tests source and expected result must be strings")
        for field in ("source_path", "symbol"):
            if field in item and not isinstance(item[field], str):
                raise ValueError(f"proposal suggested_tests `{field}` must be a string")
        if "variant" in item and not isinstance(item["variant"], dict):
            raise ValueError("proposal suggested_tests `variant` must be an object")
    if not all(isinstance(item, str) for item in proposal["open_questions"]):
        raise ValueError("proposal open_questions entries must be strings")
    candidate_template = proposal.get("candidate_template")
    if candidate_template is not None:
        if (
            not isinstance(candidate_template, dict)
            or set(candidate_template) != {"target_backend", "template"}
            or not all(isinstance(value, str) for value in candidate_template.values())
        ):
            raise ValueError(
                "proposal candidate_template must contain target_backend and template strings"
            )
    evidence_references = proposal.get("evidence_references")
    if evidence_references is not None:
        if (
            not isinstance(evidence_references, list)
            or len(evidence_references) > 32
            or not all(isinstance(item, str) for item in evidence_references)
        ):
            raise ValueError("proposal evidence_references must be an array of strings")
    return proposal


def _validate_pattern_list(items: list[Any], field: str, reason: str) -> None:
    for item in items:
        allowed = {"snippet", reason}
        if field == "positive_patterns":
            allowed.add("symbol")
        if (
            not isinstance(item, dict)
            or not {"snippet", reason}.issubset(item)
            or set(item) - allowed
        ):
            raise ValueError(f"proposal {field} entries must contain snippet and {reason}")
        if not all(isinstance(value, str) for value in item.values()):
            raise ValueError(f"proposal {field} values must be strings")


def _proposal_payload(
    model: ModelIdentity,
    evidence: dict[str, Any],
    *,
    generation_options: Mapping[str, int] = GENERATION_OPTIONS,
    prompt_template_version: str = PROMPT_TEMPLATE_VERSION,
    session: ProposalWorkerSession | None = None,
) -> dict[str, Any]:
    request_context: dict[str, Any] = {
        "prompt_template_version": prompt_template_version,
        "evidence": evidence,
    }
    if session is not None:
        request_context["worker_session"] = {
            "session_id": session.session_id,
            "batch_id": session.batch_id,
            "active_rule_digest": session.active_rule_digest,
        }
    return {
        "model": model.name,
        "stream": False,
        "keep_alive": -1,
        "format": _proposal_schema(),
        "options": dict(generation_options),
        "messages": [
            {"role": "system", "content": PROMPT_TEMPLATE_TEXT},
            {
                "role": "user",
                "content": json.dumps(request_context, sort_keys=True),
            },
        ],
    }


def _stage_response(
    response: dict[str, Any],
    payload: dict[str, Any],
    model: ModelIdentity,
    evidence: dict[str, Any],
    *,
    session: ProposalWorkerSession | None = None,
    prompt_template_version: str = PROMPT_TEMPLATE_VERSION,
    request_id: str | None = None,
) -> dict[str, Any]:
    proposal: Any = None
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence_hash = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    record: dict[str, Any] = {
        "status": "unvalidated-llm-proposal",
        "prompt_template_version": prompt_template_version,
        "prompt_template_digest": _prompt_template_digest(prompt_template_version),
        "model": asdict(model),
        "generation_options": dict(payload["options"]),
        "payload_sha256": payload_hash,
        "evidence_sha256": evidence_hash,
        "evidence": evidence,
        "proposal": proposal,
        "raw_response": response,
    }
    if session is not None:
        record["worker_session"] = session.as_dict()
    if request_id is not None:
        record["request_id"] = request_id
    message = response.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ProposalValidationError(
            "Ollama chat response does not contain message content", record
        )
    parse_error = "Ollama proposal is not a JSON object"
    try:
        proposal = json.loads(message["content"])
    except json.JSONDecodeError as error:
        proposal = None
        parse_error = f"Ollama proposal is not valid JSON: {error}"
    record["proposal"] = proposal
    if proposal is None:
        raise ProposalValidationError(parse_error, record)
    try:
        validate_proposal(proposal)
    except ValueError as error:
        raise ProposalValidationError(str(error), record) from error
    return record


def stage_proposal(
    endpoint: str,
    model: ModelIdentity,
    evidence: dict[str, Any],
    *,
    enforce_evidence: bool = False,
) -> dict[str, Any]:
    payload = _proposal_payload(model, evidence)
    response = _request_json(f"{endpoint}/api/chat", payload)
    record = _stage_response(
        response,
        payload,
        model,
        evidence,
        request_id=f"request-{uuid.uuid4().hex}",
    )
    if enforce_evidence:
        reasons, gate = evidence_gate(record["proposal"], evidence)
        record["evidence_gate"] = gate
        if reasons:
            record["status"] = "rejected-llm-proposal"
            record["rejection_reasons"] = reasons
            raise ProposalEvidenceError("; ".join(reasons), record)
        record["proposal"] = _normalize_worker_proposal(
            record["proposal"],
            gate["deterministic_test"],
            gate["evidence_references"],
        )
        record["evidence_references"] = gate["evidence_references"]
    return record


def write_staged_proposal(output_directory: Path, staged: dict[str, Any]) -> Path:
    root = output_directory.resolve()
    if root.name != "proposals" or root.parent.name != "evidence":
        raise ValueError("proposal output directory must be `evidence/proposals`")
    root.mkdir(parents=True, exist_ok=True)
    request_id = staged.get("request_id")
    prefix = f"{request_id}-" if isinstance(request_id, str) and request_id else ""
    path = root / f"{prefix}{staged['payload_sha256']}.json"
    path.write_text(json.dumps(staged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_rejected_proposal(output_directory: Path, rejected: dict[str, Any]) -> Path:
    root = output_directory.resolve() / "rejected"
    if root.parent.name != "proposals" or root.parent.parent.name != "evidence":
        raise ValueError("proposal output directory must be `evidence/proposals`")
    root.mkdir(parents=True, exist_ok=True)
    request_id = rejected.get("request_id")
    prefix = f"{request_id}-" if isinstance(request_id, str) and request_id else ""
    path = root / f"{prefix}{rejected['payload_sha256']}.json"
    path.write_text(json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _json_value(value: Any) -> str:
    """Render an evidence value without allowing representation drift."""

    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _observed_examples(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_facts = evidence.get("source_facts")
    observed = evidence.get("observed_behavior")
    if not isinstance(source_facts, Mapping) or not isinstance(observed, Mapping):
        return []
    source_path = source_facts.get("source_path")
    functions = source_facts.get("functions")
    examples = observed.get("non_overflow_examples")
    if not isinstance(source_path, str) or not isinstance(functions, list) or not isinstance(
        examples, list
    ):
        return []
    function_names = {
        item.get("name")
        for item in functions
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    return [
        {
            "source_path": source_path,
            "symbol": example["function"],
            "observed_input": example["input"],
            "expected_result": example["result"],
            "variant": evidence.get("variant"),
        }
        for example in examples
        if isinstance(example, Mapping)
        and isinstance(example.get("function"), str)
        and example["function"] in function_names
        and "input" in example
        and "result" in example
    ]


def _variant_claims_match(selector: Any, variant: Any) -> bool:
    if not isinstance(selector, Mapping) or not selector or not isinstance(variant, Mapping):
        return False

    def lookup(claim: Mapping[str, Any], actual: Mapping[str, Any], prefix: str = "") -> bool:
        for key, value in claim.items():
            if key not in actual:
                # The compact selector names the common compiler fields.
                aliases = {
                    "compiler_family": ("compiler", "family"),
                    "compiler_mode": ("compiler", "mode"),
                    "compiler_version": ("compiler", "version"),
                    "target_cpu": ("compiler", "target_cpu"),
                    "target_os": ("compiler", "target_os"),
                }
                alias = aliases.get(key)
                if alias is None:
                    return False
                current: Any = actual
                for part in alias:
                    if not isinstance(current, Mapping) or part not in current:
                        return False
                    current = current[part]
                if isinstance(value, Mapping):
                    return isinstance(current, Mapping) and lookup(value, current, key)
                if current != value:
                    return False
                continue
            current = actual[key]
            if isinstance(value, Mapping):
                if not isinstance(current, Mapping) or not lookup(value, current, key):
                    return False
            elif current != value:
                return False
        return True

    return lookup(selector, variant)


def _canonical_evidence_references(
    evidence: Mapping[str, Any], example: Mapping[str, Any] | None = None
) -> list[str]:
    references = ["source_language", "variant"]
    if evidence.get("active_rule_digest") is not None:
        references.append("active_rule_digest")
    source_facts = evidence.get("source_facts")
    if isinstance(source_facts, Mapping):
        if isinstance(source_facts.get("source_path"), str):
            references.append("source_facts.source_path")
        for index, function in enumerate(source_facts.get("functions", [])):
            if isinstance(function, Mapping):
                references.append(f"source_facts.functions[{index}]")
                if example is not None and function.get("name") == example.get("symbol"):
                    references.append(f"source_facts.functions[{index}].operation")
    if example is not None:
        observed = evidence.get("observed_behavior")
        examples = observed.get("non_overflow_examples") if isinstance(observed, Mapping) else []
        for index, item in enumerate(examples if isinstance(examples, list) else []):
            if isinstance(item, Mapping) and (
                item.get("function") == example.get("symbol")
                and item.get("input") == example.get("observed_input")
                and item.get("result") == example.get("expected_result")
            ):
                references.append(f"observed_behavior.non_overflow_examples[{index}]")
                break
    return references


def evidence_gate(
    proposal: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Check model claims against literal evidence and resolve executable fields.

    The returned deterministic test is built from the evidence envelope.  The
    model can only suggest a matching observation; it cannot choose a fixture,
    symbol, input, result, or variant.
    """

    reasons: list[str] = []
    source_language = evidence.get("source_language")
    applicability = proposal.get("applicability")
    if not isinstance(applicability, Mapping) or (
        applicability.get("source_language") != source_language
    ):
        reasons.append("proposal source language does not match evidence")

    examples = _observed_examples(evidence)
    positive = proposal.get("positive_patterns")
    tests = proposal.get("suggested_tests")
    if not isinstance(positive, list):
        positive = []
    if not isinstance(tests, list):
        tests = []
    variant_selector = applicability.get("variant_selector") if isinstance(
        applicability, Mapping
    ) else None
    variant_matches = _variant_claims_match(variant_selector, evidence.get("variant"))
    if examples and not variant_matches:
        reasons.append("proposal variant applicability is absent or differs from evidence")

    if not examples:
        if positive:
            reasons.append("weak evidence must abstain from positive patterns")
        if tests:
            reasons.append("weak evidence must abstain from executable tests")
        return reasons, {
            "grounded": False,
            "deterministic_test": None,
            "evidence_references": _canonical_evidence_references(evidence),
        }

    example = examples[0]
    matched_function: Mapping[str, Any] | None = None
    source_facts = evidence.get("source_facts")
    functions = source_facts.get("functions", []) if isinstance(source_facts, Mapping) else []
    if isinstance(functions, list):
        for function in functions:
            if isinstance(function, Mapping) and function.get("name") == example["symbol"]:
                matched_function = function
                break

    if not positive:
        reasons.append("grounded evidence needs a positive pattern")
    else:
        observed_operation = (
            matched_function.get("operation")
            if matched_function is not None
            else None
        )
        if not isinstance(observed_operation, str):
            reasons.append("behavioral example has no associated source operation")
        for item in positive:
            if (
                not isinstance(item, Mapping)
                or item.get("snippet") != observed_operation
                or (
                    "symbol" in item
                    and item.get("symbol") != example["symbol"]
                )
            ):
                reasons.append(
                    "positive pattern does not cite the behavioral example's source operation"
                )
                break

    deterministic_test = dict(example)
    if not tests:
        reasons.append("grounded evidence needs a suggested test")
    else:
        test = tests[0]
        if not isinstance(test, Mapping):
            reasons.append("suggested test is not an object")
        else:
            if test.get("source_input") != example["source_path"]:
                reasons.append("suggested test does not reference the observed source fixture")
            if _json_value(test.get("expected_result")) != _json_value(
                example["expected_result"]
            ):
                reasons.append("suggested test does not state an observed concrete result")
            optional_claims = {
                "source_path": example["source_path"],
                "symbol": example["symbol"],
                "observed_input": example["observed_input"],
                "expected_result": example["expected_result"],
                "variant": example["variant"],
            }
            for field, expected in optional_claims.items():
                if field in test and (
                    _json_value(test[field]) != _json_value(expected)
                    if field == "expected_result"
                    else test[field] != expected
                ):
                    reasons.append(f"suggested test `{field}` differs from evidence")

    candidate = proposal.get("candidate_template")
    if isinstance(candidate, Mapping) and "target_backend" in candidate:
        target_context = evidence.get("target_context")
        allowed_targets: set[str] = set()
        if isinstance(target_context, Mapping):
            if isinstance(target_context.get("backend"), str):
                allowed_targets.add(target_context["backend"])
            candidates = target_context.get("candidate_backends")
            if isinstance(candidates, list):
                allowed_targets.update(item for item in candidates if isinstance(item, str))
        if candidate["target_backend"] not in allowed_targets:
            reasons.append("candidate template target is not present in evidence")

    references = _canonical_evidence_references(evidence, example)
    supplied_references = proposal.get("evidence_references")
    if supplied_references is not None:
        if not isinstance(supplied_references, list) or not all(
            isinstance(item, str) and item in references for item in supplied_references
        ):
            reasons.append("proposal contains an evidence reference absent from the envelope")
    return reasons, {
        "grounded": True,
        "deterministic_test": deterministic_test,
        "evidence_references": references,
    }


def _normalize_worker_proposal(
    proposal: Mapping[str, Any],
    deterministic_test: Mapping[str, Any] | None,
    evidence_references: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(proposal))
    if deterministic_test is not None:
        for pattern in normalized.get("positive_patterns", []):
            if isinstance(pattern, dict):
                pattern["symbol"] = deterministic_test["symbol"]
        normalized["suggested_tests"] = [
            {
                "source_input": deterministic_test["source_path"],
                "expected_result": _json_value(deterministic_test["expected_result"]),
                "source_path": deterministic_test["source_path"],
                "symbol": deterministic_test["symbol"],
                "observed_input": deterministic_test["observed_input"],
                "variant": deterministic_test["variant"],
            }
        ]
    if evidence_references is not None:
        normalized["evidence_references"] = list(evidence_references)
    return normalized


def _duration_ms(response: Mapping[str, Any], field: str) -> float:
    value = response.get(field)
    return round(value / 1_000_000, 3) if isinstance(value, (int, float)) else 0.0


class ProposalWorker:
    """Reuse one pinned local Ollama worker for a serial evidence batch."""

    def __init__(
        self,
        endpoint: str,
        model: str | ModelIdentity = DEFAULT_MODEL,
        expected_digest: str | None = None,
        *,
        active_rule_digest: str,
        batch_id: str | None = None,
        session_id: str | None = None,
        generation_options: Mapping[str, int] = GENERATION_OPTIONS,
        prompt_template_version: str = PROMPT_TEMPLATE_VERSION,
        database: Path | None = None,
    ) -> None:
        self.endpoint = validate_local_endpoint(endpoint)
        self._model = model
        self._expected_digest = expected_digest
        self.active_rule_digest = active_rule_digest
        if not isinstance(active_rule_digest, str) or not active_rule_digest:
            raise ValueError("an active rule digest is required for a worker session")
        self.batch_id = batch_id or f"batch-{uuid.uuid4().hex}"
        self.session_id = session_id or f"session-{uuid.uuid4().hex}"
        self.generation_options = dict(generation_options)
        if self.generation_options != GENERATION_OPTIONS:
            raise ValueError("proposal worker generation options are fixed")
        self.prompt_template_version = prompt_template_version
        if not prompt_template_version:
            raise ValueError("a prompt template version is required")
        self.database = database
        self._session: ProposalWorkerSession | None = None
        self._request_count = 0
        self._session_warmup_ms = 0.0
        self._batch_finalized = False

    @property
    def session(self) -> ProposalWorkerSession | None:
        return self._session

    def start(self) -> ProposalWorkerSession:
        if self._session is not None:
            return self._session
        _assert_session_available(self.database, self.session_id)
        if isinstance(self._model, ModelIdentity):
            validate_model_name(self._model.name)
            if self._expected_digest and self._model.digest != self._expected_digest:
                raise ValueError(
                    f"Ollama model digest differs: expected `{self._expected_digest}`, "
                    f"got `{self._model.digest}`"
                )
            identity = self._model
        else:
            expected_digest = self._expected_digest
            if not expected_digest:
                raise ValueError("an expected local model digest is required")
            identity = resolve_model(self.endpoint, self._model, expected_digest)
        self._session = ProposalWorkerSession(
            session_id=self.session_id,
            batch_id=self.batch_id,
            endpoint=self.endpoint,
            model=identity,
            generation_options=dict(self.generation_options),
            prompt_template_version=self.prompt_template_version,
            prompt_template_digest=_prompt_template_digest(self.prompt_template_version),
            active_rule_digest=self.active_rule_digest,
        )
        _audit_worker_session(self.database, self._session, status="running")
        return self._session

    def _request(self, evidence: dict[str, Any], request_index: int) -> dict[str, Any]:
        session = self.start()
        evidence_rule_digest = evidence.get("active_rule_digest")
        if (
            evidence_rule_digest is not None
            and evidence_rule_digest != session.active_rule_digest
        ):
            raise ValueError("evidence active rule digest differs from worker session")
        request_id = f"request-{uuid.uuid4().hex}"
        payload = _proposal_payload(
            session.model,
            evidence,
            generation_options=session.generation_options,
            prompt_template_version=session.prompt_template_version,
            session=session,
        )
        started = time.perf_counter()
        response = _request_json(f"{self.endpoint}/api/chat", payload)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        timing = {
            "request_index": request_index,
            "request_elapsed_ms": elapsed_ms,
            "load_duration_ms": _duration_ms(response, "load_duration"),
            "prompt_evaluation_ms": _duration_ms(response, "prompt_eval_duration"),
            "generation_ms": _duration_ms(response, "eval_duration"),
            "prompt_tokens": response.get("prompt_eval_count"),
            "generated_tokens": response.get("eval_count"),
        }
        if request_index == 0:
            self._session_warmup_ms = timing["load_duration_ms"]
        self._request_count += 1
        try:
            record = _stage_response(
                response,
                payload,
                session.model,
                evidence,
                session=session,
                prompt_template_version=session.prompt_template_version,
                request_id=request_id,
            )
        except ProposalValidationError as error:
            error.record["timing"] = timing
            raise
        reasons, gate = evidence_gate(record["proposal"], evidence)
        record["evidence_gate"] = gate
        record["timing"] = timing
        if reasons:
            record["status"] = "rejected-llm-proposal"
            record["rejection_reasons"] = reasons
            raise ProposalEvidenceError("; ".join(reasons), record)
        record["proposal"] = _normalize_worker_proposal(
            record["proposal"],
            gate["deterministic_test"],
            gate["evidence_references"],
        )
        record["evidence_references"] = gate["evidence_references"]
        return record

    def propose(self, evidence: dict[str, Any], request_index: int = 0) -> dict[str, Any]:
        if not isinstance(evidence, dict):
            raise ValueError("proposal evidence must be a JSON object")
        read = json.loads(json.dumps(evidence))
        if len(json.dumps(read, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 262_144:
            raise ValueError("proposal evidence exceeds the 256 KiB proposal limit")
        return self._request(read, request_index)

    def run_batch(
        self,
        evidence_packages: Iterable[dict[str, Any]],
        *,
        output_directory: Path | None = None,
    ) -> dict[str, Any]:
        if self._batch_finalized:
            raise ProposalWorkerError(
                f"proposal worker session ID `{self.session_id}` has already been finalized"
            )
        packages = list(evidence_packages)
        if len(packages) > MAX_BATCH_SIZE:
            raise ValueError(f"proposal batches may contain at most {MAX_BATCH_SIZE} evidence packages")
        session = self.start()
        started = time.perf_counter()
        results: list[dict[str, Any]] = []
        accepted = rejected = 0
        current_record: dict[str, Any] | None = None
        try:
            for index, evidence in enumerate(packages):
                try:
                    record = self.propose(evidence, request_index=index)
                    current_record = record
                    path = (
                        write_staged_proposal(output_directory, record)
                        if output_directory is not None
                        else None
                    )
                    accepted += 1
                    results.append(
                        {
                            "request_index": index,
                            "status": "staged",
                            "payload_sha256": record["payload_sha256"],
                            "timing": record["timing"],
                            "path": path.as_posix() if path is not None else None,
                            "record": record,
                        }
                    )
                    _audit_worker_request(self.database, session, record, "staged", ())
                    current_record = None
                except (ProposalValidationError, ProposalEvidenceError) as error:
                    current_record = error.record
                    record = error.record
                    record["status"] = "rejected-llm-proposal"
                    record.setdefault("rejection_reasons", [str(error)])
                    path = (
                        write_rejected_proposal(output_directory, record)
                        if output_directory is not None
                        else None
                    )
                    rejected += 1
                    reasons = record.get("rejection_reasons", [str(error)])
                    results.append(
                        {
                            "request_index": index,
                            "status": "rejected",
                            "payload_sha256": record["payload_sha256"],
                            "timing": record.get("timing"),
                            "reasons": reasons,
                            "path": path.as_posix() if path is not None else None,
                            "record": record,
                        }
                    )
                    _audit_worker_request(self.database, session, record, "rejected", reasons)
                    current_record = None
            report = {
                "status": "completed",
                "session": session.as_dict(),
                "requests": results,
                "summary": {
                    "requested": len(packages),
                    "accepted": accepted,
                    "rejected": rejected,
                    "session_warmup_ms": self._session_warmup_ms,
                    "batch_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "request_count": self._request_count,
                },
            }
            _audit_worker_session(
                self.database,
                session,
                status="completed",
                completed=True,
                summary=report["summary"],
            )
            self._batch_finalized = True
            return report
        except Exception as error:
            self._batch_finalized = True
            if current_record is not None:
                try:
                    _audit_worker_request(
                        self.database,
                        session,
                        current_record,
                        "failed",
                        [str(error)],
                    )
                except Exception:
                    pass
            failure_summary = {
                "requested": len(packages),
                "accepted": accepted,
                "rejected": rejected,
                "request_count": self._request_count,
                "error": str(error),
            }
            try:
                _audit_worker_session(
                    self.database,
                    session,
                    status="failed",
                    completed=True,
                    summary=failure_summary,
                )
            except Exception as finalize_error:
                raise ProposalWorkerError(
                    f"proposal worker failed: {error}; unable to finalize session: "
                    f"{finalize_error}"
                ) from error
            if isinstance(error, ProposalWorkerError):
                raise
            raise ProposalWorkerError(f"proposal worker failed: {error}") from error

    process_batch = run_batch


def _audit_worker_session(
    database: Path | None,
    session: ProposalWorkerSession,
    *,
    status: str,
    completed: bool = False,
    summary: Mapping[str, Any] | None = None,
) -> None:
    if database is None:
        return
    from datetime import UTC, datetime
    from sourcebridge.database import write_transaction
    from sourcebridge.pipeline import EvidencePipelineError, initialize

    try:
        initialize(database)
        now = datetime.now(UTC).isoformat()
        with write_transaction(database) as connection:
            if status == "running":
                connection.execute(
                    """
                    INSERT INTO sourcebridge_proposal_worker_session
                        (id, batch_id, endpoint, model_name, model_digest,
                         generation_options_json, prompt_template_version,
                         prompt_template_digest, active_rule_digest, status,
                         summary_json, started_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL)
                    """,
                    (
                        session.session_id,
                        session.batch_id,
                        session.endpoint,
                        session.model.name,
                        session.model.digest,
                        json.dumps(session.generation_options, sort_keys=True),
                        session.prompt_template_version,
                        session.prompt_template_digest,
                        session.active_rule_digest,
                        json.dumps(summary or {}, sort_keys=True),
                        now,
                    ),
                )
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM sourcebridge_proposal_worker_session_event
                WHERE session_id = ?
                """,
                (session.session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO sourcebridge_proposal_worker_session_event
                    (id, session_id, sequence, status, summary_json, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"event-{uuid.uuid4().hex}",
                    session.session_id,
                    sequence,
                    status,
                    json.dumps(summary or {}, sort_keys=True),
                    now,
                ),
            )
    except sqlite3.IntegrityError as error:
        if status == "running":
            raise ProposalWorkerError(
                f"proposal worker session ID `{session.session_id}` already exists"
            ) from error
        raise ProposalWorkerError(f"cannot append proposal worker session event: {error}") from error
    except (OSError, sqlite3.Error, EvidencePipelineError, ValueError) as error:
        raise ProposalWorkerError(f"cannot audit proposal worker session: {error}") from error


def _assert_session_available(database: Path | None, session_id: str) -> None:
    if database is None:
        return
    from sourcebridge.database import read_transaction
    from sourcebridge.pipeline import EvidencePipelineError, initialize

    try:
        initialize(database)
        with read_transaction(database) as connection:
            row = connection.execute(
                "SELECT 1 FROM sourcebridge_proposal_worker_session WHERE id = ?",
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, EvidencePipelineError, ValueError) as error:
        raise ProposalWorkerError(f"cannot inspect proposal worker session: {error}") from error
    if row is not None:
        raise ProposalWorkerError(f"proposal worker session ID `{session_id}` already exists")


def _audit_worker_request(
    database: Path | None,
    session: ProposalWorkerSession,
    record: Mapping[str, Any],
    status: str,
    reasons: Iterable[str],
) -> None:
    if database is None:
        return
    from datetime import UTC, datetime

    from sourcebridge.database import write_transaction
    from sourcebridge.pipeline import EvidencePipelineError, initialize

    timing = record.get("timing", {})
    if not isinstance(timing, Mapping):
        timing = {}
    if status not in {"staged", "rejected", "failed"}:
        raise ProposalWorkerError(f"unsupported proposal worker request status `{status}`")
    request_id = record.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ProposalWorkerError("proposal worker request is missing immutable request identity")
    try:
        initialize(database)
        with write_transaction(database) as connection:
            connection.execute(
                """
                INSERT INTO sourcebridge_proposal_worker_request
                    (id, session_id, request_index, evidence_sha256, payload_sha256,
                     status, elapsed_ms, load_duration_ms, generation_ms,
                     generated_tokens, rejection_reasons_json, proposal_json,
                     raw_response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    session.session_id,
                    timing.get("request_index", 0),
                    record.get("evidence_sha256", ""),
                    record["payload_sha256"],
                    status,
                    timing.get("request_elapsed_ms", 0),
                    timing.get("load_duration_ms", 0),
                    timing.get("generation_ms", 0),
                    timing.get("generated_tokens"),
                    json.dumps(list(reasons), sort_keys=True),
                    json.dumps(record.get("proposal"), sort_keys=True),
                    json.dumps(record.get("raw_response"), sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
    except sqlite3.IntegrityError as error:
        raise ProposalWorkerError(f"cannot append proposal worker request: {error}") from error
    except (OSError, sqlite3.Error, EvidencePipelineError, ValueError) as error:
        raise ProposalWorkerError(f"cannot audit proposal worker request: {error}") from error
