#!/usr/bin/env python3
"""Extract register definitions from a born-digital TI register document.

Reads the PDF text layer directly (pdfplumber), so the result is exact and
repeatable. It serves as the reference that validate_registers.py compares a
registers JSON against. It handles TI's standard register-description template,
used by both the TPS25751 technical reference manual and the BQ25798 datasheet:

  <section> <Name> Register (Offset = <hex>h) [Reset = <hex>h | X]
  Table N-M. <Name> Register Field Descriptions
  | Bit | Field | Type | Reset | [Notes] | Description |

plus the register summary table (columns Offset / Acronym / Register Name).

Usage:
  uv run --group tools tools/extract_registers.py DOC.pdf [-o OUT.json] [--pages 12-61]
  uv run --group tools tools/extract_registers.py DOC.pdf --manifest devices/<d>/device.json -o OUT.json

With --manifest, the output is a registers JSON ready to serve: the summary table
is dropped and "device" is added when the device's schema has that property.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}

# "SLVUCR8B – SEPTEMBER 2023 – REVISED AUGUST 2026" (the dashes are en dashes).
LIT_RE = re.compile(
    r"\b(S[A-Z]{3}[A-Z0-9]{3,})\s*[–-]\s*([A-Z]+)\s+(\d{4})"
    r"(?:\s*[–-]\s*REVISED\s+([A-Z]+)\s+(\d{4}))?")

# The name may itself contain "Register" ("Command Register for I2C1 (Offset = 8h)"),
# and long reset values wrap over several lines.
HEADING_RE = re.compile(
    r"(?P<name>[^\n]*?)\s*\(Offset\s*=\s*(?P<offset>[0-9A-Fa-f]+h)\)\s*"
    r"\[[Rr]eset\s*=\s*(?P<reset>X|[0-9A-Fa-f\s]+h)\]")
SECTION_NUM_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+")
ENUM_RE = re.compile(r"^\s*([0-9A-Fa-f]+h|[01]+b)\s*=\s*(.*)$")
# Lines of TI's per-register template that are not register prose.
BOILERPLATE_RE = re.compile(
    r"^(?:(?:Table|Figure)\s+\d+-\d+\..*|.*\bis shown in (?:Figure|Table)\b.*"
    r"|Return to the Summary Table\.?|\d+-\d+\.|.*\(Offset\s*=.*|[0-9A-Fa-f]*h?\])$")

FIELD_HEADER = ["bit", "field", "type", "reset"]


def _cell(text: str | None) -> str:
    return (text or "").strip()


def _join(text: str) -> str:
    """Collapse the line breaks pdfplumber keeps inside table cells."""
    # A hyphen at a line break joins a compound ("Non-" + "Liquid"), so keep it tight.
    text = re.sub(r"(?<=\w)-\n\s*(?=\w)", "-", text)
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _join_name(text: str) -> str:
    """Join a wrapped field-name cell. 'AC2_PRESENT_MA' + 'SK' is one identifier;
    'I2C Controller' + 'NACked' is two words."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1 and all(re.fullmatch(r"[A-Z0-9_.:]+", l) for l in lines):
        return "".join(lines)
    return _join(text)


def normalize_bits(bits: str) -> str:
    """'82' -> '82-82', '15:8' -> '15-8', '7-0' unchanged."""
    bits = bits.replace(":", "-").replace(" ", "")
    if "-" not in bits:
        return f"{bits}-{bits}"
    return bits


@dataclass
class Register:
    name: str
    offset: str
    reset: str
    source_page: int
    description_lines: list[str] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        width = max((int(f["bit_field"].split("-")[0]) for f in self.fields), default=-1) + 1
        out: dict[str, Any] = {
            "offset": self.offset,
            "name": self.name,
            "width": width,
            "reset": self.reset,
        }
        desc = _join(" ".join(self.description_lines))
        if desc:
            out["description"] = desc
        out["source_page"] = self.source_page
        out["fields"] = self.fields
        return out


def document_info(pdf: pdfplumber.PDF) -> dict[str, Any]:
    """Literature number, revision letter, revision date and title."""
    text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    info: dict[str, Any] = {}
    m = LIT_RE.search(text)
    if m:
        lit, month, year, rmonth, ryear = m.groups()
        info["id"] = lit
        rev = re.search(r"\d([A-Z])$", lit)
        info["revision"] = rev.group(1) if rev else ""
        month, year = (rmonth, ryear) if rmonth else (month, year)
        if month in MONTHS:
            info["date"] = f"{year}-{MONTHS[month]:02d}"
    title = (pdf.metadata or {}).get("Title")
    if title:
        info["title"] = str(title).strip()
    return info


