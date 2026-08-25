# Building this MCP server

How `doc-extract` is put together, and why each piece is shaped the way it is.
Read alongside the source; every section names the file it describes.

---

## 0. What an MCP server actually is

An MCP server is a process that advertises a list of **tools** (functions with
typed parameters and a docstring) and executes them when a client asks. The
client here is MagOneAI's agent. It reads your tool descriptions, decides which
to call, sends JSON, gets JSON back.

Two consequences shape everything below:

**Your docstring is the API.** It is not documentation for humans — it is the
prompt the agent reads to decide whether and how to call your tool. A vague
docstring is a bug. So is a docstring that omits what to do with the result.

**The agent is a fallible caller.** It can pass the wrong argument, call in the
wrong order, or misread an ambiguous response. Design for that: fewer tools,
unambiguous outputs, error messages that say what to do next.

---

## 1. Scope the tool surface first

Before writing code, decide what this server does **not** do.

`doc-extract` reads documents. It does not write to a database, send
notifications, decide business outcomes, or route work. Those are separate
nodes in the workflow. The server's whole job is: bytes in, structured data
out, plus an honest signal about how much to trust the read.

This is not tidiness for its own sake. A server that also writes to Neon needs
database credentials, gains a failure mode where the parse succeeds and the
write fails, and forces every consumer to accept a schema decision. Keeping it
read-only means it can be tested with no infrastructure, deployed anywhere, and
reused by any consumer.

**Four tools, and the happy path is one call.**

| Tool | Purpose |
|---|---|
| `extract_document` | Everything a normal workflow needs |
| `probe_layout` | Onboarding a new vendor format |
| `list_profiles` | What can this server parse |
| `reload_profiles` | Pick up a new profile without a restart |

The earlier draft had `inspect → parse → validate → ingest` as four calls. That
is four agent decisions, four chances to branch wrong, and four times the
iteration budget. Collapsing the happy path into one call removed a whole class
of workflow bug.

**Rule of thumb:** add a tool when the agent genuinely needs to make a decision
between two steps. If it would always call B after A, that is one tool.

---

## 2. Layer the code so the engine never knows about the domain

```
server.py     MCP surface: tool defs, transport, auth
   |
pipeline.py   fetch -> inspect -> parse -> validate -> one verdict
   |
profile.py    declarative YAML: what THIS vendor's report looks like
   |
layout.py     pure geometry: words, lines, column bands, rows
```

`layout.py` has no idea what a supplier or an invoice is. It reconstructs
tables from coordinates. Everything vendor-specific is data in
`profiles/*.yaml`. That is what makes "adding a format is a config file, not a
code change" true rather than aspirational.

Test the boundary: if adding a vendor requires editing a `.py`, the split has
leaked.

---

## 3. The engine: derive tolerances, never hardcode them

This is the part most worth understanding, because it is where a coordinate
parser quietly goes wrong.

### The problem

Report generators wrap cell content that overflows a fixed column width:

```
PU 131365 SI/08781/CN/ 31.05.25 ... -43,160.250
00007
```

`00007` looks orphaned. Coordinates say otherwise — it sits at x0=124, the
exact left edge of the `BP Ref. No.` column, so it belongs to that cell.

### Four rules the engine follows

**Rows come from an anchor pattern, never from spacing.** A line starts a row
when its anchor column matches (e.g. `PU 131365`). Everything until the next
anchor belongs to the current row — zero continuation lines or five, the code
is identical. The earlier draft had a `row_pitch = 30.0` guard that silently
dropped deep wraps; that is exactly the bug this rule prevents.

**Tolerances are measured, not chosen.** The samples mix 5.8pt and 14.5pt type
on one page. A hand-tuned y-tolerance is fitted to one document. The engine
takes the median glyph height and derives:

```python
y_tol = clamp(median_glyph_height * 0.45, 1.0, 8.0)
```

