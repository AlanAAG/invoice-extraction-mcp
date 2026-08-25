"""
content.py — Full-document extraction, independent of any profile.

Why this exists
---------------
Profile parsing answers "what are the line items?". It deliberately ignores
everything else on the page. But the workflow needs the WHOLE document:

  * a document with no profile must still be usable (step 5: the team acts on
    it even when nothing is inserted into a database)
  * chatting over the document needs the header block, the footer, the
    totals -- not just the table rows
  * whatever gets stored should be able to answer "who printed this and
    when?", not only "what is line 3?"

So this module runs on EVERY document, matched or not, and produces four
representations of the same content:

  blocks      typed, ordered, positioned -- machine-consumable
  key_values  flattened Label -> value pairs
  markdown    the artifact to store for an LLM to read. This is what makes
              "chat with the PDF" work well: an agent reading clean markdown
              answers far more reliably than one reading raw text dumps or
              re-assembling rows from JSON.
  text        plain text, for search or embedding

Nothing here is SAP-specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pdfplumber

from .layout import Line, cluster_lines, measure_page

# Lines that are page furniture rather than content.
_FURNITURE = re.compile(r"^(page\s*:?\s*\d+\s*/\s*\d+|printed\s+(by|on)\b)", re.I)


@dataclass
class Block:
    kind: str                     # key_value | heading | text | table
    page: int
    text: str
    bbox: list[float] = field(default_factory=list)
    label: str | None = None
    value: str | None = None
    table: dict[str, Any] | None = None

    def to_dict(self, include_coordinates: bool) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "page": self.page}
        if self.kind == "key_value":
            d["label"] = self.label
            d["value"] = self.value
        elif self.kind == "table":
            d["table"] = self.table
        else:
            d["text"] = self.text
        if include_coordinates and self.bbox:
            d["bbox"] = self.bbox
        return d


def _bbox(line: Line) -> list[float]:
    return [round(min(w.x0 for w in line.words), 2),
            round(min(w.top for w in line.words), 2),
            round(max(w.x1 for w in line.words), 2),
            round(max(w.bottom for w in line.words), 2)]


def _is_terminator(text: str) -> bool:
    """A word that closes a label: 'Date:', ':', '#:'."""
    return text.endswith(":") or text == ":"


def _label_word(text: str) -> bool:
    """Label words are alphabetic. Digits mean we have walked into a value."""
    core = text.rstrip(":").strip("#()%.,")
    return bool(core) and not any(ch.isdigit() for ch in core)


def _line_key_values(line: Line) -> list[tuple[str, str, int]]:
    """
    Extract Label -> value pairs from ONE visual line, using word positions.

    Crystal lays labelled fields out in columns, so a single visual line often
    carries several: 'Posting Date: From To 11.07.25   Ageing Date: 11.07.25'.
    Splitting the flattened string gets this wrong -- a regex happily swallows
    'From To 11.07.25 Ageing Date' as one label. Working left to right over
    words, a label is the alphabetic run immediately preceding its colon, and
    its value runs until the next label begins.

    Returns (label, value, label_start_index).
    """
    ws = sorted(line.words, key=lambda w: w.x0)
    terminators = [i for i, w in enumerate(ws) if _is_terminator(w.text)]
    if not terminators:
        return []

    spans: list[tuple[int, int, str]] = []      # (label_start, colon_idx, label)

    # In a multi-column layout the previous field's VALUE sits to the left of
    # this field's label, separated by a wide gutter. Words inside one label
    # are separated by an ordinary space. Use the line's own median gap to
    # tell them apart, so the walk backwards stops at the gutter instead of
    # swallowing the neighbouring value.
    gaps = [ws[i + 1].x0 - ws[i].x1 for i in range(len(ws) - 1)]
    gaps = [g for g in gaps if g >= 0]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 2.0
    gutter = max(typical * 2.5, 6.0)

    for t in terminators:
        start = t
        parts: list[str] = []
        head = ws[t].text.rstrip(":").strip()
        if head and _label_word(head):
            parts.append(head)
        j = t - 1
        while (j >= 0 and len(parts) < 4
               and _label_word(ws[j].text)
               and not _is_terminator(ws[j].text)
               and (ws[j + 1].x0 - ws[j].x1) <= gutter):
            parts.insert(0, ws[j].text)
            start = j
            j -= 1
        label = " ".join(parts).strip()
        if label:
            spans.append((start, t, label))

    out: list[tuple[str, str, int]] = []
    for k, (start, colon, label) in enumerate(spans):
        end = spans[k + 1][0] if k + 1 < len(spans) else len(ws)
        value = " ".join(w.text for w in ws[colon + 1:end]).strip(" .;,-")
        out.append((label, value, start))
    return out


def _classify(line: Line, median_size: float) -> Block:
    text = line.text.strip()
    sizes = [w.size for w in line.words if w.size > 0]
    size = max(sizes) if sizes else median_size

    kvs = _line_key_values(line)
    if kvs:
        b = Block("key_value", line.page, text, _bbox(line),
                  label=kvs[0][0], value=kvs[0][1])
        b.table = {"pairs": [{"label": k, "value": v} for k, v, _ in kvs]}
        return b

    if median_size and size > median_size * 1.15 and len(text) < 80:
        return Block("heading", line.page, text, _bbox(line))

    return Block("text", line.page, text, _bbox(line))


def extract_content(pdf_path: str, *,
                    table_regions: dict[int, tuple[float, float]] | None = None,
                    max_pages: int = 100) -> dict[str, Any]:
    """
    Read every line of the document into typed blocks.

    table_regions maps page number -> (top, bottom) of a region already parsed
    as a table, so those lines are not duplicated as loose text. The table
    itself is injected by the caller, which knows its structure.
    """
    table_regions = table_regions or {}
    blocks: list[Block] = []
    page_meta: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages[:max_pages]):
            geom, words = measure_page(page, pno)
            lines = cluster_lines(words, geom)
            region = table_regions.get(pno)

            kept = 0
            for line in lines:
                text = line.text.strip()
                if not text:
                    continue
                if region and region[0] <= line.top <= region[1]:
                    continue                      # belongs to the parsed table
                if _FURNITURE.match(text):
                    blocks.append(Block("footer", pno, text, _bbox(line)))
                    continue
                blocks.append(_classify(line, geom.median_font_size))
                kept += 1

            page_meta.append({"page": pno, "lines": len(lines), "blocks": kept})

    key_values: dict[str, str] = {}
    for b in blocks:
        if b.kind != "key_value":
            continue
        pairs = (b.table or {}).get("pairs") if b.table else None
        for k, v in (pairs and [(p["label"], p["value"]) for p in pairs]) or [(b.label, b.value)]:
            if k and v and k not in key_values:
                key_values[k] = v

    return {"blocks": blocks, "key_values": key_values, "pages": page_meta}


# ---------------------------------------------------------------------------
# Renderings
# ---------------------------------------------------------------------------

def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|")


def render_table_md(columns: list[str], rows: list[dict[str, Any]],
                    title: str | None = None) -> str:
    if not rows:
        return ""
    out = [f"### {title}"] if title else []
    out.append("| " + " | ".join(_md_escape(c) for c in columns) + " |")
    out.append("|" + "|".join("---" for _ in columns) + "|")
    for r in rows:
        out.append("| " + " | ".join(
            _md_escape("" if r.get(c) is None else str(r.get(c)))
            for c in columns) + " |")
    return "\n".join(out)


def render_markdown(*, title: str | None, blocks: list[Block],
                    tables: list[dict[str, Any]],
                    fields: dict[str, Any] | None = None) -> str:
    """
    Render the document as markdown for an LLM to read.

    Ordering is deliberate: identity first, then labelled fields as a compact
    table, then the data tables, then remaining prose. An agent answering
    "what is the closing balance for supplier X?" should not have to hunt.
    """
    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")

    headings = [b for b in blocks if b.kind == "heading"]
    if headings:
        parts.append("_" + " · ".join(h.text for h in headings[:3]) + "_\n")

    # Profile-extracted fields are typed and validated, so they lead. The
    # raw labelled fields follow for completeness -- an agent asked something
    # the profile does not model can still find it.
    if fields:
        typed = [(k, v) for k, v in fields.items() if v not in (None, "")]
        if typed:
            parts.append("## Key fields\n")
            parts.append("| Field | Value |")
            parts.append("|---|---|")
            parts.extend(f"| {_md_escape(k)} | {_md_escape(str(v))} |"
                         for k, v in typed)
            parts.append("")

    kv_rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for b in blocks:
        if b.kind != "key_value":
            continue
        pairs = (b.table or {}).get("pairs") if b.table else None
        items = ([(p["label"], p["value"]) for p in pairs] if pairs
                 else [(b.label, b.value)])
        for k, v in items:
            if k and k not in seen:
                seen.add(k)
                kv_rows.append((k, v or ""))

    if kv_rows:
        parts.append("## All document fields (as printed)\n"
                     if fields else "## Document fields\n")
        parts.append("| Field | Value |")
        parts.append("|---|---|")
        parts.extend(f"| {_md_escape(k)} | {_md_escape(v)} |" for k, v in kv_rows)
        parts.append("")

    for t in tables:
        md = render_table_md(t["columns"], t["rows"], t.get("title"))
        if md:
            parts.append("## " + t.get("title", "Table") + "\n" if not t.get("title")
                         else "")
            parts.append(md)
            parts.append("")

    prose = [b.text for b in blocks if b.kind == "text"]
    if prose:
        parts.append("## Other content\n")
        parts.extend(prose)
        parts.append("")

    return "\n".join(p for p in parts if p is not None).strip() + "\n"


def render_text(blocks: list[Block]) -> str:
    return "\n".join(b.text for b in blocks if b.text)


def chunk_markdown(markdown: str, *, max_chars: int = 1800,
                   overlap: int = 150) -> list[dict[str, Any]]:
    """
    Split the markdown at heading boundaries for retrieval.

    Only needed for long documents. A one-page statement fits in a prompt
    whole, and feeding it whole beats retrieving pieces of it -- so the
    consumer should prefer `markdown` and fall back to chunks by length.
    """
    sections = re.split(r"\n(?=#{1,3} )", markdown)
    chunks: list[dict[str, Any]] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        heading = sec.splitlines()[0].lstrip("# ").strip() if sec.startswith("#") else None
        if len(sec) <= max_chars:
            chunks.append({"index": len(chunks), "heading": heading, "text": sec})
            continue
        start = 0
        while start < len(sec):
            piece = sec[start:start + max_chars]
            chunks.append({"index": len(chunks), "heading": heading, "text": piece})
            start += max_chars - overlap
    return chunks
