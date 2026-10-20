"""Evidence extraction: parse and validate model JSON, verify quotes against source text."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .models import ExtractionDraft, VerificationStatus

_WS_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip().casefold()


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Best-effort decode of a single JSON object from a model answer."""
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(m.group(1) for m in _FENCE_RE.finditer(text))
    candidates.append(_first_balanced_object(text))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_extraction(raw_answer: str) -> tuple[ExtractionDraft | None, list[str]]:
    """Return (draft, validation_notes). ``draft is None`` means parse failure."""
    obj = extract_json_object(raw_answer)
    if obj is None:
        return None, ["answer did not contain a decodable JSON object"]
    notes: list[str] = []
    claims = obj.get("claims")
    if claims is not None and not isinstance(claims, list):
        notes.append("claims is not a list; ignored")
        obj = {**obj, "claims": []}
    try:
        draft = ExtractionDraft.model_validate(obj)
    except ValidationError as exc:
        return None, [f"extraction JSON failed schema validation: {exc.errors()[:3]}"]
    kept: list[Any] = []
    for claim in draft.claims:
        if not claim.claim.strip():
            notes.append("dropped empty claim")
            continue
        kept.append(claim)
    draft.claims = kept
    return draft, notes


def verify_quote(short_quote: str | None, source_text: str | None) -> VerificationStatus:
    if not source_text:
        return VerificationStatus.text_unavailable
    if not short_quote or not short_quote.strip():
        return VerificationStatus.unchecked
    if normalize_text(short_quote) in normalize_text(source_text):
        return VerificationStatus.quote_matched
    return VerificationStatus.quote_not_found


def match_quote_to_text(short_quote: str | None, source_text: str | None) -> bool:
    return verify_quote(short_quote, source_text) == VerificationStatus.quote_matched
