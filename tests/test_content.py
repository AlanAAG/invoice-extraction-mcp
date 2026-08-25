"""
Content extraction must work on EVERY document, profiled or not.
This is what makes steps 2, 4 and 5 of the workflow possible.
"""
import asyncio, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from src.profile import Registry
from src.pipeline import fetch, extract

BASE = pathlib.Path(__file__).resolve().parent / "fixtures"

def unprofiled_pdf():
    p = tempfile.mkstemp(suffix=".pdf")[1]
    c = canvas.Canvas(p, pagesize=A4); _, H = A4
    c.setFont("Helvetica-Bold", 14); c.drawString(50, H-60, "ACME TRADING FZE")
    c.setFont("Helvetica", 9)
    c.drawString(50, H-90,  "Invoice No: INV-2026-0042")
    c.drawString(300, H-90, "Issue Date: 12.03.26")
    c.drawString(50, H-105, "Customer: Bedashing Holding")
    c.drawString(300, H-105, "Currency: AED")
    c.drawString(50, H-140, "Payment due within 30 days.")
    c.save(); return p

async def main():
    reg = Registry(); fails = []
    def ok(cond, name, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not cond: fails.append(name)

    # --- profiled document -------------------------------------------------
    f = BASE / "SYSTEM_SIDE_-_One_world_SOA_11_07_25.pdf"
    if f.exists():
        o = extract(await fetch(str(f), "path"), reg, file_name=f.name)
        c = o["content"]
        ok(o["status"] == "ok", "profiled doc -> ok")
        ok(bool(c["markdown"]), "markdown produced", f"{len(c['markdown'])} chars")
        ok("SI/08781/CN/00007" in c["markdown"], "wrapped ref appears in markdown")
        ok("## Key fields" in c["markdown"], "typed fields lead the markdown")
        ok(c["key_values"].get("Ageing Date") == "11.07.25", "key-values correct")
        ok(c["key_values"].get("Currency") == "AED", "column bleed avoided")
        # Content and structured data must agree -- no duplicate table text.
        ok(c["markdown"].count("SI/08781/CN/00007") == 1,
           "table rendered once, not duplicated as loose text")
        ok(len(o["tables"]) >= 1, "tables[] populated", f"{len(o['tables'])} tables")
        ok(bool(c["chunks"]), "chunks produced", f"{len(c['chunks'])} chunks")

    # --- unprofiled document ----------------------------------------------
    o = extract(await fetch(unprofiled_pdf(), "path"), reg, file_name="acme.pdf")
    c = o["content"]
    ok(o["status"] == "parsed_without_profile", "unprofiled -> not a dead end",
       f"status={o['status']}")
    ok(bool(c["text"]), "unprofiled still yields text", f"{len(c['text'])} chars")
    ok(c["key_values"] == {"Invoice No": "INV-2026-0042", "Issue Date": "12.03.26",
                           "Customer": "Bedashing Holding", "Currency": "AED"},
       "unprofiled key-values exact", str(c["key_values"]))
    ok("Payment due within 30 days." in c["markdown"], "prose retained")
    ok(o["validation"]["ok"], "unprofiled validates (content check only)")

    print("\nAll green." if not fails else f"\n{len(fails)} failure(s): {fails}")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
