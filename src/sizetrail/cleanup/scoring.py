"""Deterministic, explainable cleanup opportunity scoring."""

from __future__ import annotations

from dataclasses import dataclass
from math import log1p

from sizetrail.models.patterns import RiskLevel


@dataclass(frozen=True, slots=True)
class CleanupScore:
    score: float
    confidence: float
    size_component: float
    age_component: float
    risk_component: float
    rebuild_component: float


def score_cleanup_candidate(
    *,
    size: int,
    age_days: float,
    risk: RiskLevel,
    rebuild_hint: str | None,
    rule_confidence: float,
    partial: bool = False,
    inaccessible: bool = False,
    overlap: bool = False,
) -> CleanupScore:
    """Score one candidate without treating the score as a safety proof.

    Formula (0–100): 35% logarithmic size, 25% age, 20% inverse risk,
    10% rebuildability, and 10% rule confidence. Coverage and overlap only
    reduce confidence; safety still comes from CleanupPlan revalidation.
    """
    size_component = min(1.0, log1p(max(0, size)) / log1p(100 * 1024**3))
    age_component = min(1.0, max(0.0, age_days) / 180.0)
    risk_component = {
        RiskLevel.SAFE: 1.0,
        RiskLevel.MODERATE: 0.55,
        RiskLevel.DANGEROUS: 0.15,
    }[risk]
    rebuild_component = 1.0 if rebuild_hint else 0.35
    bounded_rule_confidence = min(1.0, max(0.0, rule_confidence))
    score = 100.0 * (
        0.35 * size_component
        + 0.25 * age_component
        + 0.20 * risk_component
        + 0.10 * rebuild_component
        + 0.10 * bounded_rule_confidence
    )
    confidence = bounded_rule_confidence
    if partial:
        confidence *= 0.65
    if inaccessible:
        confidence *= 0.55
    if overlap:
        confidence *= 0.75
    return CleanupScore(
        score=round(score, 2),
        confidence=round(confidence, 3),
        size_component=round(size_component, 3),
        age_component=round(age_component, 3),
        risk_component=risk_component,
        rebuild_component=rebuild_component,
    )
