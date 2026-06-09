import json

import httpx
import pytest
import respx

from payer_intel.tools.search_api import (
    SearchApiClient,
    SearchQuotaExceeded,
)


@pytest.fixture
def mock_search():
    with respx.mock(assert_all_called=False) as m:
        yield m


def test_google_normalizes(mock_search):
    mock_search.get("https://www.searchapi.io/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "organic_results": [
                    {"title": "T1", "link": "https://a", "snippet": "s1", "date": "2025-01-02"},
                    {"title": "T2", "url": "https://b", "snippet": "s2"},
                ]
            },
        )
    )
    c = SearchApiClient(max_calls=5)
    out = c.google("humana salesforce")
    assert len(out) == 2
    assert out[0] == {"title": "T1", "link": "https://a", "snippet": "s1", "date": "2025-01-02"}
    assert out[1]["link"] == "https://b"


def test_google_jobs_normalizes(mock_search):
    mock_search.get("https://www.searchapi.io/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "Salesforce Admin",
                        "company_name": "Humana",
                        "location": "Remote",
                        "description": "Build on Service Cloud and Health Cloud",
                        "apply_links": [{"link": "https://jobs.example/1"}],
                        "detected_extensions": {"posted_at": "3 days ago"},
                    }
                ]
            },
        )
    )
    c = SearchApiClient(max_calls=5)
    out = c.google_jobs('"Humana" Salesforce')
    assert out[0]["title"] == "Salesforce Admin"
    assert out[0]["link"] == "https://jobs.example/1"
    assert "Service Cloud" in out[0]["snippet"]


def test_quota_cap(mock_search):
    mock_search.get("https://www.searchapi.io/api/v1/search").mock(
        return_value=httpx.Response(200, json={"organic_results": []})
    )
    c = SearchApiClient(max_calls=1)
    c.google("q1")
    with pytest.raises(SearchQuotaExceeded):
        c.google("q2")


def test_retries_then_succeeds(mock_search):
    route = mock_search.get("https://www.searchapi.io/api/v1/search")
    route.side_effect = [
        httpx.ConnectError("boom"),
        httpx.Response(200, json={"organic_results": [{"title": "ok", "link": "https://x"}]}),
    ]
    c = SearchApiClient(max_calls=5)
    out = c.google("q")
    assert out[0]["title"] == "ok"
