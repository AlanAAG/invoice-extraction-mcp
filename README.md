# doc-extract

An MCP server that does exactly one thing: **PDF in → the entire document as
validated structured JSON out.**

Built for this workflow:

```
[1] User drops a document
[2] doc-extract MCP  ← this repo. Reads the WHOLE document, returns JSON
[3] DB node          → insert into NeonDB          (separate node)
[4] Agent node       → chats over the NeonDB content (separate node)
[5] Or: the team acts on the JSON directly, with no DB at all
```

Steps 3, 4 and 5 are deliberately NOT this server's job. It has no database
driver and every tool is read-only.

No database. No side effects. No persistence. Every tool is read-only. What
happens next — insert, redact, route, notify — is a separate node in the
MagOneAI workflow.

---

## Scope, enforced not just stated

| This server does | This server does NOT |
|---|---|
| Read a PDF's **entire** text layer | Write to any database |
| Reconstruct table geometry | Send email or notify |
| Repair wrapped cells | Redact or modify the PDF |
| Validate the read | Decide what happens next |
| Return JSON + coordinates | Store anything between calls |

Enforcement, so scope creep is structurally hard:

- All three tools are annotated `readOnlyHint: true`, `destructiveHint: false`,
  `idempotentHint: true`. An orchestrator can see this is safe to retry.
- `extract()` is a pure function of the PDF bytes. Same input → same output.
- Reloading profiles is an **HTTP admin route**, not an MCP tool. Config
  changes are an operator action; a workflow agent must not be able to choose
  to do one.
- Nothing is written to disk except a temp file for the incoming PDF.

---

## Why not OCR

Both samples are Crystal Reports exports from SAP Business One — embedded
fonts, no raster images. Every character already carries exact page
coordinates. OCR would rasterise that and re-derive those coordinates *with
error*.

The wrapped `BP Ref. No.` is a **layout reconstruction** problem:

| line  | token          | x0  | x1  |
|-------|----------------|-----|-----|
| 278.7 | `SI/08781/CN/` | 124 | 164 |
| 288.4 | `00007`        | 124 | 142 |

`00007` sits at exactly the left edge of the BP Ref column → same cell →
`SI/08781/CN/00007`.

It matters most on the Nutripharm file, where fragments are bare digits.
Reading flat text, `111` plausibly glues onto the amount giving
`-8,762.513111`. The coordinates say x0=124, not x≈450, so it is the
reference (`N-CINV-01999111`) and the amount stays `-8,762.513`.

---

## The whole document, always

Profile parsing answers "what are the line items?" and ignores everything
else. That is not enough for steps 2, 4 and 5, so full-document extraction
runs on **every** document, matched or not, producing four views of the same
content:

| Field | What it is | Use it for |
|---|---|---|
| `content.markdown` | The document rendered for an LLM | **Chatting.** Store this. |
| `content.text` | Plain text | Search, embeddings |
| `content.key_values` | Every `Label: value` on the page | Filters, lookups |
| `content.blocks` | Typed, ordered, positioned blocks | Programmatic use, redaction |
| `content.chunks` | Markdown split at headings | Retrieval on long docs |
| `tables[]` | Each table as columns + rows | Rendering, export |
| `line_items[]`, `metadata{}` | Typed + validated | SQL aggregation |

**A document with no profile is no longer a dead end.** It returns
`parsed_without_profile` with `content` fully populated — so the team can
store it, chat with it, and act on it before anyone writes a profile. A
profile only *adds* typed line items and cross-checks on top.

### Why markdown is the artifact for chat

An agent asked "what is the closing balance for One World?" answers far more
reliably reading this than re-assembling rows from JSON or scanning a raw
text dump:

```markdown
# ONE WORLD TRADING L.L.C.

## Key fields
| Field | Value |
|---|---|
| supplier_code | S00066 |
| currency | AED |
| ageing_date | 2025-07-11 |

### Line items
| document_no | bp_reference_no | due_date | amount | running_balance |
|---|---|---|---|---|
| 131365 | SI/08781/CN/00007 | 2025-07-07 | -43160.25 | -43160.25 |
...

## All document fields (as printed)
| Posting Date | From To 11.07.25 |
| Sales Employee | No Sales Employee |
...
```

