"""
Adversarial tests for the coordinate engine.

The two real samples share one font size, one generator, and one wrap depth
pattern. That is not enough evidence that the read is robust. This module
synthesises PDFs that vary the things most likely to break a coordinate
parser, and asserts the engine still reconstructs every cell exactly.

Run: python tests/test_adversarial.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from src.layout import parse_table

COLS = ["Doc", "Reference", "Date", "Amount"]
X = {"Doc": 40, "Reference": 110, "Date": 250, "Amount": 400}

ANCHOR = r"^[A-Z]{2}\s+\d+$"


def make_pdf(rows, *, font_size=8.0, leading=None, shadow=False,
             rotate=0, path=None) -> str:
    """
    rows: list of dicts with keys Doc/Reference/Date/Amount, where Reference
    may be a list of fragments to be laid out on successive lines (a wrap).
    """
    leading = leading or font_size * 1.9
    path = path or tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(path, pagesize=A4)
    if rotate:
        c.setPageRotation(rotate)
    _, H = A4

    y = H - 100
    c.setFont("Helvetica-Bold", font_size)
    for name in COLS:
        c.drawString(X[name], y, name)
    y -= leading

    c.setFont("Helvetica", font_size)
    for row in rows:
        frags = row["Reference"]
        frags = frags if isinstance(frags, list) else [frags]
        # First visual line carries every column.
        for name in COLS:
            val = frags[0] if name == "Reference" else row[name]
            c.drawString(X[name], y, val)
            if shadow:                      # fake-bold double draw
                c.drawString(X[name] + 0.05, y, val)
        # Wrapped fragments continue in the Reference band only.
        for frag in frags[1:]:
            y -= font_size * 1.2
            c.drawString(X["Reference"], y, frag)
        y -= leading
    c.save()
    return path


def run(pdf_path):
    return parse_table(pdf_path, COLS, anchor_column="Doc",
                       anchor_pattern=ANCHOR, join_with="")


def check(name, pdf, expected_refs, expect_rows=None):
    r = run(pdf)
    got = [row.cells["Reference"] for row in r.rows]
    coverage_ok = r.words_in_table_region == r.words_claimed
    rows_ok = (expect_rows is None) or (len(r.rows) == expect_rows)
    ok = got == expected_refs and coverage_ok and rows_ok and not r.orphan_lines

    geom = r.geometry[0]
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"      y_tol={geom['y_tolerance']} "
          f"coverage={r.words_claimed}/{r.words_in_table_region} "
          f"dupes_removed={geom['duplicate_glyphs_removed']} "
          f"orphans={len(r.orphan_lines)}")
    if not ok:
        print(f"      want {expected_refs}")
        print(f"      got  {got}")
    return ok


def main() -> int:
    fails = []

    # 1. Mixed wrap depth in ONE document: 0, 1, 2 and 4 continuation lines.
    #    This is the case the old absolute row_pitch guard silently truncated.
    rows = [
        {"Doc": "PU 1001", "Reference": "REF-A", "Date": "01.01.25", "Amount": "-10.00"},
        {"Doc": "PU 1002", "Reference": ["SI/0878/CN/", "00007"], "Date": "02.01.25", "Amount": "-20.00"},
        {"Doc": "PU 1003", "Reference": ["AAA", "BBB", "CCC"], "Date": "03.01.25", "Amount": "-30.00"},
        {"Doc": "PU 1004", "Reference": ["W", "X", "Y", "Z", "Q"], "Date": "04.01.25", "Amount": "-40.00"},
    ]
    fails.append(not check("mixed wrap depth (0,1,2,4)", make_pdf(rows),
                           ["REF-A", "SI/0878/CN/00007", "AAABBBCCC", "WXYZQ"], 4))

    # 2. Same content at 5pt and at 16pt. A hardcoded y-tolerance tuned for
    #    ~7pt type merges lines at 5pt and splits rows at 16pt.
    for size in (5.0, 16.0):
        fails.append(not check(f"font size {size}pt",
                               make_pdf(rows, font_size=size),
                               ["REF-A", "SI/0878/CN/00007", "AAABBBCCC", "WXYZQ"], 4))

    # 3. Tight leading: continuation lines very close to their parent.
    fails.append(not check("tight leading (1.35x)",
                           make_pdf(rows, font_size=8, leading=8 * 1.35),
                           ["REF-A", "SI/0878/CN/00007", "AAABBBCCC", "WXYZQ"], 4))

    # 4. Shadow text (fake bold double-draw) must not duplicate cell content.
    fails.append(not check("shadow / double-drawn text",
                           make_pdf(rows, shadow=True),
                           ["REF-A", "SI/0878/CN/00007", "AAABBBCCC", "WXYZQ"], 4))

    # 5. A wrapped fragment that straddles the band boundary.
    wide = [
        {"Doc": "PU 2001",
         "Reference": ["VERYLONGREFERENCEVALUE", "CONTINUED"],
         "Date": "05.01.25", "Amount": "-50.00"},
    ]
    fails.append(not check("long value straddling band edge", make_pdf(wide),
                           ["VERYLONGREFERENCEVALUECONTINUED"], 1))

    # 6. Coverage must FAIL loudly when a line cannot be attributed.
    #    A stray line before the first anchor becomes an orphan, not silence.
    stray = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(stray, pagesize=A4)
    _, H = A4
    c.setFont("Helvetica-Bold", 8)
    for n in COLS:
        c.drawString(X[n], H - 100, n)
    c.setFont("Helvetica", 8)
    c.drawString(X["Reference"], H - 115, "ORPHANED-FRAGMENT")
    c.drawString(X["Doc"], H - 130, "PU 3001")
    c.drawString(X["Reference"], H - 130, "REF-Z")
    c.save()
    r = run(stray)
    detected = len(r.orphan_lines) == 1 and r.orphan_lines[0]["reason"] == "before_first_row"
    print(f"{'PASS' if detected else 'FAIL'}  orphan line is detected, not swallowed")
    print(f"      orphans={[o['text'] for o in r.orphan_lines]}")
    fails.append(not detected)

    n = sum(fails)
    print("\nAll green." if not n else f"\n{n} failure(s).")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
