# notebook-dossier

NotebookLM Deep Research as an MCP server: start deep research, keep every source, verify each claim against the original text, compare viewpoints, and export a citation-grounded writing brief. Evidence-first knowledge groundwork for writers, analysts, and patent practitioners.

把 NotebookLM Deep Research 封装成 MCP，服务「写文章前的知识调研」：

**研究主题 → 深度检索 → 保存完整来源 → 逐篇核验依据 → 比较共识与分歧 → 输出文章研究底稿。**

设计依据：《NotebookLM接入链路与知识调研MCP方案.md》（§9 新链路、§10 接口、§11 验收）。每个重要判断都关联 `来源 / 原文 / 适用条件 / 证据局限`；观点差异区分事实冲突、时间变化、研究条件不同、术语口径不同。

## 现状

- 已实现：MCP 服务器（10 个工具 `dossier_*`）、SQLite 任务存储、上游 `notebooklm-mcp` 适配器、逐来源证据卡、观点比较不变量、研究底稿导出。
- 验证：`tests/` 37 项通过（幂等启动、跳过续跑、任务租约与心跳、调用审计、导入对账、错误信封、导出守卫）；stdio 冒烟 tools/list 通过；真实账号验收完成一轮（2026-10-16：deep 模式确认、46 来源发现、36 导入 + 9 对账未命中、4 来源 47 证据卡、限定来源问答，详见文末清单）。
- 未做：观点比较、转载去重、登录失效路径的真实账号验收（清单未勾项）。

## 架构

```
AI 写作客户端（宿主 AI）
   │  MCP (stdio)
   ▼
notebook-dossier  ── SQLite（research_jobs/sources/evidence/comparisons/answers/tasks）
   │  MCP (stdio 子进程，版本固定)
   ▼
notebooklm-mcp-cli @ a277ce7 (0.15.5)  ── 登录/Profile 由其自行管理
   │  网页内部协议（非官方）
   ▼
NotebookLM Deep Research / 来源 / 聊天与引文
```

本服务只做「研究编排 + 证据整理 + 导出」；登录、NotebookLM 协议细节全部留在上游适配器。两侧数据目录互相隔离（见下）。

## 安装

```bash
uv sync                                  # 本服务依赖 + dev（pytest）
uv sync --extra upstream                 # 可选：安装固定版本上游（提供 notebooklm-mcp 命令）
```

上游登录（**使用独立 Google 账号/Profile**）：

```bash
.venv\Scripts\nlm.exe login --profile research --storage protected   # 弹浏览器窗口，自己完成 Google 登录
.venv\Scripts\nlm.exe login --check                                  # 只检查当前登录是否有效
.venv\Scripts\nlm.exe doctor                                         # 出问题时诊断
```

MCP 客户端接入（stdio）：

```json
{
  "mcpServers": {
    "nlm-research": {
      "command": "uv",
      "args": ["--directory", "D:/OMP/app-dev/notebooklm", "run", "notebook-dossier"],
      "env": { "NLM_APP_DIR": "D:/OMP/app-dev/notebooklm/.nlm-app" }
    }
  }
}
```

### OMP 挂载（实测）

OMP 的用户级配置在 `~/.omp/agent/mcp.json`（`mcpServers` 里加条目后 `/mcp reload`）。本机已挂载 `notebook-dossier`（2026-10-16 由 `nlm-research` 改名）：

```json
"notebook-dossier": {
  "type": "stdio",
  "command": "D:\\OMP\\app-dev\\notebooklm\\.venv\\Scripts\\notebook-dossier.exe",
  "args": [],
  "env": {
    "NLM_APP_DIR": "D:\\OMP\\app-dev\\notebooklm\\.nlm-app",
    "NLM_UPSTREAM_COMMAND": "D:\\OMP\\app-dev\\notebooklm\\.venv\\Scripts\\notebooklm-mcp.exe"
  },
  "timeout": 600000
}
```

