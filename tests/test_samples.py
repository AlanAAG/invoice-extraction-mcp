"""Regression suite. Run: python tests/test_samples.py [pdf_dir]"""
import asyncio, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.profile import Registry
from src.pipeline import fetch, extract, PipelineError

EXPECTED = {
    "SYSTEM_SIDE_-_One_world_SOA_11_07_25.pdf": {
        "supplier": "ONE WORLD TRADING L.L.C.", "rows": 5,
        "closing": -69966.750, "first_ref": "SI/08781/CN/00007", "wrapped": 1},
    "SYSTEM_SIDE_-_Nutripharm_SOA_19_04_24.pdf": {
        "supplier": "NUTRIPHARM LLC", "rows": 4,
        "closing": -14095.022, "first_ref": "N-CINV-01999111", "wrapped": 4},
}
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
BASE = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURES

async def main():
    reg = Registry()
    assert not reg.load_errors, f"profile load errors: {reg.load_errors}"
    fails = []

    for name, exp in EXPECTED.items():
        path = BASE / name
        if not path.exists():
            print(f"SKIP  {name}  (drop it in tests/fixtures/ to include it)")
            continue
        out = extract(await fetch(str(path), "path"), reg, file_name=name)
        got = {
            "supplier": out["metadata"]["supplier_name"],
            "rows": out["diagnostics"]["rows"],
            "closing": out["line_items"][-1]["running_balance"],
            "first_ref": out["line_items"][0]["bp_reference_no"],
            "wrapped": out["diagnostics"]["rows_with_wrapped_cells"],
        }
        ok = got == exp and out["status"] == "ok"
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({got['wrapped']} wrapped cells repaired)")
        if not ok:
            fails.append(name); print(f"      want {exp}\n      got  {got}")

    # Failure paths must degrade to a status, not an exception.
    from reportlab.pdfgen import canvas
    t = pathlib.Path(tempfile.mkstemp(suffix=".pdf")[1])
    c = canvas.Canvas(str(t), pagesize=(595, 842)); c.showPage(); c.save()
    st = extract(t, reg, file_name="blank.pdf")["status"]
    print(f"{'PASS' if st == 'no_text_layer' else 'FAIL'}  blank pdf -> {st}")
    if st != "no_text_layer": fails.append("blank")

    try:
        await fetch("bm90IGEgcGRm", "base64"); fails.append("nonpdf"); print("FAIL  non-pdf accepted")
    except PipelineError:
        print("PASS  non-pdf rejected with hint")

    print("\nAll green." if not fails else f"\n{len(fails)} failure(s): {fails}")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