The tolerance must sit strictly between the largest intra-row baseline offset
(Crystal renders the right-most column ~0.25pt high) and the smallest genuine
line gap. Deriving it keeps it in that window across font sizes: 5pt type gets
2.25, 16pt gets 7.2, and the real statements get 2.97 — which is where the
hand-tuned constant had been. Reproducing a value you already trusted is good
evidence the rule is right rather than merely different.

**Column assignment maximises overlap, not centre containment.** A wide token
straddling a band edge — precisely what a long wrapped reference is — lands
where most of its ink sits. Anything overlapping no band is *reported*, not
dropped.

**Deduplicate at the character layer.** Some generators draw each string twice
with a sub-point offset to fake bold. Those two draws sit well inside
`x_tolerance` of each other, so word extraction merges them and the cell reads
`PUPU 131365131365`. Deduplicating extracted *words* is too late — the damage
is already inside the token. So the engine filters `page.chars`, then calls
`pdfplumber.utils.extract_words` on the clean set.

One trap: bucketed spatial lookup misses a duplicate pair straddling a bucket
edge (x=119.98 vs x=120.03), which leaves a doubled character mid-token. The
engine checks neighbouring buckets for that reason. This was a real failing
test, not a hypothetical.

### The invariant that matters most

**Every word inside the table region must land in exactly one cell.**

Arithmetic cross-checks catch a dropped or misread *figure*. They cannot catch
a mangled reference number — that still cross-foots perfectly. Word coverage
is the net under exactly the failure this project exists to fix:

```json
{"check": "word_coverage", "words_in_region": 47, "words_claimed": 47, "unclaimed": 0}
```

Alongside it: orphan lines (a line that was neither header, anchor, nor
absorbed continuation), unassigned words, and suspicious rows (an anchor line
with nothing in any other column — possibly a wrapped fragment that matched the
anchor by coincidence).

### Provenance

Every cell carries its page, bounding box, and fragment count:

```json
"BP Ref. No.": {"bbox": [123.7, 278.7, 163.8, 295.0], "fragments": 2, "wrapped": true}
```

A reviewer can trace any value back to its position on the page. For a
finance-facing reader this is the difference between "trust the output" and
"verify the output" — and it costs almost nothing to carry.

---

## 4. Writing tools the agent can actually use

### One field to branch on

`extract_document` returns a `status` string:

| `status` | Workflow action |
|---|---|
| `ok` | Post downstream |
| `needs_review` | Human queue |
| `unsupported` | Onboarding path |
| `no_text_layer` | OCR tier / manual |
| `error` | Read `hint`, retry |

The no-code canvas switches on one string. No interpretation, no parsing prose.
Returning `{"success": false, "message": "..."}` and expecting the agent to
work out why is how workflows get flaky.

### Docstrings are prompts

```python
async def extract_document(source, source_type="url", ...) -> str:
    """
    Turn a PDF statement into structured JSON. ...

    BRANCH ON THE `status` FIELD:
      "ok"             every check passed. Safe to write downstream.
      "needs_review"   parsed, but a check failed. Send to a human queue.
      ...

    Do not post a document downstream on "needs_review" without human
    sign-off: validation cross-foots the line amounts against the printed
    closing balance, so a failure means the numbers do not add up.
    """
```

Tell the agent what the tool does, what each outcome means, and what not to do.
Type annotations plus `pydantic.Field` descriptions become the JSON schema the
agent sees, so annotate every parameter.

### Errors carry a next step

```python
raise PipelineError(
    f"No file at {source!r}.",
    "The server does not share a filesystem with the workflow. "
    "Use source_type='url' or 'base64'.",
)
```

Compare with a bare `FileNotFoundError`. One tells the agent how to fix its
call; the other makes it guess. Same for the header mismatch — it returns the
header line as actually read, so the fix is mechanical.

### Never let an exception escape

Unhandled exceptions surface as opaque transport errors. Every tool catches
broadly and returns a structured `{"status": "error", ...}`.

---