要点：`timeout` 必须大于导入/长问答耗时（OMP 默认 30 秒会截断 `dossier_import` 等调用）；`NLM_UPSTREAM_COMMAND` 用绝对路径，否则 OMP 启动的子进程 PATH 里找不到上游命令。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NLM_APP_DIR` | 系统用户数据目录 `notebook-dossier` | 任务库、研究产物、导出目录 |
| `NLM_UPSTREAM_COMMAND` | `notebooklm-mcp` | 上游 MCP 命令 |
| `NLM_UPSTREAM_ARGS` | 空 | 上游命令参数（空格分隔） |
| `NLM_PROFILE` | 上游保存的默认 profile | 指定上游登录 profile（如 `research`） |
| `NLM_QUERY_TIMEOUT` | 240 | 单次问答超时（秒） |
| `NLM_SOURCE_WAIT_TIMEOUT` | 120 | 来源索引等待（秒） |
| `NLM_IMPORT_TIMEOUT` | 300 | 导入超时（秒） |
| `NLM_TASK_LEASE_SECONDS` | 120 | 任务租约：心跳超过该秒数才允许其他进程接管为 interrupted |
| `NLM_ACTOR` | `pid:<pid>:<token>` | 审计表中的调用方标识（多宿主共库时建议在各宿主配置里命名） |

### 账号 / profile 说明（实测）

- 每个 profile 必须**单独登录**才可用：`nlm.exe login --profile research --storage protected`。
- **未登录的 profile 会静默回退到默认账号会话**（实测：`NLM_PROFILE=research` 未登录时 `notebook_list` 仍返回默认账号的笔记本）。因此换账号必须 `nlm.exe login --check --profile <名>` 验证，`dossier_auth_status` 也会把 `auth_status != configured` 判为未连接。
- `nlm doctor` 提示的 `nlm auth storage set protected` 是建议项：把明文 cookie 迁入系统密钥库，执行后用 `nlm login --check` 确认，失效就重登。
- `nlm doctor` 提示的 `nlm setup add <client>` **不需要执行**：那是把原始上游 MCP 挂进 AI 工具；本项目要挂的是 `notebook-dossier`（见上方 stdio 配置）。

## 工具

| 工具 | 用途 |
| --- | --- |
| `dossier_auth_status` | 本地/在线登录状态（不返回凭据）与下一步动作 |
| `dossier_start` | 启动 Deep Research（web + deep），返回 `research_id`；`request_key` 幂等，同键不重复发起 |
| `dossier_status` | 非阻塞查状态；完成时保存完整报告与**全量**发现来源（`compact=False`）；返回逐来源 `progress`（导入/读取/分析状态与证据卡数） |
| `dossier_result` | 分页读取：`findings / evidence / comparisons / tasks / answers`；任务 result 含 `done/failed/skipped` 逐（来源,问题）进度 |
| `dossier_import` | 导入并逐来源记录映射；**收尾自动对账**（按 Notebook 实际来源清单回填漏配，`auto_reconcile` 报告结果）；`reconcile_only=True` 单独对账；`confirm_task_id` 人工放行任务 ID 冲突 |
| `dossier_source_get` | 单来源元数据 + 索引文本摘录（含内容版本哈希、取得时间） |
| `dossier_query` | 指定 `source_ids` 提问（软件强制来源范围，默认新会话）；引文编号仅在本回答内有效 |
| `dossier_evidence` | 逐来源（单独会话）提取证据卡，短引文与原文比对；返回任务号；**断点续跑**：已有证据卡的 (来源, 问题) 组合自动跳过 |
| `dossier_compare` | 按共同问题比较观点，生成共识/差异/缺口；可并入宿主 AI 的比较草稿 |
| `dossier_export` | 导出研究底稿目录（manifest/报告/sources/evidence/viewpoints/writing-brief） |

长时间工作（取证、比较）返回任务号，用 `dossier_status` / `dossier_result(view="tasks")` 读取进度。**任务生命周期语义**：

- 后台任务存活于服务进程内，MCP 连接断开/进程退出即终止；已落库的成果不丢（证据卡逐条写入）。
- **重放即恢复、不重复**：`dossier_evidence` 按 (来源, 问题) 组合跳过已有证据卡；比较按问题替换空壳行；导入按状态跳过已导入。
- **任务租约**：任务带 `owner_id` + 心跳；新进程只接管心跳超过 `NLM_TASK_LEASE_SECONDS` 的任务，不会误杀兄弟进程（如 OMP 挂载与驱动脚本共用一个 `NLM_APP_DIR`）正在跑的任务。运行期间（含上游长查询）由心跳 ticker 自动续租；被接管的任务旧运行方不再写入，同一 (来源, 问题) 不会重复出卡。
- **调用审计**：每次工具调用写入 `audit_log`（actor、工具、参数摘要哈希、时间），多宿主互扰时可直接归因；参数正文与凭据不入库。

### 错误信封（机器可判）

所有错误统一返回：`{status:"error", code, error, hint?, uncertain, retry_safe, recovery_tool?, ...}`

| code | 含义 | 调用方动作 |
| --- | --- | --- |
| `not_found` | research/source ID 不存在 | 按 `recovery_tool` 重建/查询 |
| `invalid_args` | 参数非法 | 修正参数 |
| `precondition_failed` | 前置条件未满足（未导入/缺 notebook_id） | 按 `recovery_tool` 补前置步骤 |
| `source_scope` | 来源超出强制范围 | 只用本研究已导入来源 |
| `task_id_conflict` | 任务 ID 语义存疑，导入暂停 | 人工核对后 `confirm_task_id` 放行（`retry_safe:false`） |
| `mode_mismatch` | 非 deep 模式启动，拒绝静默降级 | 核对账号能力，勿当 deep 结果使用 |
| `result_uncertain` | **结果不确定**（可能已生效） | 先对账（`reconcile_only`），勿直接重放 |
| `provider_error` | 上游调用失败/无最终回答 | 视 `retry_safe` 决定 |
| `export_refused` | 导出触发凭据/内容守卫 | 修正数据后重试 |

任务 ID 归一化：一研究一 Notebook，「本 Notebook 内完成态 + 有报告」时任务 ID 表示形式变化（base64 型 vs UUID 型）自动采纳并在 `task_id_note` 留痕；完成态无报告等语义存疑情形才保留 `task_id_conflict` 守门。

## 软件强制的证据规则

以下不是提示词愿望，而是代码不变量（`compare.py`）：

1. **仅未提及 ≠ 反对**：存在 `not_mentioned` 陈述时，`contradicts` 自动降级为 `not_mentioned` 并留校验记录。
2. **冲突必须双方原文**：`contradicts` 需要 ≥2 个不同来源的原文引文匹配，否则降级为 `unknown`。
3. **转载合并计**：同 `provenance_group`（同规范 URL 或同内容哈希）的支持合并计 1 份独立支持。
4. **报告不重复计证据**：机器生成的研究报告与其依据的网页来源不算两份独立证据。
5. **元数据不补齐**：作者/日期等拿不到就留 null/未知，不由模型编造。
6. **引文编号按回答隔离**：长期标识用 `research_id:answer_seq#编号` 关联稳定 source/evidence ID。

