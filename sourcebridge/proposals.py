"""Local-Ollama rule proposal staging with reproducible provenance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_MODEL = "qwen2.5-coder:14b"
PROMPT_TEMPLATE_VERSION = "local-rule-proposal-v1"
GENERATION_OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 8192}
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


@dataclass(frozen=True)
class ModelIdentity:
    name: str
    digest: str


class ProposalValidationError(ValueError):
    """A model response that must be retained for prompt/schema review."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


def validate_local_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("Ollama endpoint must use http on localhost, 127.0.0.1, or ::1")
    if parsed.path not in ("", "/"):
        raise ValueError("Ollama endpoint must not include a path")
    return endpoint.rstrip("/")


def validate_model_name(model: str) -> None:
    if model != DEFAULT_MODEL:
        raise ValueError(f"model must be the approved local model `{DEFAULT_MODEL}`")
    if ":cloud" in model:
        raise ValueError("cloud-routed Ollama models are not allowed")


def _request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
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
                "items": {
                    "type": "object",
                    "required": ["snippet", "why_it_matches"],
                    "properties": {"snippet": {"type": "string"}, "why_it_matches": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "negative_patterns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["snippet", "why_it_must_not_match"],
                    "properties": {"snippet": {"type": "string"}, "why_it_must_not_match": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "suggested_tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source_input", "expected_result"],
                    "properties": {"source_input": {"type": "string"}, "expected_result": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }


def validate_proposal(proposal: Any) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object")
    if set(proposal) != _REQUIRED_PROPOSAL_FIELDS:
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
    _validate_pattern_list(
        proposal["positive_patterns"], "positive_patterns", "why_it_matches")
    _validate_pattern_list(
        proposal["negative_patterns"], "negative_patterns", "why_it_must_not_match")
    for item in proposal["suggested_tests"]:
        if not isinstance(item, dict) or set(item) != {"source_input", "expected_result"}:
            raise ValueError("proposal suggested_tests entries must describe source and expected result")
        if not all(isinstance(value, str) for value in item.values()):
            raise ValueError("proposal suggested_tests values must be strings")
    if not all(isinstance(item, str) for item in proposal["open_questions"]):
        raise ValueError("proposal open_questions entries must be strings")
    return proposal


def _validate_pattern_list(items: list[Any], field: str, reason: str) -> None:
    for item in items:
        if not isinstance(item, dict) or set(item) != {"snippet", reason}:
            raise ValueError(f"proposal {field} entries must contain snippet and {reason}")
        if not all(isinstance(value, str) for value in item.values()):
            raise ValueError(f"proposal {field} values must be strings")


def stage_proposal(
    endpoint: str,
    model: ModelIdentity,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "model": model.name,
        "stream": False,
        "format": _proposal_schema(),
        "options": GENERATION_OPTIONS,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return only one JSON object matching the supplied schema. "
                    "Propose a rule candidate; never claim validation or write code."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                        "evidence": evidence,
                    },
                    sort_keys=True,
                ),
            },
        ],
    }
    response = _request_json(f"{endpoint}/api/chat", payload)
    message = response.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("Ollama chat response does not contain message content")
    try:
        proposal = json.loads(message["content"])
    except json.JSONDecodeError as error:
        proposal = None
        parse_error = f"Ollama proposal is not valid JSON: {error}"
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    record = {
        "status": "unvalidated-llm-proposal",
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "model": asdict(model),
        "generation_options": GENERATION_OPTIONS,
        "payload_sha256": payload_hash,
        "evidence": evidence,
        "proposal": proposal,
        "raw_response": response,
    }
    if proposal is None:
        raise ProposalValidationError(parse_error, record)
    try:
        validate_proposal(proposal)
    except ValueError as error:
        raise ProposalValidationError(str(error), record) from error
    return record


def write_staged_proposal(output_directory: Path, staged: dict[str, Any]) -> Path:
    root = output_directory.resolve()
    if root.name != "proposals" or root.parent.name != "evidence":
        raise ValueError("proposal output directory must be `evidence/proposals`")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{staged['payload_sha256']}.json"
    path.write_text(json.dumps(staged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_rejected_proposal(output_directory: Path, rejected: dict[str, Any]) -> Path:
    root = output_directory.resolve() / "rejected"
    if root.parent.name != "proposals" or root.parent.parent.name != "evidence":
        raise ValueError("proposal output directory must be `evidence/proposals`")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{rejected['payload_sha256']}.json"
    path.write_text(json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
