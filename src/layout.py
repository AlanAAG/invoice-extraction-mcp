"""
layout.py — Coordinate-based table reconstruction for text-layer PDFs.

SCOPE: geometry only. This module reads a PDF and returns rows of text with
their source coordinates. It knows nothing about suppliers, invoices, or
databases. Document semantics live in profiles; persistence lives in whatever
workflow node comes next.

The problem
-----------
Report generators (Crystal Reports, SAP B1, Jasper, SSRS) wrap cell content
that overflows a fixed column width onto a second visual line. Flat text
extraction turns

    PU 131365 SI/08781/CN/ 31.05.25 ... -43,160.250
    00007

into two unrelated "rows". Because the header tells us the x-band of every
column, the orphan fragment reattaches to the correct cell.

Design rules, each one a bug we already hit
-------------------------------------------
* Row boundaries come from an ANCHOR pattern, never from vertical spacing.
  Documents wrap zero, one, or five lines; the engine must not care.
* Continuations absorb until the next anchor or the stop pattern. No absolute
  distance guard -- that silently dropped deep wraps.
* Column bands derive per page from that page's own header, so nothing is
  pinned to a pixel and per-page drift is absorbed.
* Line-clustering tolerance is derived from the document's own font size,
  not hardcoded, so it survives a report rendered at a different scale.
* WORD COVERAGE is the core invariant: every word inside the table region
  must land in exactly one cell. This is the net under the failure that
  arithmetic cannot see -- a mangled reference number still cross-foots.
* Every cell keeps its source words, so callers get bounding boxes. A
  downstream redaction step needs geometry, not just text.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

import pdfplumber


class HeaderMismatch(ValueError):
    """
    The page has a table header, but it does not carry the columns this
    profile expects -- the report format has changed (a column added,
    removed, or renamed).

    Carries the header as actually printed so the caller can tell an operator
    exactly what to change, instead of surfacing a bare traceback.
    """

    def __init__(self, missing: str, actual: list[str], page: int):
        self.missing = missing
        self.actual = actual
        self.page = page
        super().__init__(
            f"Profile expects a column named {missing!r}, which is not on "
            f"page {page}. The header actually reads: {actual!r}."
        )


# Fallback when a document is too sparse to measure. Crystal renders the
# right-most column ~0.25pt above the rest of the row, so tolerance 0 splits
# every row in two.
DEFAULT_Y_TOLERANCE = 3.0

# Not a drop threshold -- a flag. A row absorbing more lines than this is
# reported, never truncated.
MAX_LINES_PER_ROW = 8

# A row carrying content in this fraction of its columns or fewer is
# suspicious: usually a continuation fragment that happened to match the
# anchor pattern and was mistaken for a new row.
SPARSE_ROW_RATIO = 0.34

# Character-offset below which two identical glyphs are the same glyph drawn
# twice (faux-bold / shadow), not two real characters.
DUPLICATE_TOLERANCE = 1.0


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int
    size: float = 0.0
    # Per-glyph (x0, x1), kept so a word that turns out to span two columns can
    # be cut at a real glyph gap rather than at a guessed character offset.
    chars: tuple[tuple[float, float], ...] = ()

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    top: float
    words: list[Word]
    page: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))


@dataclass
class PageGeometry:
    page: int
    width: float
    height: float
    median_font_size: float
    y_tolerance: float
    duplicate_glyphs_removed: int = 0


@dataclass
class Column:
    name: str
    x0: float
    x1: float
    left: float = 0.0
    right: float = 0.0


@dataclass
class Row:
    cells: dict[str, str]
    cell_words: dict[str, list[Word]]
    page: int
    pages: list[int] = field(default_factory=list)
    wrapped_lines: int = 0
    tops: list[float] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)
    near_page_bottom: bool = False

    @property
    def provenance(self) -> dict[str, dict[str, Any]]:
        """
        Per-field source geometry. Enables a downstream node to redact,
        highlight, or visually verify a value without re-parsing the PDF.
        """
        out: dict[str, dict[str, Any]] = {}
        for name, words in self.cell_words.items():
            if not words:
                continue
            out[name] = {
                "page": words[0].page,
                "bbox": [round(min(w.x0 for w in words), 2),
                         round(min(w.top for w in words), 2),
                         round(max(w.x1 for w in words), 2),
                         round(max(w.bottom for w in words), 2)],
                "wrapped": len({round(w.top, 1) for w in words}) > 1,
            }
        return out


@dataclass
class TableResult:
    rows: list[Row]
    columns: list[Column]
    orphan_lines: list[dict[str, Any]]
    unassigned_words: list[dict[str, Any]]
    words_in_table_region: int
    words_claimed: int
    pages_parsed: list[int]
    pages_without_header: list[int]
    overlong_rows: int
    unmapped_headers: list[str]
    pages_without_repeated_header: list[int]
    column_fill: dict[str, float]
    geometry: list[dict[str, Any]]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Page measurement and line clustering
# ---------------------------------------------------------------------------

def measure_page(page, page_number: int) -> tuple[PageGeometry, list[Word]]:
    """
    Read a page's words and derive its own clustering tolerance.

    Deriving tolerance from the median glyph size rather than hardcoding it
    means the engine survives the same report rendered at a different scale
    (A4 vs Letter, 100% vs 90% zoom), which a fixed 3.0pt would not.
    """
    # Faux-bold and drop-shadow are produced by drawing the same string twice
    # a fraction of a point apart. pdfplumber reports both, and at small
    # offsets it INTERLEAVES the characters -- "REF-A" becomes "RREEFF--AA",
    # which no word-level filter can repair. Deduplicating at the character
    # layer, before words are assembled, is the only correct place.
    before = len(page.chars)
    page = page.dedupe_chars(tolerance=DUPLICATE_TOLERANCE)
    dupes = before - len(page.chars)

    raw = page.extract_words(use_text_flow=False, keep_blank_chars=False,
                             extra_attrs=["size"], return_chars=True)
    words = [
        Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"],
             page_number, float(w.get("size") or 0.0),
             tuple((c["x0"], c["x1"]) for c in w.get("chars") or ()))
        for w in raw
    ]
    words.sort(key=lambda w: (w.top, w.x0))

    sizes = [w.size for w in words if w.size > 0]
    median_size = statistics.median(sizes) if sizes else 0.0
    # ~40% of a glyph height: comfortably above sub-point baseline jitter,
    # far below any real row pitch.
    tol = round(median_size * 0.4, 2) if median_size else DEFAULT_Y_TOLERANCE
    tol = max(1.5, min(tol, 6.0))

    return (PageGeometry(page_number, page.width, page.height,
                         median_size, tol, dupes), words)


def cluster_lines(words: list[Word], geom: PageGeometry) -> list[Line]:
    lines: list[Line] = []
    for w in words:
        if lines and abs(w.top - lines[-1].top) <= geom.y_tolerance:
            lines[-1].words.append(w)
        else:
            lines.append(Line(top=w.top, words=[w], page=w.page))
    return lines


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _natural_gap_threshold(ws: list[Word]) -> float:
    """
    Split intra-label spacing from inter-column spacing using the line's own
    gaps, rather than a fixed magic number.

    Words inside one label ("Balance Due") sit a space apart; separate labels
    sit a column gutter apart. Walk the sorted gaps upward and cut at the
    FIRST significant jump -- that is the boundary between the two.

    Not the widest jump anywhere: on a real statement header the gaps run
    [2.1, 2.1, 2.1, 2.1, 20.7, 24.3, 32.7, 36.0, 63.0, 136.7], where the
    widest jump is 63 -> 136.7 and cutting there merges every column into one
    cell. The meaningful boundary is the first one, 2.1 -> 20.7.

    The result is then capped by typography. A space inside a label is a
    fraction of the type size, so a gap wider than ~1.2x the font size is a
    gutter no matter what the distribution says. Without that cap, a header
    of single-word labels ("Doc Reference Date Amount") has no small cluster
    at all -- every gap is a gutter -- and any distribution-only rule happily
    merges the whole header into one cell.
    """
    gaps = sorted(g for g in
                  (ws[i + 1].x0 - ws[i].x1 for i in range(len(ws) - 1))
                  if g >= 0)
    if not gaps:
        return 8.0

    sizes = [w.size for w in ws if w.size > 0]
    cap = (max(sizes) * 1.2) if sizes else 8.0

    threshold = gaps[-1] + 1.0
    for i in range(len(gaps) - 1):
        lo, hi = gaps[i], gaps[i + 1]
        if hi > lo * 2.0 and (hi - lo) >= 2.0:
            threshold = (lo + hi) / 2
            break

    return min(threshold, cap)


def merge_header_cells(line: Line, gap: float | None = None) -> list[Word]:
    """Multi-word header labels ('BP Ref. No.') merge by horizontal proximity."""
    ws = sorted(line.words, key=lambda w: w.x0)
    if gap is None:
        gap = _natural_gap_threshold(ws)

    out: list[Word] = []
    for w in ws:
        if out and (w.x0 - out[-1].x1) < gap:
            p = out[-1]
            out[-1] = Word(f"{p.text} {w.text}", p.x0, w.x1, p.top,
                           max(p.bottom, w.bottom), p.page, p.size)
        else:
            out.append(Word(w.text, w.x0, w.x1, w.top, w.bottom, w.page, w.size))
    return out


def find_header_line(lines: list[Line], header_spec: list[str],
                     min_hits: int = 3) -> tuple[int | None, Line | None]:
    targets = [_norm(h) for h in header_spec]
    best: tuple[int, int, Line] | None = None
    for i, line in enumerate(lines):
        blob = _norm(line.text)
        hits = sum(1 for t in targets if t in blob)
        if hits >= min_hits and (best is None or hits > best[0]):
            best = (hits, i, line)
    return (best[1], best[2]) if best else (None, None)


def build_columns(header_line: Line, header_spec: list[str],
                  page_width: float) -> tuple[list[Column], list[str]]:
    """
    Band edges sit at the midpoint of the whitespace between header cells,
    because data overhangs its header on both sides: right-aligned amounts
    start left of the 'Amount' label, long 'Details' text runs right of it.
    """
    cells = merge_header_cells(header_line)
    matched: set[int] = set()

    cols: list[Column] = []
    for label in header_spec:
        match = next((c for c in cells if _norm(c.text) == _norm(label)), None)
        if match is None:
            match = next((c for c in cells
                          if _norm(label) in _norm(c.text)
                          or _norm(c.text) in _norm(label)), None)
        if match is None:
            raise HeaderMismatch(label, [c.text for c in cells],
                                 header_line.page)
        cols.append(Column(name=label, x0=match.x0, x1=match.x1))
        matched.add(id(match))

    cols.sort(key=lambda c: c.x0)
    for i, c in enumerate(cols):
        c.left = 0.0 if i == 0 else (cols[i - 1].x1 + c.x0) / 2
        c.right = page_width if i == len(cols) - 1 else (c.x1 + cols[i + 1].x0) / 2

    unmapped = [c.text for c in cells if id(c) not in matched]
    return cols, unmapped


# A cell that ends flush against the next column leaves a gap narrower than
# pdfplumber's word tolerance (~3pt at 8pt type), so the two cells arrive glued
# into one word: "FC-SIN-26/119" + "30.04.26" -> "FC-SIN-26/11930.04.26".
# Real inter-glyph gaps inside a word are 0, so anything above this is a space.
MIN_GLYPH_GAP = 0.8


def split_straddling_words(lines: list[Line], cols: list[Column]) -> int:
    """
    Cut words that span a column boundary at an internal glyph gap.

    Only words that actually cross a band edge are touched, so a document
    whose columns are cleanly separated is left exactly as it was.
    """
    edges = [c.left for c in cols[1:]]
    if not edges:
        return 0
    split_count = 0
    for line in lines:
        out: list[Word] = []
        for w in line.words:
            pieces = _split_word(w, edges)
            split_count += len(pieces) - 1
            out.extend(pieces)
        line.words = out
    return split_count


def _split_word(w: Word, edges: list[float]) -> list[Word]:
    if len(w.chars) < 2 or not any(w.x0 < e < w.x1 for e in edges):
        return [w]
    cuts = [i for i in range(1, len(w.chars))
            if w.chars[i][0] - w.chars[i - 1][1] >= MIN_GLYPH_GAP]
    if not cuts:
        return [w]                      # genuinely one word; leave it alone
    pieces, start = [], 0
    for cut in cuts + [len(w.chars)]:
        text = w.text[start:cut]
        if not text.strip():
            start = cut
            continue
        pieces.append(Word(text, w.chars[start][0], w.chars[cut - 1][1],
                           w.top, w.bottom, w.page, w.size,
                           w.chars[start:cut]))
        start = cut
    return pieces or [w]


def refine_bands(lines: list[Line], cols: list[Column]) -> list[str]:
    """
    Move a band edge that still cuts through a word into real whitespace.

    Edges start at the midpoint between header labels, which assumes the data
    sits under its own label. A wide prose column breaks that: 'Details' text
    overhangs far enough right to cross into 'Amount', and the amount is then
    lost. The columns' own content shows where the true corridor is.
    """
    spans = sorted((w.x0, w.x1) for line in lines for w in line.words)
    if not spans:
        return []
    covered: list[list[float]] = []
    for x0, x1 in spans:
        if covered and x0 <= covered[-1][1]:
            covered[-1][1] = max(covered[-1][1], x1)
        else:
            covered.append([x0, x1])

    moved = []
    for i in range(1, len(cols)):
        edge = cols[i].left
        if not any(a < edge < b for a, b in covered):
            continue                    # already in clear space
        gaps = [(covered[j][1], covered[j + 1][0])
                for j in range(len(covered) - 1)
                if covered[j + 1][0] - covered[j][1] > 0]
        if not gaps:
            continue
        lo, hi = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - edge))
        new_edge = (lo + hi) / 2
        # Never reorder columns.
        if not (cols[i - 1].left < new_edge < cols[i].right):
            continue
        cols[i - 1].right = cols[i].left = new_edge
        moved.append(f"{cols[i - 1].name}|{cols[i].name}: "
                     f"{round(edge, 1)} -> {round(new_edge, 1)}")
    return moved


def assign(line: Line, cols: list[Column]) -> dict[str, list[Word]]:
    """Bucket words by horizontal centre. Bands are contiguous, so all land."""
    out: dict[str, list[Word]] = {c.name: [] for c in cols}
    for w in sorted(line.words, key=lambda w: w.x0):
        for c in cols:
            if c.left <= w.cx < c.right:
                out[c.name].append(w)
                break
    return out


# ---------------------------------------------------------------------------
# Row reconstruction
# ---------------------------------------------------------------------------

def _absorb(row: Row, buckets: dict[str, list[Word]], cols: list[Column],
            join_with: str, line: Line) -> None:
    for c in cols:
        words = buckets[c.name]
        if not words:
            continue
        frag = " ".join(w.text for w in words).strip()
        existing = row.cells.get(c.name, "")
        row.cells[c.name] = (existing + join_with + frag) if existing else frag
        row.cell_words.setdefault(c.name, []).extend(words)
    row.wrapped_lines += 1
    row.tops.append(line.top)
    if line.page not in row.pages:
        row.pages.append(line.page)


def _build_page_rows(lines: list[Line], cols: list[Column], *,
                     anchor_column: str, anchor_pattern: str,
                     stop_pattern: str | None, join_with: str,
                     carry: Row | None, stitch_page_breaks: bool,
                     page_height: float = 842.0):
    """
    Parse one page's table region.

    `carry` is the previous page's last row, so a cell that wrapped across the
    page break can still be completed instead of surfacing as an orphan.
    """
    anchor_re = re.compile(anchor_pattern)
    stop_re = re.compile(stop_pattern) if stop_pattern else None

    rows: list[Row] = []
    orphans: list[dict[str, Any]] = []
    claimed: set[int] = set()
    region: list[Word] = []
    overlong = 0
    stitches = 0
    current: Row | None = None
    stopped = False

    for line in lines:
        buckets = assign(line, cols)
        anchor_text = " ".join(w.text for w in buckets.get(anchor_column, [])).strip()

        if stop_re and stop_re.search(anchor_text):
            stopped = True
            break

        region.extend(line.words)

        if anchor_re.match(anchor_text):
            current = Row(
                cells={c.name: " ".join(w.text for w in buckets[c.name]).strip()
                       for c in cols},
                cell_words={c.name: list(buckets[c.name]) for c in cols},
                page=line.page,
                pages=[line.page],
                tops=[line.top],
            )
            current.near_page_bottom = line.top > page_height * 0.72
            rows.append(current)
            claimed.update(id(w) for w in line.words)
            continue

        if not any(buckets.values()):
            continue

        if current is None:
            # Before this page's first anchor. If a row carried over from the
            # previous page, this is almost certainly its wrapped tail.
            # Only stitch when the carried row really was cut by the page
            # break -- i.e. it sat near the bottom of the previous page.
            # Without that guard, any stray line above the first row of a
            # page would be glued onto the previous row.
            if carry is not None and stitch_page_breaks and carry.near_page_bottom:
                _absorb(carry, buckets, cols, join_with, line)
                claimed.update(id(w) for w in line.words)
                stitches += 1
                continue
            orphans.append({"page": line.page, "top": round(line.top, 1),
                            "text": line.text,
                            "reason": "before_first_row"})
            continue

        # Continuation: absorbed until the next anchor or the stop pattern.
        # No distance guard -- wrap depth varies per document and per row.
        _absorb(current, buckets, cols, join_with, line)
        current.near_page_bottom = line.top > page_height * 0.72
        claimed.update(id(w) for w in line.words)
        if current.wrapped_lines + 1 > MAX_LINES_PER_ROW:
            overlong += 1
            if "absorbed_too_many_lines" not in current.suspicious:
                current.suspicious.append("absorbed_too_many_lines")

    # Only a row running to the bottom of the page can continue on the next.
    # If the stop pattern fired, the table ended here.
    tail = None if stopped else (rows[-1] if rows else carry)

    unassigned = [
        {"page": w.page, "text": w.text, "x0": round(w.x0, 1),
         "top": round(w.top, 1)}
        for w in region if id(w) not in claimed
    ]

    return rows, orphans, overlong, tail, stitches, len(region), len(claimed), unassigned


def _rescale_bands(cols: list[Column], page_width: float) -> list[Column]:
    """
    Reuse bands on a page whose header is absent. Only the outer edges are
    adjusted, so a page of a different width (Letter after A4) still routes
    words sensibly instead of dropping the right-hand columns off the end.
    """
    out = [Column(c.name, c.x0, c.x1, c.left, c.right) for c in cols]
    if out:
        out[0].left = 0.0
        out[-1].right = max(page_width, out[-1].x1)
    return out


def _count_anchors(lines: list[Line], cols: list[Column],
                   anchor_column: str, anchor_pattern: str) -> int:
    """Dry run: does this page actually contain table rows?"""
    rx = re.compile(anchor_pattern)
    n = 0
    for line in lines:
        buckets = assign(line, cols)
        text = " ".join(w.text for w in buckets.get(anchor_column, [])).strip()
        if rx.match(text):
            n += 1
    return n


def _flag_sparse(rows: list[Row], cols: list[Column]) -> None:
    threshold = max(1, int(len(cols) * SPARSE_ROW_RATIO))
    for r in rows:
        filled = sum(1 for c in cols if r.cells.get(c.name))
        if filled <= threshold:
            r.suspicious.append("sparse_row_may_be_false_anchor")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def has_text_layer(pdf_path: str) -> bool:
    """False => scanned => needs OCR. These SAP exports return True."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            if (page.extract_text() or "").strip():
                return True
    return False


