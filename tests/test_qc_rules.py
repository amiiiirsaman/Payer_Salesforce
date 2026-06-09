from datetime import datetime, timedelta

from payer_intel.qc import score
from payer_intel.schema import ConfidenceScore, Evidence, SalesforceProduct, UsageVerdict


def _d(days_ago: int) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_case_study_is_high_yes():
    evs = [Evidence(source_type="case_study", url="https://salesforce.com/x", date=_d(900))]
    r = score(SalesforceProduct.HEALTH_CLOUD, evs)
    assert r.verdict == UsageVerdict.YES
    assert r.confidence == ConfidenceScore.HIGH


def test_recent_job_plus_tech_is_high_yes():
    evs = [
        Evidence(source_type="job_posting", url="https://j", date=_d(60)),
        Evidence(source_type="technographic", url="https://t"),
    ]
    r = score(SalesforceProduct.SERVICE_CLOUD, evs)
    assert r.confidence == ConfidenceScore.HIGH
    assert r.verdict == UsageVerdict.YES


def test_recent_job_only_is_medium_likely():
    evs = [Evidence(source_type="job_posting", url="https://j", date=_d(120))]
    r = score(SalesforceProduct.SALES_CLOUD, evs)
    assert r.confidence == ConfidenceScore.MEDIUM
    assert r.verdict == UsageVerdict.LIKELY


def test_stale_only_is_low_unknown():
    evs = [Evidence(source_type="review", url="https://r", date=_d(900))]
    r = score(SalesforceProduct.MARKETING_CLOUD, evs)
    assert r.confidence == ConfidenceScore.LOW


def test_no_evidence_unknown():
    r = score(SalesforceProduct.DATA_CLOUD, [])
    assert r.verdict == UsageVerdict.UNKNOWN
    assert r.confidence == ConfidenceScore.LOW


def test_tech_only_is_medium_likely():
    evs = [Evidence(source_type="technographic", url="https://t")]
    r = score(SalesforceProduct.EXPERIENCE_CLOUD, evs)
    assert r.confidence == ConfidenceScore.MEDIUM
    assert r.verdict == UsageVerdict.LIKELY