Typed, validated fields lead. Raw printed fields follow, so a question the
profile does not model can still be answered. The parsed table is rendered
once — it is not duplicated as loose text.

Rule of thumb for node 4: **aggregations go to SQL, "what does this document
say?" goes to markdown.** A one-page statement fits in a prompt whole, and
feeding it whole beats retrieving chunks of it.

## Output contract

Consumers should assert on `schema_version` rather than duck-typing.

```jsonc
{
  "schema_version": "1.0",
  "status": "ok",                     // ok | needs_review | unsupported
                                      // | no_text_layer | error
  "profile": "sap_b1_supplier_statement",
  "profile_confidence": 1.0,
  "document": { "file_name": "...", "checksum": "sha256:...",
                "pages": 1, "pages_parsed": [0] },
  "metadata": { "supplier_name": "ONE WORLD TRADING L.L.C.",
                "supplier_code": "S00066", "currency": "AED",
                "ageing_date": "2025-07-11" },
  "line_items": [
    { "line_no": 1, "document_type": "PU", "document_no": "131365",
      "bp_reference_no": "SI/08781/CN/00007",
      "posting_date": "2025-05-31", "due_date": "2025-07-07",
      "amount": -43160.25, "running_balance": -43160.25,
      "_source": {                    // only when include_coordinates=true
        "page": 0,
        "cells": { "BP Ref. No.": { "page": 0, "wrapped": true,
                                    "bbox": [123.7, 278.67, 163.79, 295.02] } }
      } }
  ],
  "summary":    { "buckets": { "Balance Due": -69966.75 } },
  "validation": { "ok": true, "checks": [ ... ] },
  "diagnostics": { "rows": 5, "rows_with_wrapped_cells": 1,
                   "column_fill_rate": { ... }, "page_geometry": [ ... ],
                   "warnings": [] }
}
```

`checksum` is included so a downstream insert node can dedupe **without this
server needing to know a database exists**. That is the separation working:
we supply the fact, someone else decides what to do with it.

### `include_coordinates`

Off by default (roughly doubles payload). Turn it on when a later node needs
to redact, highlight, or visually verify. `bbox` is `[x0, top, x1, bottom]` in
PDF points, and spans **all** lines a wrapped cell occupied — so a redaction
box over `SI/08781/CN/00007` correctly covers both visual lines.

Dates are ISO 8601. Amounts are floats, negative for payables as printed.

---

## Validation, and proof that it works

`status: "ok"` means every check passed. There are two independent families:

**Arithmetic** — do the numbers we read reproduce the numbers printed?

- `running_balance_chain` — each balance advances by its own row's amount.
  Stronger than a total: it names the failing line, and catches reordered or
  duplicated rows that a sum cannot see at all.
- `sum_equals_last` — amounts sum to the closing balance.
- `summary_equals_last` — the ageing total agrees.

**Structural** — did the reconstruction consume the page?

- `word_coverage` — every word in the table region landed in exactly one
  cell. The core invariant.
- `no_unassigned_words`, `no_orphan_lines` — nothing skipped.
- `no_suspicious_rows` — flags sparse rows (a continuation fragment mistaken
  for a new row) and rows stitched across a page break.
- `field_matches` — shape check on reference numbers.

Structural checks exist because **arithmetic cannot see text corruption**: a
mangled reference number still cross-foots perfectly. `tests/test_detection.py`
corrupts data seven ways and asserts each check fires:

```
PASS  clean data validates
PASS  misread amount on line 2      -> running_balance_chain, sum_equals_last
PASS  rows out of order             -> running_balance_chain
PASS  duplicated row                -> running_balance_chain
PASS  mangled reference number      -> field_matches:bp_reference_no   <-- ONLY this
PASS  unclaimed words on page       -> word_coverage, no_unassigned_words
PASS  summary disagrees             -> summary_equals_last
PASS  missing required field        -> required_fields
```

Line 5 is the point of the whole exercise. A check that never fails is
decoration; these were proven to fire.

**The guarantee to state to stakeholders** is not "the parser handles every
layout" — unfalsifiable, and someone will find a counterexample. It is:
*every document either parses and self-verifies, or it is flagged. Nothing
reaches the next node silently wrong.*

