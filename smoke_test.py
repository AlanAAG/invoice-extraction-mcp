#!/usr/bin/env python3
"""
smoke_test.py — verify a DEPLOYED doc-extract instance.

Run this against any environment after deploying, before registering the URL
in MagOneAI. It checks the things that actually break in a new environment:
reachability, auth, tool discovery, and a real end-to-end extraction.

    python scripts/smoke_test.py https://doc-extract.internal $TOKEN
    python scripts/smoke_test.py https://doc-extract.internal $TOKEN sample.pdf

Exit code 0 means the deployment is good to register. Non-zero means do not
register it yet.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys

try:
    import httpx2 as httpx
except ImportError:  # older SDK line
    import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"{mark}  {name}" + (f"  — {detail}" if detail else ""))


async def main(base_url: str, token: str, pdf: str | None) -> int:
    base_url = base_url.rstrip("/")
    mcp_url = base_url if base_url.endswith("/mcp") else f"{base_url}/mcp"
    root = mcp_url[: -len("/mcp")]

    # 1. Health endpoint must answer WITHOUT auth (platform probes use it).
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{root}/health")
            body = r.json()
        record("health reachable, unauthenticated", r.status_code == 200,
               f"profiles={body.get('profiles')}")
        record("no profile load errors", not body.get("profile_errors"),
               str(body.get("profile_errors") or "none"))
    except Exception as exc:
        record("health reachable", False, repr(exc))
        return _summary()

    # 2. The MCP endpoint must REJECT an unauthenticated call.
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(mcp_url, json={"jsonrpc": "2.0", "id": 1,
                                            "method": "initialize"})
        record("unauthenticated request rejected", r.status_code == 401,
               f"HTTP {r.status_code}")
    except Exception as exc:
        record("unauthenticated request rejected", False, repr(exc))

    # 3. A real MCP client must be able to connect and discover tools.
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=120) as hc:
            async with streamable_http_client(mcp_url, http_client=hc) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    names = sorted(t.name for t in tools.tools)
                    record("MCP handshake + tool discovery",
                           "extract_document" in names, f"tools={names}")
                    record("all tools read-only",
                           all(t.annotations and t.annotations.read_only_hint
                               for t in tools.tools))

                    res = await s.call_tool("list_profiles", {})
                    profiles = json.loads(res.content[0].text)["profiles"]
                    record("profiles loaded in the container",
                           bool(profiles),
                           f"{[p['id'] for p in profiles]}")

                    # 4. End-to-end extraction, if a sample was supplied.
                    if pdf:
                        data = pathlib.Path(pdf).read_bytes()
                        res = await s.call_tool("extract_document", {
                            "source": base64.b64encode(data).decode(),
                            "source_type": "base64",
                            "file_name": pathlib.Path(pdf).name})
                        out = json.loads(res.content[0].text)
                        record("extraction returns a known status",
                               out.get("status") in {
                                   "ok", "needs_review", "parsed_without_profile",
                                   "profile_mismatch", "no_text_layer"},
                               f"status={out.get('status')}")
                        record("content.markdown populated",
                               bool(out.get("content", {}).get("markdown")),
                               f"{len(out.get('content', {}).get('markdown', ''))} chars")
                        record("schema_version present",
                               bool(out.get("schema_version")),
                               out.get("schema_version", ""))
                    else:
                        print("SKIP  end-to-end extraction "
                              "(pass a PDF path as the 3rd argument)")
    except Exception as exc:
        record("MCP session", False, repr(exc))

    return _summary()


def _summary() -> int:
    failed = [c for c in CHECKS if not c[1]]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed. Do NOT register this URL in "
              f"MagOneAI yet.")
        return 1
    print("All checks passed. Safe to register this URL in MagOneAI.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2],
                              sys.argv[3] if len(sys.argv) > 3 else None)))
