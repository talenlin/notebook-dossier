"""Comparison invariants: 未提及≠反对、冲突需双方原文、转载合并计、报告不重复计。"""

from __future__ import annotations

from conftest import json_answer, call
from notebook_dossier.compare import build_comparison
from notebook_dossier.models import (
    ComparisonDraft,
    ComparisonRelationDraft,
    ComparisonStatementDraft,
    EvidenceRow,
    HostStatementInput,
    ImportStatus,
    Relation,
    SourceRow,
    Stance,
    VerificationStatus,
)


def _source(sid: str, group: str, is_report: bool = False) -> SourceRow:
    return SourceRow(
        source_id=sid,
        research_id="r1",
        candidate_id=sid,
        position=int(sid[-1]),
        provenance_group=group,
        is_report=is_report,
        import_status=ImportStatus.imported,
        notebook_source_id=f"nb-{sid}",
    )


def _evidence(eid: str, sid: str, quote: str) -> EvidenceRow:
    return EvidenceRow(
        evidence_id=eid,
        research_id="r1",
        source_id=sid,
        question_id="q1",
        claim=f"{sid} 的主张",
        short_quote=quote,
        verification_status=VerificationStatus.quote_matched,
    )


def _draft(relation: Relation, statements, quotes) -> ComparisonDraft:
    return ComparisonDraft(
        statements=statements,
        relations=[
            ComparisonRelationDraft(
                relation=relation,
                from_source_id=statements[0].source_id if statements else None,
                to_source_id=statements[1].source_id if len(statements) > 1 else None,
                reasons=["原因"],
                short_quotes=quotes,
            )
        ],
        unresolved_questions=["遗留问题"],
    )


def test_not_mentioned_is_never_contradiction():
    sources = {"s1": _source("s1", "g1"), "s2": _source("s2", "g2")}
    statements = [
        ComparisonStatementDraft(source_id="s1", statement="A 有此结论", short_quote="abc", stance=Stance.supports),
        ComparisonStatementDraft(source_id="s2", statement="B 未提及", stance=Stance.not_mentioned),
    ]
    draft = _draft(
        Relation.contradicts,
        statements,
        [{"source_id": "s1", "quote": "abc"}, {"source_id": "s2", "quote": "def"}],
    )
    row = build_comparison(
        "r1",
        "q1",
        draft,
        None,
        sources,
        [_evidence("r1.e0001", "s1", "abc"), _evidence("r1.e0002", "s2", "def")],
        {"s1": "abc content", "s2": "def content"},
        1,
    )
    assert row.relation == Relation.not_mentioned
    assert any("未提及不能记为反对" in n for n in row.validation_notes)


def test_contradiction_needs_two_verified_originals():
    sources = {"s1": _source("s1", "g1"), "s2": _source("s2", "g2")}
    statements = [
        ComparisonStatementDraft(source_id="s1", statement="A 结论", short_quote="abc", stance=Stance.supports),
        ComparisonStatementDraft(source_id="s2", statement="B 相反结论", short_quote="不存在的引文", stance=Stance.opposes),
    ]
    draft = _draft(
        Relation.contradicts,
        statements,
        [{"source_id": "s1", "quote": "abc"}, {"source_id": "s2", "quote": "不存在的引文"}],
    )
    row = build_comparison(
        "r1",
        "q1",
        draft,
        None,
        sources,
        [_evidence("r1.e0001", "s1", "abc")],
        {"s1": "abc content", "s2": "其他内容"},
        1,
    )
    assert row.relation == Relation.unknown
    assert any("双方原文支撑" in n for n in row.validation_notes)
    assert row.evidence_ids == ["r1.e0001"]


def test_reprints_merge_and_report_never_counts():
    sources = {
        "s1": _source("s1", "g1"),
        "s2": _source("s2", "g1"),  # 同一转载组
        "s3": _source("s3", "g2", is_report=True),  # 机器生成报告
    }
    statements = [
        ComparisonStatementDraft(source_id="s1", statement="支持", stance=Stance.supports),
        ComparisonStatementDraft(source_id="s2", statement="同样支持", stance=Stance.supports),
        ComparisonStatementDraft(source_id="s3", statement="报告也支持", stance=Stance.supports),
    ]
    row = build_comparison("r1", "q1", _draft(Relation.supports, statements, []), None, sources, [], {}, 1)
    assert row.independent_support_count == 1
    assert any("转载组" in n for n in row.validation_notes)
    assert any("机器生成报告" in n for n in row.validation_notes)