---

## Testing

`TESTING.md` has the full ladder. Short version:

```bash
bash scripts/check_repo.sh                      # is the clone complete?
bash scripts/run_tests.sh                       # all 6 suites, no server
npx @modelcontextprotocol/inspector python -m src.server   # see it as a client
python scripts/smoke_test.py <url> <token> doc.pdf         # verify a deployment
```

Level 3 in `TESTING.md` — putting a real agent in front of it via Claude
Desktop — is the one worth not skipping. The tool docstrings are the only
instructions MagOneAI's agent will ever get, and the only way to test them is
to let an LLM try to use them.

## Quick start

```bash
pip install -r requirements.txt
export DOC_EXTRACT_TOKEN=$(openssl rand -hex 32)
MCP_TRANSPORT=http python -m src.server     # http://0.0.0.0:8000/mcp

python tests/test_samples.py                # parser regression
python tests/test_detection.py              # validation fires
python tests/e2e_http.py                    # real MCP client over HTTP
```

```bash
docker build -t doc-extract .
docker run -p 8000:8000 -e DOC_EXTRACT_TOKEN=$TOKEN doc-extract
curl localhost:8000/health
```

---

## How to build an MCP server

The mechanics, using this server as the worked example.

### 1. What an MCP server actually is

A process exposing a small JSON-RPC API that an LLM client can discover and
call. Three things over one connection: **tools** (functions the model calls),
**resources** (data it can read), **prompts** (templates). Most servers only
need tools.

The client asks `tools/list`, gets names + JSON Schemas + descriptions, and
decides what to call. **You never write the schema by hand** — it is generated
from your Python type hints.

### 2. Pick a transport, and get this right first

| Transport | How it runs | Use when |
|---|---|---|
| **stdio** | Client spawns your process, talks over stdin/stdout | Local: Claude Desktop, VS Code |
| **streamable HTTP** | Long-running service at a URL | Hosted orchestrators — MagOneAI |

This is the decision people get wrong. Almost every OCR/PDF MCP server on
GitHub is stdio-only. **MagOneAI is hosted and cannot spawn a subprocess on
your machine**, so stdio servers need a proxy in front. Build for HTTP from
the start.

### 3. Define tools — the docstring is the contract

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("doc-extract", version="0.1.0")

@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def extract_document(
    source: Annotated[str, Field(description="HTTPS URL or base64 PDF bytes.")],
    source_type: Literal["url", "base64", "path"] = "url",
) -> str:
    """
    Turn a PDF statement into structured JSON.

    BRANCH ON `status`:
      "ok"            every check passed. Safe to pass downstream.
      "needs_review"  a check failed. Send to a human queue.
      ...
    """
```

Three things doing real work here:

- **Type hints → schema.** `Literal[...]` becomes an enum the model cannot
  violate. `Field(description=...)` documents each parameter.
- **The docstring is a prompt.** It is the only instruction the agent gets
  about when and how to call this. Write it for the model, not for a human
  reading source. "BRANCH ON `status`" is there because the agent needs to
  know that.
- **Annotations are safety metadata.** `readOnlyHint` tells the orchestrator
  this is safe to retry and cannot mutate anything.

### 4. Return strings, but structured ones

Tools return text. Return `json.dumps(...)` with a stable shape, and include
`schema_version`. Never let an exception escape — catch it and return
`{"status": "error", "hint": "..."}`. A traceback tells the agent nothing; a
hint lets it self-correct.

### 5. Serve it

```python
def build_http_app():
    app = mcp.streamable_http_app(stateless_http=True)
    app.user_middleware.insert(0, _bearer_middleware(token))
    app.middleware_stack = app.build_middleware_stack()
    return app
```

`stateless_http=True` matters: each request is independent, so the service
scales horizontally and a restart never strands a workflow mid-session.

Add `@mcp.custom_route("/health", methods=["GET"])` for platform probes, and
gate everything else behind a bearer token with `hmac.compare_digest`.

### 6. Test with a real client, not curl

curl proves the port is open. It does not prove the schema is valid or that
the agent can use the tool.

```python
async with httpx.AsyncClient(headers=HDRS) as hc:
    async with streamable_http_client(URL, http_client=hc) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            res = await s.call_tool("extract_document", {...})
