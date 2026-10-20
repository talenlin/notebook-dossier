"""notebook-dossier：面向文章调研的 NotebookLM Deep Research MCP。

工具链（方案 §10）：
    dossier_auth_status → dossier_start → dossier_status → dossier_result
    → dossier_import → dossier_source_get / dossier_query
    → dossier_evidence → dossier_compare → dossier_export
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .compare import build_comparison
from .config import Config, load_config
from .evidence import parse_extraction, verify_quote
from .export import export_research
from .errors import (
    EXPORT_REFUSED,
    INVALID_ARGS,
    MODE_MISMATCH,
    NOT_FOUND,
    PRECONDITION_FAILED,
    PROVIDER_ERROR,
    RESULT_UNCERTAIN,
    SOURCE_SCOPE,
    TASK_ID_CONFLICT,
    err,
)
from .models import (
    AnalyzedStatus,
    AnswerRow,
    AuditEntry,
    ComparisonDraft,
    EvidenceRow,
    HostStatementInput,
    ImportStatus,
    JobStatus,
    ReadStatus,
    ResearchJob,
    SourceRow,
    SourceText,
    TaskRow,
    TaskStatus,
    VerificationStatus,
    utcnow_iso,
)
from .prompts import build_comparison_prompt, build_extraction_prompt, compact_claims
from .provider.base import ProviderError, ResearchProvider
from .provider.upstream import UpstreamMcpProvider
from .stats import coverage_summary
from .store import Store
from .utils import canonicalize_url, sha256_text, squash

_QUERY_RESULT_NOTE = "引文编号仅在本回答内有效（引用键格式 research_id:answer_seq#编号），跨回答不可合并。"


class App:
    def __init__(self, config: Config, store: Store, provider: ResearchProvider):
        self.config = config
        self.store = store
        self.provider = provider
        self._bg_tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self.owner_id = f"pid:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.actor = os.environ.get("NLM_ACTOR") or self.owner_id
        # P0 租约制：只接管心跳过期的任务，不误杀兄弟进程正在跑的任务
        store.interrupt_stale_tasks(config.task_lease_seconds)

    def audit(self, tool: str, **args: Any) -> None:
        """Append-only 调用审计（P1 归因）。只存参数摘要哈希与键名，不存正文/凭据。"""
        digest = hashlib.sha256(
            json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        self.store.add_audit(
            AuditEntry(
                actor=self.actor,
                tool=tool,
                research_id=args.get("research_id"),
                args_digest=digest,
                args_keys=sorted(args.keys()),
            )
        )

    # ------------------------------------------------------------- helpers

    def _lock(self, research_id: str) -> asyncio.Lock:
        return self._locks.setdefault(research_id, asyncio.Lock())

    async def drain_background(self) -> None:
        """Await currently running background tasks (tests / graceful shutdown)."""
        while self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @contextlib.asynccontextmanager
    async def _lease(self, task_id: str) -> AsyncIterator[None]:
        """运行期间自动续租（P0 补丁）。

        只在提取对边界 touch 不够：单次上游调用可超过 NLM_TASK_LEASE_SECONDS，
        期间心跳变陈旧会被兄弟进程误判接管。整个运行期间由 ticker 续租。
        """
        interval = max(0.05, min(self.config.task_lease_seconds / 3.0, 30.0))

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval)
                with contextlib.suppress(Exception):
                    self.store.touch_task(task_id)

        ticker = asyncio.get_running_loop().create_task(beat())
        try:
            yield
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    def _still_ours(self, task_id: str) -> bool:
        """任务被兄弟进程接管（置为 interrupted）后，旧运行方不得再写入或改状态。

        接管是单向的：status 变为 interrupted 后本判断恒假，从根上杜绝
        「接管后旧进程继续提交 → 同一 (来源, 问题) 重复出卡」的竞态。
        """
        row = self.store.get_task(task_id)
        return row is not None and row.status == TaskStatus.running and (
            row.owner_id is None or row.owner_id == self.owner_id
        )

    def _start_task(self, research_id: str, kind: str, params: dict[str, Any]) -> TaskRow:
        seq = len(self.store.list_tasks(research_id)) + 1
        task = TaskRow(
            task_id=f"{research_id}.t{seq:03d}",
            research_id=research_id,
            kind=kind,
            params=params,
            owner_id=self.owner_id,
            heartbeat_at=utcnow_iso(),
        )
        self.store.create_task(task)
        return task

    def _source_progress(self, research_id: str) -> list[dict[str, Any]]:
        """P1 可观测性：逐来源进度，任务中断后无需查库外取证。"""
        counts: dict[str, int] = {}
        for ev in self.store.list_evidence(research_id):
            counts[ev.source_id] = counts.get(ev.source_id, 0) + 1
        return [
            {
                "source_id": s.source_id,
                "title": s.title,
                "import_status": s.import_status.value,
                "read_status": s.read_status.value,
                "analyzed_status": s.analyzed_status.value,
                "evidence_cards": counts.get(s.source_id, 0),
            }
            for s in self.store.list_sources(research_id)
        ]

    def _job_or_error(self, research_id: str) -> tuple[ResearchJob | None, dict[str, Any] | None]:
        job = self.store.get_job(research_id)
        if job is None:
            return None, err(
                NOT_FOUND,
                f"未知 research_id: {research_id}",
                hint="先调用 dossier_start 获取 research_id；本服务重启后任务保存在本地存储，可直接续查。",
                recovery_tool="dossier_start",
            )
        return job, None

    def _resolve_sources(self, research_id: str, refs: list[str]) -> tuple[list[SourceRow], dict[str, Any] | None]:
        resolved: list[SourceRow] = []
        missing: list[str] = []
        for ref in refs:
            source = self.store.resolve_source(research_id, ref)
            if source is None:
                missing.append(ref)
            else:
                resolved.append(source)
        if missing:
            return [], err(
                NOT_FOUND,
                f"以下来源不属于本次研究或不存在: {missing}",
                hint="用 dossier_result(view='findings') 查看本研究的来源 ID 清单。",
                recovery_tool="dossier_result",
            )
        return resolved, None

    # ------------------------------------------------------- research flow

    async def research_start(self, topic: str, scope: str | None, request_key: str | None, title: str | None) -> dict[str, Any]:
        topic = (topic or "").strip()
        if not topic:
            return err(INVALID_ARGS, "topic 不能为空", hint="给出明确的研究主题。")
        if request_key:
            existing = self.store.get_job_by_request_key(request_key)
            if existing is not None:
                return {
                    "status": "success",
                    "idempotent_replay": True,
                    "research_id": existing.research_id,
                    "notebook_id": existing.notebook_id,
                    "upstream_task_id": existing.upstream_task_id,
                    "mode_actual": existing.mode_actual,
                    "job_status": existing.status.value,
                    "next_action": "dossier_status",
                }

        query = topic
        if scope:
            query = f"{topic}\n\n研究范围与约束（请遵守）：\n{scope}"
        import uuid

        research_id = f"r_{uuid.uuid4().hex[:12]}"
        job = ResearchJob(
            research_id=research_id,
            request_key=request_key,
            topic=topic,
            scope=scope,
            mode_requested="deep",
        )
        self.store.create_job(job)

        try:
            resp = await self.provider.research_start(
                query=query,
                source="web",
                mode="deep",
                title=title or f"调研：{squash(topic)[:60]}",
            )
        except ProviderError as exc:
            self.store.update_job(
                research_id,
                status=JobStatus.error,
                errors=[f"启动结果不确定（未自动重试）：{exc.message}"],
            )
            out = exc.as_dict()
            out["code"] = RESULT_UNCERTAIN
            out["uncertain"] = True
            out["retry_safe"] = False
            out["recovery_tool"] = "dossier_status"
            out.update({"research_id": research_id})
            out["hint"] = (out.get("hint") or "") + " 请先用 dossier_status 或上游 research_status 核对是否已开始研究，勿直接重发。"
            return out

        if resp.get("status") != "success":
            self.store.update_job(research_id, status=JobStatus.error, errors=[str(resp.get("error") or resp)])
            return err(
                PROVIDER_ERROR,
                str(resp.get("error") or resp),
                research_id=research_id,
                retry_safe=False,
            )

        mode_actual = resp.get("mode")
        if mode_actual != "deep":
            self.store.update_job(
                research_id,
                status=JobStatus.error,
                mode_actual=str(mode_actual),
                errors=[f"mode_mismatch: 请求 deep，上游实际启动 {mode_actual!r}；未静默降级"],
            )
            return err(
                MODE_MISMATCH,
                f"上游以 mode={mode_actual!r} 启动，与要求的 deep 不符",
                hint="请核对账号是否支持 Deep Research（年满 18 岁等限制见方案 §8），不要当作 deep 研究结果使用。",
                retry_safe=False,
                research_id=research_id,
                mode_actual=str(mode_actual),
            )

        job = self.store.update_job(
            research_id,
            notebook_id=str(resp.get("notebook_id") or "") or None,
            upstream_task_id=str(resp.get("task_id") or "") or None,
            mode_actual="deep",
            status=JobStatus.researching,
        )
        return {
            "status": "success",
            "research_id": research_id,
            "notebook_id": job.notebook_id,
            "upstream_task_id": job.upstream_task_id,
            "mode_actual": job.mode_actual,
            "job_status": job.status.value,
            "next_action": "dossier_status（Deep Research 通常需数分钟，建议 30 秒后查询）",
        }

    async def research_status(self, research_id: str) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        if job.status == JobStatus.error:
            return {
                "status": "success",
                "research_id": research_id,
                "job_status": job.status.value,
                "errors": job.errors,
                "coverage": coverage_summary(self.store, research_id),
                "next_action": "查看 errors；必要时以新 request_key 重新发起研究",
            }
        if not job.notebook_id:
            return err(PRECONDITION_FAILED, "任务缺少 notebook_id，无法查询", research_id=research_id)

        try:
            resp = await self.provider.research_status(
                notebook_id=job.notebook_id,
                task_id=job.upstream_task_id,
                query=job.topic,
                compact=False,
                max_wait=0,
            )
        except ProviderError as exc:
            out = exc.as_dict()
            out["research_id"] = research_id
            return out

        upstream_state = str(resp.get("status") or "unknown")
        observed_task_id = resp.get("task_id")
        task_id_note: dict[str, Any] | None = None
        if (
            upstream_state == "completed"
            and job.upstream_task_id
            and observed_task_id
            and str(observed_task_id) != job.upstream_task_id
        ):
            report_ok = bool(str(resp.get("report") or "").strip())
            if report_ok:
                # P1-8 归一化：一研究一 Notebook，"本 Notebook 内完成态 + 有报告"即本研究；
                # 任务 ID 差异只是表示形式变化（base64 型 vs UUID 型），自动采纳并留痕
                task_id_note = {
                    "renormalized_from": job.upstream_task_id,
                    "renormalized_to": str(observed_task_id),
                    "basis": "completed in our notebook with report saved",
                }
                job = self.store.update_job(
                    research_id,
                    upstream_task_id=str(observed_task_id),
                    upstream_task_id_observed=str(observed_task_id),
                )
            else:
                self.store.update_job(
                    research_id,
                    task_id_conflict=True,
                    upstream_task_id_observed=str(observed_task_id),
                    errors=job.errors
                    + [
                        f"upstream_task_id 不一致：存储 {job.upstream_task_id}，观察 {observed_task_id}；"
                        "完成态无报告，语义存疑，已暂停导入，人工核对后用 dossier_import(confirm_task_id=观察到的 ID) 放行"
                    ],
                )
                job = self.store.get_job(research_id)  # type: ignore[assignment]
                assert job is not None

        if upstream_state == "completed" and not job.report_saved_at:
            self._persist_findings(job, resp)

        if upstream_state == "completed":
            job = self.store.update_job(research_id, status=JobStatus.results_ready)
        elif upstream_state == "no_research" and job.upstream_task_id:
            job = self.store.update_job(
                research_id,
                errors=_append_once(job.errors, "上游返回 no_research：刚启动的任务可能暂不可见，或任务 ID 匹配失败；稍后重查，勿重复发起"),
            )

        return {
            "status": "success",
            "research_id": research_id,
            "upstream_state": upstream_state,
            "job_status": job.status.value,
            "task_id_observed": observed_task_id,
            "task_id_conflict": job.task_id_conflict,
            "task_id_note": task_id_note,
            "coverage": coverage_summary(self.store, research_id),
            "progress": self._source_progress(research_id),
            "errors": job.errors,
            "suggested_next_poll_seconds": 0 if upstream_state != "in_progress" else 30,
            "next_action": (
                "dossier_result(view='findings') 查看完整发现结果"
                if upstream_state == "completed"
                else ("稍后再次 dossier_status" if upstream_state == "in_progress" else "核对上游任务状态")
            ),
        }

    def _persist_findings(self, job: ResearchJob, resp: dict[str, Any]) -> None:
        research_id = job.research_id
        report = str(resp.get("report") or "")
        report_dir = self.config.research_path(research_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "notebooklm-report.md"
        report_path.write_text(report, encoding="utf-8")

        now = utcnow_iso()
        raw_sources = resp.get("sources") or []
        for position, raw in enumerate(raw_sources):
            if not isinstance(raw, dict):
                continue
            url = raw.get("url") or raw.get("original_url")
            title = raw.get("title")
            result_type = raw.get("result_type")
            is_report = bool(result_type and "report" in str(result_type).lower()) or not url
            canonical = canonicalize_url(url if isinstance(url, str) else None)
            provenance = canonical or (f"title:{squash(str(title)).casefold()}" if title else f"unique:{research_id}:{position}")
            source_id = f"{research_id}.s{position:03d}"
            self.store.upsert_source(
                SourceRow(
                    source_id=source_id,
                    research_id=research_id,
                    candidate_id=source_id,
                    position=position,
                    original_url=url if isinstance(url, str) else None,
                    canonical_url=canonical,
                    title=str(title) if title else None,
                    retrieved_at=now,
                    result_type=str(result_type) if result_type else None,
                    is_report=is_report,
                    cited_in_report=bool(raw.get("cited")),
                    importable=bool(url) and not is_report,
                    provenance_group=provenance,
                )
            )
        self.store.update_job(
            research_id,
            report_path=str(report_path),
            report_text_hash=sha256_text(report),
            report_saved_at=now,
        )

    def research_result(
        self, research_id: str, view: str, cursor: int, limit: int, include_report_text: bool
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        limit = max(1, min(limit, 100))
        cursor = max(0, cursor)

        if view == "findings":
            sources = self.store.list_sources(research_id)
            page = sources[cursor : cursor + limit]
            report_text = None
            report_truncated = False
            if include_report_text and job.report_path and Path(job.report_path).exists():
                text = Path(job.report_path).read_text(encoding="utf-8")
                if len(text) > 20000:
                    report_text = text[:20000]
                    report_truncated = True
                else:
                    report_text = text
            return {
                "status": "success",
                "research_id": research_id,
                "view": view,
                "cursor": cursor,
                "limit": limit,
                "total": len(sources),
                "next_cursor": cursor + len(page) if cursor + len(page) < len(sources) else None,
                "report": {
                    "path": job.report_path,
                    "text_hash": job.report_text_hash,
                    "saved_at": job.report_saved_at,
                    "text": report_text,
                    "truncated": report_truncated,
                },
                "sources": [
                    {
                        "source_id": s.source_id,
                        "candidate_id": s.candidate_id,
                        "position": s.position,
                        "title": s.title,
                        "url": s.original_url,
                        "canonical_url": s.canonical_url,
                        "cited_in_report": s.cited_in_report,
                        "is_report": s.is_report,
                        "importable": s.importable,
                        "import_status": s.import_status.value,
                        "import_reason": s.import_reason,
                        "read_status": s.read_status.value,
                        "analyzed_status": s.analyzed_status.value,
                        "provenance_group": s.provenance_group,
                    }
                    for s in page
                ],
                "coverage": coverage_summary(self.store, research_id),
            }

        if view in ("evidence", "comparisons", "tasks", "answers"):
            rows: list[Any]
            if view == "evidence":
                rows = self.store.list_evidence(research_id)
            elif view == "comparisons":
                rows = self.store.list_comparisons(research_id)
            elif view == "tasks":
                rows = self.store.list_tasks(research_id)
            else:
                rows = self.store.list_answers(research_id)
            page_rows = rows[cursor : cursor + limit]
            return {
                "status": "success",
                "research_id": research_id,
                "view": view,
                "cursor": cursor,
                "limit": limit,
                "total": len(rows),
                "next_cursor": cursor + len(page_rows) if cursor + len(page_rows) < len(rows) else None,
                "items": [r.model_dump(mode="json") for r in page_rows],
            }

        return err(
            INVALID_ARGS,
            f"未知 view: {view}",
            hint="view 可选 findings | evidence | comparisons | tasks | answers",
        )

    async def research_import(
        self,
        research_id: str,
        candidate_ids: list[str] | None,
        exclude_reasons: dict[str, str] | None,
        confirm_task_id: str | None = None,
        reconcile_only: bool = False,
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        if job.task_id_conflict:
            observed = job.upstream_task_id_observed
            if confirm_task_id and confirm_task_id in {job.upstream_task_id, observed}:
                # 人工核对完成（报告/主题/Notebook 归属一致）：采用完成态观察到的任务 ID 放行
                job = self.store.update_job(
                    research_id,
                    task_id_conflict=False,
                    upstream_task_id=observed or job.upstream_task_id,
                )
            else:
                return err(
                    TASK_ID_CONFLICT,
                    "upstream_task_id 不一致，导入已暂停（避免串用别次研究）",
                    hint=(
                        "人工核对 dossier_status 输出、报告内容与 Notebook 归属后，"
                        f"以 confirm_task_id={observed or job.upstream_task_id!r} 重新调用即放行。"
                    ),
                    retry_safe=False,
                    recovery_tool="dossier_import",
                    research_id=research_id,
                    observed_task_id=observed,
                )
        sources = self.store.list_sources(research_id)
        if not sources:
            return err(
                PRECONDITION_FAILED,
                "尚无发现结果",
                hint="先 dossier_status 同步完整发现来源，再导入。",
                recovery_tool="dossier_status",
                research_id=research_id,
            )
        if reconcile_only:
            return await self._reconcile_import(research_id, job, sources)
        if not job.notebook_id or not job.upstream_task_id:
            return err(
                PRECONDITION_FAILED,
                "缺少 notebook_id / upstream_task_id，无法导入",
                research_id=research_id,
            )

        exclude_reasons = exclude_reasons or {}
        if candidate_ids:
            selected, err_out = self._resolve_sources(research_id, candidate_ids)
            if err_out:
                return err_out
        else:
            selected = [s for s in sources if s.importable and s.import_status not in (ImportStatus.imported,)]

        selected_ids = {s.source_id for s in selected}
        for s in sources:
            if s.source_id not in selected_ids and s.import_status == ImportStatus.discovered:
                self.store.update_source(
                    research_id,
                    s.source_id,
                    import_status=ImportStatus.excluded,
                    import_reason=exclude_reasons.get(s.source_id)
                    or exclude_reasons.get(s.candidate_id)
                    or ("not_importable" if not s.importable else "not_selected"),
                )

        if not selected:
            return {
                "status": "success",
                "research_id": research_id,
                "imported": [],
                "note": "没有可导入的来源（全部排除或已导入）",
                "coverage": coverage_summary(self.store, research_id),
            }

        for s in selected:
            self.store.update_source(research_id, s.source_id, import_status=ImportStatus.selected)

        indices = [s.position for s in selected]
        try:
            resp = await self.provider.research_import(
                notebook_id=job.notebook_id,
                task_id=job.upstream_task_id,
                source_indices=indices,
            )
        except ProviderError as exc:
            for s in selected:
                self.store.update_source(
                    research_id,
                    s.source_id,
                    import_status=ImportStatus.import_failed,
                    import_reason=f"import failed: {exc.message}",
                )
            out = exc.as_dict()
            out["code"] = RESULT_UNCERTAIN
            out["uncertain"] = True
            out["retry_safe"] = False
            out["recovery_tool"] = "dossier_import"
            out["research_id"] = research_id
            out["hint"] = (out.get("hint") or "") + " 导入结果不确定：先用 dossier_status / 上游 source 列表对账（reconcile_only=True），勿直接重放导入。"
            return out

        imported_items = resp.get("imported_sources") or []
        by_url: dict[str, dict[str, Any]] = {}
        for item in imported_items:
            if isinstance(item, dict):
                canon = canonicalize_url(str(item.get("url") or "")) or ""
                if canon:
                    by_url[canon] = item

        mapping: list[dict[str, Any]] = []
        for s in selected:
            item = by_url.get(s.canonical_url or "")
            if item is None and s.title:
                item = next(
                    (
                        it
                        for it in imported_items
                        if isinstance(it, dict) and squash(str(it.get("title") or "")) == squash(s.title or "")
                    ),
                    None,
                )
            if item is not None:
                nb_sid = str(item.get("source_id") or item.get("id") or "") or None
                self.store.update_source(
                    research_id,
                    s.source_id,
                    import_status=ImportStatus.imported,
                    notebook_source_id=nb_sid,
                    import_reason=None,
                )
            else:
                self.store.update_source(
                    research_id,
                    s.source_id,
                    import_status=ImportStatus.import_uncertain,
                    import_reason="导入结果未返回该来源：结果不确定，保留待核对（方案 §11）",
                )
            mapping.append(
                {
                    "source_id": s.source_id,
                    "candidate_id": s.candidate_id,
                    "position": s.position,
                    "import_status": self.store.get_source(research_id, s.source_id).import_status.value,
                    "import_reason": self.store.get_source(research_id, s.source_id).import_reason,
                    "notebook_source_id": self.store.get_source(research_id, s.source_id).notebook_source_id,
                }
            )

        # P0：导入收尾自动对账——import_uncertain 当场收敛为 imported / 仍待核对
        uncertain_ids = [m["source_id"] for m in mapping if m["import_status"] == "import_uncertain"]
        auto: dict[str, Any] = {"attempted": bool(uncertain_ids)}
        if uncertain_ids:
            remapped, still, nb_count = await self._remap_by_notebook(research_id, job, uncertain_ids)
            auto.update(
                {"remapped": remapped, "still_uncertain": still, "notebook_source_count": nb_count}
            )
            for m in mapping:
                row = self.store.get_source(research_id, m["source_id"])
                if row is not None:
                    m["import_status"] = row.import_status.value
                    m["notebook_source_id"] = row.notebook_source_id
                    m["import_reason"] = row.import_reason

        task = self._start_task(
            research_id,
            "source_text_fetch",
            {"source_ids": [m["source_id"] for m in mapping if m["import_status"] == "imported"]},
        )
        self._spawn(self._run_source_text_fetch(task.task_id, research_id))

        return {
            "status": "success",
            "research_id": research_id,
            "imported_count": resp.get("imported_count"),
            "mapping": mapping,
            "auto_reconcile": auto,
            "text_fetch_task_id": task.task_id,
            "coverage": coverage_summary(self.store, research_id),
            "next_action": "dossier_result(view='tasks') 查看来源文本预取进度；也可直接 dossier_source_get 按需取文本",
        }

    # -------------------------------------------------- import reconciliation

    async def _remap_by_notebook(
        self, research_id: str, job: ResearchJob, source_ids: list[str]
    ) -> tuple[list[str], list[str], int | None]:
        """Map imported-but-unmatched sources against the notebook's real source list.

        Returns (matched, still_uncertain, notebook_source_count). Never imports.
        """
        if not job.notebook_id:
            return [], list(source_ids), None
        try:
            resp = await self.provider.notebook_sources(job.notebook_id)
        except ProviderError:
            return [], list(source_ids), None
        remote = [r for r in (resp.get("sources") or []) if isinstance(r, dict) and r.get("id")]
        claimed = {s.notebook_source_id for s in self.store.list_sources(research_id) if s.notebook_source_id}

        def norm(title: object) -> str:
            return squash(str(title or "")).casefold()

        by_title: dict[str, str] = {}
        for item in remote:
            if str(item["id"]) not in claimed:
                by_title.setdefault(norm(item.get("title")), str(item["id"]))

        matched: list[str] = []
        still: list[str] = []
        for sid in source_ids:
            source = self.store.get_source(research_id, sid)
            if source is None or source.notebook_source_id:
                continue
            nb_id = by_title.pop(norm(source.title), None)
            if nb_id is None:
                prefix = norm(source.title)[:40]
                hit = next(
                    (k for k in by_title if prefix and (k.startswith(prefix) or prefix.startswith(k[:40]))),
                    None,
                )
                if hit:
                    nb_id = by_title.pop(hit)
            if nb_id:
                self.store.update_source(
                    research_id,
                    sid,
                    import_status=ImportStatus.imported,
                    notebook_source_id=nb_id,
                    import_reason="reconcile: notebook_get 标题匹配回填",
                )
                matched.append(sid)
            else:
                self.store.update_source(
                    research_id,
                    sid,
                    import_reason="reconcile 未命中：Notebook 来源清单中无同名来源，保留 import_uncertain 待人工核对",
                )
                still.append(sid)
        return matched, still, resp.get("notebook", {}).get("source_count") or len(remote)

    async def _reconcile_import(
        self, research_id: str, job: ResearchJob, sources: list[SourceRow]
    ) -> dict[str, Any]:
        """对账入口：只回填映射，绝不重复导入（reconcile_only 模式）。"""
        if not job.notebook_id:
            return err(PRECONDITION_FAILED, "缺少 notebook_id，无法对账", research_id=research_id)
        pendings = [
            s.source_id
            for s in sources
            if not s.notebook_source_id
            and s.import_status
            in (ImportStatus.import_uncertain, ImportStatus.selected, ImportStatus.import_failed)
        ]
        matched, still, count = await self._remap_by_notebook(research_id, job, pendings)
        task = self._start_task(research_id, "source_text_fetch", {"source_ids": matched})
        self._spawn(self._run_source_text_fetch(task.task_id, research_id))
        return {
            "status": "success",
            "research_id": research_id,
            "reconciled": matched,
            "still_uncertain": still,
            "notebook_source_count": count,
            "text_fetch_task_id": task.task_id,
            "coverage": coverage_summary(self.store, research_id),
        }

    # ---------------------------------------------------- source text fetch

    async def _fetch_source_text(self, source: SourceRow) -> SourceText:
        cached = self.store.get_source_text(source.research_id, source.source_id)
        if cached and cached.fetch_status == "ok":
            return cached
        assert source.notebook_source_id
        resp = await self.provider.source_get_content(
            source.notebook_source_id, wait=True, wait_timeout=self.config.source_wait_timeout
        )
        content = str(resp.get("content") or "")
        now = utcnow_iso()
        if not content:
            record = SourceText(
                source_id=source.source_id,
                research_id=source.research_id,
                title=resp.get("title"),
                source_type=resp.get("source_type"),
                fetch_status="empty",
                retrieved_at=now,
            )
            self.store.set_source_text(record)
            self.store.update_source(source.research_id, source.source_id, read_status=ReadStatus.unreadable)
            return record

        path = self.config.research_path(source.research_id) / "source-text" / f"{source.source_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        text_hash = sha256_text(content)
        record = SourceText(
            source_id=source.source_id,
            research_id=source.research_id,
            text_path=str(path),
            text_hash=text_hash,
            char_count=len(content),
            title=resp.get("title"),
            source_type=resp.get("source_type"),
            fetch_status="ok",
            retrieved_at=now,
        )
        self.store.set_source_text(record)
        self.store.update_source(
            source.research_id,
            source.source_id,
            text_hash=text_hash,
            retrieved_at=now,
            read_status=ReadStatus.readable,
        )
        self._merge_provenance_by_hash(source.research_id, source.source_id, text_hash)
        return record

    def _merge_provenance_by_hash(self, research_id: str, source_id: str, text_hash: str) -> None:
        """Identical indexed text = same origin story; count once as independent support."""
        peers = [
            s
            for s in self.store.list_sources(research_id)
            if s.text_hash == text_hash and s.source_id != source_id and s.provenance_group
        ]
        if peers:
            self.store.update_source(research_id, source_id, provenance_group=peers[0].provenance_group)

    async def _run_source_text_fetch(self, task_id: str, research_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        fetched: list[str] = []
        failed: list[str] = []
        errors: list[str] = []
        async with self._lease(task_id), self._lock(research_id):
            for source_id in task.params.get("source_ids", []):
                self.store.touch_task(task_id, result={"fetched": fetched, "failed": failed})
                source = self.store.get_source(research_id, source_id)
                if source is None or source.import_status != ImportStatus.imported or not source.notebook_source_id:
                    continue
                try:
                    record = await self._fetch_source_text(source)
                    fetched.append(source_id if record.fetch_status == "ok" else f"{source_id}:empty")
                except ProviderError as exc:
                    failed.append(source_id)
                    errors.append(f"{source_id}: {exc.message}")
        if not self._still_ours(task_id):
            return
        self.store.update_task(
            task_id,
            status=TaskStatus.completed if not errors else TaskStatus.error,
            finished_at=utcnow_iso(),
            result={"fetched": fetched, "failed": failed},
            error="; ".join(errors) if errors else None,
        )

    # ---------------------------------------------------------- source get

    async def source_get(self, research_id: str, source_id: str, offset: int, limit: int) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        source, err_out = self._resolve_sources(research_id, [source_id])
        if err_out:
            return err_out
        row = source[0]
        if row.import_status != ImportStatus.imported or not row.notebook_source_id:
            return err(
                PRECONDITION_FAILED,
                f"来源 {row.source_id} 尚未导入（当前 {row.import_status.value}）",
                hint="先 dossier_import 导入该来源。",
                recovery_tool="dossier_import",
                research_id=research_id,
            )
        try:
            async with self._lock(research_id):
                record = await self._fetch_source_text(row)
        except ProviderError as exc:
            out = exc.as_dict()
            out["research_id"] = research_id
            return out

        text = ""
        if record.text_path and Path(record.text_path).exists():
            text = Path(record.text_path).read_text(encoding="utf-8")
        limit = max(1, min(limit, 20000))
        offset = max(0, offset)
        excerpt = text[offset : offset + limit]
        row = self.store.get_source(research_id, row.source_id)
        assert row is not None
        return {
            "status": "success",
            "research_id": research_id,
            "source": {
                "source_id": row.source_id,
                "candidate_id": row.candidate_id,
                "title": row.title or record.title,
                "url": row.original_url,
                "canonical_url": row.canonical_url,
                "author": row.author,
                "publisher": row.publisher,
                "published_at": row.published_at,
                "retrieved_at": row.retrieved_at,
                "text_hash": record.text_hash,
                "char_count": record.char_count,
                "source_type": record.source_type,
                "read_status": row.read_status.value,
                "provenance_group": row.provenance_group,
            },
            "offset": offset,
            "excerpt": excerpt,
            "truncated": offset + len(excerpt) < record.char_count,
            "next_offset": offset + len(excerpt) if offset + len(excerpt) < record.char_count else None,
        }

    # ------------------------------------------------------- notebook query

    async def notebook_query(
        self,
        research_id: str,
        question: str,
        source_ids: list[str],
        conversation_id: str | None,
        question_id: str | None,
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        if not question or not question.strip():
            return err(INVALID_ARGS, "question 不能为空", research_id=research_id)
        if not source_ids:
            return err(
                INVALID_ARGS,
                "必须显式给出 source_ids",
                hint="逐来源取证只传一个来源 ID；跨来源比较传入参与比较的来源 ID。",
                research_id=research_id,
            )
        rows, err_out = self._resolve_sources(research_id, source_ids)
        if err_out:
            return err_out
        invalid = [r.source_id for r in rows if r.import_status != ImportStatus.imported or not r.notebook_source_id]
        if invalid:
            return err(
                SOURCE_SCOPE,
                f"以下来源未导入，不能用于提问: {invalid}",
                hint="来源限制是软件强制的：只允许本次研究已导入的来源，防止混入其他材料或历史会话。",
                recovery_tool="dossier_import",
                research_id=research_id,
            )
        if not job.notebook_id:
            return err(PRECONDITION_FAILED, "缺少 notebook_id", research_id=research_id)

        try:
            resp = await self.provider.notebook_query(
                notebook_id=job.notebook_id,
                query=question,
                source_ids=[r.notebook_source_id for r in rows if r.notebook_source_id],
                conversation_id=conversation_id,
                new_conversation=conversation_id is None,
            )
        except ProviderError as exc:
            out = exc.as_dict()
            out["research_id"] = research_id
            return out

        answer_id = f"{research_id}.a{self.store.next_answer_seq(research_id):03d}"
        nb_to_ours = {r.notebook_source_id: r.source_id for r in rows if r.notebook_source_id}
        citations_raw = resp.get("citations") or {}
        citations: list[dict[str, Any]] = []
        if isinstance(citations_raw, dict):
            for number, value in citations_raw.items():
                nb_sid = value if isinstance(value, str) else (value.get("source_id") if isinstance(value, dict) else None)
                citations.append(
                    {
                        "citation_key": f"{answer_id}#{number}",
                        "number": str(number),
                        "notebook_source_id": nb_sid,
                        "source_id": nb_to_ours.get(nb_sid),
                    }
                )
        references: list[dict[str, Any]] = []
        for ref in resp.get("references") or []:
            if isinstance(ref, dict):
                nb_sid = ref.get("source_id")
                references.append(
                    {
                        **ref,
                        "citation_key": f"{answer_id}#{ref.get('citation_number')}",
                        "source_id": nb_to_ours.get(nb_sid),
                    }
                )
        answer_text = str(resp.get("answer") or resp.get("text") or "")
        if not answer_text:
            return err(
                PROVIDER_ERROR,
                "上游未返回最终回答（partial/parse_error），未记录为完成结果",
                research_id=research_id,
            )

        qid = question_id
        if qid is None:
            qid = self.store.upsert_question(research_id, question).question_id
        self.store.add_answer(
            AnswerRow(
                answer_id=answer_id,
                research_id=research_id,
                notebook_id=job.notebook_id,
                question=question,
                question_id=qid,
                conversation_id=resp.get("conversation_id"),
                source_ids=[r.source_id for r in rows],
                answer_text=answer_text,
                citations=citations,
                references=references,
            )
        )
        return {
            "status": "success",
            "research_id": research_id,
            "answer_id": answer_id,
            "answer": answer_text,
            "conversation_id": resp.get("conversation_id"),
            "sources_used": [{"source_id": r.source_id, "title": r.title} for r in rows],
            "citations": citations,
            "references": references,
            "note": _QUERY_RESULT_NOTE,
        }

    # ------------------------------------------------------- evidence build

    async def evidence_build(
        self, research_id: str, source_ids: list[str], questions: list[str] | None
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        rows, err_out = self._resolve_sources(research_id, source_ids)
        if err_out:
            return err_out
        invalid = [r.source_id for r in rows if r.import_status != ImportStatus.imported or not r.notebook_source_id]
        if invalid:
            return err(
                PRECONDITION_FAILED,
                f"以下来源未导入: {invalid}",
                hint="先 dossier_import。",
                recovery_tool="dossier_import",
                research_id=research_id,
            )
        q_texts = [q.strip() for q in (questions or [job.topic]) if q and q.strip()]
        question_ids = [self.store.upsert_question(research_id, t).question_id for t in q_texts]

        task = self._start_task(
            research_id,
            "evidence_build",
            {"source_ids": [r.source_id for r in rows], "question_ids": question_ids},
        )
        self._spawn(self._run_evidence_build(task.task_id, research_id))
        return {
            "status": "success",
            "research_id": research_id,
            "task_id": task.task_id,
            "planned_extractions": len(rows) * len(question_ids),
            "next_action": "dossier_result(view='tasks'/'evidence') 查看进度与证据卡",
        }

    async def _run_evidence_build(self, task_id: str, research_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        job = self.store.get_job(research_id)
        if job is None:
            return
        source_ids = list(task.params.get("source_ids", []))
        question_ids = list(task.params.get("question_ids", []))
        stored = 0
        skipped = 0
        errors: list[str] = []
        notes_out: list[str] = []
        done_pairs: list[str] = []
        failed_pairs: list[str] = []
        skipped_pairs: list[str] = []
        done_set = {
            (ev.source_id, ev.question_id) for ev in self.store.list_evidence(research_id)
        }

        def progress() -> dict[str, Any]:
            return {
                "evidence_stored": stored,
                "skipped_pairs": skipped,
                "done": done_pairs,
                "failed": failed_pairs,
                "skipped": skipped_pairs,
                "notes": notes_out[:20],
            }

        seq = len(self.store.list_evidence(research_id))
        async with self._lease(task_id), self._lock(research_id):
            for source_id in source_ids:
                source = self.store.get_source(research_id, source_id)
                if source is None or not source.notebook_source_id:
                    continue
                pending_qids = [q for q in question_ids if (source_id, q) not in done_set]
                for q in question_ids:
                    if (source_id, q) in done_set:
                        skipped_pairs.append(f"{source_id}|{q}")
                skipped += len(question_ids) - len(pending_qids)
                self.store.touch_task(task_id, result=progress())
                if not pending_qids:
                    continue
                try:
                    record = await self._fetch_source_text(source)
                    source_text = (
                        Path(record.text_path).read_text(encoding="utf-8") if record.text_path and Path(record.text_path).exists() else None
                    )
                except ProviderError as exc:
                    errors.append(f"{source_id}: 取原文失败 {exc.message}")
                    record = None
                    source_text = None
                for qid in pending_qids:
                    self.store.touch_task(task_id, result=progress())
                    question = self.store.get_question(qid)
                    if question is None:
                        continue
                    prompt = build_extraction_prompt(question.text, source.title)
                    try:
                        resp = await self.provider.notebook_query(
                            notebook_id=job.notebook_id or "",
                            query=prompt,
                            source_ids=[source.notebook_source_id],
                            new_conversation=True,
                        )
                    except ProviderError as exc:
                        failed_pairs.append(f"{source_id}|{qid}")
                        errors.append(f"{source_id}/{qid}: 提取失败 {exc.message}")
                        continue
                    if not self._still_ours(task_id):
                        # 被兄弟进程接管后旧运行方不再写入：避免同一 (来源, 问题) 重复出卡
                        return
                    answer = str(resp.get("answer") or resp.get("text") or "")
                    draft, notes = parse_extraction(answer)
                    if draft is None:
                        self.store.update_source(
                            research_id, source_id, analyzed_status=AnalyzedStatus.parse_error
                        )
                        self._save_extraction_error(research_id, source_id, qid, answer)
                        failed_pairs.append(f"{source_id}|{qid}")
                        errors.append(f"{source_id}/{qid}: 解析失败 {'; '.join(notes)}")
                        continue
                    notes_out.extend(f"{source_id}/{qid}: {n}" for n in notes)
                    if draft.source_view:
                        self.store.update_source(research_id, source_id, source_view=draft.source_view)
                    kept_claims = 0
                    for claim in draft.claims:
                        verification = verify_quote(claim.short_quote, source_text)
                        if verification == VerificationStatus.quote_not_found:
                            notes_out.append(f"{source_id}/{qid}: 引文未在原文中定位: {claim.claim[:40]}")
                        seq += 1
                        self.store.add_evidence(
                            EvidenceRow(
                                evidence_id=f"{research_id}.e{seq:04d}",
                                research_id=research_id,
                                source_id=source_id,
                                source_text_hash=record.text_hash if record else None,
                                question_id=qid,
                                claim=claim.claim,
                                claim_type=claim.claim_type,
                                short_quote=claim.short_quote,
                                locator=claim.locator,
                                evidence_kind=claim.evidence_kind,
                                conditions=claim.conditions,
                                limitations=claim.limitations,
                                verification_status=verification,
                                extraction_status="ok",
                            )
                        )
                        stored += 1
                        kept_claims += 1
                    self.store.update_source(
                        research_id,
                        source_id,
                        analyzed_status=AnalyzedStatus.analyzed if kept_claims else AnalyzedStatus.partial,
                    )
                    done_pairs.append(f"{source_id}|{qid}")
        if not self._still_ours(task_id):
            return
        self.store.update_task(
            task_id,
            status=TaskStatus.completed if not errors else TaskStatus.error,
            finished_at=utcnow_iso(),
            result=progress(),
            error="; ".join(errors) if errors else None,
        )

    def _save_extraction_error(self, research_id: str, source_id: str, question_id: str, raw: str) -> None:
        path = self.config.research_path(research_id) / "extraction-errors"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{source_id}-{question_id}.txt").write_text(raw, encoding="utf-8")

    # ---------------------------------------------------- viewpoints compare

    async def viewpoints_compare(
        self, research_id: str, questions: list[str], statements: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        q_texts = [q.strip() for q in questions if q and q.strip()]
        if not q_texts:
            return err(INVALID_ARGS, "questions 不能为空", research_id=research_id)
        host_inputs: list[HostStatementInput] = []
        for raw in statements or []:
            try:
                host_inputs.append(HostStatementInput.model_validate(raw))
            except Exception as exc:  # noqa: BLE001
                return err(
                    INVALID_ARGS,
                    f"statements 条目非法: {exc}",
                    hint="按 HostStatementInput schema 提供。",
                    research_id=research_id,
                )
        task = self._start_task(
            research_id,
            "viewpoints_compare",
            {"questions": q_texts, "host_statement_count": len(host_inputs)},
        )
        self._spawn(self._run_viewpoints_compare(task.task_id, research_id, q_texts, host_inputs))
        return {
            "status": "success",
            "research_id": research_id,
            "task_id": task.task_id,
            "next_action": "dossier_result(view='comparisons') 查看比较结果",
        }

    async def _run_viewpoints_compare(
        self,
        task_id: str,
        research_id: str,
        q_texts: list[str],
        host_inputs: list[HostStatementInput],
    ) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        job = self.store.get_job(research_id)
        if job is None:
            return
        seq = len(self.store.list_comparisons(research_id))
        errors: list[str] = []
        trunc_all: list[str] = []
        async with self._lease(task_id), self._lock(research_id):
            for q_text in q_texts:
                if not self._still_ours(task_id):
                    return
                self.store.touch_task(task_id, result={"comparisons": seq})
                question = self.store.upsert_question(research_id, q_text)
                host_for_q = next((h for h in host_inputs if h.question.strip() == q_text), None)
                evidence_rows = self.store.list_evidence(research_id, question_id=question.question_id)
                involved_ids = {ev.source_id for ev in evidence_rows}
                if host_for_q:
                    involved_ids.update({st.source_id for st in host_for_q.statements})
                involved = [
                    s
                    for s in (self.store.get_source(research_id, sid) for sid in sorted(involved_ids))
                    if s is not None
                ]
                draft: ComparisonDraft | None = None
                q_notes: list[str] = []
                if involved and job.notebook_id:
                    raw_claims = [
                        {
                            "source_id": ev.source_id,
                            "claim": ev.claim,
                            "short_quote": ev.short_quote,
                            "verification_status": ev.verification_status.value,
                        }
                        for ev in evidence_rows
                    ]
                    compact = compact_claims(raw_claims)
                    if len(compact) < len(raw_claims):
                        # P0 截断声明：调用方必须知道比较未覆盖全部证据卡
                        note = (
                            f"证据卡负载截断：{len(raw_claims)}→{len(compact)} 条"
                            "（上游查询参数上限；比较结论未覆盖全部证据卡，详见 evidence 视图）"
                        )
                        q_notes.append(note)
                        trunc_all.append(f"{question.question_id}: {note}")
                    prompt = build_comparison_prompt(
                        q_text,
                        [{"source_id": s.source_id, "title": s.title or s.source_id} for s in involved],
                        raw_claims,
                    )
                    try:
                        resp = await self.provider.notebook_query(
                            notebook_id=job.notebook_id,
                            query=prompt,
                            source_ids=[s.notebook_source_id for s in involved if s.notebook_source_id],
                            new_conversation=True,
                        )
                        answer = str(resp.get("answer") or resp.get("text") or "")
                        from .evidence import extract_json_object

                        obj = extract_json_object(answer)
                        if obj is not None:
                            draft = ComparisonDraft.model_validate(obj)
                        else:
                            errors.append(f"{question.question_id}: 比较结果未解析为 JSON，仅使用宿主提供的陈述")
                    except ProviderError as exc:
                        errors.append(f"{question.question_id}: 比较查询失败 {exc.message}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{question.question_id}: 比较草稿校验失败 {exc}")
                source_map = {s.source_id: s for s in self.store.list_sources(research_id)}
                source_texts: dict[str, str | None] = {}
                for sid in involved_ids:
                    record = self.store.get_source_text(research_id, sid)
                    if record and record.text_path and Path(record.text_path).exists():
                        source_texts[sid] = Path(record.text_path).read_text(encoding="utf-8")
                seq += 1
                comparison = build_comparison(
                    research_id=research_id,
                    question_id=question.question_id,
                    draft=draft,
                    host_input=host_for_q,
                    sources=source_map,
                    evidence=evidence_rows,
                    source_texts=source_texts,
                    sequence=seq,
                    extra_notes=q_notes,
                )
                self.store.delete_empty_comparisons(research_id, question.question_id)
                self.store.add_comparison(comparison)
        if not self._still_ours(task_id):
            return
        self.store.update_task(
            task_id,
            status=TaskStatus.completed if not errors else TaskStatus.error,
            finished_at=utcnow_iso(),
            result={"comparisons": seq, "truncated_notes": trunc_all},
            error="; ".join(errors) if errors else None,
        )

    # ---------------------------------------------------------- brief export

    def writing_brief_export(
        self, research_id: str, output_dir: str | None, angles: list[str] | None
    ) -> dict[str, Any]:
        job, err_out = self._job_or_error(research_id)
        if err_out:
            return err_out
        assert job is not None
        try:
            return export_research(self.store, self.config, research_id, output_dir=output_dir, angles=angles)
        except ValueError as exc:
            return err(
                EXPORT_REFUSED, str(exc), retry_safe=False, research_id=research_id
            )


def _append_once(errors: list[str], message: str) -> list[str]:
    return errors if message in errors else errors + [message]


def build_server(config: Config | None = None, provider: ResearchProvider | None = None) -> FastMCP:
    config = config or load_config()
    store = Store(config.db_path)
    if provider is None:
        provider = UpstreamMcpProvider(
            command=config.upstream_command,
            args=config.upstream_args,
            query_timeout=config.query_timeout,
            source_wait_timeout=config.source_wait_timeout,
            import_timeout=config.import_timeout,
        )
    app = App(config, store, provider)
    mcp = FastMCP("notebook-dossier")

    @mcp.tool
    async def dossier_auth_status(profile: str | None = None) -> dict[str, Any]:
        """查询 NotebookLM 上游的本地登录与在线状态（不返回任何凭据）。

        Args:
            profile: 可选，切换到指定的上游账号 profile。
        """
        app.audit("dossier_auth_status", profile=profile)
        try:
            return await app.provider.auth_probe(profile)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_start(
        topic: str, scope: str | None = None, request_key: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        """启动一次 NotebookLM Deep Research（web + deep），立即返回 research_id。

        每个独立主题一个研究 Notebook。request_key 用于幂等：同键重复调用返回已有任务，不会重复发起研究。

        Args:
            topic: 研究主题。
            scope: 可选范围/时间/地域/读者等约束，会写入研究问题。
            request_key: 可选幂等键（同一调研流程保持一致）。
            title: 可选 Notebook 标题。
        """
        app.audit("dossier_start", topic=topic, scope=scope, request_key=request_key, title=title)
        try:
            return await app.research_start(topic, scope, request_key, title)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_status(research_id: str) -> dict[str, Any]:
        """查询研究任务状态（非阻塞单次检查），并同步完整报告与发现来源清单。

        完成时会保存 compact=False 的完整结果；来源统计与发现/导入不是同一指标。
        """
        app.audit("dossier_status", research_id=research_id)
        try:
            return await app.research_status(research_id)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_result(
        research_id: str,
        view: str = "findings",
        cursor: int = 0,
        limit: int = 20,
        include_report_text: bool = False,
    ) -> dict[str, Any]:
        """分页读取研究结果。view: findings | evidence | comparisons | tasks | answers。

        findings 为完整发现来源（非简略列表）；报告全文可选，截断时会明确标注。
        """
        app.audit(
            "dossier_result",
            research_id=research_id,
            view=view,
            cursor=cursor,
            limit=limit,
            include_report_text=include_report_text,
        )
        return app.research_result(research_id, view, cursor, limit, include_report_text)

    @mcp.tool
    async def dossier_import(
        research_id: str,
        candidate_ids: list[str] | None = None,
        exclude_reasons: dict[str, str] | None = None,
        confirm_task_id: str | None = None,
        reconcile_only: bool = False,
    ) -> dict[str, Any]:
        """把选中的发现来源导入 NotebookLM，并逐个记录导入映射与不确定结果。

        Args:
            research_id: 研究任务 ID。
            candidate_ids: 要导入的来源 ID（source_id 或 candidate_id）；缺省导入全部可导入来源。
            exclude_reasons: 未导入原因说明 {来源 ID: 理由}。
            confirm_task_id: 任务 ID 不一致时，人工核对后填入观察到的任务 ID 放行导入。
            reconcile_only: 只做导入对账（按 Notebook 实际来源清单回填映射），不重复导入。
        """
        app.audit(
            "dossier_import",
            research_id=research_id,
            candidate_ids=candidate_ids,
            confirm_task_id=confirm_task_id,
            reconcile_only=reconcile_only,
        )
        try:
            return await app.research_import(
                research_id, candidate_ids, exclude_reasons, confirm_task_id, reconcile_only
            )
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_source_get(
        research_id: str, source_id: str, offset: int = 0, limit: int = 4000
    ) -> dict[str, Any]:
        """取得单个来源的元数据与索引文本摘录（含内容版本哈希与取得时间）。

        来源元数据缺失的字段保持 null/未知，不从模型补齐。
        """
        app.audit("dossier_source_get", research_id=research_id, source_id=source_id, offset=offset, limit=limit)
        try:
            return await app.source_get(research_id, source_id, offset, limit)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_query(
        research_id: str,
        question: str,
        source_ids: list[str],
        conversation_id: str | None = None,
        question_id: str | None = None,
    ) -> dict[str, Any]:
        """向 NotebookLM 提问；来源集合由软件强制限定（默认新会话，逐来源取证只传一个来源）。

        返回回答与可核验引用；引文编号仅在本回答内有效（citation_key 形如 research:answer_seq#n）。
        """
        app.audit(
            "dossier_query",
            research_id=research_id,
            question=question,
            source_ids=source_ids,
            conversation_id=conversation_id,
            question_id=question_id,
        )
        try:
            return await app.notebook_query(research_id, question, source_ids, conversation_id, question_id)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_evidence(
        research_id: str, source_ids: list[str], questions: list[str] | None = None
    ) -> dict[str, Any]:
        """逐来源生成证据卡（每来源单独会话、只依据该来源），返回任务号。

        证据卡含主张、依据类型、短引文、定位、适用条件、局限与验证状态；短引文会与来源原文比对。
        """
        app.audit("dossier_evidence", research_id=research_id, source_ids=source_ids, questions=questions)
        try:
            return await app.evidence_build(research_id, source_ids, questions)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_compare(
        research_id: str,
        questions: list[str],
        statements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按共同问题比较各来源观点，生成共识/差异/缺口（后台任务，返回任务号）。

        软件强制规则：仅未提及≠反对；冲突结论必须有双方原文引文；转载合并计、报告不重复计证据。
        statements 可传宿主 AI 已形成的比较草稿（HostStatementInput schema），与 NotebookLM 比较结果合并校验。
        """
        app.audit("dossier_compare", research_id=research_id, questions=questions)
        try:
            return await app.viewpoints_compare(research_id, questions, statements)
        except ProviderError as exc:
            return exc.as_dict()

    @mcp.tool
    async def dossier_export(
        research_id: str, output_dir: str | None = None, angles: list[str] | None = None
    ) -> dict[str, Any]:
        """导出文章研究底稿目录：manifest / 报告 / sources / evidence / viewpoints / writing-brief。

        导出物不含 Cookie、令牌或浏览器 Profile；每个重要判断关联来源与证据 ID。
        """
        app.audit("dossier_export", research_id=research_id, output_dir=output_dir, angles=angles)
        return app.writing_brief_export(research_id, output_dir, angles)

    # DI/test handle: the composed application behind the tools.
    mcp.app = app  # type: ignore[attr-defined]
    return mcp


def main() -> None:
    mcp = build_server()
    mcp.run()


if __name__ == "__main__":
    main()
