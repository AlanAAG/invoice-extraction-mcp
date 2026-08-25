"""
profile.py — Declarative document profiles.

A profile is a YAML file. Adding a vendor format means adding a file to
profiles/ and restarting; it never means touching layout.py. That is the
whole point of the split: the engine knows geometry, the profile knows this
particular report.

Schema (see profiles/sap_b1_supplier_statement.yaml for a worked example):

  id, name, description
  detect:            how to recognise the document
  metadata:          regex captures from the page text above the table
  table:             column labels + anchor/stop patterns
  fields:            column -> output field, with type coercion
  summary:           optional right-aligned summary block (e.g. ageing)
  validation:        named cross-checks
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
import yaml

from .layout import Line, find_header_line, merge_header_cells

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

def to_decimal(raw: str) -> float | None:
    """'-43,160.250' -> -43160.25 ; '(1,200.00)' -> -1200.0 ; '' -> None"""
    if not raw:
        return None
    s = raw.replace(",", "").replace(" ", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


_DATE_PATTERNS = {
    "DMY_DOT":   (r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", ("d", "m", "y")),
    "DMY_SLASH": (r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$",   ("d", "m", "y")),
    "MDY_SLASH": (r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$",   ("m", "d", "y")),
    "YMD_DASH":  (r"^(\d{4})-(\d{1,2})-(\d{1,2})$",     ("y", "m", "d")),
}


def to_date(raw: str, fmt: str = "DMY_DOT", century_pivot: int = 70) -> str | None:
    """Returns ISO 8601. Two-digit years below the pivot are 20xx."""
    if not raw:
        return None
    pattern, order = _DATE_PATTERNS.get(fmt, _DATE_PATTERNS["DMY_DOT"])
    m = re.match(pattern, raw.strip())
    if not m:
        return None
    parts = dict(zip(order, m.groups()))
    y = int(parts["y"])
    if y < 100:
        y += 2000 if y < century_pivot else 1900
    try:
        return date(y, int(parts["m"]), int(parts["d"])).isoformat()
    except ValueError:
        return None


def coerce(raw: str, spec: dict[str, Any]) -> Any:
    kind = spec.get("type", "text")
    raw = (raw or "").strip()

    if kind == "token":
        parts = raw.split()
        i = spec.get("index", 0)
        return parts[i] if i < len(parts) else None
    if kind == "decimal":
        return to_decimal(raw)
    if kind == "date":
        return to_date(raw, spec.get("format", "DMY_DOT"))
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return None
    return raw or None


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class Profile:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.raw = data
        self.id: str = data["id"]
        self.name: str = data.get("name", self.id)
        self.description: str = data.get("description", "")
        self.detect_spec: dict = data.get("detect", {})
        self.metadata_spec: dict = data.get("metadata", {})
        self.table: dict = data["table"]
        self.fields: list[dict] = data.get("fields", [])
        self.summary_spec: dict = data.get("summary", {})
        self.validation_spec: list[dict] = data.get("validation", [])

    # -- detection ---------------------------------------------------------

    def score(self, first_page_text: str, metadata: dict) -> dict[str, Any]:
        """
        Confidence in [0,1] from cheap signals. Kept transparent so a
        misrouted document can be diagnosed from the response alone.
        """
        text = first_page_text.lower()
        signals: dict[str, bool] = {}

        for phrase in self.detect_spec.get("text_contains", []):
            signals[f"text:{phrase}"] = phrase.lower() in text
        for pattern in self.detect_spec.get("text_matches", []):
            signals[f"regex:{pattern}"] = bool(re.search(pattern, first_page_text))
        for key, expected in (self.detect_spec.get("pdf_metadata") or {}).items():
            actual = str(metadata.get(key, "")).lower()
            signals[f"meta:{key}"] = expected.lower() in actual

        if not signals:
            return {"confidence": 0.0, "signals": {}}

        required = self.detect_spec.get("require", [])
        for phrase in required:
            if phrase.lower() not in text:
                return {"confidence": 0.0, "signals": signals,
                        "failed_required": phrase}

        conf = sum(signals.values()) / len(signals)
        return {"confidence": round(conf, 2), "signals": signals}

    # -- metadata ----------------------------------------------------------

    def extract_metadata(self, lines: list[Line]) -> dict[str, Any]:
        # Reconstructed line text joins words with single spaces, so how a
        # label tokenises is a rendering detail: "BP: From" and "BP : From"
        # are the same label. Normalising space-before-punctuation keeps
        # profile regexes from being brittle to that. Learned the hard way --
        # a tolerance change silently broke every label pattern.
        blob = "\n".join(l.text for l in lines)
        blob = re.sub(r"\s+([:#,])", r"\1", blob)
        blob = re.sub(r"[ \t]{2,}", " ", blob)

        out: dict[str, Any] = {}
        for name, spec in self.metadata_spec.items():
            # MULTILINE by default so ^/$ anchor to visual lines, not the blob.
            # IGNORECASE is opt-in: patterns like the ALL-CAPS supplier name
            # rely on case to discriminate.
            flags = re.MULTILINE
            if spec.get("ignore_case"):
                flags |= re.IGNORECASE
            m = re.search(spec["pattern"], blob, flags)
            value = m.group(1).strip() if m else None
            if value is not None and spec.get("strip_prefix_field"):
                prefix = out.get(spec["strip_prefix_field"])
                if prefix and value.startswith(str(prefix)):
                    value = value[len(str(prefix)):].strip(" -:")
            out[name] = coerce(value, spec) if value else None
        return out

    # -- summary block (e.g. ageing buckets) -------------------------------

    def extract_summary(self, lines: list[Line]) -> dict[str, Any]:
        """
        Summary figures are right-aligned under their headers, so each value
        maps to the header whose RIGHT edge is nearest. Centre-matching gets
        this wrong when a value is wider than its label.
        """
        if not self.summary_spec:
            return {}

        labels = self.summary_spec["columns"]
        idx, header = find_header_line(lines, labels, min_hits=self.summary_spec.get("min_hits", 4))
        if header is None:
            return {}

        # Gap is derived from the line's own spacing unless a profile
        # explicitly overrides it; a fixed value is scale-dependent.
        cells = merge_header_cells(header, gap=self.summary_spec.get("merge_gap"))

        def nearest(x1: float) -> str:
            return min(cells, key=lambda c: abs(c.x1 - x1)).text

        out: dict[str, dict[str, Any]] = {}
        for row_spec in self.summary_spec.get("rows", []):
            key = row_spec["name"]
            row_re = re.compile(row_spec["match"])
            val_re = re.compile(row_spec.get("value_pattern", r"^-?[\d,]+\.\d+$"))
            out[key] = {}
            for line in lines[idx + 1: idx + 1 + self.summary_spec.get("scan_lines", 6)]:
                if not row_re.search(line.text.strip()):
                    continue
                for w in sorted(line.words, key=lambda w: w.x0):
                    if val_re.match(w.text):
                        out[key][nearest(w.x1)] = to_decimal(w.text)
                break
        return out

    # -- row mapping -------------------------------------------------------

    def map_row(self, cells: dict[str, str], line_no: int) -> dict[str, Any]:
        rec: dict[str, Any] = {"line_no": line_no}
        for spec in self.fields:
            rec[spec["name"]] = coerce(cells.get(spec["source"], ""), spec)
        return rec

    # -- validation --------------------------------------------------------

    def validate(self, records: list[dict], summary: dict,
                 table: Any) -> dict[str, Any]:
        """
        Two independent families of check.

        ARITHMETIC checks confirm the numbers we read reproduce the numbers
        the document printed. They catch a dropped or misread FIGURE.

        STRUCTURAL checks confirm the reconstruction consumed the page. They
        catch what arithmetic cannot: a mangled reference number still
        cross-foots perfectly. Word coverage is the strongest of these --
        every word inside the table region must land in exactly one cell.
        """
        orphans = table.orphan_lines
        overlong = table.overlong_rows
        checks: list[dict[str, Any]] = []

        for rule in self.validation_spec:
            kind = rule["type"]
            tol = rule.get("tolerance", 0.01)

            if kind == "sum_equals_last":
                vals = [r.get(rule["sum_field"]) for r in records]
                vals = [v for v in vals if v is not None]
                total = round(sum(vals), 3)
                last = records[-1].get(rule["equals_field"]) if records else None
                ok = last is not None and abs(total - last) < tol
                checks.append({"check": kind, "passed": ok,
                               "sum": total, "expected": last})

            elif kind == "running_balance_chain":
                # Stronger than the total. A running balance that fails to
                # advance by its own row's amount pins the fault to ONE line,
                # instead of only telling you the document does not foot.
                # Also catches reordered or duplicated rows, which a sum
                # cannot see at all.
                amt_f, bal_f = rule["amount_field"], rule["balance_field"]
                breaks: list[dict[str, Any]] = []
                prev = float(rule.get("opening_balance", 0.0))
                for r in records:
                    a, b = r.get(amt_f), r.get(bal_f)
                    if a is None or b is None:
                        breaks.append({"line_no": r["line_no"],
                                       "reason": "missing_value"})
                        prev = b if b is not None else prev
                        continue
                    if abs((prev + a) - b) >= tol:
                        breaks.append({"line_no": r["line_no"],
                                       "expected": round(prev + a, 3),
                                       "found": b})
                    prev = b
                checks.append({"check": kind, "passed": not breaks,
                               "breaks": breaks[:10], "break_count": len(breaks)})

            elif kind == "field_matches":
                bad = [
                    {"line_no": r["line_no"], "value": r.get(rule["field"])}
                    for r in records
                    if r.get(rule["field"]) is not None
                    and not re.match(rule["pattern"], str(r[rule["field"]]))
                ]
                checks.append({"check": f"{kind}:{rule['field']}",
                               "passed": not bad,
                               "violations": bad[:10], "count": len(bad)})

            elif kind == "summary_equals_last":
                got = (summary.get(rule["summary_row"], {}) or {}).get(rule["summary_column"])
                last = records[-1].get(rule["equals_field"]) if records else None
                ok = got is not None and last is not None and abs(got - last) < tol
                checks.append({"check": kind, "passed": ok,
                               "summary_value": got, "expected": last})

            elif kind == "required_fields":
                missing = [
                    {"line_no": r["line_no"], "field": f}
                    for r in records for f in rule["fields"] if r.get(f) is None
                ]
                checks.append({"check": kind, "passed": not missing,
                               "missing": missing[:10],
                               "missing_count": len(missing)})

        # -- structural checks: always run, independent of the profile -------

        # Every word in the table region must end up in exactly one cell.
        # This is the net under text-field corruption.
        total = table.words_in_table_region
        claimed = table.words_claimed
        checks.append({
            "check": "word_coverage",
            "passed": total == claimed,
            "words_in_region": total,
            "words_claimed": claimed,
            "unclaimed": total - claimed,
        })

        checks.append({"check": "no_orphan_lines", "passed": not orphans,
                       "orphan_count": len(orphans), "orphans": orphans[:5]})

        checks.append({"check": "no_unassigned_words",
                       "passed": not table.unassigned_words,
                       "count": len(table.unassigned_words),
                       "sample": table.unassigned_words[:5]})

        suspicious = [
            {"line_no": i, "reasons": r.suspicious}
            for i, r in enumerate(table.rows, start=1) if r.suspicious
        ]
        checks.append({"check": "no_suspicious_rows", "passed": not suspicious,
                       "rows": suspicious[:5]})

        checks.append({"check": "no_overlong_rows", "passed": overlong == 0,
                       "count": overlong})

        # A column present on the page but absent from the profile has its
        # content absorbed into a neighbouring cell. Arithmetic may still
        # foot, so this must fail loudly rather than pass quietly.
        unmapped = getattr(table, "unmapped_headers", [])
        checks.append({"check": "all_header_columns_mapped",
                       "passed": not unmapped,
                       "unmapped": unmapped})

        checks.append({"check": "rows_found", "passed": bool(records),
                       "count": len(records)})

        return {"ok": all(c["passed"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class Registry:
    def __init__(self, directory: Path = PROFILE_DIR):
        self.directory = Path(directory)
        self.profiles: dict[str, Profile] = {}
        self.load_errors: list[dict[str, str]] = []
        self.reload()

    def reload(self) -> None:
        self.profiles.clear()
        self.load_errors.clear()
        if not self.directory.is_dir():
            return
        for f in sorted(self.directory.glob("*.y*ml")):
            try:
                data = yaml.safe_load(f.read_text())
                p = Profile(data, f)
                self.profiles[p.id] = p
            except Exception as exc:
                self.load_errors.append({"file": f.name, "error": str(exc)})

    def get(self, profile_id: str) -> Profile | None:
        return self.profiles.get(profile_id)

    def match(self, pdf_path: str) -> list[dict[str, Any]]:
        """Rank profiles against a document. Highest confidence first."""
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""
            meta = pdf.metadata or {}

        results = []
        for p in self.profiles.values():
            s = p.score(text, meta)
            if s["confidence"] > 0:
                results.append({"profile": p.id, "name": p.name, **s})
        results.sort(key=lambda r: -r["confidence"])
        return results
