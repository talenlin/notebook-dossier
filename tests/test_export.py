"""Export package: layout, evidence-linked brief, credential guard."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import EXTRACTION_ANSWER, call, json_answer, start_research


async def _prepare(client, provider, server, tmp_path):
    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()
    provider.answer_fn = lambda prompt: json_answer(EXTRACTION_ANSWER)
    await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000", f"{research_id}.s001"],
        questions=["温度如何影响寿命？"],
    )
    await server.app.drain_background()
    return research_id


async def test_export_writes_research_package(client, provider, server, tmp_path):
    research_id = await _prepare(client, provider, server, tmp_path)
    out_dir = tmp_path / "out"

    out = await call(client, "dossier_export", research_id=research_id, output_dir=str(out_dir))
    assert out["status"] == "success", out
    files = set(out["files"])
    assert files == {
        "manifest.json",
        "notebooklm-report.md",
        "sources.json",
        "evidence.json",
        "viewpoints.md",
        "writing-brief.md",
    }
    for name in files:
        assert (out_dir / name).exists()

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["topic"] == "温度与寿命的关系"
    assert manifest["mode_actual"] == "deep"
    assert manifest["coverage"]["discovered"] == 3
    assert manifest["coverage"]["imported"] == 2
    assert manifest["verification_summary"].get("quote_matched", 0) >= 1
    assert all(f["sha256"] for f in manifest["files"])

    brief = (out_dir / "writing-brief.md").read_text(encoding="utf-8")
    for section in ("一、知识框架与关键概念", "二、逐来源支撑表", "三、观点差异及原因", "四、可写角度和论证提纲", "五、尚缺证据与待核验项"):
        assert section in brief
    # 逐来源支撑表带验证状态与适用条件
    assert "quote_matched" in brief and "仅限小鼠实验" in brief
    # 缺元数据的来源保持未知，不由模型补齐
    assert "未知" in brief

    evidence = json.loads((out_dir / "evidence.json").read_text(encoding="utf-8"))
    assert len(evidence) == 4
    assert {e["verification_status"] for e in evidence} == {"quote_matched", "quote_not_found"}

    # 导出物不含凭据
    for name in files:
        text = (out_dir / name).read_text(encoding="utf-8").lower()
        for needle in ("access_token", "refresh_token", "set-cookie"):
            assert needle not in text


async def test_export_refuses_credential_like_keys(client, provider, server, tmp_path):
    research_id = await start_research(client, provider)
    await call(client, "dossier_import", research_id=research_id)
    await server.app.drain_background()
    poisoned = dict(EXTRACTION_ANSWER)
    poisoned["source_view"] = {"main_thesis": "正常论点", "token": "sekrit"}
    provider.answer_fn = lambda prompt: json_answer(poisoned)
    await call(
        client,
        "dossier_evidence",
        research_id=research_id,
        source_ids=[f"{research_id}.s000"],
        questions=["温度如何影响寿命？"],
    )
    await server.app.drain_background()

    out = await call(client, "dossier_export", research_id=research_id, output_dir=str(tmp_path / "poisoned"))
    assert out["status"] == "error"
    assert out["code"] == "export_refused"
    assert "credential-like key" in out["error"]


async def test_export_unknown_research_errors(client, provider, tmp_path):
    out = await call(client, "dossier_export", research_id="r_nope", output_dir=str(tmp_path / "x"))
    assert out["status"] == "error"
    assert "未知 research_id" in out["error"]