验证状态分层记录：`quote_matched`（原文摘录已匹配）≠ `analyst_confirmed`（原文支持该解释，人工确认）≠ `independent_corroboration`（独立证据互证）。

## 导出目录

```
<export_root>/<research_id>/
  manifest.json          主题、任务、覆盖统计（discovered/selected/imported/ready/readable/analyzed/verified）、文件 sha256
  notebooklm-report.md   完整原始研究报告（机器生成，单独标记）
  sources.json           发现/选择/导入/读取状态、去重映射、内容版本（不含正文）
  evidence.json          逐来源主张与原文支撑（含适用条件、局限、验证状态）
  viewpoints.md          共识、差异、原因与证据缺口
  writing-brief.md       五部分：知识框架 / 逐来源支撑表 / 观点差异 / 可写角度 / 待核验项
```

导出前执行凭据扫描（cookie/token/authorization 等键或行即中止）；导出物不含 Cookie、令牌、浏览器 Profile。

## 已知边界

- NotebookLM 个人版网页协议非官方，方法 ID/响应结构随时可能变；失败时错误带修复提示，不静默降级（如 deep → fast 会直接报错）。
- 导入映射（上游返回 → 本地来源）可能漏配（URL/标题字段不齐）导致 `import_uncertain`；用 `reconcile_only=True` 对账回填，实测有效（39/45 全量命中）。
- 上游研究任务 ID 在运行中会变化（base64 型 ↔ UUID 型）：「同 Notebook 完成态 + 有报告」自动归一化（`task_id_note` 留痕），仅语义存疑时才 `task_id_conflict` + `confirm_task_id` 人工放行（两种路径均实测过）。
- `NLM_UPSTREAM_COMMAND` 必须是绝对路径：PATH 解析可能命中另一份安装（实测那份读不到受保护凭据，报 "No authentication found"）。
- Deep Research 当前要求账号年满 18 岁，来源额度不足时可能只导入部分；`import_uncertain` 表示结果不确定，需对账。
- URL 来源只抓页面文本（无图片/嵌入视频/子页面）；索引文本是导入时快照，不等于网页当前全文。
- 转载识别是近似：同规范 URL / 同内容哈希归并；不同转载 URL 的同稿无法自动识别，可在 `sources.json` 人工改 `provenance_group`。
- fastmcp 4 客户端与上游（较旧 SDK）握手时，上游 stderr 会打印 `server/discover` 校验告警；工具调用不受影响。
- 可选后端（Gemini API Deep Research、NotebookLM Enterprise API）未接入；需要时另做 provider。

## 测试与验收

```bash
uv run pytest          # 37 项离线测试
```

### 真实账号验收清单（对齐方案 §11.2）

- [x] 用独立应用目录 + 独立账号完成一次 deep 研究：确认记录的是 Deep Research（mode_actual=deep），失败不静默降级
- [x] `dossier_result(view="findings")` 列出完整发现集合与实际导入集合；部分失败有说明
- [x] 至少两篇来源单独取证（`dossier_evidence` 单来源），证据可定位原文；来源限制不混入其他材料/历史会话
- [ ] 至少一个比较判断可追溯双方原文；「未提及」未被写成「反对」（查看 `validation_notes`）
- [ ] 转载不重复计独立支持；报告与原始来源不重复计证据
- [x] 中断后重启：恢复任务、不重复建 Notebook / 发起研究 / 重复导入（`request_key` 幂等）
- [ ] 登录失效 / 额度 / 部分来源失败有明确状态；凭据不出现在 MCP 返回、导出物与日志
