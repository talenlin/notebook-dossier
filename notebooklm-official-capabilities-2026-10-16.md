# NotebookLM 调研 MCP：官方能力核验

核验日期：2026-10-16（Asia/Shanghai）。范围：Google 官方帮助、Google Cloud / Gemini API 官方开发文档、MCP 官方规范；只读浏览，未登录用户账号、未调用收费 API，未启动研究任务。本文是文档证据，不能替代真实账号验收。

## 1. 先区分三个产品入口

本项目讨论继续沿用用户熟悉的 **NotebookLM** 名称。此次访问 `support.google.com/notebooklm/...`，部分页面已重定向到 `support.google.com/gemininotebook/...`，标题显示 Gemini Notebook。Google Cloud 2026-07-16 发布记录明确：NotebookLM Enterprise 更名为 Gemini Notebook Enterprise，API 端点保持不变。[Google Cloud 发布记录](https://docs.cloud.google.com/gemini/enterprise/docs/release-notes)

| 入口 | 本次确认的能力 | 对本项目的意义 |
| --- | --- | --- |
| NotebookLM / Gemini Notebook 消费者网页 | Deep Research、导入报告及来源、按来源聊天、可定位原文的引用 | 用户希望真正接入的产品，网页功能不能自动视为公开 API |
| NotebookLM Enterprise / Gemini Notebook Enterprise | 官方笔记本、来源及 Audio Overview 管理 API | 属于 Google Cloud 授权与资源体系，不能把个人账号的网页会话直接当作其授权 |
| Gemini API Deep Research agent | 官方异步研究 API、带引用报告 | 属于独立的可选后端；不是“调用 NotebookLM 笔记本”，不能在用户未选择时替换默认路线 |

## 2. NotebookLM 网页 Deep Research 链路

官方帮助确认的流程是：在 Sources 搜索框输入研究问题 → 选 Web + Deep Research → 启动并等待 → View 查看研究报告及所有相关来源（包括已引用和未引用来源）→ 勾选所需结果并导入笔记本 → 后续以这些来源提问。未导入的结果退出后会丢弃；来源额度不足可能只导入部分结果。该功能当前要求用户年满 18 岁。[来源发现帮助](https://support.google.com/gemininotebook/answer/16215270?hl=en)

2025-11-13 官方发布说明也描述了“研究计划—浏览网站—形成有来源依据报告—报告及来源导入笔记本”的工作流，Fast Research 与 Deep Research 是两种研究模式。[Google 官方发布](https://blog.google/innovation-and-ai/models-and-research/google-labs/notebooklm-deep-research-file-types/)

**设计推论：** MCP 要完成用户目标，不能只把普通 notebook chat 包成工具。必须分别实现研究启动、完成状态判定、结果列表提取、报告和来源导入，并明确“发现了来源”与“已成功导入可问答”两个状态。

## 3. 来源与引用的真实边界

NotebookLM 聊天可以选择一组来源；回答使用来源内的引用，悬停可看完整引文，点击可跳到上下文；保存为 note 时可保留表格及可点击内嵌引用。官方描述聊天回答依据笔记本来源。[聊天与引用帮助](https://support.google.com/gemininotebook/answer/16179559)

网页 URL 导入只抓取该 HTML 页面的文本，不包含图片、嵌入视频或嵌套网页；不支持付费墙页面。URL 指向 PDF 时作为 PDF 来源。每个来源上限为 500,000 词，上传文件上限 200 MB；免费账号最多 50 个来源。[来源类型与限制](https://support.google.com/gemininotebook/answer/16215270?hl=en)

当前使用限制页说明额度受计算量、提示复杂度、模型和功能等影响；额度可能改变，不能把早期固定“每天几次 Deep Research”硬编码成事实。应读取账号实际可用状态，并在结果中报告额度/导入截断。[当前额度帮助](https://support.google.com/gemininotebook/answer/17670842)

**设计推论：** “各网页提供何种基础支撑、观点如何不同”需要额外的证据数据模型：来源原始 URL、标题、导入状态、文章时间、具体主张、可定位原文、支持/反对/条件不同关系。只返回一段总结、或来源的 id/title/status，尚不能验收用户目标。来源未提及某事不能直接等同于反对；网页与 NotebookLM 导入快照的内容差异也需要记录。

## 4. 官方 NotebookLM Enterprise API 能确认什么

笔记本管理文档列出 create、get、listRecentlyViewed、delete、share；需要先配置 Enterprise 并取得许可证。文档示例使用 Google Cloud Bearer token 和 `...discoveryengine.googleapis.com/v1alpha/projects/.../locations/.../notebooks` 资源。该 API 指南当前页面标记 Preview。[管理笔记本 API](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/api-notebooks)

来源 API 提供 batchCreate、uploadFile、get、batchDelete；batchCreate 接受 Google Docs/Slides、原始文本、网页 URL 和 YouTube；文件上传支持 PDF、TXT、MD、DOCX、PPTX、XLSX 等。Google Drive 来源另外需要用户 Drive 授权。get 示例返回标识、标题、元数据、处理状态。[来源 API](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/api-notebooks-sources)

完整 Source REST schema 可确认字段为 name、title、sourceId、metadata、settings；metadata 包含词数、token 数、添加时间及特定来源标识。**该 schema 没有来源全文、聊天回答、引用片段或统一网页 URL 正文字段。** 因此不能把 `sources.get` 宣传成“获取来源全文”。[Source REST schema](https://docs.cloud.google.com/gemini/enterprise/docs/reference/rest/v1alpha/projects.locations.notebooks.sources)

另有创建/获取/删除 Audio Overview 的 API，但它不等于网页深度检索或研究报告 API。[Audio Overview API](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/api-audio-overview)

**本次未在检索到的 Google NotebookLM / Gemini Notebook 官方开发资料中找到：** 面向消费者账号、直接启动 NotebookLM Deep Research、导出 NotebookLM 聊天及其可定位引用、获取所有来源全文的公开 API 文档。此结论只表示本次文档核验未找到证据，不等于证明 Google 所有内部、受邀或未来接口都不存在。网页内部 RPC 和第三方非官方适配器不能标为官方 NotebookLM API。

## 5. 可选后端：Gemini API Deep Research（独立产品）

官方 Gemini API 文档明确可通过 **Interactions API** 使用 Deep Research（Preview），不能使用 generate_content。它支持后台任务、轮询/流式恢复、引用报告及研究计划。当前文档列出 `deep-research-preview-04-2026` 和 `deep-research-max-preview-04-2026`；要求 background=true（并要求 store=true），没有 schema 约束的 structured output。官方写明最长研究 60 分钟，通常 20 分钟内完成。费用按模型与工具用量计算。[Gemini Deep Research 官方开发文档](https://ai.google.dev/gemini-api/docs/deep-research)

Interactions API 文本块的 annotations 描述了引用信息；URL citation schema 有 URL、标题及归属文本区段等可选字段。它可以帮助保留“报告哪段对应哪个来源”的机器可读关系；不能据此假设每条引文都必有完整网页正文，或已经完成观点冲突判定。[Interactions API 参考](https://ai.google.dev/api/interactions-api)

Google Cloud 另有 Gemini Enterprise 的 Deep Research `streamAssist` API（官方文档写 GA with allowlist），以及 Gemini Enterprise Agent Platform 的 Preview Deep Research。它们有各自授权、限制及计费，均应单独评估，不能与个人 NotebookLM 或彼此混写。[Gemini Enterprise Deep Research](https://docs.cloud.google.com/gemini/enterprise/docs/research-assistant)、[Agent Platform Deep Research](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/use-deep-research)

**方案定位：** 若用户明确接受独立研究 API，可把 Gemini Deep Research 作为可选 provider；若要求真实 NotebookLM 笔记本体验，则继续建设 NotebookLM adapter，并验收网页研究、来源导入及引用提取的完整链路。此次没有比较两者实际输出质量，也没有验证当前账号/区域的可用性。

## 6. MCP 层的适用建议

MCP tools 可以声明 outputSchema，并用 structuredContent 返回 JSON；如声明 schema，服务器必须返回符合 schema 的数据。规范特别区分 MCP 的结构化结果与模型是否支持 schema 约束生成。因此即使研究模型不支持 structured outputs，MCP 仍可在校验、规范化后返回结构化证据。[MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

MCP 2025-11-25 Tasks 支持持久任务、状态轮询、延迟取回结果，但规范明确是 experimental，不能假设所有客户端均已实现。建议保底提供 research_start / research_status / research_result / research_cancel 这类工具并保存任务状态；支持 Tasks 的客户端再映射为协议任务。这是工程建议，不是 MCP 规定的工具名。[MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)

本地单用户 MCP 可以使用 stdio；官方规范要求 stdout 只输出协议消息，日志写 stderr。远程服务可另选 Streamable HTTP。浏览器自动化的登录会话应留在本地 adapter 管理，不放进工具返回或研究报告。[MCP Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

## 7. 后续应做的最小真实验收

以下为建议验收，不是已完成测试：

1. 新笔记本发起一次有争议或有时间变化的知识问题，确认进入真正 Deep Research，而非普通聊天或 Fast Research。
2. 等研究结果生成，保存报告、发现来源清单与原始 URL；逐项检查导入成功和额度截断。
3. 至少两篇来源单独选取并提问，验证每一条主张能回到具体原文段落；若正文/引用提取暂不可得，明确标注缺口。
4. 再跨来源总结共识、分歧、分歧原因与尚缺证据，抽查原网页内容，区分事实证据与作者解释。
5. 中断后恢复任务，验证不重复建笔记本、重复研究或重复导入；验证登录失效/限额/部分来源失败有明确状态。