## 5. Input: assume no shared filesystem

The most common integration mistake is a path-based tool. MagOneAI runs
elsewhere; a path means nothing to your container. So `source_type` accepts:

- `url` — preferred; a signed link the server fetches
- `base64` — inline bytes; ~33% payload inflation
- `path` — local testing only

Validate before parsing: size cap (25MB), and check the bytes start with
`%PDF`. Without that check, an expired signed URL returning an HTML login page
becomes a confusing parser crash instead of a clear error.

---

## 6. Transport: HTTP, because stdio cannot work here

Most MCP servers you find on GitHub are **stdio** — the client spawns them as a
subprocess. That works for Claude Desktop. It cannot work for a hosted
orchestrator, which has no way to spawn a process on your machine.

```python
def main():
    if os.getenv("MCP_TRANSPORT", "stdio") != "http":
        mcp.run(transport="stdio")     # local testing
        return
    uvicorn.run(build_http_app(), host=..., port=...)
```

Both transports, one codebase. Develop against Claude Desktop over stdio,
deploy over HTTP.

Use `stateless_http=True`: each request is independent, so the service scales
horizontally and a restart never strands a workflow mid-session.

**Check the SDK version before you write.** The Python SDK moved to 2.x and
`FastMCP` no longer exists at the old import path — it is now
`mcp.server.mcpserver.MCPServer`. Half the tutorials online are pre-2.x. Read
the installed package rather than a blog post:

```bash
python -c "from mcp.server.mcpserver import MCPServer; import inspect; \
           print(inspect.signature(MCPServer.run_streamable_http_async))"
```

---

## 7. Auth and hardening

An HTTP MCP endpoint accepts files from the network. Do not run it open.

```python
if not hmac.compare_digest(got, expected):     # constant time
    return JSONResponse({"error": "unauthorized"}, status_code=401)
```

- Bearer token from an env var, never through the workflow canvas — a token
  that transits a no-code node ends up in run logs
- `/health` stays unauthenticated for platform probes
- Non-root container user: this process parses untrusted files
- Size cap and magic-byte check before parsing

**Extracted text is data, not instructions.** If a document contains text that
reads like a command, nothing downstream should act on it. Say so in the tool
docstring, because the agent reads that.

---

## 8. Testing: two suites, different jobs

**Regression** (`test_samples.py`) pins known-good output on the real files —
exact reference strings, row counts, closing balances.

**Adversarial** (`test_adversarial.py`) synthesises PDFs with reportlab to vary
what the real samples cannot: wrap depths of 0/1/2/4 in one document, 5pt and
16pt type, tight leading, shadow text, a value straddling a band edge, and a
deliberately orphaned line that must be *detected*.

The second suite is what turns "it works on the two files we have" into "it
works across the variation we expect." It found two real bugs — the shadow-text
merge and the bucket-boundary miss — that the real samples could never surface,
because both are clean single-font Crystal exports.

Also test that failures fail correctly: a blank PDF returns `no_text_layer`, a
non-PDF is rejected with a hint, a broken profile YAML is isolated while the
others keep working.

---

## 9. Deploy

```bash
docker build -t doc-extract .
docker run -p 8000:8000 -e DOC_EXTRACT_TOKEN=$TOKEN doc-extract
curl localhost:8000/health
```

`pdfplumber` is pure Python, so the image needs no system packages. Register
`https://your-host/mcp` in MagOneAI with the bearer header, exactly as the
Outlook MCP is configured.

Set `max_iterations` around 15: one call on the happy path, but the review and
onboarding branches chain more.

---

## Checklist for the next MCP server

1. Write down what it does **not** do
2. Fewest tools that let the agent make real decisions
3. Happy path is one call
4. One unambiguous field to branch on
5. Docstrings written for the agent, not for humans
6. Errors carry a next step
7. No tool raises
8. Never assume a shared filesystem
9. HTTP transport, auth on, non-root
10. Adversarial tests, not just the happy sample
