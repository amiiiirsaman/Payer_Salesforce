from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from crewai import Crew, Process, Task

from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .agents import (
    case_study_agent,
    classifier_agent,
    export_agent,
    jobs_agent,
    news_agent,
    orchestrator_agent,
    qc_agent,
    recency_agent,
    reviews_agent,
    target_identification_agent,
    technographic_agent,
)
from .export import write_excel
from .qc import score as qc_score
from .schema import (
    ConfidenceScore,
    Evidence,
    EXCEL_COLUMNS,
    PRODUCT_COLUMNS,
    PayerRecord,
    SalesforceProduct,
    UsageVerdict,
)
from .tools.search_api import SearchApiClient, SearchQuotaExceeded
from .tools.tech_fingerprint import fingerprint_domain

log = logging.getLogger(__name__)


# Canonical case-study URLs that Google Search routinely ranks outside the top
# 20; injected directly so the body enricher always fetches them. Only true
# deployment case studies — not aspirational blog posts — should appear here,
# because qc rule 1 auto-promotes any case_study evidence to Yes/High.
_KNOWN_CASE_STUDIES: dict[str, str] = {
    "UnitedHealthcare": "https://www.salesforce.com/customer-success-stories/united-healthcare/",
    "Humana Inc.": "https://www.salesforce.com/customer-success-stories/humana/",
}