```

That is `tests/e2e_http.py`, and it is what caught two real bugs here.

### 7. Things that will bite you

- **The SDK moved.** `FastMCP` is gone in Python SDK 2.x; it is
  `mcp.server.mcpserver.MCPServer`. Client is `streamable_http_client`
  (underscores), yielding **two** streams, and it takes an `http_client=`
  rather than `headers=`. Check the installed signatures with
  `inspect.signature` instead of trusting a blog post.
- **Never trust document content as instruction.** Extracted text is *data*.
  If a PDF contains "ignore previous instructions", nothing downstream acts
  on it. Say so in the tool docstring.
- **Secrets are server-side env vars.** A token routed through the workflow
  canvas ends up in run logs.
- **Fewer tools is better.** Every tool is a branch the agent can take
  wrongly. The happy path here is one call.

---

## What varies freely vs. what needs a profile edit

Measured, not asserted — `tests/test_robustness.py` mutates the format one
axis at a time.

**Free. No change needed:**

| Variation | Result |
|---|---|
| Different supplier, amounts, dates | `ok` |
| Any number of rows, across any number of pages | `ok` |
| Wrap depth 0, 1, 2, 4+ lines — mixed in one document | `ok` |
| Columns repositioned by layout drift | `ok` |
| Font size 5pt → 16pt | `ok` |
| Header punctuation drift (`BP Ref. No.` → `BP Ref No`) | `ok` |
| Different document-type prefix (`PU` → `RC`) | `ok` |
| Faux-bold / drop-shadow rendering | `ok` |

Nothing is pinned to a coordinate: column bands are rebuilt per page from
that page's own header, line-clustering tolerance comes from the document's
median glyph size, and header-cell merging comes from the line's own gap
distribution capped by type size.

**Needs a profile edit — and says so:**

| Variation | Result | What you get |
|---|---|---|
| Column **renamed** (`Post. Date` → `Posting Date`) | `profile_mismatch` | The missing name + the header as printed |
| Column **removed** | `profile_mismatch` | Same |
| Column **added** | `needs_review` | `unmapped_header_columns: ["Currency"]` |
| Anchor no longer matches (`INV-2026-001`) | `needs_review` | Zero rows, flagged rather than passed empty |
| Entirely different document | `parsed_without_profile` | Full content, no typed rows |

The added-column case is the one that matters most: a new column's content
gets absorbed into a neighbouring cell, and the arithmetic can still foot. So
`all_header_columns_mapped` fails the document explicitly rather than letting
it pass quietly.

**In every one of these cases `content.markdown` is still complete**, so the
document remains storable and chattable while someone fixes the profile.

A `profile_mismatch` response is directly actionable:

```json
{
  "status": "profile_mismatch",
  "header_missing": "Post. Date",
  "header_actual": ["Document","BP Ref. No.","Posting Date","Due Date",
                    "Details","Amount","Balance"],
  "next_step": "Update its `columns` to the printed header, then
                POST /admin/reload-profiles."
}
```

Fixing it is a one-line YAML change and a reload — no redeploy.

## Multi-page behaviour

A statement run is not "the same page N times". Each of these is tested in
`tests/test_multipage.py`:

| Scenario | Result |
|---|---|
| Header repeated on every page | `ok` — all rows |
| **Header printed only on page 1** | `ok` — bands carried forward |
| **A row cut by the page break** | `ok` — the wrapped tail is stitched to its row |
| Page size / orientation changes mid-document | `ok` — bands rebuilt per page |
| Unrelated page (terms, remittance) stapled in | `ok` — skipped, no invented rows |

Two of these needed real fixes.

**Header only on page 1** silently lost every row after the first page. Now
the previous page's bands are carried forward — but only committed if the page
actually contains anchor-matching rows, so a terms-and-conditions page is not
force-fitted into a table it has nothing to do with. `diagnostics.
pages_without_repeated_header` lists which pages this applied to.

**Row cut by the page break** — the reference `SI/08781/CN/` at the bottom of
page 1 with `00007` at the top of page 2 reassembles to
`SI/08781/CN/00007`. The stitch is gated on the carried row having actually
sat near the bottom of the previous page. Without that guard, any stray line
above the first row of a page would be glued onto the previous row; with it,
a stray fragment instead surfaces as an orphan and fails the document:

```
status: needs_review
refs  : ['SI/2000', 'SI/2001', 'SI/2100']      <-- NOT corrupted
FAILED: no_orphan_lines  {'text': 'STRAY-FRAGMENT', 'reason': 'before_first_row'}
```

A correct page-break stitch no longer forces human review by itself — it is
reported as a warning. Otherwise every long statement would need signing off.

## Adding a vendor format: config, not code

Profiles are YAML in `profiles/`. Adding a format never touches `layout.py`.

1. `extract_document` returns `parsed_without_profile`
2. `probe_layout` → every line with per-word x-coordinates
3. Copy the header labels **verbatim** into `columns`
4. Pick an `anchor_column` + `anchor_pattern` matching the first cell of each
   row and nothing else
5. Drop the file in `profiles/`, `POST /admin/reload-profiles`

```yaml
id: acme_invoice
detect:
  require: ["Tax Invoice"]
  text_contains: ["Tax Invoice", "Invoice No."]
