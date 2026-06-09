from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .fetcher import fetch
from ..schema import SalesforceProduct

# (product, regex). First-match wins per product; multiple products may match.
_PATTERNS: list[tuple[SalesforceProduct, re.Pattern[str]]] = [
    (SalesforceProduct.EXPERIENCE_CLOUD, re.compile(r"my\.site\.com|force\.com/s/|\.lightning\.force\.com|community\.force\.com", re.I)),
    (SalesforceProduct.SERVICE_CLOUD, re.compile(r"my\.salesforce\.com|service-cloud|servicecloud", re.I)),
    (SalesforceProduct.SALES_CLOUD, re.compile(r"sales[-\s]?cloud|sfdc-lightning", re.I)),
    (SalesforceProduct.MARKETING_CLOUD, re.compile(r"exacttarget\.com|marketingcloud\.com|cloud\.s7\.exct\.net|et\.com|mc\.[a-z0-9-]+\.salesforce-experience\.com", re.I)),
    (SalesforceProduct.PARDOT, re.compile(r"pardot\.com|pi\.pardot\.com|go\.pardot\.com", re.I)),
    (SalesforceProduct.HEALTH_CLOUD, re.compile(r"health[-\s]?cloud", re.I)),
    (SalesforceProduct.AGENTFORCE_HEALTHCARE, re.compile(r"agentforce", re.I)),
    (SalesforceProduct.DATA_CLOUD, re.compile(r"salesforce data cloud|sfdc data cloud", re.I)),
    (SalesforceProduct.REVENUE_CLOUD, re.compile(r"revenue cloud|salesforce cpq", re.I)),
    (SalesforceProduct.FINANCIAL_SERVICES_CLOUD, re.compile(r"financial services cloud", re.I)),
    (SalesforceProduct.LIFE_SCIENCES_CLOUD, re.compile(r"life sciences cloud", re.I)),
]

_PATHS = ["/", "/members", "/login", "/careers", "/about", "/s/login", "/s/", "/contact-us"]


@dataclass
class FingerprintHit:
    product: SalesforceProduct
    url: str
    matched: str
    source_type: str = "technographic"


def fingerprint_domain(domain: str) -> list[FingerprintHit]:
    if not domain:
        return []
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    hits: list[FingerprintHit] = []
    seen: set[tuple[str, str]] = set()
    for path in _PATHS:
        url = base + path
        resp = fetch(url, timeout=8.0)
        if resp is None:
            continue
        haystacks: list[str] = [str(resp.url), resp.text or ""]
        for h in resp.headers.values():
            haystacks.append(h)
        blob = "\n".join(haystacks)
        for product, pat in _PATTERNS:
            m = pat.search(blob)
            if m:
                key = (product.value, m.group(0).lower())
                if key in seen:
                    continue
                seen.add(key)
                hits.append(FingerprintHit(product=product, url=str(resp.url), matched=m.group(0)))
    return hits


def summarize_hits(hits: Iterable[FingerprintHit]) -> str:
    parts = [f"{h.product.value} via '{h.matched}' at {h.url}" for h in hits]
    return "; ".join(parts) if parts else "No Salesforce fingerprints found."
