"""
Robustness matrix.

Question this answers: given a document in "the SAP supplier statement
format", how much can vary before the profile stops working?

Each case mutates ONE axis of the real format and reports whether the read
survives. The point is to draw the line precisely -- what is free, what needs
a profile edit, and what fails LOUDLY versus silently.

Run: python tests/test_robustness.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from src.pipeline import extract, fetch
from src.profile import Registry

DEFAULT_COLS = ["Document", "BP Ref. No.", "Post. Date", "Due Date",
                "Details", "Amount", "Balance"]
DEFAULT_X = {"Document": 40, "BP Ref. No.": 110, "Post. Date": 210,
             "Due Date": 275, "Details": 340, "Amount": 450, "Balance": 520}


def make_statement(*, supplier="ONE WORLD TRADING L.L.C.", code="S00066",
                   rows=None, columns=None, xs=None, font_size=7.0,
                   currency="AED", header_labels=None, rows_per_page=None,
                   path=None) -> str:
    """
    Synthesise a document in the SAP B1 supplier-statement format.

    rows: list of dicts keyed by column name. 'BP Ref. No.' may be a list of
    fragments to simulate the report wrapping a long reference.
    """
    columns = columns or DEFAULT_COLS
    xs = xs or DEFAULT_X
    header_labels = header_labels or {c: c for c in columns}
    rows = rows if rows is not None else _default_rows()
    path = path or tempfile.mkstemp(suffix=".pdf")[1]

    c = canvas.Canvas(path, pagesize=A4)
    _, H = A4
    per_page = rows_per_page or len(rows)
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]

    running = 0.0
    for pi, page_rows in enumerate(pages):
        y = H - 60
        c.setFont("Helvetica-Bold", font_size + 2)
        c.drawString(40, y, "Supplier Statement")
        y -= 16
        c.setFont("Helvetica", font_size)
        c.drawString(40, y, f"Ageing Date: 11.07.25")
        c.drawString(300, y, f"Currency: {currency}")
        y -= 11
        c.drawString(40, y, f"BP: From {code} To {code}")
        y -= 11
        c.drawString(40, y, f"{code} {supplier}")
        y -= 11
        c.drawString(40, y, "Prior Period Balance")
        y -= 20

        c.setFont("Helvetica-Bold", font_size)
        for col in columns:
            c.drawString(xs[col], y, header_labels[col])
        y -= 14
        c.setFont("Helvetica", font_size)

        for row in page_rows:
            frags = row["BP Ref. No."]
            frags = frags if isinstance(frags, list) else [frags]
            running += float(str(row["Amount"]).replace(",", ""))
            for col in columns:
                if col == "BP Ref. No.":
                    val = frags[0]
                elif col == "Balance":
                    val = f"{running:,.3f}"
                else:
                    val = str(row.get(col, ""))
                c.drawString(xs[col], y, val)
            for frag in frags[1:]:
                y -= font_size * 1.25
                c.drawString(xs["BP Ref. No."], y, frag)
            y -= font_size * 2.2

        if pi == len(pages) - 1:
            c.drawString(40, y - 6, "Total")
            c.drawString(xs["Amount"], y - 6, f"{running:,.3f}")
            y -= 30
            c.setFont("Helvetica-Bold", font_size)
            # Spaced so the row label cannot collide with the first bucket
            # even at large font sizes.
            for label, x in [("Balance Due", 175), ("Future Remit", 250),
                             ("0 - 30", 320), ("31 - 60", 385),
                             ("61 - 90", 445), ("91 - 120", 505), ("121+", 560)]:
                c.drawRightString(x, y, label)
            y -= 12
            c.setFont("Helvetica", font_size)
            c.drawString(40, y, "Total")
            c.drawRightString(175, y, f"{running:,.3f}")
            c.drawRightString(320, y, f"{running:,.3f}")
        c.showPage()
    c.save()
    return path


def _default_rows():
    return [
        {"Document": "PU 131365", "BP Ref. No.": ["SI/08781/CN/", "00007"],
         "Post. Date": "31.05.25", "Due Date": "07.07.25",
         "Details": "A/P Invoices", "Amount": "-43160.250"},
        {"Document": "PU 131366", "BP Ref. No.": "SI/08793",
         "Post. Date": "31.05.25", "Due Date": "11.07.25",
         "Details": "A/P Invoices", "Amount": "-6678.000"},
        {"Document": "PU 131367", "BP Ref. No.": "SI/08843",
         "Post. Date": "31.05.25", "Due Date": "21.07.25",
         "Details": "A/P Invoices", "Amount": "-3003.000"},
    ]


# ---------------------------------------------------------------------------

REG = Registry()
RESULTS: list[tuple[str, str, str]] = []


async def case(name, pdf, *, expect_status, expect_rows=None,
               expect_refs=None, note=""):
    o = extract(await fetch(pdf, "path"), REG, file_name="synth.pdf")
    status = o["status"]
    rows = len(o.get("line_items", []))
    refs = [r.get("bp_reference_no") for r in o.get("line_items", [])]

    ok = status == expect_status
    if ok and expect_rows is not None:
        ok = rows == expect_rows
    if ok and expect_refs is not None:
        ok = refs == expect_refs

    verdict = "PASS" if ok else "FAIL"
    detail = f"status={status} rows={rows}"
    if not ok:
        detail += f"  (wanted status={expect_status} rows={expect_rows})"
        if expect_refs:
            detail += f"\n           refs={refs}"
    print(f"{verdict}  {name:38s} {detail}")
    if note:
        print(f"        {note}")
    RESULTS.append((verdict, name, detail))
    return o


async def main() -> int:
    print("=" * 78)
    print("AXIS 1 — content varies, layout identical")
    print("=" * 78)

    await case("different supplier + amounts",
               make_statement(supplier="ACME GENERAL TRADING FZE", code="S00999"),
               expect_status="ok", expect_rows=3,
               expect_refs=["SI/08781/CN/00007", "SI/08793", "SI/08843"])

    many = [dict(_default_rows()[1], Document=f"PU 14{i:04d}",
                 **{"BP Ref. No.": f"SI/{9000+i}"}) for i in range(20)]
    await case("20 rows over 3 pages",
               make_statement(rows=many, rows_per_page=8),
               expect_status="ok", expect_rows=20)

    deep = [{"Document": "PU 200001",
             "BP Ref. No.": ["AAAA/", "BBBB/", "CCCC/", "DDDD"],
             "Post. Date": "01.01.25", "Due Date": "01.02.25",
             "Details": "A/P", "Amount": "-100.000"}]
    await case("reference wrapping 4 lines deep",
               make_statement(rows=deep),
               expect_status="ok", expect_rows=1,
               expect_refs=["AAAA/BBBB/CCCC/DDDD"])

    print()
    print("=" * 78)
    print("AXIS 2 — design/layout changes, same columns")
    print("=" * 78)

    shifted = dict(DEFAULT_X)
    shifted.update({"Details": 330, "Amount": 470, "Balance": 535})
    await case("columns repositioned",
               make_statement(xs=shifted),
               expect_status="ok", expect_rows=3)

    for size in (5.0, 11.0):
        await case(f"font size {size}pt",
                   make_statement(font_size=size),
                   expect_status="ok", expect_rows=3)

    await case("header PUNCTUATION drift ('BP Ref No')",
               make_statement(header_labels={**{c: c for c in DEFAULT_COLS},
                                             "BP Ref. No.": "BP Ref No",
                                             "Balance": "Balance."}),
               expect_status="ok", expect_rows=3,
               note="punctuation is normalised out of header matching")

    print()
    print("=" * 78)
    print("AXIS 3 — the format itself changes (profile edit territory)")
    print("=" * 78)

    cols_extra = DEFAULT_COLS[:5] + ["Currency"] + DEFAULT_COLS[5:]
    xs_extra = dict(DEFAULT_X, **{"Details": 330, "Currency": 415,
                                  "Amount": 460, "Balance": 525})
    rows_cur = [dict(r, Currency="AED") for r in _default_rows()]
    o = await case("NEW COLUMN inserted mid-table",
                   make_statement(columns=cols_extra, xs=xs_extra, rows=rows_cur),
                   expect_status="needs_review",
                   note="must be flagged, never silently absorbed")
    unmapped = [c for c in o.get("diagnostics", {}).get("unmapped_header_columns", [])]
    print(f"        unmapped_header_columns={unmapped}")

    cols_gone = [c for c in DEFAULT_COLS if c != "Due Date"]
    xs_gone = {k: v for k, v in DEFAULT_X.items() if k != "Due Date"}
    await case("COLUMN REMOVED from table",
               make_statement(columns=cols_gone, xs=xs_gone),
               expect_status="profile_mismatch",
               note="should name the missing column, not throw")

    await case("different document type prefix (RC)",
               make_statement(rows=[dict(r, Document=r["Document"].replace("PU", "RC"))
                                    for r in _default_rows()]),
               expect_status="ok", expect_rows=3,
               note="anchor regex is ^[A-Z]{2,3}\\s+\\d+$, so any 2-3 letter type works")

    await case("header WORD change ('Posting Date')",
               make_statement(header_labels={**{c: c for c in DEFAULT_COLS},
                                             "Post. Date": "Posting Date"}),
               expect_status="profile_mismatch",
               note="a renamed column is a real format change -> profile edit, "
                    "reported with the printed header")

    await case("anchor no longer matches (INV-2026-001)",
               make_statement(rows=[dict(r, Document="INV-2026-001")
                                    for r in _default_rows()]),
               expect_status="needs_review",
               note="profile still DETECTS the document, but zero rows parse. "
                    "Flagged for review rather than passing empty.")

    print()
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print("All green." if not fails else f"{len(fails)} failure(s).")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
