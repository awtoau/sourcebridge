"""Reusable local Ollama evidence-grounded proposal worker.

The implementation lives with the existing proposal staging contract so the
single-request and repeated-batch paths share model, schema, and provenance
guards.  This module provides a discoverable worker import without creating a
second proposal protocol.
"""

from sourcebridge.proposals import (
    MAX_BATCH_SIZE,
    ProposalEvidenceError,
    ProposalWorker,
    ProposalWorkerError,
    ProposalWorkerSession,
    evidence_gate,
)

__all__ = [
    "MAX_BATCH_SIZE",
    "ProposalEvidenceError",
    "ProposalWorker",
    "ProposalWorkerError",
    "ProposalWorkerSession",
    "evidence_gate",
]
