"""Deterministic QC rules from §5 of the design doc.

Inputs: list of evidence items per payer/product. Output: ConfidenceScore + verdict
suggestion + optional review flag.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .schema import ConfidenceScore, Evidence, SalesforceProduct, UsageVerdict


@dataclass
class QCResult:
    verdict: UsageVerdict
    confidence: ConfidenceScore
    note: str = ""


_RECENT_JOB_DAYS = 365  # < 12 months
_RECENT_REVIEW_DAYS = 548  # < 18 months
_STALE_DAYS = 548


def _is_within(date_str: str | None, days: int) -> bool:
    if not date_str:
        return False
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%Y-%m"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return (datetime.utcnow() - dt) <= timedelta(days=days)
        except ValueError:
            continue
    # ISO-with-time
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return (datetime.utcnow() - dt.replace(tzinfo=None)) <= timedelta(days=days)
    except ValueError:
        return False


def score(
    product: SalesforceProduct, evidences: Iterable[Evidence]
) -> QCResult:
    evs = list(evidences)
    if not evs:
        return QCResult(UsageVerdict.UNKNOWN, ConfidenceScore.LOW, "no evidence")

    types = {e.source_type for e in evs}
    has_case_study = "case_study" in types
    has_tech = "technographic" in types
    recent_job = any(
        e.source_type == "job_posting" and _is_within(e.date, _RECENT_JOB_DAYS) for e in evs
    )
    any_job = "job_posting" in types
    recent_review = any(
        e.source_type == "review" and _is_within(e.date, _RECENT_REVIEW_DAYS) for e in evs
    )
    any_review = "review" in types
    any_news_recent = any(
        e.source_type == "news" and _is_within(e.date, _RECENT_JOB_DAYS) for e in evs
    )
    any_news = "news" in types

    # 1. Official case study — strongest signal
    if has_case_study:
        return QCResult(UsageVerdict.YES, ConfidenceScore.HIGH, "official case study")
    # 2-4. Multi-source corroboration anchored on a recent job posting
    if recent_job and recent_review:
        return QCResult(UsageVerdict.YES, ConfidenceScore.HIGH, "recent job + recent review")
    if recent_job and any_news_recent:
        return QCResult(UsageVerdict.YES, ConfidenceScore.HIGH, "recent job + recent news")
    if recent_job and has_tech:
        return QCResult(UsageVerdict.YES, ConfidenceScore.HIGH, "recent job + technographic")
    # 5-8. Single recent signal → Likely / Medium
    if recent_job:
        return QCResult(UsageVerdict.LIKELY, ConfidenceScore.MEDIUM, "recent job posting only")
    if recent_review:
        return QCResult(UsageVerdict.LIKELY, ConfidenceScore.MEDIUM, "recent review only")
    if any_news_recent:
        return QCResult(UsageVerdict.LIKELY, ConfidenceScore.MEDIUM, "recent news only")
    if has_tech:
        return QCResult(UsageVerdict.LIKELY, ConfidenceScore.MEDIUM, "technographic only")
    # 9. Only stale signals
    if any_job or any_review or any_news:
        return QCResult(UsageVerdict.UNKNOWN, ConfidenceScore.LOW, "only stale signals")
    # 10. Fallback
    return QCResult(UsageVerdict.UNKNOWN, ConfidenceScore.LOW, "no qualifying signals")