# ─────────────────────────────────────────────────────────────────────────────
# Seed loading (Agent 2: Target Identification)
# ─────────────────────────────────────────────────────────────────────────────
def load_seed(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append({k.strip(): (v or "").strip() for k, v in row.items()})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic sourcing (Agents 3–7) — fast, low-cost, deterministic
# ─────────────────────────────────────────────────────────────────────────────
_REVIEW_SITES = "site:g2.com OR site:capterra.com OR site:trustradius.com"
_PARTNER_SITES = (
    "site:salesforce.com/customer-success-stories "
    "OR site:salesforce.com/news/stories "
    "OR site:salesforce.com/blog "
    "OR site:salesforce.com/resources/customer-stories "
    "OR site:silverlinecrm.com OR site:penrod.co "
    "OR site:slalom.com OR site:deloitte.com OR site:accenture.com "
    "OR site:cognizant.com OR site:ibm.com"
)
_COMMUNITY_SITES = "site:trailhead.salesforce.com OR site:appexchange.salesforce.com"
# CIO / executive interview & trade press sources. Surfaced the Geisinger /
# Salesforce Marketing+Health Cloud quote that v5 missed (Aarete MS-01).
_CIO_INTERVIEW_SITES = (
    "site:deloitte.wsj.com OR site:deloitte.com/insights OR site:hbr.org "
    "OR site:healthcareitnews.com OR site:modernhealthcare.com "
    "OR site:healthtechmagazine.net"
)
# LinkedIn posts + member profiles + Pulse articles. Snippet-only evidence
# (LinkedIn blocks unauthenticated httpx, so no _FETCH_DOMAINS entry).
_LINKEDIN_SITES = (
    "site:linkedin.com/posts/ OR site:linkedin.com/in/ OR site:linkedin.com/pulse/"
)
_LINKEDIN_TITLE_TERMS = (
    '"Salesforce Marketing Cloud Specialist" OR "Health Cloud Administrator" '
    'OR "Salesforce Developer" OR "Agentforce Developer" OR "Vlocity"'
)
_JOB_PRODUCT_TERMS = (
    'Salesforce OR "Sales Cloud" OR "Service Cloud" OR "Health Cloud" '
    'OR "Marketing Cloud" OR "Experience Cloud" OR "Data Cloud" '
    'OR Pardot OR ExactTarget OR "CRM Analytics" OR Agentforce OR Vlocity'
)
_NEWS_PRODUCT_TERMS = (
    'Salesforce OR "Health Cloud" OR "Data Cloud" OR "Marketing Cloud" '
    'OR Agentforce'
)


def build_name_clause(name: str, aliases_raw: str | None) -> str:
    names = [name] + [a.strip() for a in (aliases_raw or "").split("|") if a.strip()]
    seen: set[str] = set()
    deduped: list[str] = []
    for n in names:
        if n.lower() in seen:
            continue
        seen.add(n.lower())
        deduped.append(n)
    if len(deduped) == 1:
        return f'"{deduped[0]}"'
    return "(" + " OR ".join(f'"{n}"' for n in deduped) + ")"


def build_excludes_set(payer: dict[str, str]) -> set[str]:
    """Lowercased set of sibling-entity names to reject during attribution.

    Mirrors build_name_clause but for the optional `search_excludes` CSV
    column. Independence Blue Cross excludes "AmeriHealth Caritas" so its
    sibling entity's job postings don't get cross-attributed (Aarete MS-05).
    """
    raw = payer.get("search_excludes") or ""
    return {x.strip().lower() for x in raw.split("|") if x.strip()}


def _safe_search(fn, *args, **kwargs) -> list[dict]:
    try:
        return fn(*args, **kwargs)
    except SearchQuotaExceeded:
        log.warning("SearchApi quota exceeded; skipping further calls.")
        return []
    except Exception as e:  # noqa: BLE001  – best-effort sourcing
        log.warning("Search call failed: %s", e)
        return []


def gather_evidence(payer: dict[str, str], client: SearchApiClient) -> list[Evidence]:
    name = payer["payer_name"]
    domain = payer.get("domain", "")
    name_clause = build_name_clause(name, payer.get("search_aliases"))
    evidence: list[Evidence] = []

    if name in _KNOWN_CASE_STUDIES:
        evidence.append(
            Evidence(
                source_type="case_study",
                url=_KNOWN_CASE_STUDIES[name],
                snippet="Official Salesforce case study.",
                date=None,
            )
        )

    # Agent 3 — Jobs (broadened: explicit cloud names catch postings where
    # 'Salesforce' is not adjacent to the product name)
    for r in _safe_search(client.google_jobs, f"{name_clause} ({_JOB_PRODUCT_TERMS})", num=20):
        evidence.append(
            Evidence(
                source_type="job_posting",
                url=r.get("link", "") or "",
                snippet=(r.get("snippet") or "")[:1500],
                date=r.get("date"),
            )
        )

    # Agent 4 — News (broadened with product terms)
    for r in _safe_search(client.google_news, f"{name_clause} ({_NEWS_PRODUCT_TERMS})", num=20):
        evidence.append(
            Evidence(
                source_type="news",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 4b — Dreamforce / Agentforce / Einstein Copilot sessions on salesforce.com
    # (not indexed as news; eligible for v6 page-body fetch since salesforce.com is whitelisted)
    for r in _safe_search(
        client.google,
        f'site:salesforce.com {name_clause} ("Agentforce" OR "Dreamforce" OR "Einstein Copilot")',
        num=10,
    ):
        evidence.append(
            Evidence(
                source_type="case_study",
                url=r.get("link", "") or "",
                snippet=(r.get("snippet") or "")[:1200],
                date=r.get("date"),
            )
        )

    # Agent 5 — Reviews
    for r in _safe_search(client.google, f"{_REVIEW_SITES} {name_clause} Salesforce", num=20):
        evidence.append(
            Evidence(
                source_type="review",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 6 — Case studies / partners
    for r in _safe_search(client.google, f"{_PARTNER_SITES} {name_clause} Salesforce case study", num=20):
        evidence.append(
            Evidence(
                source_type="case_study",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 6.5 — Trailblazer Community / AppExchange (high-signal source for
    # CVS Sales Cloud and BCBSM Experience Cloud in prior runs). Mapped to
    # 'review' so existing QC recency rules apply.
    for r in _safe_search(client.google, f"{_COMMUNITY_SITES} {name_clause}", num=20):
        evidence.append(
            Evidence(
                source_type="review",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 6.6 — CIO / executive interviews & healthcare trade press.
    # The Geisinger Marketing+Health Cloud quote ran on deloitte.wsj.com which
    # was not previously in scope (Aarete MS-01).
    for r in _safe_search(
        client.google,
        f"{_CIO_INTERVIEW_SITES} {name_clause} Salesforce",
        num=10,
    ):
        evidence.append(
            Evidence(
                source_type="case_study",
                url=r.get("link", "") or "",
                snippet=(r.get("snippet") or "")[:1200],
                date=r.get("date"),
            )
        )

    # Agent 6.7 — LinkedIn posts/profiles/pulse. Surfaces first-person
    # platform-usage statements (Sanford intern, IBX developer) and
    # product-specific employee titles (Aarete MS-02, MS-05, MS-06). Snippet-
    # only — LinkedIn blocks unauthenticated httpx. Mapped to 'review' so QC's
    # recent_review path applies.
    for r in _safe_search(
        client.google,
        f'{_LINKEDIN_SITES} {name_clause} '
        f'(Salesforce OR "Marketing Cloud" OR "Health Cloud" OR Vlocity OR Agentforce)',
        num=15,
    ):
        evidence.append(
            Evidence(
                source_type="review",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 6.8 — LinkedIn employee-title pass. A named employee with a
    # product-specific title ("Salesforce Marketing Cloud Specialist") is
    # Tier-1 evidence per Aarete Part 3.
    for r in _safe_search(
        client.google,
        f'site:linkedin.com/in/ {name_clause} ({_LINKEDIN_TITLE_TERMS})',
        num=10,
    ):
        evidence.append(
            Evidence(
                source_type="review",
                url=r.get("link", "") or "",
                snippet=r.get("snippet", ""),
                date=r.get("date"),
            )
        )

    # Agent 7 — Technographic fingerprint
    for h in fingerprint_domain(domain):
        evidence.append(
            Evidence(
                source_type="technographic",
                url=h.url,
                snippet=f"matched marker '{h.matched}'",
                matched_product=h.product,
            )
        )

    # drop empties / dedupe by (source_type, url)
    seen: set[tuple[str, str]] = set()
    out: list[Evidence] = []
    for e in evidence:
        if not e.url:
            continue
        key = (e.source_type, e.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out = _enrich_with_page_bodies(out)
    return out


# Agent 8 — Page Body Enricher: domains whose pages reliably contain named
# Salesforce product evidence beyond the search snippet teaser.
_FETCH_DOMAINS: frozenset[str] = frozenset({
    # Salesforce-owned
    "salesforce.com",
    "trailhead.salesforce.com",
    # Payer-owned newsrooms / IR
    "news.blueshieldca.com",
    "newsroom.humana.com",
    "newsroom.cigna.com",
    "newsroom.elevancehealth.com",
    "ir.molinahealthcare.com",
    "newsroom.kaiserpermanente.org",
    "newsroom.highmark.com",
    # Wire services
    "businesswire.com",
    "prnewswire.com",
    "globenewswire.com",
    # Trade press
    "fiercehealthcare.com",
    "healthcaredive.com",
    "mobihealthnews.com",
    "medcitynews.com",
    "beckershospitalreview.com",
    # CIO / executive interview sources (Aarete MS-01)
    "deloitte.wsj.com",
    "deloitte.com",
    "hbr.org",
    "healthcareitnews.com",
    "modernhealthcare.com",
    "healthtechmagazine.net",
})
_MAX_BODY_CHARS = 4000
_WS_RE = re.compile(r"\s+")


def _enrich_with_page_bodies(evidence: list[Evidence]) -> list[Evidence]:
    from .tools.fetcher import fetch

    for ev in evidence:
        if not ev.url:
            continue
        host = (urlparse(ev.url).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in _FETCH_DOMAINS):
            continue
        resp = fetch(ev.url, timeout=15.0)
        if resp is None:
            continue
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            cleaned = _WS_RE.sub(" ", text).strip()
            # AppExchange pages are JS-rendered; httpx returns a near-empty shell.
            # Keep the search snippet rather than overwriting with empty body.
            if host.endswith("appexchange.salesforce.com") and len(cleaned) < 200:
                continue
            ev.full_text = cleaned[:_MAX_BODY_CHARS]
        except Exception:  # noqa: BLE001 — best-effort enrichment
            continue
    return evidence


# ─────────────────────────────────────────────────────────────────────────────
# Agent 8b — Deterministic Product Extractor (Layer 1)
# Runs on fetched page bodies only. Python regex, no LLM, no API call.
# Only fires when a payer alias appears in the body (false-positive guard).
# ──────────────────────────────────────────────────────────────────────────
# Keys MUST equal SalesforceProduct.value exactly (no regex escaping in keys).
# Values are regex patterns; escape special characters only inside patterns.
_PRODUCT_PATTERNS: dict[str, list[str]] = {
    "Marketing Cloud Account Engagement (Pardot)": [
        r"Pardot",
        r"Account Engagement",
    ],
    "Agentforce for Healthcare": [
        r"Agentforce for Healthcare",
        r"Agentforce for Health",
        r"Einstein Copilot for Health",
        r"Agentforce",
    ],
    "Health Cloud": [
        r"Health Cloud",
        r"Care Connect",
        r"prior authori[sz]ation",
        r"Vlocity Health",
        r"Vlocity Insurance",
        r"OmniStudio",
        r"Salesforce Industries",
        r"Health Cloud Industry Edition",
    ],
    "Life Sciences Cloud": [r"Life Sciences Cloud"],
    "Financial Services Cloud": [r"Financial Services Cloud"],
    "Revenue Cloud (CPQ)": [
        r"Revenue Cloud",
        r"\bCPQ\b",
        r"SteelBrick",
    ],
    "Data Cloud": [
        r"Data Cloud",
        r"CRM Analytics",
        r"Tableau CRM",
        r"Einstein Analytics",
    ],
    "Marketing Cloud": [
        r"\bMarketing Cloud\b",
        r"Marketing Platform",
        r"\bSFMC\b",
        r"ExactTarget",
        r"Email Studio",
        r"\bet\.com\b",
    ],
    "Experience Cloud": [
        r"\bExperience Cloud\b",
        r"Community Cloud",
        r"my\.site\.com",
        r"Digital Experience Cloud",
    ],
    "Service Cloud": [
        r"\bService Cloud\b",
        r"Field Service Lightning",
        r"\bFSL\b",
        r"Service Console",
        r"Omni.Channel",
    ],
    "Sales Cloud": [
        r"\bSales Cloud\b",
        r"CareIQ",
        r"Care IQ",
    ],
}

# Maximum distance (chars) between a payer-alias mention and a product-pattern
# match for the match to count. Prevents pages that mention the payer once in a
# header and discuss unrelated Salesforce products elsewhere from producing
# false-positive Layer 1 hits (e.g. Devoted Health / Alameda Alliance picking
# up Agentforce from distant boilerplate).
_PROXIMITY_WINDOW = 600

# "Agentforce" appears on nearly every Salesforce marketing page, Trailhead
# tutorial and blog post. Standard proximity is not strong enough — require
# both a payer alias AND a deployment-signal word inside a tighter window.
_AGENTFORCE_PROXIMITY = 300
_AGENTFORCE_DEPLOYMENT_INDICATORS = {
    "deploy", "implement", "launch", "partner", "customer story",
    "use case", "solution", "contract", "agreement", "pilot",
    "rollout", "go live", "go-live", "production", "signed",
}

# Phrases in the LLM narrative indicating it itself found no real evidence —
# used by _classify_with_llm to clear any spurious product mappings post-hoc.
_NO_EVIDENCE_PHRASES: tuple[str, ...] = (
    "no credible evidence",
    "no evidence",
    "minimal credible evidence",
    "minimal evidence",
    "generic navigation",
    "generic trailhead",
    "tutorial page",
    "generic salesforce marketing",
    "generic marketing material",
    "case studies about other organizations",
    "not specifically mention",
    "does not mention",
    "none specifically mention",
    "different entity",
    "different organization",
    "does not appear to use",
)


# URLs to drop from BD output when the payer has no positive verdicts —
# generic Salesforce marketing/tutorial pages and payer's own-domain pages.
_NON_EVIDENCE_URL_PATTERNS: tuple[str, ...] = (
    "salesforce.com/eu/",
    "salesforce.com/nl/",
    "salesforce.com/es/",
    "salesforce.com/de/",
    "trailhead.salesforce.com/content/learn/",
)


def _alias_in_text(alias_lower: str, text_lower: str) -> bool:
    # Word-boundary alias match prevents short aliases (e.g. "Blue Shield"
    # for BCBS Louisiana) from spuriously matching unrelated contexts.
    if not alias_lower:
        return False
    return bool(re.search(r"\b" + re.escape(alias_lower) + r"\b", text_lower))


# Salesforce blog category/tag/author listings and paginated index pages
# aggregate teasers from unrelated articles. A payer name appearing on
# such a page is not evidence (Aarete FP-02, FP-07).
_ZERO_EVIDENCE_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/blog/category/", re.I),
    re.compile(r"/blog/tag/", re.I),
    re.compile(r"/blog/author/", re.I),
    re.compile(r"/blog/page/\d+/", re.I),
    re.compile(r"/blog/?$", re.I),
)


def _is_zero_evidence_url(url: str) -> bool:
    if not url:
        return False
    return any(p.search(url) for p in _ZERO_EVIDENCE_URL_PATTERNS)


# SI partner hosts whose brochures/whitepapers are only valid evidence when
# the target payer is literally named in the body (Aarete FP-06).
_SI_PARTNER_HOSTS: frozenset[str] = frozenset({
    "accenture.com", "deloitte.com", "ibm.com", "cognizant.com",
    "slalom.com", "silverlinecrm.com", "penrod.co",
})


def _si_partner_requires_payer_mention(
    url: str, body: str | None, payer_aliases_lower: set[str]
) -> bool:
    """Return True ('drop this evidence') for an SI-partner page that never
    names the payer in its body. Snippet-only items (body is None) get the
    benefit of the doubt and are kept; the LLM will see them with full
    SI-partner caveats in the prompt."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _SI_PARTNER_HOSTS):
        return False
    if not body:
        return False
    body_lower = body.lower()
    return not any(_alias_in_text(a, body_lower) for a in payer_aliases_lower)


# Customer-verb proximity check for salesforce.com /blog/ articles. Without
# a deployment verb near a payer mention, the payer is just a backdrop in
# an industry thought-leadership post and the article is not evidence
# (Aarete FP-01).
_CUSTOMER_VERB_RE = re.compile(
    r"\b(implement(?:ed|s|ing)?|deployed|deploys?|deploying|uses?|using|"
    r"selected|chose|migrated\s+to|customer\s+of|partnered\s+with|"
    r"is\s+using|adopted)\b",
    re.I,
)
_CUSTOMER_VERB_WINDOW = 400


def _salesforce_blog_lacks_customer_verb(
    url: str, body: str | None, payer_aliases_lower: set[str]
) -> bool:
    """Return True ('drop this evidence') for a salesforce.com /blog/ URL
    that surfaced via search but never pairs a payer mention with a
    customer/deployment verb within ±_CUSTOMER_VERB_WINDOW chars."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not (host == "salesforce.com" or host == "www.salesforce.com"):
        return False
    if "/blog/" not in url.lower():
        return False
    if not body:
        # Snippet-only \u2014 keep; LLM sees the FP-01 guardrail in the prompt.
        return False
    body_lower = body.lower()
    for alias in payer_aliases_lower:
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", body_lower):
            start = max(0, m.start() - _CUSTOMER_VERB_WINDOW)
            end = m.end() + _CUSTOMER_VERB_WINDOW
            if _CUSTOMER_VERB_RE.search(body_lower[start:end]):
                return False
    return True


def _evidence_body_contains_exclude(
    body: str | None, excludes_lower: set[str], payer_aliases_lower: set[str]
) -> bool:
    """Return True ('drop this evidence') if the body names a sibling-entity
    exclude term but never names the primary payer within ±_CUSTOMER_VERB_WINDOW
    chars of any alias. AmeriHealth Caritas job postings should not be
    cross-attributed to Independence Blue Cross (Aarete MS-05)."""
    if not body or not excludes_lower:
        return False
    body_lower = body.lower()
    hit_exclude = any(_alias_in_text(x, body_lower) for x in excludes_lower)
    if not hit_exclude:
        return False
    # Body mentions a sibling. Keep only if it ALSO mentions the primary
    # payer with no co-mention of the sibling in the same window.
    return not any(_alias_in_text(a, body_lower) for a in payer_aliases_lower)


def _should_drop_evidence(
    ev: Evidence, payer_aliases_lower: set[str], excludes_lower: set[str]
) -> bool:
    """Composite URL/body gate used by both the deterministic extractor and
    the LLM evidence_blob builder. Centralises FP-01/FP-02/FP-06/FP-07 and
    MS-05 rejection logic so the two layers stay consistent."""
    if _is_zero_evidence_url(ev.url):
        return True
    body = ev.full_text
    if _si_partner_requires_payer_mention(ev.url, body, payer_aliases_lower):
        return True
    if _salesforce_blog_lacks_customer_verb(ev.url, body, payer_aliases_lower):
        return True
    if _evidence_body_contains_exclude(body, excludes_lower, payer_aliases_lower):
        return True
    return False


# Single-word aliases that are common English words and produce cross-payer
# contamination when used for proximity matching (e.g. "devoted" appearing
# near "Health Cloud" in an NYU Langone case study triggers a false positive
# for Devoted Health). Filtered out of the proximity guard but still usable
# by Layer 2 (LLM classifier) which has full context.
_WEAK_ALIASES: frozenset[str] = frozenset({
    "health", "care", "blue", "cross", "plan", "group", "first",
    "community", "devoted", "oscar", "alliance", "essence", "kaiser",
    "sanford", "emblem", "horizon", "independent", "priority", "partnership",
    "point32health", "medstar", "geisinger", "excellus", "fallon",
    "cigna", "aetna", "humana", "anthem", "optum",
})


def _is_strong_alias(alias: str) -> bool:
    # Aliases used for proximity matching must be either multi-word,
    # an acronym (all uppercase, length ≥ 3), or a distinctive long token
    # not in the common-English-word stoplist. Common single words are
    # rejected to prevent cross-payer contamination.
    a = alias.strip()
    if not a:
        return False
    if " " in a:
        return True
    if a.isupper() and len(a) >= 3:
        return True
    return len(a) > 6 and a.lower() not in _WEAK_ALIASES


def _extract_products_from_body(
    ev: Evidence,
    payer_aliases: set[str],
    excludes: set[str] | None = None,
) -> set[str]:
    """Layer 1 deterministic extractor.

    Returns the set of SalesforceProduct.value strings literally present in
    ev.full_text within ±_PROXIMITY_WINDOW chars of a payer-alias mention.
    Agentforce additionally requires a deployment-indicator word in the same
    tight window (±_AGENTFORCE_PROXIMITY). Returns empty set when full_text
    is None (snippet-only items stay on the LLM path), when no alias
    mentions appear in the body, or when URL/body gating rejects the item.
    """
    if not ev.full_text:
        return set()
    payer_aliases_lower = {a.lower() for a in payer_aliases if a}
    excludes_lower = excludes or set()
    if _should_drop_evidence(ev, payer_aliases_lower, excludes_lower):
        return set()
    body = ev.full_text
    body_lower = body.lower()
    # Strict alias set: multi-word, acronym, or distinctive long token.
    # Falls back to the full payer name if every alias is filtered out.
    strong = {a for a in payer_aliases if _is_strong_alias(a)}
    if not strong:
        strong = set(payer_aliases)
    strong_aliases_lower = {a.lower() for a in strong if a}
    alias_positions: list[int] = []
    for needle in strong_aliases_lower:
        for m in re.finditer(r"\b" + re.escape(needle) + r"\b", body_lower):
            alias_positions.append(m.start())
    if not alias_positions:
        return set()
    found: set[str] = set()

    # Agentforce: tighter check — needs payer alias AND deployment indicator
    # in same ±_AGENTFORCE_PROXIMITY window. Generic nav/tutorial pages fail.
    for m in re.finditer(r"agentforce", body_lower):
        win_start = max(0, m.start() - _AGENTFORCE_PROXIMITY)
        win_end = m.end() + _AGENTFORCE_PROXIMITY
        window = body_lower[win_start:win_end]
        if any(_alias_in_text(a, window) for a in strong_aliases_lower) and any(
            ind in window for ind in _AGENTFORCE_DEPLOYMENT_INDICATORS
        ):
            found.add("Agentforce for Healthcare")
            break

    for product, patterns in _PRODUCT_PATTERNS.items():
        if product == "Agentforce for Healthcare":
            continue  # handled above with stricter check
        combined = "|".join(f"(?:{p})" for p in patterns)
        for m in re.finditer(combined, body, re.IGNORECASE):
            mid = (m.start() + m.end()) // 2
            if any(abs(mid - p) <= _PROXIMITY_WINDOW for p in alias_positions):
                found.add(product)
                break
    return found


# ──────────────────────────────────────────────────────────────────────────
# Agent 8 — Classifier (Bedrock-backed CrewAI task; Layer 2)
# ──────────────────────────────────────────────────────────────────────────
def _classify_with_llm(
    payer: dict[str, str], evidence: list[Evidence]
) -> tuple[dict[str, list[Evidence]], str]:
    """Return (product_name -> list[Evidence], key_evidence_summary)."""
    payer_name: str = payer["payer_name"]
    aliases_raw: str = payer.get("search_aliases") or ""
    payer_aliases: set[str] = {payer_name} | {
        a.strip() for a in aliases_raw.split("|") if a.strip()
    }
    payer_aliases_lower: set[str] = {a.lower() for a in payer_aliases}
    excludes_lower: set[str] = build_excludes_set(payer)
    if not evidence:
        return {}, ""
    # Drop URL/body-gated items before the LLM sees them. Keeps prompt focused
    # and prevents the LLM from being tempted to map rejected content.
    filtered_evidence: list[Evidence] = [
        e for e in evidence
        if not _should_drop_evidence(e, payer_aliases_lower, excludes_lower)
    ]
    dropped = len(evidence) - len(filtered_evidence)
    if dropped:
        log.info("Pre-classifier gate dropped %d evidence item(s) for %s", dropped, payer_name)
    if not filtered_evidence:
        return {}, ""
    products_list = "\n".join(f"- {p.value}" for p in SalesforceProduct)
    evidence_blob = json.dumps(
        [
            {
                "i": i,
                "source_type": e.source_type,
                "url": e.url,
                "text": (e.full_text or e.snippet)[:3000],
                "date": e.date,
                "fingerprint_product": e.matched_product.value if e.matched_product else None,
                "regex_products": sorted(
                    _extract_products_from_body(e, payer_aliases, excludes_lower)
                ),
            }
            for i, e in enumerate(filtered_evidence)
        ],
        ensure_ascii=False,
    )
    description = f"""
You are mapping evidence about the US health plan **{payer_name}** to specific Salesforce products,
and writing a short narrative summary suitable for a business-development analyst.

ALLOWED PRODUCTS (use these exact strings as JSON keys):
{products_list}

Rules:
- Only assign an evidence item to a product if the text (or its fingerprint_product) explicitly names that product
  or uses a clearly equivalent term.
- 'Service Cloud' requires the source to explicitly name "Service Cloud", "contact center",
  "case management", or "omni-channel service". A generic Salesforce case study that describes
  member journeys, marketing, or data capabilities maps to Marketing Cloud or Data Cloud —
  NEVER Service Cloud.
- Whitepapers/brochures from Accenture, Deloitte, IBM, Cognizant, or Slalom are only valid evidence
  when the payer is literally named in the body. If the payer name is absent, skip the item.
- A named employee with a product-specific job title in their LinkedIn profile is Tier-1 evidence.
  Titles like "Salesforce Marketing Cloud Specialist", "Health Cloud Administrator",
  "Agentforce Developer", or "Vlocity Manager" at the target payer count as direct deployment
  signals for the named product. CIO/VP-level executive interviews that name a Salesforce product
  are also Tier-1.
- Map specific technical terms / legacy product names to their parent clouds:
    "Pardot"                                          ⇒ 'Marketing Cloud Account Engagement (Pardot)'
    "SFMC", "ExactTarget", "Email Studio", "et.com", "Marketing Platform"   ⇒ 'Marketing Cloud'
    "Community Cloud", "Digital Experience", "my.site.com", "force.com/s/" ⇒ 'Experience Cloud'
    "Service Console", "Field Service Lightning", "FSL", "Omni-Channel" ⇒ 'Service Cloud'
    "CRM Analytics", "Tableau CRM", "Einstein Analytics" ⇒ 'Data Cloud'
      (use 'Sales Cloud' ONLY when the text explicitly mentions sales pipeline, opportunities, or leads;
       for healthcare/payer contexts — member analytics, claims data, population health — always map to 'Data Cloud')
    "CPQ", "SteelBrick", "Revenue Cloud"               ⇒ 'Revenue Cloud (CPQ)'
    "Vlocity Health", "Vlocity Insurance", "OmniStudio", "Salesforce Industries",
      "Health Cloud Industry Edition", "Health Cloud"   ⇒ 'Health Cloud'
    "Agentforce", "Einstein Copilot for Health"        ⇒ 'Agentforce for Healthcare'
    "CareIQ", "Care IQ"                                ⇒ 'Sales Cloud' (Cigna's CareIQ platform per published case study)
    "Care Connect"                                     ⇒ 'Health Cloud' (Blue Shield of California's Care Connect per Sep 2023 press release)
    "prior authorization", "prior auth"                ⇒ 'Health Cloud' (in payer context, prior-auth automation is built on Health Cloud)
- A generic 'Salesforce' mention with no product hint does NOT map to anything — skip it.
- One evidence item MAY map to multiple products if it clearly names multiple.
- The `key_evidence_summary` is a 2-3 sentence plain-English narrative for a BD analyst:
  what Salesforce products the payer appears to use, what the strongest evidence is (cite source
  type and recency, e.g. "a January 2025 Health Cloud admin job posting"), and any caveats.
  If there is no credible evidence, say so plainly. Do NOT invent details that are not in the evidence.
- REGEX PRE-EXTRACTION: Each evidence item includes a `regex_products` list. These products
  were identified deterministically by Python regex in the full fetched page body — the product
  name is literally written on the page. You MUST include every product in `regex_products` in
  your `mappings` output for that evidence item. You may add additional products if the text
  clearly supports them, but you may NOT omit any product that appears in `regex_products`.
- CONSISTENCY RULE: If you mention a Salesforce product by name in the `key_evidence_summary`, you MUST
  include it in the `mappings` dict with at least one supporting evidence index. The narrative and the
  mappings must agree. Do not write about a product in the summary that has no entry in `mappings`,
  and do not omit from `mappings` any product you reference in the summary.
- Output STRICT JSON only — no prose outside the JSON, no markdown fences. Schema:
  {{"mappings": {{"<Product Name>": [<evidence index>, ...], ...}},
    "key_evidence_summary": "<2-3 sentence narrative>"}}
- Omit products with zero supporting evidence.

EVIDENCE (JSON array):
{evidence_blob}
""".strip()

    task = Task(
        description=description,
        expected_output=(
            'Strict JSON: {"mappings": {"<Product>": [<idx>, ...]}, '
            '"key_evidence_summary": "<2-3 sentences>"}'
        ),
        agent=classifier_agent(),
    )
    crew = Crew(
        agents=[task.agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    result = crew.kickoff()
    text = str(result).strip()
    # Strip accidental code fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Classifier returned non-JSON; raw=%r", text[:300])
        return {}, ""
    mappings: dict[str, list[int]] = data.get("mappings", {})
    summary: str = (data.get("key_evidence_summary") or "").strip()
    valid_products = {p.value for p in SalesforceProduct}
    out: dict[str, list[Evidence]] = {}
    for product, idxs in mappings.items():
        if product not in valid_products:
            continue
        out[product] = [filtered_evidence[i] for i in idxs if 0 <= i < len(filtered_evidence)]

    # ── Post-processing safety net (Layer 1 enforcement) ────────────────────
    # If the LLM missed a product that regex found explicitly in the page
    # body, add it here in Python. Deterministic, cannot be overridden by
    # LLM instruction-following failures.
    for i, ev in enumerate(filtered_evidence):
        regex_hits = _extract_products_from_body(ev, payer_aliases, excludes_lower)
        for product in regex_hits:
            if product not in valid_products:
                continue
            if product not in out:
                out[product] = [ev]
                log.info(
                    "Post-processing: added %s via regex from evidence[%d] url=%s",
                    product, i, ev.url,
                )
            else:
                existing_ids = {id(e) for e in out[product]}
                if id(ev) not in existing_ids:
                    out[product].append(ev)

    # ── Narrative override ──────────────────────────────────────────────────
    # If the LLM's own summary explicitly says there is no real evidence,
    # clear any products the LLM or post-processing added. The narrative is
    # the authoritative signal for these edge cases; mismatches show up as
    # Yes verdicts paired with "no credible evidence" summaries.
    summary_lower = summary.lower()
    if any(phrase in summary_lower for phrase in _NO_EVIDENCE_PHRASES):
        if out:
            log.info(
                "Narrative override: clearing %d product(s) for %s — summary indicates no evidence",
                len(out), payer_name,
            )
            out.clear()
            summary = (
                summary
                + " [All verdicts cleared by narrative override — no credible evidence detected.]"
            )
    return out, summary


# ─────────────────────────────────────────────────────────────────────────────
# Agents 9 + 10 — Recency & QC (deterministic per §5)
# ─────────────────────────────────────────────────────────────────────────────
def assemble_record(
    payer: dict[str, str], product_evidence: dict[str, list[Evidence]], all_evidence: list[Evidence]
) -> PayerRecord:
    rec = PayerRecord(
        payer_name=payer["payer_name"],
        payer_type=payer.get("payer_type", ""),
        domain=payer.get("domain", ""),
    )

    confidences: list[ConfidenceScore] = []
    for product in SalesforceProduct:
        evs = product_evidence.get(product.value, [])
        result = qc_score(product, evs)
        rec.verdicts[product.value] = result.verdict.value
        if result.verdict != UsageVerdict.UNKNOWN:
            confidences.append(result.confidence)

    # Source URLs: prioritize evidence that drove Yes/Likely verdicts, then
    # append any other evidence so payers with only Unknown verdicts still
    # surface job-posting / news / community URLs for BD verification.
    urls: list[str] = []
    for product, evs in product_evidence.items():
        if rec.verdicts.get(product) in {"Yes", "Likely"}:
            urls.extend(e.url for e in evs if e.url)
    for e in all_evidence:
        if e.url and e.url not in urls:
            urls.append(e.url)
    rec.source_urls = list(dict.fromkeys(urls))[:5]

    # Most recent evidence date
    rec.date_identified = _most_recent_date(all_evidence) or ""

    # Overall confidence = max(High > Medium > Low) across positive verdicts
    order = {ConfidenceScore.HIGH: 3, ConfidenceScore.MEDIUM: 2, ConfidenceScore.LOW: 1}
    if confidences:
        rec.confidence = max(confidences, key=lambda c: order[c])
    else:
        rec.confidence = ConfidenceScore.LOW

    # Low-confidence payers: drop generic marketing/tutorial pages and the
    # payer's own-domain pages so BD doesn't mistake them for evidence.
    if rec.confidence == ConfidenceScore.LOW:
        payer_domain = (payer.get("domain") or "").lower().strip()
        filtered: list[str] = []
        for u in rec.source_urls:
            ul = u.lower()
            if any(p in ul for p in _NON_EVIDENCE_URL_PATTERNS):
                continue
            if payer_domain and payer_domain in ul:
                continue
            filtered.append(u)
        rec.source_urls = filtered or ["No Salesforce-specific evidence found"]

    if rec.confidence == ConfidenceScore.HIGH:
        rec.bd_notes = "Confirmed deployment \u2014 reference in BD outreach."
    elif rec.confidence == ConfidenceScore.MEDIUM:
        rec.bd_notes = "Likely deployment \u2014 validate via direct outreach before referencing."
    else:
        rec.bd_notes = "No confirmed deployment \u2014 potential greenfield opportunity."

    return rec


_DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%Y-%m"]


def _parse(d: str) -> datetime | None:
    if not d:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _most_recent_date(evs: Iterable[Evidence]) -> str:
    dts = [(_parse(e.date or ""), e.date or "") for e in evs]
    dts = [(d, s) for d, s in dts if d is not None]
    if not dts:
        return ""
    return max(dts, key=lambda t: t[0])[1]


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry (Agents 1 + 11)
# ─────────────────────────────────────────────────────────────────────────────
def run(seed_path: Path, out_dir: Path) -> Path:
    payers = load_seed(seed_path)
    client = SearchApiClient()

    records: list[PayerRecord] = []
    for p in payers:
        log.info("Processing payer: %s", p["payer_name"])
        evidence = gather_evidence(p, client)
        if evidence:
            product_map, key_evidence_summary = _classify_with_llm(p, evidence)
        else:
            product_map, key_evidence_summary = {}, ""
        rec = assemble_record(p, product_map, evidence)
        rec.key_evidence = key_evidence_summary
        records.append(rec)

    return write_excel(records, out_dir)


__all__ = [
    "EXCEL_COLUMNS",
    "PRODUCT_COLUMNS",
    "assemble_record",
    "gather_evidence",
    "load_seed",
    "run",
]