ENCODING_RES = {
    "range": re.compile(r"Range\s*:\s*(\S+)"),
    "fixed_offset": re.compile(r"Fixed Offset\s*:\s*(\S+)"),
    "bit_step_size": re.compile(r"Bit Step Size\s*:\s*(\S+)"),
}


META_RE = re.compile(
    r"Type\s*:\s*\S+|POR\s*:\s*\S+(?:\s*\([0-9A-Fa-f]+[hb]\))?|Range\s*:\s*\S+"
    r"|Fixed Offset\s*:\s*\S+|Bit Step Size\s*:\s*\S+|Clamped\s+(?:High|Low)", re.I)


def split_metadata(prose: str) -> tuple[str, dict[str, Any]]:
    """Datasheets put 'Type : RW POR: 120mA (3h) Range : ... Bit Step Size : ...
    Clamped High' inside field descriptions. Return (prose without it, encoding)."""
    m = re.search(r"\s*\bType\s*:", prose)
    if not m:
        return prose, {}
    meta = prose[m.start():]
    enc: dict[str, Any] = {}
    for key, rx in ENCODING_RES.items():
        mm = rx.search(meta)
        if mm:
            enc[key] = mm.group(1)
    clamp = [c.lower() for c in re.findall(r"Clamped\s+(High|Low)", meta, re.I)]
    if clamp:
        enc["clamp"] = clamp
    # Prose can follow the metadata block ("The IBAT ADC reports ...").
    trailing = _join(META_RE.sub(" ", meta))
    return _join(prose[:m.start()] + " " + trailing), enc


def _split_description(desc: str) -> tuple[str, dict[str, str]]:
    """Separate prose from 'Nh = meaning' enumerations in a description cell."""
    # The text layer sometimes glues the reset note to the first value: 'POR: 0b0h = ...'.
    desc = re.sub(r"(POR:\s*[0-9A-Fa-f]+[bh])(?=[0-9A-Fa-f]+h\s*=)", r"\1\n", desc)
    prose: list[str] = []
    values: dict[str, str] = {}
    last_key: str | None = None
    for line in desc.splitlines():
        m = ENUM_RE.match(line)
        if m:
            last_key = m.group(1)
            values[last_key] = m.group(2).strip()
        elif last_key is not None:
            values[last_key] = (values[last_key] + " " + line.strip()).strip()
        else:
            prose.append(line)
    return _join(" ".join(prose)), {k: _join(v) for k, v in values.items()}


def _reset_sources(notes: str) -> list[str]:
    m = re.search(r"Reset by:\s*(.*)", notes, re.S)
    if not m:
        return []
    return [_join(t) for t in re.split(r"[\n,]+", m.group(1)) if t.strip()]


def _column_map(header: list[str]) -> dict[str, int] | None:
    names = [_join(c).lower() for c in header]
    if names[:4] != FIELD_HEADER or "description" not in names:
        return None
    return {n: i for i, n in enumerate(names)}


def _summary_rows(rows: list[list[str | None]]) -> list[dict[str, str]] | None:
    header = [_join(_cell(c)).lower() for c in rows[0]]
    if "offset" not in header or not ({"acronym", "register name"} & set(header)):
        return None
    oi = header.index("offset")
    ai = header.index("acronym") if "acronym" in header else header.index("register name")
    out = []
    for row in rows[1:]:
        off = _join(_cell(row[oi]))
        if re.fullmatch(r"[0-9A-Fa-f]+h", off):
            out.append({"offset": off, "name": _join(_cell(row[ai]))})
    return out


def _rows_from_words(page, table) -> list[list[str]]:
    edges = [c[0] for c in table.rows[0].cells[1:] if c]
    lines: list[tuple[float, list[str]]] = []
    for w in sorted(page.within_bbox(table.bbox).extract_words(), key=lambda w: (round(w["top"]), w["x0"])):
        col = sum(1 for e in edges if w["x0"] >= e - 1)
        if not lines or abs(lines[-1][0] - w["top"]) > 3:
            lines.append((w["top"], [""] * (len(edges) + 1)))
        cells = lines[-1][1]
        cells[col] = (cells[col] + " " + w["text"]).strip()
    rows: list[list[str]] = []
    for _, cells in lines:
        if rows and not cells[0]:
            # A wrapped name: no offset on this line, so it continues the row above.
            rows[-1] = [(a + " " + b).strip() for a, b in zip(rows[-1], cells)]
        else:
            rows.append(cells)
    return rows


