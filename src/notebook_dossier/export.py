"""Export the article-research package (方案 §12 目录) for one research job.

Layout written under ``<output_dir>/``:
    manifest.json          主题、任务、覆盖统计、文件清单与校验摘要
    notebooklm-report.md   完整原始研究报告（若已保存）
    sources.json           发现/选择/导入/读取状态与去重映射（不含正文）
    evidence.json          逐来源主张与原文支撑
    viewpoints.md          共识、差异、条件与证据缺口
    writing-brief.md       文章角度、论证提纲、引用建议

Secrets: the export only serializes allowlisted model fields and passes a
credential-key scan before writing; a hit aborts the export.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import Config
from .models import VerificationStatus, utcnow_iso
from .stats import coverage_summary
from .store import Store

_SECRET_KEYS = {
    "cookie",
    "cookies",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "session",
    "sid",
    "hsid",
    "ssid",
    "apisid",
    "sapisid",
}
_SECRET_LINE_RE = re.compile(r"^\s*(cookie|set-cookie|authorization)\s*:", re.IGNORECASE)

RELATION_LABELS = {
    "supports": "直接支持/一致",
    "contradicts": "直接反对/冲突",
    "conditional_difference": "适用条件不同",
    "definitional_difference": "术语口径不同",
    "temporal_difference": "时间变化导致",
    "evidence_strength_difference": "证据强度不同",
    "not_mentioned": "仅未提及",
    "unrelated_scope": "讨论对象/范围不同",
    "unknown": "待判定",
}


def export_research(
    store: Store,
    config: Config,
    research_id: str,
    output_dir: str | None = None,
    angles: list[str] | None = None,
) -> dict[str, Any]:
    job = store.get_job(research_id)
    if job is None:
        raise KeyError(f"unknown research_id: {research_id}")
    out = Path(output_dir) if output_dir else config.export_root / research_id
    out.mkdir(parents=True, exist_ok=True)

    sources = store.list_sources(research_id)
    questions = store.list_questions(research_id)
    evidence = store.list_evidence(research_id)
    comparisons = store.list_comparisons(research_id)
    answers = store.list_answers(research_id)
    tasks = store.list_tasks(research_id)
    coverage = coverage_summary(store, research_id)

    question_text = {q.question_id: q.text for q in questions}
    source_by_id = {s.source_id: s for s in sources}

    sources_payload = [
        {
            **s.model_dump(mode="json"),
            "text_version": _text_version(store, research_id, s.source_id),
        }
        for s in sources
    ]
    evidence_payload = [ev.model_dump(mode="json") for ev in evidence]

    files: dict[str, str] = {}

    report_src = Path(job.report_path) if job.report_path else None
    report_text = ""
    if report_src and report_src.exists():
        report_text = report_src.read_text(encoding="utf-8")
        (out / "notebooklm-report.md").write_text(report_text, encoding="utf-8")
        files["notebooklm-report.md"] = _sha256(report_text)

    _write_json(out, "sources.json", sources_payload, files)
    _write_json(out, "evidence.json", evidence_payload, files)

    viewpoints_md = _render_viewpoints(question_text, comparisons, source_by_id, evidence)
    _write_text(out, "viewpoints.md", viewpoints_md, files)

    brief_md = _render_brief(
        job=job,
        coverage=coverage,
        questions=questions,
        sources=sources,
        evidence=evidence,
        comparisons=comparisons,
        answers=answers,
        tasks=tasks,
        angles=angles or [],
    )
    _write_text(out, "writing-brief.md", brief_md, files)

    manifest = {
        "research_id": research_id,
        "topic": job.topic,
        "scope": job.scope,
        "provider": job.provider,
        "notebook_id": job.notebook_id,
        "upstream_task_id": job.upstream_task_id,
        "mode_requested": job.mode_requested,
        "mode_actual": job.mode_actual,
        "job_status": job.status.value,
        "generated_at": utcnow_iso(),
        "coverage": coverage,
        "files": [{"name": name, "sha256": digest} for name, digest in sorted(files.items())],
        "verification_summary": _verification_summary(evidence),
        "errors": job.errors,
    }
    _write_json(out, "manifest.json", manifest, files)

    return {
        "status": "success",
        "research_id": research_id,
        "output_dir": str(out),
        "files": sorted(files),
        "coverage": coverage,
        "verification_summary": manifest["verification_summary"],
    }


# ---------------------------------------------------------------- rendering


def _render_viewpoints(question_text, comparisons, source_by_id, evidence) -> str:
    lines = ["# 观点比较：共识、差异与证据缺口", ""]
    by_question: dict[str, list] = {}
    for comp in comparisons:
        by_question.setdefault(comp.question_id, []).append(comp)
    if not by_question:
        lines.append("（尚无观点比较结果；先调用 dossier_compare）")
    for qid, comps in by_question.items():
        lines.append(f"## {question_text.get(qid, qid)}")
        lines.append("")
        for comp in comps:
            label = RELATION_LABELS.get(comp.relation.value, comp.relation.value)
            lines.append(f"### 关系判定：{label}（`{comp.relation.value}`）")
            lines.append("")
            lines.append("| 来源 | 立场 | 陈述 | 短引文 |")
            lines.append("| --- | --- | --- | --- |")
            for st in comp.statements:
                src = source_by_id.get(st.source_id)
                title = (src.title if src else None) or st.source_id
                quote = (st.short_quote or "").replace("|", "\\|").replace("\n", " ")
                statement = st.statement.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {title} | {st.stance.value} | {statement} | {quote} |")
            lines.append("")
            if comp.reasons:
                lines.append("**差异原因**")
                lines.extend(f"- {r}" for r in comp.reasons)
                lines.append("")
            if comp.evidence_ids:
                lines.append("**证据引用**")
                for eid in comp.evidence_ids:
                    ev = next((e for e in evidence if e.evidence_id == eid), None)
                    if ev:
                        lines.append(
                            f"- `{eid}`（{ev.verification_status.value}）：{ev.claim} —— “{ev.short_quote or ''}”"
                        )
                    else:
                        lines.append(f"- `{eid}`")
                lines.append("")
            lines.append(f"**独立支持数**（同转载组合并、报告不计）：{comp.independent_support_count}")
            lines.append("")
            if comp.validation_notes:
                lines.append("**校验记录**")
                lines.extend(f"- {n}" for n in comp.validation_notes)
                lines.append("")
            if comp.unresolved_questions:
                lines.append("**未决问题**")
                lines.extend(f"- {u}" for u in comp.unresolved_questions)
                lines.append("")
    return "\n".join(lines) + "\n"


def _render_brief(job, coverage, questions, sources, evidence, comparisons, answers, tasks, angles) -> str:
    lines = ["# 文章研究底稿", ""]
    lines.append(f"研究主题：{job.topic}")
    if job.scope:
        lines.append(f"研究范围：{job.scope}")
    lines.append(f"生成时间：{utcnow_iso()}；来源统计：{json.dumps(coverage, ensure_ascii=False)}")
    lines.append("")

    lines.append("## 一、知识框架与关键概念")
    lines.append("")
    lines.append("原始研究报告见 `notebooklm-report.md`（机器生成的综合材料，单独标记，不与其依据的网页来源重复计证据）。")
    lines.append("")
    views = [s for s in sources if s.source_view]
    if views:
        lines.append("| 来源 | 核心论点 | 立场 | 时间范围 | 对象范围 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s in views:
            v = s.source_view
            lines.append(
                "| {title} | {thesis} | {stance} | {time} | {pop} |".format(
                    title=(s.title or s.source_id).replace("|", "\\|"),
                    thesis=str(v.get("main_thesis") or "").replace("|", "\\|").replace("\n", " "),
                    stance=str(v.get("stance") or "未知"),
                    time=str(v.get("time_scope") or "未知"),
                    pop=str(v.get("population_scope") or "未知"),
                )
            )
        lines.append("")

    lines.append("## 二、逐来源支撑表")
    lines.append("")
    if not evidence:
        lines.append("（尚无证据卡；先调用 dossier_evidence）")
        lines.append("")
    by_source: dict[str, list] = {}
    for ev in evidence:
        by_source.setdefault(ev.source_id, []).append(ev)
    for s in sources:
        evs = by_source.get(s.source_id, [])
        if not evs:
            continue
        lines.append(f"### {s.title or s.source_id}")
        lines.append("")
        lines.append(f"来源：{s.original_url or s.canonical_url or '（无 URL）'}")
        if s.published_at:
            lines.append(f"发布/更新时间：{s.published_at}")
        lines.append(f"证据强度标签：{', '.join(sorted({e.evidence_kind.value for e in evs}))}")
        lines.append("")
        lines.append("| 主张 | 依据类型 | 短引文 | 定位 | 适用条件 | 局限 | 验证 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for ev in evs:
            lines.append(
                "| {claim} | {kind} | {quote} | {loc} | {cond} | {lim} | {ver} |".format(
                    claim=ev.claim.replace("|", "\\|").replace("\n", " "),
                    kind=ev.evidence_kind.value,
                    quote=(ev.short_quote or "").replace("|", "\\|").replace("\n", " "),
                    loc=ev.locator or "未知",
                    cond=ev.conditions or "未知",
                    lim=ev.limitations or "未知",
                    ver=ev.verification_status.value,
                )
            )
        lines.append("")

    lines.append("## 三、观点差异及原因")
    lines.append("")
    if not comparisons:
        lines.append("（尚无观点比较；先调用 dossier_compare）")
    for comp in comparisons:
        label = RELATION_LABELS.get(comp.relation.value, comp.relation.value)
        qtext = next((q.text for q in questions if q.question_id == comp.question_id), comp.question_id)
        lines.append(f"- **{qtext}** → {label}（`{comp.relation.value}`）")
        for r in comp.reasons:
            lines.append(f"  - 原因：{r}")
        for eid in comp.evidence_ids:
            lines.append(f"  - 证据：`{eid}`")
        for u in comp.unresolved_questions:
            lines.append(f"  - 未决：{u}")
    lines.append("")

    lines.append("## 四、可写角度和论证提纲")
    lines.append("")
    host_angles = [a.strip() for a in angles if a and a.strip()]
    if host_angles:
        lines.append("### 给定角度")
        lines.extend(f"- {a}" for a in host_angles)
        lines.append("")
    if comparisons:
        lines.append("### 由证据生成的角度候选")
        for comp in comparisons:
            qtext = next((q.text for q in questions if q.question_id == comp.question_id), comp.question_id)
            matched = sum(
                1
                for eid in comp.evidence_ids
                for ev in evidence
                if ev.evidence_id == eid and ev.verification_status == VerificationStatus.quote_matched
            )
            lines.append(
                f"- 角度：围绕「{qtext}」—— 关系 `{comp.relation.value}`，"
                f"引用证据 {len(comp.evidence_ids)} 条（原文匹配 {matched} 条），"
                f"独立支持 {comp.independent_support_count} 组。"
            )
        lines.append("")
    if not host_angles and not comparisons:
        lines.append("（证据与比较齐备后自动生成角度候选；也可通过 dossier_export 的 angles 参数补充）")
        lines.append("")

    lines.append("## 五、尚缺证据与待核验项")
    lines.append("")
    gaps: list[str] = []
    for ev in evidence:
        if ev.verification_status == VerificationStatus.quote_not_found:
            gaps.append(f"引文未在原文中定位：`{ev.evidence_id}`（{ev.claim[:60]}）")
        if ev.verification_status == VerificationStatus.text_unavailable:
            gaps.append(f"原文不可用，未核验：`{ev.evidence_id}`")
    for comp in comparisons:
        for u in comp.unresolved_questions:
            gaps.append(f"未决问题：{u}")
    missing_meta = [s for s in sources if not (s.author and s.published_at)]
    if missing_meta:
        gaps.append(f"{len(missing_meta)} 个来源缺作者或发布时间元数据（保持未知，不由模型补齐）")
    for t in tasks:
        if t.status.value == "interrupted":
            gaps.append(f"任务 {t.task_id}（{t.kind}）被中断，需重新调用对应工具续跑")
    if coverage["discovered"] and not coverage["imported"]:
        gaps.append("尚未导入任何来源，证据面不完整")
    if not gaps:
        gaps.append("无")
    lines.extend(f"- {g}" for g in gaps)
    lines.append("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ helpers


def _text_version(store: Store, research_id: str, source_id: str) -> dict[str, Any] | None:
    text = store.get_source_text(research_id, source_id)
    if text is None:
        return None
    return {
        "text_hash": text.text_hash,
        "char_count": text.char_count,
        "retrieved_at": text.retrieved_at,
        "fetch_status": text.fetch_status,
    }


def _verification_summary(evidence) -> dict[str, int]:
    summary: dict[str, int] = {}
    for ev in evidence:
        summary[ev.verification_status.value] = summary.get(ev.verification_status.value, 0) + 1
    return summary


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_no_secrets(payload: Any, where: str) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).strip().lower() in _SECRET_KEYS:
                raise ValueError(f"export aborted: credential-like key {key!r} in {where}")
            _assert_no_secrets(value, where)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_secrets(item, where)
    elif isinstance(payload, str):
        for line in payload.splitlines():
            if _SECRET_LINE_RE.match(line):
                raise ValueError(f"export aborted: credential-like line in {where}: {line[:40]!r}")


def _write_json(out: Path, name: str, payload: Any, files: dict[str, str]) -> None:
    _assert_no_secrets(payload, name)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _assert_no_secrets(text, name)
    (out / name).write_text(text, encoding="utf-8")
    files[name] = _sha256(text)


def _write_text(out: Path, name: str, text: str, files: dict[str, str]) -> None:
    _assert_no_secrets(text, name)
    (out / name).write_text(text, encoding="utf-8")
    files[name] = _sha256(text)
