from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from crewai import Crew, Process, Task

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
_JOB_PRODUCT_TERMS = (
    'Salesforce OR "Sales Cloud" OR "Service Cloud" OR "Health Cloud" '
    'OR "Marketing Cloud" OR "Experience Cloud" OR "Data Cloud" '
    'OR Pardot OR ExactTarget OR "CRM Analytics" OR Agentforce'
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
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Agent 8 — Classifier (Bedrock-backed CrewAI task)
# ─────────────────────────────────────────────────────────────────────────────
def _classify_with_llm(
    payer_name: str, evidence: list[Evidence]
) -> tuple[dict[str, list[Evidence]], str]:
    """Return (product_name -> list[Evidence], key_evidence_summary)."""
    if not evidence:
        return {}, ""
    products_list = "\n".join(f"- {p.value}" for p in SalesforceProduct)
    evidence_blob = json.dumps(
        [
            {
                "i": i,
                "source_type": e.source_type,
                "url": e.url,
                "snippet": e.snippet[:1200],
                "date": e.date,
                "fingerprint_product": e.matched_product.value if e.matched_product else None,
            }
            for i, e in enumerate(evidence)
        ],
        ensure_ascii=False,
    )
    description = f"""
You are mapping evidence about the US health plan **{payer_name}** to specific Salesforce products,
and writing a short narrative summary suitable for a business-development analyst.

ALLOWED PRODUCTS (use these exact strings as JSON keys):
{products_list}

Rules:
- Only assign an evidence item to a product if the snippet (or its fingerprint_product) explicitly names that product
  or uses a clearly equivalent term.
- Map specific technical terms / legacy product names to their parent clouds:
    "Pardot"                                          ⇒ 'Marketing Cloud Account Engagement (Pardot)'
    "SFMC", "ExactTarget", "Email Studio", "et.com"   ⇒ 'Marketing Cloud'
    "Community Cloud", "Digital Experience", "my.site.com", "force.com/s/" ⇒ 'Experience Cloud'
    "Service Console", "Field Service Lightning", "FSL", "Omni-Channel" ⇒ 'Service Cloud'
    "CRM Analytics", "Tableau CRM", "Einstein Analytics" ⇒ 'Data Cloud' (or 'Sales Cloud' if context is sales pipeline)
    "CPQ", "SteelBrick", "Revenue Cloud"               ⇒ 'Revenue Cloud (CPQ)'
    "Vlocity Health", "Vlocity Insurance", "Health Cloud" ⇒ 'Health Cloud'
    "Agentforce", "Einstein Copilot for Health"        ⇒ 'Agentforce for Healthcare'
- A generic 'Salesforce' mention with no product hint does NOT map to anything — skip it.
- One evidence item MAY map to multiple products if it clearly names multiple.
- The `key_evidence_summary` is a 2-3 sentence plain-English narrative for a BD analyst:
  what Salesforce products the payer appears to use, what the strongest evidence is (cite source
  type and recency, e.g. "a January 2025 Health Cloud admin job posting"), and any caveats.
  If there is no credible evidence, say so plainly. Do NOT invent details that are not in the evidence.
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
        out[product] = [evidence[i] for i in idxs if 0 <= i < len(evidence)]
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

    # Source URLs = union from positive-verdict evidence
    urls: list[str] = []
    for product, evs in product_evidence.items():
        if rec.verdicts.get(product) in {"Yes", "Likely"}:
            urls.extend(e.url for e in evs if e.url)
    rec.source_urls = list(dict.fromkeys(urls))

    # Most recent evidence date
    rec.date_identified = _most_recent_date(all_evidence) or ""

    # Overall confidence = max(High > Medium > Low) across positive verdicts
    order = {ConfidenceScore.HIGH: 3, ConfidenceScore.MEDIUM: 2, ConfidenceScore.LOW: 1}
    if confidences:
        rec.confidence = max(confidences, key=lambda c: order[c])
    else:
        rec.confidence = ConfidenceScore.LOW

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
            product_map, key_evidence_summary = _classify_with_llm(p["payer_name"], evidence)
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
