"""Coverage counters. Each stage is a distinct metric; never conflate them."""

from __future__ import annotations

from typing import Any

from .models import AnalyzedStatus, ImportStatus, ReadStatus, VerificationStatus
from .store import Store


def coverage_summary(store: Store, research_id: str) -> dict[str, Any]:
    sources = store.list_sources(research_id)
    evidence = store.list_evidence(research_id)
    verified_sources = {
        ev.source_id
        for ev in evidence
        if ev.verification_status == VerificationStatus.quote_matched
    }
    selected_states = {
        ImportStatus.selected,
        ImportStatus.imported,
        ImportStatus.import_failed,
        ImportStatus.import_uncertain,
    }
    return {
        "discovered": len(sources),
        "cited_in_report": sum(1 for s in sources if s.cited_in_report),
        "importable": sum(1 for s in sources if s.importable),
        "selected": sum(1 for s in sources if s.import_status in selected_states),
        "imported": sum(1 for s in sources if s.import_status == ImportStatus.imported),
        "import_failed": sum(1 for s in sources if s.import_status == ImportStatus.import_failed),
        "import_uncertain": sum(1 for s in sources if s.import_status == ImportStatus.import_uncertain),
        "ready": sum(1 for s in sources if s.import_status == ImportStatus.imported),
        "readable": sum(1 for s in sources if s.read_status == ReadStatus.readable),
        "analyzed": sum(
            1
            for s in sources
            if s.analyzed_status in (AnalyzedStatus.analyzed, AnalyzedStatus.partial)
        ),
        "verified": len(verified_sources),
        "evidence_cards": len(evidence),
        "evidence_quote_matched": sum(
            1 for ev in evidence if ev.verification_status == VerificationStatus.quote_matched
        ),
    }
