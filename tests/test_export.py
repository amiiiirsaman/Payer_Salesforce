from pathlib import Path

from openpyxl import load_workbook

from payer_intel.export import write_excel
from payer_intel.schema import EXCEL_COLUMNS, ConfidenceScore, PayerRecord


def test_excel_schema_and_freeze(tmp_path: Path):
    recs = [
        PayerRecord(
            payer_name="Humana Inc.",
            payer_type="National",
            domain="humana.com",
            verdicts={"Sales Cloud": "Yes", "Service Cloud": "Yes", "Health Cloud": "Likely"},
            source_urls=["https://a", "https://b"],
            date_identified="2025-10-01",
            confidence=ConfidenceScore.HIGH,
            bd_notes="recent case study + tech fingerprint",
        ),
        PayerRecord(payer_name="Centene Corporation", payer_type="National"),
    ]
    out = write_excel(recs, tmp_path)
    assert out.exists()
    assert out.name.startswith("Aarete_BD_Salesforce_Payer_Intelligence_")
    assert out.suffix == ".xlsx"

    wb = load_workbook(out)
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == EXCEL_COLUMNS, f"Header mismatch: {header}"
    assert ws.freeze_panes == "A2"

    # Row 2 — Humana — Sales Cloud cell == "Yes"
    sales_idx = EXCEL_COLUMNS.index("Sales Cloud") + 1
    assert ws.cell(row=2, column=sales_idx).value == "Yes"
    # Missing products default to "Unknown"
    data_cloud_idx = EXCEL_COLUMNS.index("Data Cloud") + 1
    assert ws.cell(row=2, column=data_cloud_idx).value == "Unknown"
    # Confidence in last 4 cols
    conf_idx = EXCEL_COLUMNS.index("Confidence Score") + 1
    assert ws.cell(row=2, column=conf_idx).value == "High"
    # Source URLs pipe-joined
    src_idx = EXCEL_COLUMNS.index("Source URLs") + 1
    assert "|" in ws.cell(row=2, column=src_idx).value