def parse_table(pdf_path: str, header_spec: list[str], *,
                anchor_column: str, anchor_pattern: str,
                stop_pattern: str | None = None,
                join_with: str = "",
                stitch_page_breaks: bool = True,
                max_pages: int = 100) -> TableResult:
    """
    Parse a table that may span several pages. The header repeats per page, so
    columns are rebuilt per page; this also absorbs small layout drift.
    """
    all_rows: list[Row] = []
    all_orphans: list[dict[str, Any]] = []
    all_unassigned: list[dict[str, Any]] = []
    geometry: list[dict[str, Any]] = []
    warnings: list[str] = []
    parsed: list[int] = []
    no_header: list[int] = []
    columns: list[Column] = []
    overlong = region_total = claimed_total = stitches = 0
    unmapped_headers: list[str] = []
    pages_without_repeated_header: list[int] = []
    carry: Row | None = None

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        if page_count > max_pages:
            warnings.append(
                f"Document has {page_count} pages; only the first {max_pages} "
                f"were parsed."
            )
        for pno, page in enumerate(pdf.pages[:max_pages]):
            geom, words = measure_page(page, pno)
            lines = cluster_lines(words, geom)
            idx, header = find_header_line(lines, header_spec)

            if header is not None:
                cols, unmapped = build_columns(header, header_spec, page.width)
                for label in unmapped:
                    if label not in unmapped_headers:
                        unmapped_headers.append(label)
                body = lines[idx + 1:]
            else:
                # Many report templates print the header ONLY on page 1 and
                # run the table straight on. Skipping those pages silently
                # dropped every row after the first page. Carry the previous
                # page's bands forward -- but only commit if the page really
                # contains rows, so an unrelated page (terms, remittance
                # advice) is not force-fitted into a table it has nothing to
                # do with.
                if not columns:
                    no_header.append(pno)
                    continue
                cols = _rescale_bands(columns, page.width)
                if _count_anchors(lines, cols, anchor_column, anchor_pattern) == 0:
                    no_header.append(pno)
                    continue
                body = lines
                pages_without_repeated_header.append(pno)

            # Repair column geometry from the data itself before assigning.
            # Restricted to the rows above the stop line, so the ageing table
            # and the page footer cannot drag a band edge around.
            region = body
            if stop_pattern:
                for k, ln in enumerate(body):
                    if re.match(stop_pattern, ln.text.strip()):
                        region = body[:k]
                        break
            splits = split_straddling_words(region, cols)
            moved = refine_bands(region, cols)
            if moved:
                # Edges shifted, so a word may straddle a boundary it did not
                # cross before.
                splits += split_straddling_words(region, cols)
            for m in moved:
                warnings.append(f"page {pno}: column edge moved into "
                                f"whitespace ({m})")

            if not columns:
                columns = cols

            (rows, orphans, ol, carry, st,
             region_n, claimed_n, unassigned) = _build_page_rows(
                body, cols,
                anchor_column=anchor_column,
                anchor_pattern=anchor_pattern,
                stop_pattern=stop_pattern,
                join_with=join_with,
                carry=carry,
                stitch_page_breaks=stitch_page_breaks,
                page_height=geom.height,
            )
            all_rows.extend(rows)
            all_orphans.extend(orphans)
            all_unassigned.extend(unassigned)
            overlong += ol
            stitches += st
            region_total += region_n
            claimed_total += claimed_n
            parsed.append(pno)
            geometry.append({
                "page": pno,
                "width": round(page.width, 1),
                "height": round(page.height, 1),
                "median_font_size": round(geom.median_font_size, 2),
                "y_tolerance": geom.y_tolerance,
                "duplicate_glyphs_removed": geom.duplicate_glyphs_removed,
                "words_split_at_column_edge": splits,
                "columns": [{"name": c.name,
                             "band": [round(c.left, 1), round(c.right, 1)]}
                            for c in cols],
            })

    if not parsed:
        raise ValueError(
            f"No page contained a header matching {header_spec!r}. Either the "
            f"profile is wrong for this document, or the PDF has no text layer."
        )

    _flag_sparse(all_rows, columns)

    if no_header:
        warnings.append(
            f"Pages {no_header} contained no recognisable header and were "
            f"skipped. If they hold table rows, those rows are missing."
        )
    if unmapped_headers:
        warnings.append(
            f"The page header contains column(s) the profile does not map: "
            f"{unmapped_headers}. Their content is being absorbed into a "
            f"neighbouring column. The report format has probably changed -- "
            f"add them to the profile."
        )
    if pages_without_repeated_header:
        warnings.append(
            f"Pages {pages_without_repeated_header} carried no header; the "
            f"previous page's column layout was reused. Their rows are "
            f"included."
        )
    if stitches:
        warnings.append(
            f"{stitches} line(s) were stitched across a page break into the "
            f"preceding row. Verify those rows."
        )

    fill: dict[str, float] = {}
    if all_rows:
        for c in columns:
            n = sum(1 for r in all_rows if r.cells.get(c.name))
            fill[c.name] = round(n / len(all_rows), 3)

    return TableResult(
        rows=all_rows,
        columns=columns,
        orphan_lines=all_orphans,
        unassigned_words=all_unassigned,
        words_in_table_region=region_total,
        words_claimed=claimed_total,
        pages_parsed=parsed,
        pages_without_header=no_header,
        overlong_rows=overlong,
        unmapped_headers=unmapped_headers,
        pages_without_repeated_header=pages_without_repeated_header,
        column_fill=fill,
        geometry=geometry,
        warnings=warnings,
    )


def probe(pdf_path: str, page_index: int = 0,
          max_lines: int = 80) -> dict[str, Any]:
    """
    Diagnostic for onboarding an unknown format: lines with word coordinates,
    so header labels and the anchor pattern can be read off rather than
    guessed.
    """
    with pdfplumber.open(pdf_path) as pdf:
        if page_index >= len(pdf.pages):
            raise ValueError(
                f"Page {page_index} does not exist; the document has "
                f"{len(pdf.pages)} page(s)."
            )
        page = pdf.pages[page_index]
        geom, words = measure_page(page, page_index)
        lines = cluster_lines(words, geom)
        return {
            "page": page_index,
            "page_count": len(pdf.pages),
            "width": round(page.width, 1),
            "height": round(page.height, 1),
            "median_font_size": round(geom.median_font_size, 2),
            "y_tolerance": geom.y_tolerance,
            "duplicate_glyphs_removed": geom.duplicate_glyphs_removed,
            "lines": [
                {
                    "top": round(l.top, 1),
                    "text": l.text,
                    "words": [{"t": w.text, "x0": round(w.x0), "x1": round(w.x1)}
                              for w in sorted(l.words, key=lambda w: w.x0)],
                }
                for l in lines[:max_lines]
            ],
        }
