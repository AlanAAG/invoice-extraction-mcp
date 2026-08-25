"""Regression suite. Run: python tests/test_samples.py [pdf_dir]"""
import asyncio, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.profile import Registry
from src.pipeline import fetch, extract, PipelineError

EXPECTED = {
    "SYSTEM_SIDE_-_One_world_SOA_11_07_25.pdf": {
        "supplier": "ONE WORLD TRADING L.L.C.", "phone": "97145588567", "rows": 5,
        "closing": -69966.750, "first_ref": "SI/08781/CN/00007", "wrapped": 1},
    "SYSTEM_SIDE_-_Nutripharm_SOA_19_04_24.pdf": {
        "supplier": "NUTRIPHARM LLC", "phone": "+9714 5610000", "rows": 4,
        "closing": -14095.022, "first_ref": "N-CINV-01999111", "wrapped": 4},
    # Parenthesised legal suffix -- "CO.(LLC)" broke the supplier_name pattern.
    "SYSTEM_SIDE_-_Nazih_SOA_24_09_25.pdf": {
        "supplier": "NAZIH TRADING CO.(LLC)", "phone": "97126777122", "rows": 8,
        "closing": -13897.050, "first_ref": "9000395933/9300073415", "wrapped": 2},
    # Apostrophe in the supplier name.
    "SYSTEM_SIDE_-_Loreal_SOA_20_06_25.pdf": {
        "supplier": "L'OREAL UAE GENERAL TRADING LLC", "phone": "97142749400",
        "rows": 7, "closing": -1217839.140, "first_ref": "9004246804",
        "wrapped": 2},
    # 'Details' prose overhangs into the Amount band; the edge must move into
    # whitespace or every amount comes back null.
    "SYSTEM_SIDE_-_Oasis_SOA_19_04_26.pdf": {
        "supplier": "OASIS PURE WATER COMPANY LLC", "phone": "97125582808",
        "rows": 2, "closing": -3752.560, "first_ref": "1791380226/01",
        "wrapped": 2},
    # Reference sits flush against the posting date, so pdfplumber glues them
    # into one word; it must be cut at the glyph gap.
    "SYSTEM_SIDE_-_Valencia_SOA_27_07_26.pdf": {
        "supplier": "VALENCIA COSMETIC TRDNG.(CASINOVA FZE)",
        "phone": "97165312311", "rows": 6, "closing": -6982.500,
        "first_ref": "FC-SIN-26/119697", "wrapped": 6},
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
            "phone": out["metadata"]["phone"],
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
