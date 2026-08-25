"""
pipeline.py — Fetch, detect, parse, validate. One call, one verdict.

The critical design choice for a no-code orchestrator: the happy path is a
SINGLE tool call. Chaining inspect -> parse -> validate -> ingest gives the
agent four chances to take a wrong turn and burns iteration budget. Here the
agent calls extract_document once and branches on a `status` string.

status values, which the workflow switches on:

  ok             parsed and every check passed -> safe to post
  needs_review   parsed but a check failed     -> human queue
  unsupported    no profile matched            -> onboarding path
  no_text_layer  scanned PDF                   -> OCR tier (not built yet)
  error          fetch or parse failed         -> retry / alert
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pdfplumber

from .content import (chunk_markdown, extract_content, render_markdown,
                      render_table_md, render_text)
from .layout import (HeaderMismatch, cluster_lines, has_text_layer,
                     measure_page, parse_table)
from .profile import Registry

# Bumped when the output shape changes in a way a consumer must react to.
# Downstream nodes should assert on this rather than duck-typing the JSON.
SCHEMA_VERSION = "2.0"

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 100
FETCH_TIMEOUT = 60.0


class PipelineError(Exception):
    """Carries a hint the agent can act on rather than a bare traceback."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

async def fetch(source: str, source_type: str) -> Path:
    """
    MagOneAI realistically supplies a signed URL or base64 bytes; `path` is
    for local testing only, since the server shares no filesystem with the
    orchestrator.
    """
    if source_type == "path":
        p = Path(source)
        if not p.is_file():
            raise PipelineError(
                f"No file at {source!r}.",
                "The server does not share a filesystem with the workflow. "
                "Use source_type='url' or 'base64'.",
            )
        return p

    if source_type == "url":
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT,
                                         follow_redirects=True) as client:
                r = await client.get(source)
                r.raise_for_status()
                data = r.content
        except httpx.HTTPError as exc:
            raise PipelineError(
                f"Could not fetch the URL: {exc}",
                "Check the link has not expired and is reachable from the server.",
            ) from exc
    elif source_type == "base64":
        try:
            data = base64.b64decode(source, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PipelineError(
                f"Invalid base64: {exc}",
                "Send raw base64 with no data: URI prefix.",
            ) from exc
    else:
        raise PipelineError(
            f"Unknown source_type {source_type!r}.",
            "Use 'url', 'base64', or 'path'.",
        )

    if len(data) > MAX_PDF_BYTES:
        raise PipelineError(
            f"PDF is {len(data) // 1024}KB; the limit is {MAX_PDF_BYTES // 1024}KB.",
            "Split the document and submit the parts separately.",
        )
    if not data.startswith(b"%PDF"):
        raise PipelineError(
            "The fetched bytes are not a PDF.",
            "Check the URL returns the file itself, not an HTML download page.",
        )

    tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
    tmp.write_bytes(data)
    return tmp


def checksum(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def inspect(path: Path, registry: Registry) -> dict[str, Any]:
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        meta = pdf.metadata or {}
        sample = (pdf.pages[0].extract_text() or "")[:500]

    text_layer = has_text_layer(str(path))
    matches = registry.match(str(path)) if text_layer else []

    return {
        "pages": pages,
        "has_text_layer": text_layer,
        "producer": meta.get("Producer"),
        "creator": meta.get("Creator"),
        "profile_matches": matches,
        "text_sample": sample,
    }


# ---------------------------------------------------------------------------
# Content assembly
# ---------------------------------------------------------------------------

def _run_parse(path: Path, prof):
    return parse_table(
        str(path),
        prof.table["columns"],
        anchor_column=prof.table["anchor_column"],
        anchor_pattern=prof.table["anchor_pattern"],
        stop_pattern=prof.table.get("stop_pattern"),
        join_with=prof.table.get("join_with", ""),
    )


def _build_content(path: Path, *, title: str | None,
                   tables: list[dict[str, Any]],
                   table_regions: dict[int, tuple[float, float]] | None,
                   include_coordinates: bool,
                   fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Assemble the four representations of the document. Always runs, whether
    or not a profile matched, because step 5 of the workflow requires the
    team to be able to act on any document.
    """
    raw = extract_content(str(path), table_regions=table_regions)
    blocks = raw["blocks"]
    markdown = render_markdown(title=title, blocks=blocks, tables=tables,
                               fields=fields)
    return {
        "markdown": markdown,
        "text": render_text(blocks),
        "key_values": raw["key_values"],
        "blocks": [b.to_dict(include_coordinates) for b in blocks],
        "chunks": chunk_markdown(markdown),
        "pages": raw["pages"],
    }


def _content_only_result(path: Path, info: dict, file_name: str,
                         include_coordinates: bool, *, status: str,
                         message: str, next_step: str,
                         candidates: list | None = None) -> dict[str, Any]:
    content = _build_content(path, title=file_name or None, tables=[],
                             table_regions=None,
                             include_coordinates=include_coordinates)
    out = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": message,
        "next_step": next_step,
        "profile": None,
        "document": {"file_name": file_name, "checksum": checksum(path),
                     "pages": info["pages"]},
        "metadata": {},
        "content": content,
        "tables": [],
        "line_items": [],
        "summary": {},
        "validation": {"ok": True, "checks": [
            {"check": "content_extracted", "passed": bool(content["text"]),
             "characters": len(content["text"])}
        ]},
        "diagnostics": {"rows": 0, "profile_matched": False},
    }
    if candidates:
        out["candidates"] = candidates
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract(path: Path, registry: Registry, *,
            profile_id: str | None = None,
            min_confidence: float = 0.5,
            file_name: str = "",
            include_coordinates: bool = False) -> dict[str, Any]:
    """
    Read a document and return structured JSON. Pure function of the bytes:
    no writes, no side effects, no persistence. Whatever comes next in the
    workflow -- database insert, redaction, routing -- is a separate node.

    include_coordinates attaches a `_source` block per line item giving the
    page and bounding box of every field, so a redaction or highlight step
    downstream has geometry without re-parsing the PDF. Off by default
    because it roughly doubles payload size.
    """
    info = inspect(path, registry)

    if not info["has_text_layer"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "no_text_layer",
            "message": "This PDF is a scan with no embedded text.",
            "next_step": "Route to the OCR tier, or to manual entry.",
            "document": {"file_name": file_name, "checksum": checksum(path),
                         "pages": info["pages"]},
            "content": {"markdown": "", "text": "", "blocks": [],
                        "key_values": {}, "chunks": []},
            "tables": [],
            "line_items": [],
        }

    if profile_id:
        prof = registry.get(profile_id)
        if prof is None:
            raise PipelineError(
                f"Unknown profile {profile_id!r}.",
                f"Available profiles: {sorted(registry.profiles)}",
            )
        confidence = next((m["confidence"] for m in info["profile_matches"]
                           if m["profile"] == profile_id), 0.0)
    else:
        best = info["profile_matches"][0] if info["profile_matches"] else None
        if not best or best["confidence"] < min_confidence:
            # NOT a dead end. The document is still fully read and returned,
            # so the team can act on it (and store it, and chat with it)
            # before anyone writes a profile. A profile only adds typed line
            # items and cross-checks on top of this.
            return _content_only_result(
                path, info, file_name, include_coordinates,
                status="parsed_without_profile",
                message="No profile matched, so line items are untyped. "
                        "Full document content was still extracted.",
                next_step="Use `content.markdown` as-is, or call probe_layout "
                          "and add a profile YAML to get typed line items.",
                candidates=info["profile_matches"][:3],
            )
        prof = registry.get(best["profile"])
        confidence = best["confidence"]

    try:
        table = _run_parse(path, prof)
    except HeaderMismatch as exc:
        # The format changed. Content is still returned in full, so the
        # document remains storable and chattable while the profile is fixed.
        result = _content_only_result(
            path, info, file_name, include_coordinates,
            status="profile_mismatch",
            message=str(exc),
            next_step=(f"Profile {prof.id!r} no longer matches this layout. "
                       f"Update its `columns` to the printed header, then "
                       f"POST /admin/reload-profiles."),
        )
        result["profile"] = prof.id
        result["header_actual"] = exc.actual
        result["header_missing"] = exc.missing
        return result


    with pdfplumber.open(path) as pdf:
        page_lines = []
        for pno, page in enumerate(pdf.pages[:MAX_PAGES]):
            geom, words = measure_page(page, pno)
            page_lines.append(cluster_lines(words, geom))

    first_page_lines = page_lines[0] if page_lines else []

    # The header block is on page 1. The summary/ageing block is at the END
    # of the statement, which on a multi-page document is NOT page 1 --
    # searching only the first page silently lost it and tripped
    # summary_equals_last on every multi-page statement.
    metadata = prof.extract_metadata(first_page_lines)
    summary = {}
    for lines in reversed(page_lines):
        summary = prof.extract_summary(lines)
        if summary and any(summary.values()):
            break
    records = []
    for i, row in enumerate(table.rows, start=1):
        rec = prof.map_row(row.cells, i)
        if include_coordinates:
            rec["_source"] = {"page": row.page, "cells": row.provenance}
        records.append(rec)
    validation = prof.validate(records, summary, table)

    # The parsed table's vertical span per page, so those lines are not also
    # emitted as loose text in the content blocks.
    regions: dict[int, tuple[float, float]] = {}
    for row in table.rows:
        for pg in (row.pages or [row.page]):
            tops = row.tops or [0.0]
            lo, hi = min(tops), max(tops) + 15.0
            if pg in regions:
                regions[pg] = (min(regions[pg][0], lo), max(regions[pg][1], hi))
            else:
                regions[pg] = (lo, hi)

    field_names = [f["name"] for f in prof.fields]
    tables = [{
        "title": prof.raw.get("table", {}).get("title", "Line items"),
        "profile": prof.id,
        "columns": field_names,
        "rows": [{k: r.get(k) for k in field_names} for r in records],
    }]
    if summary:
        for name, buckets in summary.items():
            if buckets:
                tables.append({
                    "title": f"Summary — {name}",
                    "columns": list(buckets.keys()),
                    "rows": [buckets],
                })

    title = (metadata.get("supplier_name") or metadata.get("customer_name")
             or file_name or prof.name)
    content = _build_content(path, title=title, tables=tables,
                             table_regions=regions,
                             include_coordinates=include_coordinates,
                             fields=metadata)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if validation["ok"] else "needs_review",
        "profile": prof.id,
        "profile_confidence": confidence,
        "document": {
            "file_name": file_name,
            "checksum": checksum(path),
            "pages": info["pages"],
            "pages_parsed": table.pages_parsed,
        },
        "metadata": metadata,
        "content": content,
        "tables": tables,
        "line_items": records,
        "summary": summary,
        "validation": validation,
        "diagnostics": {
            "rows": len(records),
            "rows_with_wrapped_cells": sum(1 for r in table.rows if r.wrapped_lines),
            "total_wrapped_lines": sum(r.wrapped_lines for r in table.rows),
            "pages_without_header": table.pages_without_header,
            "unmapped_header_columns": table.unmapped_headers,
            "pages_without_repeated_header": table.pages_without_repeated_header,
            "column_fill_rate": table.column_fill,
            "page_geometry": table.geometry,
            "warnings": table.warnings,
            "profile_matched": True,
            "content_blocks": len(content["blocks"]),
            "markdown_characters": len(content["markdown"]),
        },
    }
