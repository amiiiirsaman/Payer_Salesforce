import httpx
import respx

from payer_intel.schema import SalesforceProduct
from payer_intel.tools.tech_fingerprint import fingerprint_domain


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, html=body)


def test_detects_experience_cloud():
    with respx.mock(assert_all_called=False) as m:
        m.get("https://humana.com/").mock(return_value=_html("<a href='https://members.humana.com/login'>login</a>"))
        m.get("https://humana.com/members").mock(
            return_value=httpx.Response(200, text="<script src='https://humana.my.site.com/x.js'></script>")
        )
        m.route().mock(return_value=httpx.Response(404, text=""))
        hits = fingerprint_domain("humana.com")
    products = {h.product for h in hits}
    assert SalesforceProduct.EXPERIENCE_CLOUD in products


def test_detects_pardot_and_marketing_cloud():
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<img src='https://pi.pardot.com/pixel.gif'/>"
                    "<script>var et = 'cloud.s7.exct.net'</script>"
                ),
            )
        )
        hits = fingerprint_domain("example.com")
    products = {h.product for h in hits}
    assert SalesforceProduct.PARDOT in products
    assert SalesforceProduct.MARKETING_CLOUD in products


def test_empty_domain_returns_nothing():
    assert fingerprint_domain("") == []


def test_no_signal_returns_empty():
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(return_value=httpx.Response(200, text="<html><body>nothing</body></html>"))
        hits = fingerprint_domain("plain.com")
    assert hits == []


def test_generic_salesforce_word_does_not_synthesize_service_cloud():
    """A page that only mentions the word 'salesforce' (e.g. job perk, press release
    boilerplate) must NOT trigger a synthetic SERVICE_CLOUD hit."""
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(
            return_value=httpx.Response(
                200,
                text="<html><body>Our team uses Salesforce daily.</body></html>",
            )
        )
        hits = fingerprint_domain("example.com")
    assert hits == []
    products = {h.product for h in hits}
    assert SalesforceProduct.SERVICE_CLOUD not in products
