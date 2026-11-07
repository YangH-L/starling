"""Result types. Plain dataclasses; no dependency on the Starling core."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RememberResult:
    engram_ref: str
    statement_ids: list = field(default_factory=list)
    outcome: str = ""
    extraction_failed: bool = False
    failure_category: str = ""
    failure_detail: str = ""
    catalog_version: str = ""
    source_preserved: bool = False
    structured_claims_persisted: bool = False
    accepted_by_predicate: dict = field(default_factory=dict)
    rejected_by_predicate: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PendingEvent:
    key: str
    text: str
    interlocutor: str
    seq: int
    failure_category: str
    holder: str = ""


@dataclass
class EmbedStats:
    embedded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class StatementRow:
    id: str
    tenant_id: str
    holder_id: str
    holder_perspective: str
    subject_kind: str
    subject_id: str
    predicate: str
    object_kind: str
    object_value: str
    canonical_object_hash: str
    modality: str
    polarity: str
    confidence: float
    observed_at: str
    valid_from: str
    valid_to: str
    consolidation_state: str
    review_status: str
    evidence_json: str
