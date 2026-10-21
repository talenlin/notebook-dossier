"""Domain model: persisted rows and validated ingestion drafts.

Row models are stored in SQLite via ``model_dump``/``model_validate`` round-trips.
Draft models validate text produced by NotebookLM before it is persisted; a
draft that fails validation is reported as partial/parse_error, never silently
coerced into a passing result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Relation(str, Enum):
    supports = "supports"  # 直接支持 / 一致
    contradicts = "contradicts"  # 直接反对 / 事实或结论冲突
    conditional_difference = "conditional_difference"  # 适用条件不同
    definitional_difference = "definitional_difference"  # 术语口径不同
    temporal_difference = "temporal_difference"  # 时间变化导致
    evidence_strength_difference = "evidence_strength_difference"  # 证据强度不同
    not_mentioned = "not_mentioned"  # 仅未提及
    unrelated_scope = "unrelated_scope"  # 讨论对象/范围不同，不构成冲突
    unknown = "unknown"


class Stance(str, Enum):
    supports = "supports"
    opposes = "opposes"
    partial = "partial"
    unclear = "unclear"
    not_mentioned = "not_mentioned"


class ClaimType(str, Enum):
    fact = "fact"
    opinion = "opinion"
    interpretation = "interpretation"
    prediction = "prediction"
    definition = "definition"
    other = "other"


class EvidenceKind(str, Enum):
    experiment = "experiment"
    data = "data"
    citation = "citation"
    example = "example"
    assertion = "assertion"
    definition = "definition"
    other = "other"


class VerificationStatus(str, Enum):
    unchecked = "unchecked"
    quote_matched = "quote_matched"  # 原文摘录已匹配
    quote_not_found = "quote_not_found"
    text_unavailable = "text_unavailable"
    analyst_confirmed = "analyst_confirmed"  # 原文支持该解释（人工确认）
    independent_corroboration = "independent_corroboration"  # 已有独立证据互证


class ImportStatus(str, Enum):
    discovered = "discovered"
    selected = "selected"
    excluded = "excluded"
    imported = "imported"
    import_failed = "import_failed"
    import_uncertain = "import_uncertain"  # 结果不确定，保留而不是猜测


class ReadStatus(str, Enum):
    pending = "pending"
    readable = "readable"
    unreadable = "unreadable"
    skipped = "skipped"


class AnalyzedStatus(str, Enum):
    pending = "pending"
    analyzed = "analyzed"
    partial = "partial"
    parse_error = "parse_error"


class JobStatus(str, Enum):
    researching = "researching"
    results_ready = "results_ready"
    error = "error"


class TaskStatus(str, Enum):
    running = "running"
    completed = "completed"
    error = "error"
    interrupted = "interrupted"


# ---------------------------------------------------------------- row models


class ResearchJob(BaseModel):
    research_id: str
    request_key: str | None = None
    provider: str = "notebooklm"
    account_ref: str | None = None
    notebook_id: str | None = None
    upstream_task_id: str | None = None
    topic: str
    scope: str | None = None
    mode_requested: str = "deep"
    mode_actual: str | None = None
    status: JobStatus = JobStatus.researching
    task_id_conflict: bool = False
    upstream_task_id_observed: str | None = None
    report_path: str | None = None
    report_text_hash: str | None = None
    report_saved_at: str | None = None
    cite_map: dict[str, str] = Field(default_factory=dict)
    cite_map_basis: str | None = None
    citation_warnings: list[dict[str, Any]] = Field(default_factory=list)
    citation_score: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class Question(BaseModel):
    question_id: str
    research_id: str
    text: str
    created_at: str = Field(default_factory=utcnow_iso)


class SourceRow(BaseModel):
    source_id: str
    research_id: str
    candidate_id: str
    position: int
    original_url: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    text_hash: str | None = None
    result_type: str | None = None
    is_report: bool = False
    cited_in_report: bool = False
    importable: bool = True
    provenance_group: str | None = None
    import_status: ImportStatus = ImportStatus.discovered
    import_reason: str | None = None
    notebook_source_id: str | None = None
    source_view: dict[str, Any] = Field(default_factory=dict)
    read_status: ReadStatus = ReadStatus.pending
    analyzed_status: AnalyzedStatus = AnalyzedStatus.pending
    verification_status: VerificationStatus = VerificationStatus.unchecked
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class SourceText(BaseModel):
    source_id: str
    research_id: str
    text_path: str | None = None
    text_hash: str | None = None
    char_count: int = 0
    title: str | None = None
    source_type: str | None = None
    fetch_status: str = "pending"
    retrieved_at: str | None = None


class EvidenceRow(BaseModel):
    evidence_id: str
    research_id: str
    source_id: str
    source_text_hash: str | None = None
    question_id: str
    claim: str
    claim_type: ClaimType = ClaimType.other
    short_quote: str | None = None
    locator: str | None = None
    evidence_kind: EvidenceKind = EvidenceKind.other
    conditions: str | None = None
    limitations: str | None = None
    verification_status: VerificationStatus = VerificationStatus.unchecked
    extraction_status: str = "ok"
    created_at: str = Field(default_factory=utcnow_iso)


class Statement(BaseModel):
    source_id: str
    statement: str
    short_quote: str | None = None
    stance: Stance = Stance.unclear


class ComparisonRow(BaseModel):
    comparison_id: str
    research_id: str
    question_id: str
    statements: list[Statement] = Field(default_factory=list)
    relation: Relation = Relation.unknown
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    independent_support_count: int = 0
    validation_notes: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)


class AnswerRow(BaseModel):
    answer_id: str
    research_id: str
    notebook_id: str
    question: str
    question_id: str | None = None
    conversation_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    answer_text: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)


class TaskRow(BaseModel):
    task_id: str
    research_id: str
    kind: str
    status: TaskStatus = TaskStatus.running
    owner_id: str | None = None
    heartbeat_at: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=utcnow_iso)
    started_at: str | None = None
    finished_at: str | None = None


class AuditEntry(BaseModel):
    """Append-only call audit record (P1: 多宿主共库时的归因)。"""

    id: int | None = None
    ts: str = Field(default_factory=utcnow_iso)
    actor: str
    tool: str
    research_id: str | None = None
    args_digest: str
    args_keys: list[str] = Field(default_factory=list)


# ------------------------------------------------------- ingestion drafts


class ExtractionClaimDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str
    claim_type: ClaimType = ClaimType.other
    short_quote: str | None = None
    locator: str | None = None
    evidence_kind: EvidenceKind = EvidenceKind.other
    conditions: str | None = None
    limitations: str | None = None


class ExtractionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_view: dict[str, Any] = Field(default_factory=dict)
    claims: list[ExtractionClaimDraft] = Field(default_factory=list)


class ComparisonStatementDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_id: str
    statement: str
    short_quote: str | None = None
    stance: Stance = Stance.unclear


class ComparisonRelationDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    relation: Relation = Relation.unknown
    from_source_id: str | None = None
    to_source_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    short_quotes: list[dict[str, str]] = Field(default_factory=list)


class ComparisonDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statements: list[ComparisonStatementDraft] = Field(default_factory=list)
    relations: list[ComparisonRelationDraft] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class HostStatementInput(BaseModel):
    """Optional host-AI supplied comparison draft, merged before validation."""

    model_config = ConfigDict(extra="ignore")

    question: str
    statements: list[ComparisonStatementDraft] = Field(default_factory=list)
    relation: Relation | None = None
    reasons: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
