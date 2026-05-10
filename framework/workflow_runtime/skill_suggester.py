"""Skill suggester — Phase 4 deliverable per ADR-018. Stub for now."""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


class SkillSuggester:
    """Phase 4. Logs Tier-4 misses + Tier-2 repeated patterns; clusters; emits weekly digest."""
    def log_miss(self, persona: str, query: str, tier: int) -> None:
        log.info("skill_suggester [stub] miss persona=%s tier=%d query=%s", persona, tier, query)

    def cluster_nightly(self) -> list[dict]:
        return []  # Phase 4 implementation
