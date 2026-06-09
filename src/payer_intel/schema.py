from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SalesforceProduct(str, Enum):
    SALES_CLOUD = "Sales Cloud"
    SERVICE_CLOUD = "Service Cloud"
    EXPERIENCE_CLOUD = "Experience Cloud"
    MARKETING_CLOUD = "Marketing Cloud"
    PARDOT = "Marketing Cloud Account Engagement (Pardot)"
    HEALTH_CLOUD = "Health Cloud"
    AGENTFORCE_HEALTHCARE = "Agentforce for Healthcare"
    LIFE_SCIENCES_CLOUD = "Life Sciences Cloud"
    FINANCIAL_SERVICES_CLOUD = "Financial Services Cloud"
    REVENUE_CLOUD = "Revenue Cloud (CPQ)"
    DATA_CLOUD = "Data Cloud"


PRODUCT_COLUMNS: list[str] = [p.value for p in SalesforceProduct]


class ConfidenceScore(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    REVIEW = "Requires Review"


class UsageVerdict(str, Enum):
    YES = "Yes"
    LIKELY = "Likely"
    NO = "No"
    UNKNOWN = "Unknown"


class Evidence(BaseModel):
    source_type: str  # job_posting | news | review | case_study | technographic
    url: str
    snippet: str = ""
    date: Optional[str] = None  # ISO-ish or human; recency agent normalizes
    matched_product: Optional[SalesforceProduct] = None


class SalesforceSignal(BaseModel):
    payer_name: str
    product: SalesforceProduct
    verdict: UsageVerdict = UsageVerdict.UNKNOWN
    evidence: List[Evidence] = Field(default_factory=list)


class PayerRecord(BaseModel):
    payer_name: str
    payer_type: str = ""
    domain: str = ""
    verdicts: dict[str, str] = Field(default_factory=dict)  # product -> Yes/Likely/No/Unknown
    source_urls: List[str] = Field(default_factory=list)
    date_identified: str = ""
    confidence: ConfidenceScore = ConfidenceScore.LOW
    bd_notes: str = ""
    key_evidence: str = ""


EXCEL_COLUMNS: list[str] = [
    "Payer Name",
    "Payer Type",
    *PRODUCT_COLUMNS,
    "Source URLs",
    "Date Identified",
    "Confidence Score",
    "BD Notes",
    "Key Evidence",
]