def test_host_statements_merge_and_foreign_source_dropped():
    sources = {"s1": _source("s1", "g1")}
    host = HostStatementInput(
        question="Q？",
        statements=[
            ComparisonStatementDraft(source_id="s1", statement="宿主判断", stance=Stance.supports),
            ComparisonStatementDraft(source_id="foreign", statement="外来来源", stance=Stance.supports),
        ],
        relation=Relation.conditional_difference,
        reasons=["研究条件不同：样本量差异"],
        unresolved_questions=["仍需原始数据"],
    )
    row = build_comparison("r1", "q1", None, host, sources, [], {}, 1)
    assert row.relation == Relation.conditional_difference
    assert [s.source_id for s in row.statements] == ["s1"]
    assert any("不属于本次研究" in n for n in row.validation_notes)
    assert row.unresolved_questions == ["仍需原始数据"]


def test_comparison_prompt_payload_is_bounded():
    from notebook_dossier.prompts import build_comparison_prompt

    claims = [
        {"source_id": f"s{i}", "claim": "长主张" * 100, "short_quote": "长引文" * 100, "verification_status": "quote_matched"}
        for i in range(40)
    ]
    prompt = build_comparison_prompt("问题？", [{"source_id": "s0", "title": "t"}], claims)
    # 上游会拒绝超长查询参数（实测 "The query request is invalid"）
    assert len(prompt) < 5000
    assert prompt.count('"claim"') <= 14


def test_extra_notes_flow_into_validation_notes():
    from notebook_dossier.compare import build_comparison

    row = build_comparison(
        "r1",
        "q1",
        None,
        None,
        {},
        [],
        {},
        1,
        extra_notes=["证据卡负载截断：40→14 条（上游查询参数上限）"],
    )
    assert any("截断" in n for n in row.validation_notes)


async def test_viewpoints_compare_tool_end_to_end(client, provider, server):
    from conftest import start_research

    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()

    # 先逐来源生成证据卡（观点比较以证据卡为基础）
    provider.answer_fn = lambda prompt: json_answer(
        {
            "source_view": {"main_thesis": "温度是主因", "stance": "支持"},
            "claims": [
                {
                    "claim": "温度升高会加速老化",
                    "claim_type": "fact",
                    "short_quote": "温度每升高 10 度，寿命减半",
                    "evidence_kind": "experiment",
                }
            ],
        }
    )
    await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000", f"{research_id}.s001"],
        questions=["温度如何影响寿命？"],
    )
    await server.app.drain_background()

    s0, s1 = f"{research_id}.s000", f"{research_id}.s001"

    def answer_fn(prompt: str) -> str:
        return json_answer(
            {
                "statements": [
                    {
                        "source_id": s0,
                        "statement": "A 认为温度是寿命主因",
                        "short_quote": "温度每升高 10 度，寿命减半",
                        "stance": "supports",
                    },
                    {"source_id": s1, "statement": "B 未讨论温度", "stance": "not_mentioned"},
                ],
                "relations": [
                    {
                        "relation": "contradicts",
                        "from_source_id": s0,
                        "to_source_id": s1,
                        "reasons": ["研究条件不同：样本与指标不同"],
                        "short_quotes": [{"source_id": s0, "quote": "温度每升高 10 度，寿命减半"}],
                    }
                ],
                "unresolved_questions": ["湿度的影响？"],
            }
        )

    provider.answer_fn = answer_fn
    out = await call(client, "dossier_compare", research_id=research_id, questions=["温度如何影响寿命？"])
    assert out["status"] == "success"
    await server.app.drain_background()

    result = await call(client, "dossier_result", research_id=research_id, view="comparisons")
    assert result["total"] == 1
    comp = result["items"][0]
    # 仅未提及 ≠ 反对：关系被软件降级
    assert comp["relation"] == "not_mentioned"
    assert any("未提及不能记为反对" in n for n in comp["validation_notes"])
    assert comp["unresolved_questions"] == ["湿度的影响？"]
    assert {s["source_id"] for s in comp["statements"]} == {s0, s1}
