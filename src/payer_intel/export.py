from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .schema import EXCEL_COLUMNS, PRODUCT_COLUMNS, PayerRecord


def _record_to_row(rec: PayerRecord) -> dict[str, str]:
    row = {
        "Payer Name": rec.payer_name,
        "Payer Type": rec.payer_type,
        "Source URLs": " | ".join(dict.fromkeys(rec.source_urls)),
        "Date Identified": rec.date_identified,
        "Confidence Score": rec.confidence.value,
        "BD Notes": rec.bd_notes,
    }
    for col in PRODUCT_COLUMNS:
        row[col] = rec.verdicts.get(col, "Unknown")
    return row


def write_excel(records: Iterable[PayerRecord], out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d")
    path = out_dir / f"Aarete_BD_Salesforce_Payer_Intelligence_{stamp}.xlsx"

    rows = [_record_to_row(r) for r in records]
    df = pd.DataFrame(rows, columns=EXCEL_COLUMNS)

    wb = Workbook()
    ws = wb.active
    ws.title = "Payer Intelligence"
    ws.append(EXCEL_COLUMNS)
    for r in df.itertuples(index=False):
        ws.append(list(r))

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for col_idx, _ in enumerate(EXCEL_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill

    ws.freeze_panes = "A2"

    widths = {
        "Payer Name": 28, "Payer Type": 14, "Source URLs": 60,
        "Date Identified": 16, "Confidence Score": 18, "BD Notes": 30,
    }
    for col_idx, name in enumerate(EXCEL_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(name, 22)

    # Conditional color scale on Confidence Score
    if rows:
        conf_idx = EXCEL_COLUMNS.index("Confidence Score") + 1
        col_letter = get_column_letter(conf_idx)
        rng = f"{col_letter}2:{col_letter}{len(rows) + 1}"
        ws.conditional_formatting.add(
            rng, CellIsRule(operator="equal", formula=['"High"'], fill=PatternFill("solid", fgColor="C6EFCE"))
        )
        ws.conditional_formatting.add(
            rng, CellIsRule(operator="equal", formula=['"Medium"'], fill=PatternFill("solid", fgColor="FFEB9C"))
        )
        ws.conditional_formatting.add(
            rng, CellIsRule(operator="equal", formula=['"Low"'], fill=PatternFill("solid", fgColor="FFC7CE"))
        )

    wb.save(path)
    return path
