"""Tests for the deterministic Layer 1 product extractor."""
from payer_intel.crew import _extract_products_from_body
from payer_intel.schema import Evidence


def _ev(full_text: str | None, snippet: str = "teaser") -> Evidence:
    ev = Evidence(source_type="case_study", url="https://salesforce.com/x", snippet=snippet)
    ev.full_text = full_text
    return ev


UHC_ALIASES = {"unitedhealthcare", "unitedhealth group", "uhg", "optum"}
BSC_ALIASES = {"blue shield of california", "blue shield"}
CENTENE_ALIASES = {"centene corporation", "centene", "fidelis care", "wellcare health plans"}


def test_products_used_section_extracts_list():
    ev = _ev(
        "UnitedHealthcare customer since 2010. "
        "PRODUCTS USED Service Cloud Marketing Cloud see all salesforce products"
    )
    result = _extract_products_from_body(ev, UHC_ALIASES)
    assert "Service Cloud" in result
    assert "Marketing Cloud" in result


def test_marketing_platform_synonym():
    ev = _ev("UnitedHealthcare uses Marketing Platform to drive member engagement.")
    result = _extract_products_from_body(ev, UHC_ALIASES)
    assert "Marketing Cloud" in result


def test_health_cloud_via_care_connect():
    ev = _ev("Blue Shield of California built Care Connect on Salesforce to coordinate care.")
    result = _extract_products_from_body(ev, BSC_ALIASES)
    assert "Health Cloud" in result


def test_prior_auth_maps_to_health_cloud():
    ev = _ev("Blue Shield uses Salesforce for prior authorization workflow automation.")
    result = _extract_products_from_body(ev, BSC_ALIASES)
    assert "Health Cloud" in result


def test_no_full_text_returns_empty_set():
    ev = Evidence(
        source_type="job_posting",
        url="https://linkedin.com/jobs/123",
        snippet="Salesforce administrator required",
    )
    result = _extract_products_from_body(ev, UHC_ALIASES)
    assert result == set()


def test_multiple_products_detected():
    ev = _ev(
        "UnitedHealthcare uses Service Cloud for member services, "
        "Marketing Cloud for personalised campaigns, "
        "and Health Cloud for care management programmes."
    )
    result = _extract_products_from_body(ev, UHC_ALIASES)
    assert result == {"Service Cloud", "Marketing Cloud", "Health Cloud"}


def test_generic_page_without_payer_name_returns_empty():
    generic_body = (
        "See how Agentforce Health is cutting paperwork. "
        "Health Cloud puts patients at the heart of every decision. "
        "Marketing Cloud drives action with intelligent marketing. "
        "Service Cloud builds loyalty while lowering cost of healthcare."
    )
    ev = _ev(generic_body)
    result = _extract_products_from_body(ev, CENTENE_ALIASES)
    assert result == set(), f"False positive: generic page returned {result} for Centene"
