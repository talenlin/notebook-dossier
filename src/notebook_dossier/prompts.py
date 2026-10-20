"""Prompt builders for single-source evidence extraction and cross-source comparison.

Both prompts demand strict JSON; the server still validates every decoded
object (see evidence.py / compare.py). Prompts never ask the model to invent
metadata it does not have.
"""

from __future__ import annotations

EXTRACTION_TEMPLATE = """你只能依据下面指定的【单一来源】回答；禁止使用该来源之外的知识或记忆。
如果该来源没有涉及研究问题，输出空的 claims 数组。

研究问题：{question}
来源标题：{title}

任务：提取该来源对研究问题的支撑内容，输出一个 JSON 对象。除 JSON 外不要输出任何其他文字；如需代码块请用 ```json 包裹。
{{
  "source_view": {{
    "main_thesis": "该来源的核心论点",
    "stance": "该来源对研究问题的立场，未知则 null",
    "time_scope": "该来源讨论的时间范围，未知则 null",
    "population_scope": "该来源讨论的对象/人群/样本范围，未知则 null"
  }},
  "claims": [
    {{
      "claim": "该来源提出的一个具体主张（用自己的话概括）",
      "claim_type": "fact|opinion|interpretation|prediction|definition|other",
      "short_quote": "支持该主张的原文短引文，必须逐字复制自该来源",
      "locator": "定位信息（小节/段落/出现位置），未知则 null",
      "evidence_kind": "experiment|data|citation|example|assertion|definition|other",
      "conditions": "该主张成立的适用条件，未知则 null",
      "limitations": "该来源自述或明显存在的局限，未知则 null"
    }}
  ]
}}

要求：
1. short_quote 必须逐字复制（10~200 字符），不得翻译、改写或杜撰；无法定位原文支撑的主张不要输出。
2. 不要补齐作者、日期、机构等元数据；没有的信息写 null。
3. claims 按重要性排序，最多 10 条。
4. 只输出 JSON。"""


COMPARISON_TEMPLATE = """你只能依据下面选定的一组【来源】回答；禁止使用来源之外的知识或记忆。
研究问题：{question}
选定来源：{sources}
各来源已提取的关键主张（可能为空，仅供定位）：{claims}

任务：比较这些来源对该问题的观点，输出一个 JSON 对象。除 JSON 外不要输出任何其他文字；如需代码块请用 ```json 包裹。
{{
  "statements": [
    {{
      "source_id": "选定来源中的来源 ID",
      "statement": "该来源对该问题的立场陈述",
      "short_quote": "支持该陈述的原文短引文，必须逐字复制；无法定位则 null",
      "stance": "supports|opposes|partial|unclear|not_mentioned"
    }}
  ],
  "relations": [
    {{
      "relation": "supports|contradicts|conditional_difference|definitional_difference|temporal_difference|evidence_strength_difference|not_mentioned|unrelated_scope|unknown",
      "from_source_id": "来源 ID",
      "to_source_id": "来源 ID",
      "reasons": ["差异的具体原因：事实冲突/时间变化/研究条件不同/术语口径不同等"],
      "short_quotes": [{{"source_id": "来源 ID", "quote": "原文短引文，必须逐字复制"}}]
    }}
  ],
  "unresolved_questions": ["这些来源都未回答、仍需外部证据的问题"]
}}

规则：
1. 来源未提及某观点只能标 not_mentioned，绝不能标 contradicts。
2. 两篇来源讨论的对象、指标、人群或时间不同的，标 unrelated_scope、definitional_difference 或 temporal_difference，不要写成冲突。
3. short_quote 必须逐字复制自对应来源；无法定位原文就填 null。
4. 只输出 JSON。"""


def build_extraction_prompt(question: str, title: str | None) -> str:
    return EXTRACTION_TEMPLATE.format(question=question.strip(), title=title or "(无标题)")


def build_comparison_prompt(question: str, sources: list[dict[str, str]], claims: list[dict[str, object]]) -> str:
    import json

    sources_line = json.dumps(sources, ensure_ascii=False)
    claims_line = json.dumps(compact_claims(claims), ensure_ascii=False)
    return COMPARISON_TEMPLATE.format(question=question.strip(), sources=sources_line, claims=claims_line)


def compact_claims(claims: list[dict[str, object]]) -> list[dict[str, object]]:
    """Bound the prompt payload: max 3 claims per source, 14 total, truncated fields.

    The upstream query rejects oversized prompts (实测: 全量证据卡 JSON 触发
    "The query request is invalid")。
    """
    per_source: dict[str, list] = {}
    for claim in claims:
        sid = str(claim.get("source_id") or "")
        bucket = per_source.setdefault(sid, [])
        if len(bucket) >= 3:
            continue
        bucket.append(
            {
                "source_id": sid,
                "claim": str(claim.get("claim") or "")[:80],
                "short_quote": (str(claim.get("short_quote") or "")[:50] or None),
                "verification_status": claim.get("verification_status"),
            }
        )
    flat = [item for bucket in per_source.values() for item in bucket]
    return flat[:14]
