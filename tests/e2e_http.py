"""End-to-end: real MCP client -> HTTP -> extract_document -> structured JSON."""
import asyncio, base64, json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
try:
    import httpx2
except ImportError:      # SDK lines that vendor plain httpx
    import httpx as httpx2

URL = os.environ.get("DOC_EXTRACT_URL", "http://127.0.0.1:8000/mcp")
HDRS = {"Authorization": f"Bearer {os.environ.get('DOC_EXTRACT_TOKEN', 'testtoken')}"}
def _sample_pdf() -> pathlib.Path:
    """A fixture if one exists; otherwise synthesise a statement so this test
    runs on any machine with no real documents present."""
    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    pdfs = sorted(fixtures.glob("*.pdf"))
    if pdfs:
        return pdfs[0]
    from test_robustness import make_statement          # needs reportlab
    return pathlib.Path(make_statement())

PDF = _sample_pdf()

async def main():
    async with httpx2.AsyncClient(headers=HDRS, timeout=60) as hc:
     async with streamable_http_client(URL, http_client=hc) as (r, w):
         async with ClientSession(r, w) as s:
             await s.initialize()
             tools = await s.list_tools()
             print("tools:", [t.name for t in tools.tools])

             res = await s.call_tool("list_profiles", {})
             print("profiles:", [p["id"] for p in json.loads(res.content[0].text)["profiles"]])

             b64 = base64.b64encode(PDF.read_bytes()).decode()
             res = await s.call_tool("extract_document", {
                 "source": b64, "source_type": "base64", "file_name": PDF.name})
             out = json.loads(res.content[0].text)
             print("\nstatus:", out["status"], "| profile:", out["profile"])
             print("supplier:", out["metadata"]["supplier_name"], out["metadata"]["currency"])
             print("rows:", out["diagnostics"]["rows"],
                   "| wrapped repaired:", out["diagnostics"]["rows_with_wrapped_cells"])
             print("first ref:", out["line_items"][0]["bp_reference_no"])
             print("validation.ok:", out["validation"]["ok"])

             # unsupported path
             import pypdf, tempfile
             wtr = pypdf.PdfWriter(); wtr.add_blank_page(width=595, height=842)
             t = tempfile.mkstemp(suffix=".pdf")[1]; wtr.write(t)
             res = await s.call_tool("extract_document", {
                 "source": base64.b64encode(pathlib.Path(t).read_bytes()).decode(),
                 "source_type": "base64", "file_name": "blank.pdf"})
             print("\nblank pdf status:", json.loads(res.content[0].text)["status"])

asyncio.run(main())
