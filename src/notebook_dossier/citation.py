"""Citation consistency check：报告 [cite: N] 论断与被引来源文本的实体级比对（B 项）。

只做编号/数字级比对（案号、法条号、判例报告号、日期、普通数字），不冒充语义核验：
- 命中：实体在被引来源文本（或标题）中定位成功；
- 未定位 ≠ 错误：多引时对被引来源并集比对，仅当来源中出现**同类型冲突实体**（如案号 vs 案号）才升 high；
- 来源文本不可得时该实体标 unverifiable，不谎报通过。

校验分两层调用（server 层接线）：
- title 层：findings 同步后立即用来源标题校验（零成本、无需导入）；
- text 层：来源原文预取完成后对全文校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ 归一化

_FULL_TO_HALF = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "〔": "[",
        "〕": "]",
        "［": "[",
        "］": "]",
        "　": "",
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    }
)


def normalize(text: str) -> str:
    """去空白 + 全半角归一，供实体定位比对。"""
    return re.sub(r"\s+", "", (text or "").translate(_FULL_TO_HALF)).casefold()


# ------------------------------------------------------------------ 实体抽取

# 各类型：抽取时先具体后泛化，后级跳过已被前级覆盖的字符区间。
_ENTITY_PATTERNS: list[tuple[str, str]] = [
    # 中国裁判文书案号：（2020）最高法知行终3421号 / (2019)京民终123号
    ("case_cn", r"[（(]\d{4}[）)][^()（）,，。;；、\s]{0,20}?\d{1,6}\s*号"),
    # 法释/法发等司法解释编号：法释〔2020〕15号
    ("statute_cn", r"法[释发][〔\[]\d{4}[〕\]]\s*\d{1,4}\s*号"),
    # 中国法条：《xx法》第x条 / 第xxx条
    ("article_cn", r"第[一二三四五六七八九十百千0-9]{1,6}\s*条"),
    # 美国判例报告号：820 F. Supp. 802 / 270 F.3d 1313 / 534 U.S. 318
    ("case_us", r"\d{1,4}\s+(?:F\.\s?Supp\.(?:\s?2d|\s?3d)?|F\.\s?2d|F\.\s?3d|F\.\s?4th|U\.S\.|S\.\s?Ct\.)\s+\d{1,4}"),
    # 美国法典条文：35 U.S.C. § 271(a)
    ("statute_us", r"\d+\s+U\.S\.C\.\s*§+\s*\d+[a-z()0-9]*"),
    # 日期/年份：2020年 / 2021-03-27 / 2011
    ("date", r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?|\d{4}-\d{2}-\d{2}|\b(?:19|20)\d{2}\b"),
    # 兜底：普通数字（≥2 位，含小数/百分比）
    ("number", r"\d+(?:\.\d+)?%?"),
]

_HIGH_KINDS = {"case_cn", "statute_cn", "article_cn", "case_us", "statute_us"}


@dataclass
class Entity:
    kind: str
    raw: str
    span: tuple[int, int]

    @property
    def norm(self) -> str:
        return normalize(self.raw)


def extract_entities(text: str) -> list[Entity]:
    """按类型优先级抽取硬实体；后级跳过前级已覆盖区间，避免案号里的年份被重复计为 date。"""
    clean = re.sub(r"\[cite:[^\]]*\]", " ", text or "")
    taken: list[tuple[int, int]] = []
    out: list[Entity] = []

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for kind, pattern in _ENTITY_PATTERNS:
        for m in re.finditer(pattern, clean):
            span = (m.start(), m.end())
            if not _free(span):
                continue
            taken.append(span)
            out.append(Entity(kind=kind, raw=m.group(0).strip(), span=span))
    return out


# ------------------------------------------------------------------ 比对


@dataclass
class CitationWarning:
    cites: list[str]
    claim: str
    kind: str
    severity: str  # high | medium | unverifiable
    source_ids: list[str]
    source_says: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "cites": self.cites,
            "claim": self.claim,
            "kind": self.kind,
            "severity": self.severity,
            "source_ids": self.source_ids,
            "source_says": self.source_says,
            "note": self.note,
        }


_CITE_RE = re.compile(r"\[cite:\s*([\d,\s]+)\]")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")


def _iter_cited_chunks(report_text: str):
    """按句切分，产出 (句子文本, [cite 编号...])；长句内多 cite 合并处理。"""
    for sent in _SENT_SPLIT_RE.split(report_text or ""):
        cites = [n.strip() for m in _CITE_RE.finditer(sent) for n in m.group(1).split(",") if n.strip()]
        if cites:
            yield sent, cites


def _find_candidates(entity: Entity, raw_text: str, limit: int = 3) -> list[str]:
    """在来源文本中找同类型实体作为 source_says 候选（用于纠正指向）。"""
    same_kind = [e for e in extract_entities(raw_text) if e.kind == entity.kind]
    return [e.raw for e in same_kind[:limit]]


def check_citations(
    report_text: str,
    sources: dict[str, str],
    cite_map: dict[str, str],
    tier: str,
    unverified_ids: set[str] | None = None,
    kinds: set[str] | None = None,
) -> tuple[list[CitationWarning], dict]:
    """比对报告论断实体与被引来源文本。

    - sources: {source_id: 可比对文本}；无文本的来源不出现在 dict 中，改由 unverified_ids 声明。
    - cite_map: {cite 编号: source_id}。
    - kinds: 限定校验的实体类型（标题层只查案号/法条等高价值实体）。
    返回 (warnings, score)；score 含命中统计，供 cite_map 映射自检。
    """
    warnings: list[CitationWarning] = []
    total = 0
    hits = 0
    unverifiable = 0
    unverified_ids = unverified_ids or set()

    for sent, cites in _iter_cited_chunks(report_text):
        body = _CITE_RE.sub(" ", sent)
        entities = [e for e in extract_entities(body) if not kinds or e.kind in kinds]
        if not entities:
            continue
        mapped_ids = [cite_map[c] for c in cites if c in cite_map]  # 未映射编号不猜测
        corpus = {sid: sources[sid] for sid in mapped_ids if sid in sources}
        has_gap = any(sid in unverified_ids for sid in mapped_ids)
        for ent in entities:
            total += 1
            if not corpus:
                unverifiable += 1
                warnings.append(
                    CitationWarning(
                        cites=cites,
                        claim=ent.raw,
                        kind=ent.kind,
                        severity="unverifiable",
                        source_ids=mapped_ids,
                        note="被引来源文本不可得（未导入或抓取为空），未能校验",
                    )
                )
                continue
            corpus_norm = {sid: normalize(t) for sid, t in corpus.items()}
            if any(ent.norm in t for t in corpus_norm.values()):
                hits += 1
                continue
            if has_gap:
                # 部分被引来源无文本：命中缺失可能是文本不全，不判错误
                unverifiable += 1
                warnings.append(
                    CitationWarning(
                        cites=cites,
                        claim=ent.raw,
                        kind=ent.kind,
                        severity="unverifiable",
                        source_ids=mapped_ids,
                        note="部分被引来源无文本，未能完全校验",
                    )
                )
                continue
            # 未定位：找同类型冲突实体升级 severity，否则 medium
            conflict = [
                cand
                for sid, raw in corpus.items()
                for cand in _find_candidates(ent, raw)
                if normalize(cand) != ent.norm
            ]
            severity = "high" if ent.kind in _HIGH_KINDS and conflict else "medium"
            warnings.append(
                CitationWarning(
                    cites=cites,
                    claim=ent.raw,
                    kind=ent.kind,
                    severity=severity,
                    source_ids=mapped_ids,
                    source_says=conflict[:3],
                    note=(
                        "同类实体在被引来源中取值不同，建议人工复核"
                        if severity == "high"
                        else "实体未在被引来源文本中定位（可能是报告归纳或来源文本不全）"
                    ),
                )
            )

    score = {
        "tier": tier,
        "entities": total,
        "matched": hits,
        "unverifiable": unverifiable,
        "match_rate": round(hits / total, 3) if total else None,
    }
    return warnings, score


def dedupe_warnings(warnings: list[CitationWarning], cap: int = 50) -> list[dict]:
    """同一实体的重复告警合并为一条（cites 合并），并设条数上限降噪。"""
    order: list[tuple[str, str]] = []
    merged: dict[tuple[str, str], CitationWarning] = {}
    for w in warnings:
        key = (w.kind, normalize(w.claim))
        if key in merged:
            base = merged[key]
            base.cites = sorted(set(base.cites) | set(w.cites), key=lambda c: int(c) if c.isdigit() else c)
            base.source_ids = sorted(set(base.source_ids) | set(w.source_ids))
            base.source_says = (base.source_says + [s for s in w.source_says if s not in base.source_says])[:3]
            if w.severity == "high":
                base.severity = "high"
        else:
            merged[key] = w
            order.append(key)
    return [merged[k].as_dict() for k in order[:cap]]


def build_cite_map(report_text: str, sources: dict[str, str], report_source_id: str) -> tuple[dict[str, str], str]:
    """构建 cite 编号 → source_id 映射并自检。

    基准假设：web 来源 1 起（position 1 = cite 1），报告自身（position 0）不占引用位。
    用实体命中率对「基准」与「编号整体偏移 1（报告占位）」两个假设打分，取高者；
    差距不明显时如实返回 assumed/ambiguous，不给假确定性。
    """
    items = sorted((sid, t) for sid, t in sources.items() if sid != report_source_id)
    if not items:
        return {}, "assumed:no-text"
    base = {str(i + 1): sid for i, (sid, _) in enumerate(items)}

    def rate(mapping: dict[str, str]) -> tuple[int, int]:
        warns, score = check_citations(report_text, sources, mapping, tier="probe")
        return (score["matched"] or 0), (score["entities"] or 0)

    # 偏移假设：cite N → 基准映射的 N-1（模拟报告占 1 号位）
    shifted = {str(i + 2): sid for i, (sid, _) in enumerate(items)}
    m_base, t_base = rate(base)
    m_shift, t_shift = rate(shifted)
    if t_base == 0:
        return base, "assumed:no-entities"
    if m_base > m_shift:
        return base, f"validated:match_rate={m_base}/{t_base}"
    if m_shift > m_base:
        return shifted, f"ambiguous:offset1_better={m_shift}/{t_shift}_vs_{m_base}/{t_base}"
    return base, f"ambiguous:tied={m_base}/{t_base}"
