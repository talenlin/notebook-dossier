"""Viewpoint comparison: merge model/host drafts and enforce comparison invariants.

Invariants (方案 §9.4/§11.2), enforced in code, not in prompts:
- ``not_mentioned``（仅未提及）can never be recorded as ``contradicts``（反对）.
- a ``contradicts`` judgement must trace to verified quotes from at least two
  distinct sources, otherwise it degrades to ``unknown`` with a note.
- reprints sharing a provenance group count as one independent support; the
  machine-generated research report never counts as independent evidence on
  top of the web sources it synthesizes.
"""

from __future__ import annotations

from typing import Iterable

from .evidence import normalize_text, verify_quote
from .models import (
    ComparisonDraft,
    ComparisonRelationDraft,
    ComparisonRow,
    EvidenceRow,
    HostStatementInput,
    Relation,
    SourceRow,
    Stance,
    Statement,
    VerificationStatus,
    utcnow_iso,
)


def build_comparison(
    research_id: str,
    question_id: str,
    draft: ComparisonDraft | None,
    host_input: HostStatementInput | None,
    sources: dict[str, SourceRow],
    evidence: Iterable[EvidenceRow],
    source_texts: dict[str, str | None],
    sequence: int,
    extra_notes: list[str] | None = None,
) -> ComparisonRow:
    notes: list[str] = list(extra_notes or [])
    evidence_rows = list(evidence)

    statements = _merge_statements(draft, host_input, sources, notes)
    relation = _pick_relation(draft, host_input, statements, notes)
    reasons = list(draft.relations[0].reasons) if draft and draft.relations else []
    if host_input and host_input.reasons:
        reasons.extend(host_input.reasons)
    unresolved = list(draft.unresolved_questions) if draft else []
    if host_input:
        unresolved.extend(host_input.unresolved_questions)

    evidence_ids, matched_source_ids = _link_evidence(draft, statements, evidence_rows, source_texts, notes)
    matched_sources = {sid for sid in matched_source_ids if sid in sources}

    # Invariant: "仅未提及" is not opposition.
    if relation == Relation.contradicts and any(s.stance == Stance.not_mentioned for s in statements):
        relation = Relation.not_mentioned
        notes.append("关系由 contradicts 降级为 not_mentioned：存在仅未提及的来源，未提及不能记为反对")

    # Invariant: contradiction must trace to verified quotes from >= 2 distinct sources.
    if relation == Relation.contradicts and len(matched_sources) < 2:
        relation = Relation.unknown
        notes.append(
            f"关系由 contradicts 降级为 unknown：仅匹配到 {len(matched_sources)} 个来源的原文引文，"
            "冲突结论需要双方原文支撑"
        )

    independent = _independent_support_count(statements, sources, notes)

    if any(sources[sid].is_report for sid in matched_sources if sid in sources):
        notes.append("比较中包含机器生成的研究报告：报告不与其依据的网页来源重复计证据")

    return ComparisonRow(
        comparison_id=f"{research_id}.c{sequence:04d}",
        research_id=research_id,
        question_id=question_id,
        statements=statements,
        relation=relation,
        reasons=_dedupe(reasons),
        evidence_ids=evidence_ids,
        unresolved_questions=_dedupe(unresolved),
        independent_support_count=independent,
        validation_notes=_dedupe(notes),
        created_at=utcnow_iso(),
    )


def _merge_statements(
    draft: ComparisonDraft | None,
    host_input: HostStatementInput | None,
    sources: dict[str, SourceRow],
    notes: list[str],
) -> list[Statement]:
    merged: list[Statement] = []
    seen: set[tuple[str, str]] = set()
    candidates = list(draft.statements) if draft else []
    if host_input:
        candidates.extend(host_input.statements)
    for item in candidates:
        if item.source_id not in sources:
            notes.append(f"丢弃来源 {item.source_id} 的陈述：不属于本次研究的来源集合")
            continue
        key = (item.source_id, normalize_text(item.statement))
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            Statement(
                source_id=item.source_id,
                statement=item.statement,
                short_quote=item.short_quote,
                stance=item.stance,
            )
        )
    return merged


def _pick_relation(
    draft: ComparisonDraft | None,
    host_input: HostStatementInput | None,
    statements: list[Statement],
    notes: list[str],
) -> Relation:
    relation = Relation.unknown
    if draft and draft.relations:
        relation = draft.relations[0].relation
    if host_input and host_input.relation is not None:
        relation = host_input.relation
    if relation == Relation.contradicts and not statements:
        notes.append("关系由 contradicts 降级为 unknown：没有可用的来源陈述")
        return Relation.unknown
    return relation


def _link_evidence(
    draft: ComparisonDraft | None,
    statements: list[Statement],
    evidence_rows: list[EvidenceRow],
    source_texts: dict[str, str | None],
    notes: list[str],
) -> tuple[list[str], set[str]]:
    """Match draft quotes to stored evidence cards / source text.

    Returns (evidence_ids, source_ids with at least one verified quote).
    """
    quotes: list[tuple[str, str]] = []
    if draft:
        for relation in draft.relations:
            for item in relation.short_quotes:
                sid = str(item.get("source_id") or "")
                quote = str(item.get("quote") or "")
                if sid and quote:
                    quotes.append((sid, quote))
    for statement in statements:
        if statement.short_quote:
            quotes.append((statement.source_id, statement.short_quote))

    evidence_ids: list[str] = []
    matched_source_ids: set[str] = set()
    for sid, quote in quotes:
        norm = normalize_text(quote)
        card = next(
            (
                ev
                for ev in evidence_rows
                if ev.source_id == sid
                and ev.short_quote
                and (
                    normalize_text(ev.short_quote) == norm
                    or norm in normalize_text(ev.short_quote)
                    or normalize_text(ev.short_quote) in norm
                )
            ),
            None,
        )
        if card is not None:
            if card.evidence_id not in evidence_ids:
                evidence_ids.append(card.evidence_id)
            matched_source_ids.add(sid)
            continue
        status = verify_quote(quote, source_texts.get(sid))
        if status == VerificationStatus.quote_matched:
            matched_source_ids.add(sid)
            notes.append(f"来源 {sid} 的引文原文可匹配，但未对应已有证据卡：未计入 evidence_ids")
        elif status == VerificationStatus.quote_not_found:
            notes.append(f"来源 {sid} 的引文未能在其原文中定位：不计入证据，待人工核验")
        else:
            notes.append(f"来源 {sid} 的原文不可用，无法核验引文")
    return evidence_ids, matched_source_ids


def _independent_support_count(
    statements: list[Statement],
    sources: dict[str, SourceRow],
    notes: list[str],
) -> int:
    groups: set[str] = set()
    report_only = 0
    for statement in statements:
        if statement.stance != Stance.supports:
            continue
        source = sources.get(statement.source_id)
        if source is None:
            continue
        if source.is_report:
            report_only += 1
            continue
        groups.add(source.provenance_group or f"unique:{source.source_id}")
    if report_only:
        notes.append(f"{report_only} 条支持性陈述来自机器生成报告，未计入独立支持数")
    if len(groups) < sum(1 for s in statements if s.stance == Stance.supports and not _is_report(sources, s)):
        notes.append("多个支持性来源属于同一转载组（provenance_group），合并计为 1 份独立支持")
    return len(groups)


def _is_report(sources: dict[str, SourceRow], statement: Statement) -> bool:
    source = sources.get(statement.source_id)
    return bool(source and source.is_report)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