def extract(pdf_path: Path, pages: tuple[int, int] | None = None) -> dict[str, Any]:
    with pdfplumber.open(pdf_path) as pdf:
        doc = document_info(pdf)
        registers: list[Register] = []
        summary: list[dict[str, str]] = []
        current: Register | None = None
        first, last = pages if pages else (1, len(pdf.pages))
        for pno in range(first, last + 1):
            page = pdf.pages[pno - 1]
            items: list[tuple[float, str, Any]] = []
            for hit in page.search(HEADING_RE.pattern, regex=True):
                m = HEADING_RE.search(hit["text"])
                if m:
                    items.append((hit["top"], "heading", (m, hit["bottom"])))
            for table in page.find_tables():
                items.append((table.bbox[1], "table", table))
            items.sort(key=lambda t: t[0])

            for idx, (top, kind, obj) in enumerate(items):
                if kind == "heading":
                    m, bottom = obj
                    name = SECTION_NUM_RE.sub("", m.group("name")).strip()
                    name = re.sub(r"\s+Register$", "", name)
                    reset = re.sub(r"\s+", "", m.group("reset"))
                    current = Register(name=name, offset=m.group("offset"),
                                       reset=reset, source_page=pno)
                    registers.append(current)
                    # Register prose sits between the heading and the next table.
                    nxt = next((t for t, k, _ in items[idx + 1:] if k == "table"), page.height - 60)
                    if nxt > bottom:
                        text = page.within_bbox((0, bottom, page.width, nxt)).extract_text() or ""
                        for line in text.splitlines():
                            line = line.strip()
                            if line and not BOILERPLATE_RE.match(line) and line != name:
                                current.description_lines.append(line)
                    continue

                rows = obj.extract(y_tolerance=5)
                if not rows:
                    continue
                if _summary_rows(rows) is not None and len(rows) > 1 and rows[1][1:] == [None] * (len(rows[1]) - 1):
                    # Summary tables have no vertical rules (and partial horizontal
                    # ones), so pdfplumber merges the columns. Rebuild the rows from
                    # the words, split at the header cells' left edges.
                    rows = _rows_from_words(page, obj)
                rows_summary = _summary_rows(rows)
                if rows_summary is not None:
                    summary.extend(rows_summary)
                    continue
                cols = _column_map([_cell(c) for c in rows[0]])
                if cols is None or current is None:
                    continue
                for row in rows[1:]:
                    get = lambda key: _cell(row[cols[key]]) if key in cols and cols[key] < len(row) else ""
                    bits, name, desc = get("bit"), get("field"), get("description")
                    if not bits:
                        # A field whose cell continued from the previous page.
                        if current.fields:
                            prev = current.fields[-1]
                            prose, values = _split_description(desc)
                            if prose:
                                prev["description"] = _join(prev.get("description", "") + " " + prose)
                            if values:
                                prev.setdefault("values", {}).update(values)
                        continue
                    prose, values = _split_description(desc)
                    prose, encoding = split_metadata(prose)
                    fld: dict[str, Any] = {
                        "bit_field": normalize_bits(bits),
                        "name": _join_name(name),
                        "access_type": _join(get("type")),
                        "reset": _join(get("reset")),
                    }
                    if prose:
                        fld["description"] = prose
                    if values:
                        fld["values"] = values
                    if encoding:
                        fld["encoding"] = encoding
                    if "notes" in cols:
                        sources = _reset_sources(get("notes"))
                        if sources:
                            fld["reset_sources"] = sources
                    current.fields.append(fld)

    return {
        "document": doc,
        "summary": summary,
        "registers": [r.to_json() for r in registers],
    }


def to_registers_json(ref: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Shape an extraction as the device's registers JSON."""
    manifest = json.loads(manifest_path.read_text())
    out: dict[str, Any] = {}
    schema_file = manifest.get("schema_file")
    if schema_file:
        schema = json.loads((manifest_path.parent / schema_file).read_text())
        if "device" in schema.get("properties", {}):
            out["device"] = manifest.get("device_name", "")
        if "register_count" in schema.get("properties", {}):
            out["register_count"] = len(ref["registers"])
    out["document"] = ref["document"]
    out["registers"] = ref["registers"]
    return out


def _parse_pages(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    a, _, b = spec.partition("-")
    return int(a), int(b or a)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="write JSON here (default: stdout)")
    ap.add_argument("--pages", help="1-based inclusive page range, e.g. 12-61 (default: all)")
    ap.add_argument("--manifest", type=Path, help="device.json; emit a registers JSON for that device")
    args = ap.parse_args()
    data = extract(args.pdf, _parse_pages(args.pages))
    summary_rows = len(data["summary"])
    if args.manifest:
        data = to_registers_json(data, args.manifest)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text)
        print(f"{len(data['registers'])} registers, {summary_rows} summary rows "
              f"from {data['document'].get('id', '?')} -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
