#!/usr/bin/env python3
"""Validate a device's registers JSON, optionally against its source PDF.

Without --pdf, runs checks that need only the JSON:
  schema        JSON Schema validation against the device's schema file
  unique        register offsets and names are unique
  coverage      fields cover bits 0..width-1 exactly once
  width         the width agrees with the reset value's zero-padded hex digit count
  reset         the register reset equals the field resets composed by bit position
  values        enumerated value keys parse and fit the field's width
  access        access types are ones TI uses
  leaked-text   descriptions contain no template text (table titles, "(continued)", ...)
  line-break    descriptions and values contain no line breaks

Findings explained by an entry in the device's known_document_issues.json
(see document_issues.py) are reported as warnings, so a faithful transcription
of a flawed document passes without the PDF.

With --pdf, also extracts the document's text layer (extract_registers.py) and checks:
  document      the JSON's document block matches the PDF's literature number
  summary       every register in the PDF's summary table is in the JSON, and vice versa
  register      name, width and reset match, per register
  field         bit ranges, names, access types, resets and enumerations match, per field
  description   register and field prose match after normalization (warnings only)

Usage:
  uv run --group tools tools/validate_registers.py devices/tps25751/device.json \\
      --pdf "devices/tps25751/docs/TPS25751 Technical Reference Manual - March 2024.pdf"
  uv run --group tools tools/validate_registers.py devices/tps25751/device.json --fetch

Exits 1 if any errors are found; warnings alone exit 0.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from document_issues import load_issues, matches, problems  # noqa: E402
# extract_registers (and pdfplumber) load only when a PDF is compared, so the
# offline checks need nothing beyond jsonschema.

ACCESS_TYPES = {"R", "W", "R/W", "RC", "R/WC", "W1C", "R/W1C", "W1S", "R/W1S", "RH", "R/W0C"}
LEAK_RE = re.compile(
    r"Table\s+\d+-\d+\.|Figure\s+\d+-\d+\.|Register Field Descriptions|\(continued\)"
    r"|Return to the Summary Table|Submit Document Feedback|Copyright ©|www\.ti\.com")
ENUM_TAIL_RE = re.compile(r"\s(?:[0-9A-Fa-f]+h|[01]+b)\s*=\s.*$")
DESC_MIN_RATIO = 0.90


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    check: str
    where: str
    message: str
    page: int | None = None


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, check: str, where: str, message: str, page: int | None = None) -> None:
        self.findings.append(Finding("error", check, where, message, page))

    def warn(self, check: str, where: str, message: str, page: int | None = None) -> None:
        self.findings.append(Finding("warning", check, where, message, page))

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warning" for f in self.findings)


# ---- value helpers -----------------------------------------------------------

def bits(spec: str) -> tuple[int, int]:
    hi, _, lo = spec.partition("-")
    return int(hi), int(lo or hi)


def hex_value(text: str) -> int | None:
    """'03915052h' -> int; 'X' or anything unparseable -> None."""
    text = text.strip()
    if re.fullmatch(r"[0-9A-Fa-f]+h", text):
        return int(text[:-1], 16)
    if re.fullmatch(r"[01]+b", text):
        return int(text[:-1], 2)
    return None


def norm_access(text: str) -> str:
    return "R/W" if text == "RW" else text  # datasheets occasionally print RW for R/W


def norm_name(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


TITLE_RE = re.compile(r"\s*Table\s+\d+-\d+\.\s+.*?Field Descriptions(?:\s*\(continued\))?")


def norm_prose(text: str) -> str:
    text = TITLE_RE.sub(" ", text or "")  # reported separately by leaked-text
    text = ENUM_TAIL_RE.sub("", " " + text)  # TPS JSON repeats enum text in prose
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)      # line-break hyphenation
    return re.sub(r"\s+", " ", text).strip().casefold()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def reg_label(reg: dict[str, Any]) -> str:
    return f"{reg.get('offset', '?')} {reg.get('name', '?')}"


# ---- checks on the JSON alone -----------------------------------------------

def check_schema(data: dict, schema_path: Path, report: Report) -> None:
    if not schema_path.exists():
        report.warn("schema", str(schema_path), "schema file not found; skipped")
        return
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "(root)"
        report.error("schema", path, err.message)


def check_structure(data: dict, report: Report) -> None:
    regs = data.get("registers", [])
    seen_off: dict[int, str] = {}
    seen_name: dict[str, str] = {}
    for reg in regs:
        label = reg_label(reg)
        off = hex_value(reg.get("offset", ""))
        if off is not None:
            if off in seen_off:
                report.error("unique", label, f"offset also used by {seen_off[off]}")
            seen_off[off] = label
        key = norm_name(reg.get("name", ""))
        if key in seen_name:
            report.error("unique", label, f"name also used by {seen_name[key]}")
        seen_name[key] = label

        width = reg.get("width", 0)
        owner: dict[int, str] = {}
        composed = 0
        reset_known = True
        for fld in reg.get("fields", []):
            fname = f"{label} [{fld.get('bit_field')}] {fld.get('name')}"
            try:
                hi, lo = bits(fld["bit_field"])
            except (KeyError, ValueError):
                report.error("coverage", fname, "bit_field does not parse")
                continue
            if hi < lo:
                report.error("coverage", fname, "high bit is below low bit")
                continue
            if hi >= width:
                report.error("coverage", fname, f"bit {hi} is outside the {width}-bit register")
            for b in range(lo, hi + 1):
                if b in owner:
                    report.error("coverage", fname, f"bit {b} overlaps {owner[b]}")
                owner[b] = fld.get("name", "?")

            access = fld.get("access_type", "")
            if access not in ACCESS_TYPES:
                report.warn("access", fname, f"unusual access type {access!r}")

            n = hi - lo + 1
            fr = hex_value(fld.get("reset", ""))
            if fr is None:
                reset_known = False
            elif fr >= 1 << n:
                report.error("reset", fname, f"reset {fld['reset']} does not fit in {n} bit(s)")
            else:
                composed |= fr << lo

            for key in (fld.get("values") or {}):
                v = hex_value(key)
                if v is None:
                    report.error("values", fname, f"value key {key!r} does not parse")
                elif v >= 1 << n:
                    report.error("values", fname, f"value key {key} does not fit in {n} bit(s)")

            for text, what in ((fld.get("description", ""), "description"),
                               *((v, f"value {k}") for k, v in (fld.get("values") or {}).items())):
                m = LEAK_RE.search(text or "")
                if m:
                    report.warn("leaked-text", fname, f"{what} contains template text {m.group(0)!r}")
                if "\n" in (text or ""):
                    report.warn("line-break", fname, f"{what} contains a line break; use a space")

        gaps = [b for b in range(width) if b not in owner]
        if gaps:
            report.error("coverage", label, f"bits not covered by any field: {_ranges(gaps)}")

        m = LEAK_RE.search(reg.get("description", "") or "")
        if m:
            report.warn("leaked-text", label, f"description contains template text {m.group(0)!r}")
        if "\n" in (reg.get("description", "") or ""):
            report.warn("line-break", label, "description contains a line break; use a space")

        reset_text = reg.get("reset", "")
        if re.fullmatch(r"[0-9A-Fa-f]+h", reset_text):
            digits, needed = len(reset_text) - 1, -(-width // 4)
            padded = digits > 1 and reset_text[0] == "0"
            # TRMs zero-pad resets to the register width; datasheets often do not pad.
            if digits > needed or (padded and digits != needed):
                report.warn("width", label, f"width {width} but reset {reset_text} has {digits} hex digits")

        rr = hex_value(reg.get("reset", ""))
        if rr is not None and reset_known and rr != composed:
            report.error("reset", label,
                         f"register reset {reg['reset']} != field resets composed ({composed:X}h)")


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(nums) + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{prev}-{start}" if prev != start else f"{start}")
            start = prev = n
    return ", ".join(reversed(out))


# ---- comparison against the PDF ---------------------------------------------

def compare(data: dict, ref: dict, report: Report) -> None:
    doc, rdoc = data.get("document"), ref.get("document", {})
    if not doc:
        report.warn("document", "(root)",
                    f"JSON has no document block; PDF is {rdoc.get('id', '?')} ({rdoc.get('date', '?')})")
    elif doc.get("id") != rdoc.get("id"):
        report.error("document", "(root)",
                     f"JSON was extracted from {doc.get('id')} but the PDF is {rdoc.get('id')}")

    by_off = {hex_value(r["offset"]): r for r in data.get("registers", [])}
    ref_by_off = {hex_value(r["offset"]): r for r in ref.get("registers", [])}

    for row in ref.get("summary", []):
        if hex_value(row["offset"]) not in by_off:
            report.error("summary", f"{row['offset']} {row['name']}", "in the PDF's summary table but missing from the JSON")
    summary_offs = {hex_value(r["offset"]) for r in ref.get("summary", [])}
    for off, reg in by_off.items():
        if summary_offs and off not in summary_offs:
            report.error("summary", reg_label(reg), "in the JSON but not in the PDF's summary table")

    for off, reg in sorted(by_off.items(), key=lambda kv: kv[0] or 0):
        rref = ref_by_off.get(off)
        if rref is None:
            continue
        label, page = reg_label(reg), rref.get("source_page")
        if norm_name(reg["name"]) != norm_name(rref["name"]):
            report.error("register", label, f"name differs: PDF has {rref['name']!r}", page)
        if reg.get("width") != rref.get("width"):
            report.error("register", label, f"width {reg.get('width')} != PDF {rref.get('width')}", page)
        a, b = hex_value(reg.get("reset", "")), hex_value(rref.get("reset", ""))
        if (a is None) != (b is None) or (a is not None and a != b):
            report.error("register", label, f"reset {reg.get('reset')} != PDF {rref.get('reset')}", page)
        _compare_prose(reg.get("description", ""), rref.get("description", ""), label, page, report)
        _compare_fields(reg, rref, label, page, report)


def _compare_prose(ours: str, theirs: str, where: str, page: int | None, report: Report) -> None:
    a, b = norm_prose(ours), norm_prose(theirs)
    if not a and not b:
        return
    ratio = similarity(a, b)
    if ratio < DESC_MIN_RATIO:
        report.warn("description", where, f"prose differs (similarity {ratio:.2f}); PDF: {theirs[:160]!r}", page)


def _compare_fields(reg: dict, rref: dict, label: str, page: int | None, report: Report) -> None:
    ours = {bits(f["bit_field"]): f for f in reg.get("fields", [])}
    theirs = {bits(f["bit_field"]): f for f in rref.get("fields", [])}
    for rng in sorted(set(ours) | set(theirs), reverse=True):
        where = f"{label} [{rng[0]}-{rng[1]}]"
        f, g = ours.get(rng), theirs.get(rng)
        if g is None:
            report.error("field", f"{where} {f['name']}", "bit range not in the PDF", page)
            continue
        if f is None:
            report.error("field", f"{where} {g['name']}", "in the PDF but missing from the JSON", page)
            continue
        where = f"{where} {f['name']}"
        if norm_name(f["name"]) != norm_name(g["name"]):
            report.error("field", where, f"name differs: PDF has {g['name']!r}", page)
        if norm_access(f.get("access_type", "")) != norm_access(g.get("access_type", "")):
            report.error("field", where, f"access {f.get('access_type')} != PDF {g.get('access_type')}", page)
        a, b = hex_value(f.get("reset", "")), hex_value(g.get("reset", ""))
        if (a is None) != (b is None) or (a is not None and a != b):
            report.error("field", where, f"reset {f.get('reset')} != PDF {g.get('reset')}", page)
        fv = {hex_value(k): v for k, v in (f.get("values") or {}).items()}
        gv = {hex_value(k): v for k, v in (g.get("values") or {}).items()}
        for key in sorted(set(fv) | set(gv), key=lambda k: -1 if k is None else k):
            if key not in gv:
                report.error("field", where, f"value {key:X}h not in the PDF", page)
            elif key not in fv:
                report.error("field", where, f"value {key:X}h = {gv[key]!r} missing from the JSON", page)
            elif similarity(norm_prose(fv[key]), norm_prose(gv[key])) < DESC_MIN_RATIO:
                report.warn("description", where, f"value {key:X}h text differs; PDF: {gv[key]!r}", page)
        if "encoding" in g:
            fe = f.get("encoding") or {}
            for key, val in g["encoding"].items():
                if fe.get(key) != val:
                    report.error("field", where, f"encoding {key} {fe.get(key)!r} != PDF {val!r}", page)
        # Subscripts can move a source's line breaks, so compare the joined text.
        squash = lambda xs: re.sub(r"\s+", "", "".join(xs))
        if "reset_sources" in g and squash(f.get("reset_sources", [])) != squash(g["reset_sources"]):
            report.error("field", where,
                         f"reset_sources {f.get('reset_sources')} != PDF {g['reset_sources']}", page)
        if g.get("description") or norm_prose(f.get("description", "")) != norm_name(f["name"]):
            _compare_prose(f.get("description", ""), g.get("description", ""), where, page, report)


def mark_source_issues(report: Report, ref: dict) -> None:
    """Downgrade JSON structural errors that the PDF itself also has.

    TI documents occasionally contain their own inconsistencies (overlapping
    RESERVED rows, a register reset that disagrees with its field resets). A
    faithful transcription inherits them; they are reported as warnings so the
    errors left are the ones the JSON introduced.
    """
    ref_report = Report()
    check_structure(ref, ref_report)
    def key(f: Finding) -> tuple[str, str, str]:
        # Which neighbour an overlap names depends on field order; the bit does not.
        if f.check == "coverage" and " overlaps " in f.message:
            return f.check, f.where.split(" [")[0], re.sub(r" overlaps .*", "", f.message)
        return f.check, f.where, f.message

    in_pdf = {key(f) for f in ref_report.findings}
    for f in report.findings:
        if f.severity == "error" and key(f) in in_pdf:
            f.severity = "warning"
            f.message = "same in source document: " + f.message


STRUCTURAL_CHECKS = {"reset", "coverage", "width", "values", "access"}


def apply_known_issues(report: Report, data: dict, issues: list[dict], compared_pdf: bool) -> None:
    """Downgrade findings explained by a recorded document issue, and flag
    recorded issues that no longer reproduce."""
    document = (data.get("document") or {}).get("id")
    used: set[str] = set()
    for f in report.findings:
        for issue in issues:
            if matches(issue, f.check, f.where, document):
                used.add(issue["id"])
                if f.severity == "error":
                    f.severity = "warning"
                f.message = f"known document issue {issue['id']} ({issue['status']}): " + \
                    re.sub(r"^same in source document: ", "", f.message)
                break
    for issue in issues:
        if issue["id"] in used or issue.get("status") == "fixed" or issue.get("check") == "text":
            continue
        if document and issue.get("document") != document:
            continue
        if issue.get("check") in STRUCTURAL_CHECKS or compared_pdf:
            report.warn("known-issues", issue["id"],
                        "recorded document issue did not reproduce; mark it fixed or correct its location")


# ---- driver -----------------------------------------------------------------

def print_report(report: Report, title: str) -> None:
    print(title)
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in report.findings:
        groups[f.check].append(f)
    for check, items in groups.items():
        print(f"\n[{check}] {len(items)}")
        for f in items:
            page = f" (p.{f.page})" if f.page else ""
            print(f"  {f.severity.upper():7} {f.where}{page}: {f.message}")
    print(f"\n{report.errors} error(s), {report.warnings} warning(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path, help="the device's device.json")
    ap.add_argument("--pdf", type=Path, help="source PDF to compare against")
    ap.add_argument("--fetch", action="store_true",
                    help="compare against the latest revision from the manifest's document_url, "
                         "downloading it to docs/ if it is not cached")
    ap.add_argument("--registers", type=Path, help="registers JSON to check (default: the manifest's registers_file)")
    ap.add_argument("--pages", help="1-based page range to extract, e.g. 12-61 (default: all)")
    ap.add_argument("--report", type=Path, help="also write findings as JSON here")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    base = args.manifest.resolve().parent
    reg_path = args.registers or base / manifest["registers_file"]
    data = json.loads(Path(reg_path).read_text())

    report = Report()
    issues = load_issues(args.manifest)
    for problem in problems(issues):
        report.error("known-issues", "known_document_issues.json", problem)
    check_schema(data, base / manifest.get("schema_file", ""), report)
    check_structure(data, report)
    title = f"{manifest.get('device_name', '?')}: {reg_path}"
    if args.fetch and not args.pdf:
        from fetch_document import fetch
        args.pdf = fetch(args.manifest)
    if args.pdf:
        from extract_registers import _parse_pages, extract
        ref = extract(args.pdf, _parse_pages(args.pages))
        mark_source_issues(report, ref)
        compare(data, ref, report)
        title += f"\n  vs {ref['document'].get('id', '?')} ({ref['document'].get('date', '?')}): {args.pdf}"
    apply_known_issues(report, data, issues, compared_pdf=bool(args.pdf))
    unrecorded = sum("same in source document" in f.message for f in report.findings)
    if unrecorded:
        title += (f"\n  {unrecorded} source-document issue(s) are not in known_document_issues.json; "
                  "record them with tools/document_issues.py discover")

    print_report(report, title)
    if args.report:
        args.report.write_text(json.dumps([f.__dict__ for f in report.findings], indent=2, ensure_ascii=False) + "\n")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
