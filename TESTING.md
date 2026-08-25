# Testing before MagOneAI

Five levels. Each one catches a different class of failure, and each takes
longer than the last. Work up; stop when you hit something.

The one people skip is **Level 3**. Levels 0–2 prove the code runs. Level 3
proves an *agent* can actually use it, which is what MagOneAI will be doing.

---

## Level 0 — the suites (30 seconds, no server)

```bash
pip install -r requirements.txt
pip install reportlab                # test-only, synthesises PDFs
./scripts/run_tests.sh
```

```
test_samples         All green.     the two real statements + failure paths
test_adversarial     All green.     wrap depth, 5–16pt, shadow text
test_detection       All green.     every validation check actually fires
test_content         All green.     full-document extraction, profiled or not
test_robustness      All green.     renamed / added / removed columns
test_multipage       All green.     header-once, page-break splits, mixed sizes
```

Run this after every change. It needs no network, no Docker, no server.

### Test with real PDFs

Real statements go in `tests/fixtures/` — the folder is **gitignored for
PDFs on purpose**, because the samples carry supplier names, balances and
phone numbers that should not enter git history. Everyone drops their own
local copies in:

```bash
cp ~/Downloads/SYSTEM_SIDE_*.pdf tests/fixtures/
python tests/test_samples.py                 # or point it anywhere:
python tests/test_samples.py /path/to/folder-of-pdfs
```

Without fixtures, the two real-document suites skip those cases and still
pass; the other four suites are fully synthetic and never need them.

Or one document, printing everything:

```python
import asyncio
from src.profile import Registry
from src.pipeline import fetch, extract

async def main():
    doc = extract(await fetch("statement.pdf", "path"), Registry(),
                  file_name="statement.pdf")
    print(doc["status"])
    print(doc["content"]["markdown"])          # what a chat agent would read
    for r in doc["line_items"]:
        print(r)
    for c in doc["validation"]["checks"]:
        if not c["passed"]:
            print("FAILED:", c)

asyncio.run(main())
```

If a new document comes back `parsed_without_profile` or `profile_mismatch`, the
response tells you what to fix. `probe_layout` shows the printed header and
every word's x-position.

---

## Level 1 — the MCP Inspector (see it as a client does)

The official Inspector renders the tool list, the generated JSON Schemas, and
lets you call tools by hand. This is where you find out your schema is wrong.

```bash
npx @modelcontextprotocol/inspector python -m src.server
```

It launches the server over stdio and opens a browser UI. Check that:

- three tools appear: `extract_document`, `probe_layout`, `list_profiles`
- `source_type` renders as an **enum**, not a free string
- each description reads like an instruction to an agent, not a code comment
- calling `extract_document` with a base64 PDF returns populated JSON

---

## Level 2 — HTTP + smoke test (what deployment will look like)

```bash
export DOC_EXTRACT_TOKEN=$(openssl rand -hex 32)
MCP_TRANSPORT=http python -m src.server
```

In another terminal:

```bash
python scripts/smoke_test.py http://localhost:8000 $DOC_EXTRACT_TOKEN \
       /path/to/statement.pdf
```

This checks reachability, that `/health` answers **without** auth, that
`/mcp` **rejects** unauthenticated calls, tool discovery through a real
client, and an end-to-end extraction.

Or via Docker, which is closer to production:

```bash
cp .env.example .env          # set DOC_EXTRACT_TOKEN
docker compose up --build
python scripts/smoke_test.py http://localhost:8000 $DOC_EXTRACT_TOKEN
```

---

## Level 3 — a real agent uses it (do not skip)

Levels 0–2 prove the code works. They do not prove an agent can *use* it. The
tool docstrings are the only instructions MagOneAI's agent will ever get, and
the only way to test them is to put a real LLM in front of them.

Add to Claude Desktop's config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "doc-extract": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/absolute/path/to/doc-extract"
    }
  }
}
```

Restart Claude Desktop, then try prompts the workflow will actually produce:

| Prompt | What it tests |
|---|---|
| "Extract this statement" + attach a PDF | Does the agent pick the right tool? |
| "What's the total payable and when is it due?" | Is the JSON readable enough to answer from? |
| "Which invoices are past due?" | Does it use `due_date` correctly? |
| Attach an unrelated PDF | Does it handle `parsed_without_profile` sensibly? |
| Attach a scan | Does it explain `no_text_layer` rather than retrying? |

**What to watch for**, because these are the failures MagOneAI will inherit:

- the agent calls `probe_layout` when it should call `extract_document` →
  the descriptions are ambiguous
- it treats `needs_review` as success → the docstring is not emphatic enough
- it retries forever on `no_text_layer` → the terminal statuses are unclear
- it cannot answer a question the document plainly contains → the markdown
  rendering needs work, not the parser

Fixing these means editing docstrings in `src/server.py`, not logic.

---

## Level 4 — public URL, then MagOneAI

MagOneAI is hosted, so it needs a URL it can reach. Two ways.

### GitHub Codespaces

The repo has a devcontainer, so a Codespace comes up ready.

```bash
export DOC_EXTRACT_TOKEN=$(openssl rand -hex 32)
MCP_TRANSPORT=http python -m src.server
```

Port 8000 auto-forwards. In the **Ports** panel, right-click port 8000 →
Port Visibility → **Public**. Private forwarding requires a GitHub session
cookie, which MagOneAI will not have, so it must be Public or the connection
silently fails with an HTML login page instead of JSON.

Copy the `https://...app.github.dev` URL and verify from outside first:

```bash
python scripts/smoke_test.py https://<your-codespace>-8000.app.github.dev $TOKEN
```

The URL dies when the Codespace stops, so this is for testing, not for
anything anyone depends on.

### Local + tunnel

```bash
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

Faster iteration than a Codespace since the code is on your machine.

### Then register

Use `https://<host>/mcp` as the MCP URL, `Authorization: Bearer <token>` as
the header, and set `max_iterations` ≈ 15. Use a throwaway token for testing
and take the tunnel down afterwards.

---

## Which to use

| | Local | Codespaces |
|---|---|---|
| Levels 0–3 | **faster** — no container spin-up | works, slower loop |
| Level 4 public URL | needs cloudflared/ngrok | **built in** |
| Claude Desktop (Level 3) | **works** | no, it is remote |
| Matches production | with Docker | closer by default |

Local for building and for Level 3. Codespaces when you want a public URL
without installing a tunnel — or when your lead wants to run it without
setting anything up.
