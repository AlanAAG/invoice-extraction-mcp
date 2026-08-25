"""
Multi-page edge cases.

A real statement run is not "the same page N times". Headers may or may not
repeat, a row can be cut by the page break, page size and orientation can
change mid-document, and unrelated pages (terms, remittance advice) get
stapled on. Each behaves differently and each is tested here.

Run: python tests/test_multipage.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.pdfgen import canvas

from src.pipeline import extract, fetch
from src.profile import Registry

COLS = ["Document", "BP Ref. No.", "Post. Date", "Due Date",
        "Details", "Amount", "Balance"]
X = {"Document": 40, "BP Ref. No.": 110, "Post. Date": 210, "Due Date": 275,
     "Details": 340, "Amount": 450, "Balance": 520}
FS = 7.0

REG = Registry()
RESULTS: list[str] = []


def _preamble(c, y, H, first_page: bool):
    c.setFont("Helvetica-Bold", FS + 2)
    c.drawString(40, y, "Supplier Statement")
    y -= 16
    c.setFont("Helvetica", FS)
    if first_page:
        c.drawString(40, y, "Ageing Date: 11.07.25")
        c.drawString(300, y, "Currency: AED")
        y -= 11
        c.drawString(40, y, "BP: From S00066 To S00066")
        y -= 11
        c.drawString(40, y, "S00066 ONE WORLD TRADING L.L.C.")
        y -= 11
    return y - 14


def _header(c, y, xs=None):
    xs = xs or X
    c.setFont("Helvetica-Bold", FS)
    for col in COLS:
        c.drawString(xs[col], y, col)
    c.setFont("Helvetica", FS)
    return y - 14


def _row(c, y, doc, ref_frags, amount, running, xs=None):
    xs = xs or X
    vals = {"Document": doc, "BP Ref. No.": ref_frags[0],
            "Post. Date": "31.05.25", "Due Date": "07.07.25",
            "Details": "A/P Invoices", "Amount": f"{amount:.3f}",
            "Balance": f"{running:,.3f}"}
    for col in COLS:
        c.drawString((xs or X)[col], y, vals[col])
    for frag in ref_frags[1:]:
        y -= FS * 1.25
        c.drawString((xs or X)["BP Ref. No."], y, frag)
    return y - FS * 2.2


def _ageing(c, y, running):
    c.drawString(40, y - 6, "Total")
    c.drawString(X["Amount"], y - 6, f"{running:,.3f}")
    y -= 30
    c.setFont("Helvetica-Bold", FS)
    for label, x in [("Balance Due", 175), ("Future Remit", 250),
                     ("0 - 30", 320), ("31 - 60", 385), ("61 - 90", 445),
                     ("91 - 120", 505), ("121+", 560)]:
        c.drawRightString(x, y, label)
    y -= 12
    c.setFont("Helvetica", FS)
    c.drawString(40, y, "Total")
    c.drawRightString(175, y, f"{running:,.3f}")
    c.drawRightString(320, y, f"{running:,.3f}")


# ---------------------------------------------------------------------------
# Generators, one per scenario
# ---------------------------------------------------------------------------

def pdf_header_every_page(n=12, per_page=5) -> str:
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4)
    _, H = A4
    run = 0.0
    pages = [list(range(i, min(i + per_page, n))) for i in range(0, n, per_page)]
    for pi, idx in enumerate(pages):
        y = _header(c, _preamble(c, H - 60, H, pi == 0))
        for i in idx:
            run += -100.0
            y = _row(c, y, f"PU 20{i:04d}", [f"SI/{7000+i}"], -100.0, run)
        if pi == len(pages) - 1:
            _ageing(c, y, run)
        c.showPage()
    c.save()
    return p


def pdf_header_only_first_page(n=12, per_page=5) -> str:
    """The common real-world variant: header printed once."""
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4)
    _, H = A4
    run = 0.0
    pages = [list(range(i, min(i + per_page, n))) for i in range(0, n, per_page)]
    for pi, idx in enumerate(pages):
        if pi == 0:
            y = _header(c, _preamble(c, H - 60, H, True))
        else:
            y = H - 60                      # straight into rows, no header
        for i in idx:
            run += -100.0
            y = _row(c, y, f"PU 20{i:04d}", [f"SI/{7000+i}"], -100.0, run)
        if pi == len(pages) - 1:
            _ageing(c, y, run)
        c.showPage()
    c.save()
    return p


def pdf_row_split_by_page_break() -> str:
    """
    A row's first visual line sits at the bottom of page 1; its wrapped
    reference fragment continues at the top of page 2.
    """
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4)
    _, H = A4
    run = 0.0

    y = _header(c, _preamble(c, H - 60, H, True))
    for i in range(2):
        run += -100.0
        y = _row(c, y, f"PU 30{i:04d}", [f"SI/{5000+i}"], -100.0, run)
    # Straddling row: first line on page 1, fragment on page 2.
    run += -250.0
    c.drawString(X["Document"], 70, "PU 309999")
    c.drawString(X["BP Ref. No."], 70, "SI/08781/CN/")
    c.drawString(X["Post. Date"], 70, "31.05.25")
    c.drawString(X["Due Date"], 70, "07.07.25")
    c.drawString(X["Details"], 70, "A/P Invoices")
    c.drawString(X["Amount"], 70, "-250.000")
    c.drawString(X["Balance"], 70, f"{run:,.3f}")
    c.showPage()

    y = _header(c, _preamble(c, H - 60, H, False))
    c.drawString(X["BP Ref. No."], y, "00007")       # the wrapped tail
    y -= FS * 2.2
    run += -100.0
    y = _row(c, y, "PU 310000", ["SI/6000"], -100.0, run)
    _ageing(c, y, run)
    c.showPage()
    c.save()
    return p


def pdf_mixed_page_geometry() -> str:
    """A4 portrait, then Letter, then A4 landscape -- header repeated."""
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4)
    run = 0.0
    for pi, size in enumerate([A4, LETTER, landscape(A4)]):
        c.setPageSize(size)
        H = size[1]
        y = _header(c, _preamble(c, H - 60, H, pi == 0))
        for i in range(3):
            run += -100.0
            y = _row(c, y, f"PU 40{pi}{i:03d}", [f"SI/{4000+pi*10+i}"], -100.0, run)
        if pi == 2:
            _ageing(c, y, run)
        c.showPage()
    c.save()
    return p


def pdf_unrelated_page_in_middle() -> str:
    """Terms & conditions stapled between two table pages."""
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4)
    _, H = A4
    run = 0.0

    y = _header(c, _preamble(c, H - 60, H, True))
    for i in range(3):
        run += -100.0
        y = _row(c, y, f"PU 50{i:04d}", [f"SI/{3000+i}"], -100.0, run)
    c.showPage()

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, H - 80, "Terms and Conditions")
    c.setFont("Helvetica", 9)
    for i, line in enumerate([
            "Payment is due within the agreed credit period.",
            "Interest may be charged on overdue balances.",
            "All disputes are subject to Dubai jurisdiction.",
            "This statement does not constitute a demand for payment."]):
        c.drawString(40, H - 110 - i * 14, line)
    c.showPage()

    y = _header(c, _preamble(c, H - 60, H, False))
    for i in range(3, 5):
        run += -100.0
        y = _row(c, y, f"PU 50{i:04d}", [f"SI/{3000+i}"], -100.0, run)
    _ageing(c, y, run)
    c.showPage()
    c.save()
    return p


# ---------------------------------------------------------------------------

async def case(name, pdf, *, expect_rows, expect_status="ok", expect_ref=None):
    o = extract(await fetch(pdf, "path"), REG, file_name="mp.pdf")
    rows = len(o.get("line_items", []))
    refs = [r.get("bp_reference_no") for r in o.get("line_items", [])]
    ok = rows == expect_rows and o["status"] == expect_status
    if ok and expect_ref:
        ok = expect_ref in refs
    print(f"{'PASS' if ok else 'FAIL'}  {name:42s} rows={rows} status={o['status']}")
    if not ok:
        print(f"        wanted rows={expect_rows} status={expect_status}"
              + (f" containing {expect_ref!r}" if expect_ref else ""))
        bad = [c["check"] for c in o.get("validation", {}).get("checks", [])
               if not c["passed"]]
        print(f"        failing checks: {bad}")
        print(f"        warnings: {o.get('diagnostics', {}).get('warnings')}")
        print(f"        refs: {refs}")
    RESULTS.append("PASS" if ok else "FAIL")
    return o


async def main() -> int:
    print("=" * 82)
    await case("header repeated on every page", pdf_header_every_page(),
               expect_rows=12)
    await case("header printed ONLY on page 1", pdf_header_only_first_page(),
               expect_rows=12)
    await case("row split by the page break", pdf_row_split_by_page_break(),
               expect_rows=4, expect_ref="SI/08781/CN/00007")
    await case("page size/orientation changes mid-doc", pdf_mixed_page_geometry(),
               expect_rows=9)
    await case("unrelated page stapled in the middle",
               pdf_unrelated_page_in_middle(), expect_rows=5)
    print("=" * 82)
    n = RESULTS.count("FAIL")
    print("All green." if not n else f"{n} failure(s).")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
