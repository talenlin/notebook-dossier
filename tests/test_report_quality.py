"""Report quality & visibility: citation consistency, status phases, cite map, digest paging.

对齐外部评审四条：①引用可靠性 ②进度可见 ③中间态可读 ④结果体积。
"""

from __future__ import annotations

from conftest import EXTRACTION_ANSWER, call, json_answer, start_research


def _mismatched_case_status() -> dict:
    return {
        "status": "completed",
        "notebook_id": "nb-1",
        "task_id": "task-1",
        "sources_found": 2,
        "sources": [
            {"url": "", "title": "Deep Report", "result_type": "deep_report", "cited": False},
            {"url": "https://a.example/x", "title": "（2021）最高法知行终847号行政判决书", "cited": True},
        ],
        "report": "最高人民法院在（2020）最高法知行终3421号裁定中确立了相应规则 [cite: 1]。",
        "message": "done",
    }


async def test_citation_check_flags_mismatched_case_number(client, provider, server):
    """① 复现评审案例：报告案号与被引来源案号不符 → high 告警 + source_says 给出来源取值。"""
    provider.status_response = _mismatched_case_status()
    research_id = await start_research(client, provider)

    status = await call(client, "dossier_status", research_id=research_id)
    assert status["citation_warnings_summary"].get("high") == 1

    result = await call(client, "dossier_result", research_id=research_id, view="findings")
    assert result["cite_map"] == {"1": f"{research_id}.s001"}  # 报告自身不占引用位
    w = result["citation_warnings"][0]
    assert w["claim"] == "（2020）最高法知行终3421号"
    assert w["severity"] == "high"
    assert w["source_says"] == ["（2021）最高法知行终847号"]
    assert w["source_ids"] == [f"{research_id}.s001"]


async def test_citation_check_validates_cite_map_basis(client, provider, server):
    """①③：实体命中时 cite_map 自检标记 validated，未命中实体不阻止映射输出。"""
    provider.status_response = {
        **_mismatched_case_status(),
        "report": "参见（2021）最高法知行终847号裁定 [cite: 1]。",
    }
    research_id = await start_research(client, provider)
    result = await call(client, "dossier_result", research_id=research_id, view="findings")
    assert result["cite_map_basis"].startswith("validated")
    assert result["citation_score"]["matched"] == 1
    assert result["citation_warnings"] == []


async def test_status_exposes_phase_elapsed_and_adaptive_poll(client, provider):
    """②：status 给出 phase/elapsed_ms/sources_discovered 与自适应轮询建议。"""
    provider.status_response = {**provider.status_response, "status": "in_progress", "sources_found": 3, "sources": []}
    out = await call(client, "dossier_start", topic="进度可见", request_key="phase-1")
    research_id = out["research_id"]

    status = await call(client, "dossier_status", research_id=research_id)
    assert status["phase"] == "researching"
    assert status["elapsed_ms"] >= 0
    assert status["sources_discovered"] == 3
    assert status["suggested_next_poll_seconds"] == 20  # 起步 20s，随后 45/90 自适应


async def test_status_wait_seconds_long_polls(client, provider):
    """②：wait_seconds 长轮询把 N 次轮询压成 1 次调用；到期返回 wait_expired。"""
    provider.status_response = {**provider.status_response, "status": "in_progress", "sources": []}
    out = await call(client, "dossier_start", topic="长轮询", request_key="wait-1")
    research_id = out["research_id"]

    resp = await call(client, "dossier_status", research_id=research_id, wait_seconds=0.3)
    assert resp["upstream_state"] == "in_progress"
    assert resp["wait_expired"] is True


async def test_findings_cite_map_takeaway_report_and_digest(client, provider, server):
    """③④：findings 带 cite_map/takeaway；report 分页；digest 提取式摘要与 export 提示。"""
    provider.status_response = {
        **_mismatched_case_status(),
        "report": (
            "# 报告\n\n开篇综述：温度与饮食均影响寿命。\n\n## 一、背景\n"
            + "背景内容。" * 40
            + "\n## 结论\n结论内容：应控制温度。"
        ),
        "sources": [
            {"url": "https://a.example/x", "title": "来源 A", "cited": True},
            {"url": "", "title": "Deep Report", "result_type": "deep_report", "cited": False},
        ],
    }
    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()

    findings = await call(client, "dossier_result", research_id=research_id, view="findings")
    assert findings["cite_map"]["1"] == f"{research_id}.s000"
    row = next(s for s in findings["sources"] if s["source_id"] == f"{research_id}.s000")
    assert row["one_line_takeaway"] and "温度" in row["one_line_takeaway"]
    assert "dossier_export" in findings["next_action"]

    rep = await call(client, "dossier_result", research_id=research_id, view="report", max_chars=100)
    assert rep["total_chars"] > 100
    assert rep["next_offset"] is not None
    assert len(rep["text"]) == 100
    assert any("结论" in s["heading"] for s in rep["sections"])

    rep2 = await call(client, "dossier_result", research_id=research_id, view="report", cursor=rep["next_offset"], max_chars=100)
    assert rep2["offset"] == rep["next_offset"]

    digest = await call(client, "dossier_result", research_id=research_id, view="digest")
    assert digest["extractive"] is True
    assert len(digest["digest_text"]) <= 1500
    assert "温度" in digest["digest_text"] and "应控制温度" in digest["digest_text"]
    assert digest["top_sources"][0]["source_id"] == f"{research_id}.s000"


async def test_findings_takeaway_survives_extraction_view(client, provider, server):
    """③ 边界：提取式摘要跟随原文快照，报告自身无 takeaway 噪音。"""
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)
    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()
    findings = await call(client, "dossier_result", research_id=research_id, view="findings")
    report_rows = [s for s in findings["sources"] if s["is_report"]]
    assert report_rows and all(s["one_line_takeaway"] is None for s in report_rows)