table:
  columns: ["Line", "Item Code", "Description", "Qty", "Amount"]
  anchor_column: "Line"
  anchor_pattern: '^\d+$'
  stop_pattern: '^Subtotal\b'
  join_with: ""          # "" for codes/refs, " " for prose
fields:
  - {name: item_code, source: "Item Code", type: text}
  - {name: amount,    source: "Amount",    type: decimal}
validation:
  - {type: required_fields, fields: [item_code, amount]}
```

Types: `text`, `decimal`, `date` (+`format`), `int`, `token` (+`index`).
A broken YAML is isolated — it lands in `load_errors`, other profiles keep
working.

---

## MagOneAI wiring

```
[1] Trigger: user drops a document / Outlook attachment
        ↓
[2] Agent node: extract_document(source=<url>, file_name=...)
        ↓
    switch on status:
      ok                     -> [3] insert -> [4] chat agent
      needs_review           -> human approval -> insert / reject
      parsed_without_profile -> [3] insert anyway (content is complete)
                                 + alert: new vendor format seen
      no_text_layer          -> OCR queue
      error                  -> retry, then alert
        ↓
[3] DB node: run neon_schema.sql once, then upsert on document.checksum
        ↓
[4] Agent node with NeonDB access:
      "what does this say?"  -> SELECT markdown FROM documents WHERE ...
      "how much is past due?" -> SELECT SUM(amount) FROM v_document_lines ...
```

`neon_schema.sql` in this repo holds the DDL, the JSON-path → column mapping
for node 3, and the queries node 4 should run. Note that
`parsed_without_profile` still inserts: `content.markdown` is complete, so
the document is chattable immediately; typed line items arrive later when a
profile is added, and re-ingesting is idempotent on checksum.

- `DOC_EXTRACT_TOKEN` server-side, sent as `Authorization: Bearer <token>`.
- `max_iterations` ≈ 15. One call on the happy path; review/onboarding
  branches chain more.
- Prefer `source_type="url"`; base64 inflates payloads ~33%.
- The insert node owns the schema. This server does not know it exists.

---

## Engineering notes

- **Line-clustering tolerance is derived per document** from median glyph
  size, not hardcoded, so the same report at a different scale still parses.
  This change surfaced a real bug: `BP :` vs `BP:` tokenises differently, so
  metadata regexes now run against punctuation-normalised text.
- **Page-break stitching** — a cell wrapping across a page boundary is joined
  to the carried row and flagged `stitched_across_page_break`.
- **Sparse-row detection** — a row filling ≤1/3 of its columns is flagged as
  a possible false anchor, the one failure orphan detection cannot catch.

## Known limits

- If a wrapped fragment landed in the anchor column *and* matched the anchor
  pattern, it would read as a new row. Sparse-row detection flags the likely
  cases; a tight anchor regex is the real defence.
- Ageing bucket mapping is verified on two documents, both landing in near
  buckets. **Run a statement with real 90+ ageing before trusting bucket
  labels in production.**
- Encrypted or password-protected PDFs are not handled; they surface as
  `error`.
