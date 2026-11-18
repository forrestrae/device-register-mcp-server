#!/usr/bin/env python3
"""Track errors in a manufacturer's document and prepare reports for them.

Each device can keep devices/<device>/known_document_issues.json: a list of
errors found in its source document (a register reset that disagrees with its
own field resets, missing text, misspellings). The file has two uses:

- validate_registers.py treats a finding that matches an open issue as a
  warning, so a faithful transcription of a flawed document still passes the
  offline check (and the pytest suite) with no PDF at hand.
- This tool prints each issue as the fields of TI's "Technical Documents:
  Documentation Feedback" form, ready to paste. The form has a CAPTCHA, so
  submission stays manual.

Issue fields:
  id           unique, stable name, e.g. "REG48-reset"
  document     literature number the issue was found in, e.g. "SLUSDV2C"
  page         1-based page number in that document, or a list of them
  register     register offset, e.g. "48h" (omit for issues outside a register)
  bits         field bit range, e.g. "3-3" (optional)
  check        the validator check it explains: "reset", "coverage", "width",
               "values", "access", "description", or "text" for issues the
               validator does not detect (misspellings)
  severity     the form's severity: "S1-Critical", "S2-Major", or "S3-Minor"
  summary      one line for the form's Summary field
  description  the form's Detailed description: what the page says, and why it
               is wrong
  evidence     optional list of {"page": N, "text": "..."}: exact strings on the
               cited pages that show the problem; `review` boxes them in red
  status       "open", "reported", or "fixed"
  reported     {"date": "YYYY-MM-DD"}: when the issue was filed. TI's form gives
               no reference number, so the date is the whole record. Set by the
               `reported` command.

Before filing, `review` renders each cited page with its evidence boxed and
writes a local HTML page beside the form text, so a person can confirm every
claim against the document. The PDF is found in devices/<device>/docs/ by the
issue's literature number; the page and images stay there, under a gitignored
folder, because TI documents are not redistributable.

Usage:
  uv run --group tools tools/document_issues.py list devices/bq25798/device.json
  uv run --group tools tools/document_issues.py discover devices/bq25798/device.json --pdf DOC.pdf [--write]
  uv run --group tools tools/document_issues.py review devices/bq25798/device.json [ID ...] [--pdf DOC.pdf] [--out DIR] [--open]
  uv run --group tools tools/document_issues.py feedback devices/bq25798/device.json [ID ...]
  uv run --group tools tools/document_issues.py reported devices/bq25798/device.json ID [ID ...] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ISSUES_FILE = "known_document_issues.json"
FEEDBACK_URL = "https://www.ti.com/feedbackform/techdocfeedback"
SEVERITIES = ("S1-Critical", "S2-Major", "S3-Minor")
STATUSES = ("open", "reported", "fixed")
CHECKS = ("reset", "coverage", "width", "values", "access", "description", "text")
REQUIRED = ("id", "document", "page", "check", "severity", "summary", "description", "status")


def issues_path(manifest_path: Path) -> Path:
    return manifest_path.resolve().parent / ISSUES_FILE


def load_issues(manifest_path: Path) -> list[dict[str, Any]]:
    path = issues_path(manifest_path)
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("issues", [])


def problems(issues: list[dict[str, Any]]) -> list[str]:
    """Describe malformed entries; an empty list means the file is well formed."""
    out = []
    seen = set()
    for i, issue in enumerate(issues):
        label = issue.get("id", f"#{i}")
        for key in REQUIRED:
            if not issue.get(key):
                out.append(f"{label}: missing {key}")
        if issue.get("id") in seen:
            out.append(f"{label}: duplicate id")
        seen.add(issue.get("id"))
        if issue.get("severity") not in SEVERITIES:
            out.append(f"{label}: severity must be one of {', '.join(SEVERITIES)}")
        if issue.get("status") not in STATUSES:
            out.append(f"{label}: status must be one of {', '.join(STATUSES)}")
        if issue.get("check") not in CHECKS:
            out.append(f"{label}: check must be one of {', '.join(CHECKS)}")
        if issue.get("register") and not re.fullmatch(r"[0-9A-Fa-f]+h", issue["register"]):
            out.append(f"{label}: register must be a hex offset like '48h'")
        if issue.get("bits") and not re.fullmatch(r"\d+-\d+", issue["bits"]):
            out.append(f"{label}: bits must be 'high-low'")
        out += [f"{label}: {p}" for p in evidence_problems(issue)]
        out += [f"{label}: {p}" for p in form_problems(issue)]
        out += [f"{label}: {p}" for p in reported_problems(issue)]
    return out


def parse_date(text: str) -> date:
    """A YYYY-MM-DD date; raises ValueError otherwise."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{text!r} is not a YYYY-MM-DD date")
    return date.fromisoformat(text)


def date_arg(text: str) -> date:
    try:
        return parse_date(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


def reported_problems(issue: dict[str, Any]) -> list[str]:
    if "reported" not in issue:
        return []
    rep = issue["reported"]
    if not isinstance(rep, dict):
        return ['reported must be an object: {"date": "YYYY-MM-DD"}']
    out = []
    if issue.get("status") == "open":
        out.append("reported is set but status is still open")
    try:
        parse_date(rep.get("date") if isinstance(rep.get("date"), str) else "")
    except ValueError:
        out.append("reported.date must be a YYYY-MM-DD date")
    extra = set(rep) - {"date"}
    if extra:
        out.append(f"reported has unknown key(s): {', '.join(sorted(extra))}")
    return out


# The feedback form's client-side checks (techdocfeedback page script): every
# text field must be printable ASCII or whitespace and must not look like markup
# or script; Page Number(s) is limited to 50 characters. A field that fails is
# rejected on submit, so catch it here instead.
FORM_TEXT = re.compile(r"[\x20-\x7e\s]*")
FORM_REJECTED = (re.compile(r"<(.*?)>"), re.compile(r"alert\(.*?\)"), re.compile(r"on\(.*?\)"))
FORM_PAGE_MAX = 50


def form_problems(issue: dict[str, Any]) -> list[str]:
    """Reasons the feedback form would reject this issue's text."""
    out = []
    fields = {"summary": issue.get("summary"), "description": issue.get("description"),
              "page": pages_text(issue["page"]) if issue.get("page") else None}
    for key, value in fields.items():
        if not isinstance(value, str):
            continue
        bad = sorted({c for c in value if not FORM_TEXT.fullmatch(c)})
        if bad:
            out.append(f"{key} has characters the feedback form rejects (plain ASCII only): "
                       + " ".join(f"{c!r} (U+{ord(c):04X})" for c in bad))
        for pattern in FORM_REJECTED:
            m = pattern.search(value)
            if m:
                out.append(f"{key} contains {m.group(0)!r}, which the feedback form rejects as markup")
    if fields["page"] and len(fields["page"]) > FORM_PAGE_MAX:
        out.append(f"page list is {len(fields['page'])} characters; the form's Page Number(s) "
                   f"field holds {FORM_PAGE_MAX}")
    return out


def issue_pages(issue: dict[str, Any]) -> list[int]:
    page = issue.get("page")
    return page if isinstance(page, list) else [page] if page else []


def evidence_problems(issue: dict[str, Any]) -> list[str]:
    if "evidence" not in issue:
        return []
    evidence = issue["evidence"]
    if not isinstance(evidence, list) or not evidence:
        return ["evidence must be a non-empty list of {page, text}"]
    out = []
    pages = issue_pages(issue)
    for n, e in enumerate(evidence):
        if not isinstance(e, dict):
            out.append(f"evidence[{n}] must be an object with page and text")
            continue
        if not isinstance(e.get("page"), int) or e["page"] not in pages:
            out.append(f"evidence[{n}] page must be one of the issue's pages ({pages_text(issue.get('page', []))})")
        if not isinstance(e.get("text"), str) or not e["text"].strip():
            out.append(f"evidence[{n}] text must be a non-empty string")
    return out


def select(issues: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    """The named issues, or every open issue when none are named."""
    missing = set(ids) - {i["id"] for i in issues}
    if missing:
        print(f"unknown issue id(s): {', '.join(sorted(missing))}", file=sys.stderr)
        raise SystemExit(2)
    return [i for i in issues if (i["id"] in ids if ids else i["status"] == "open")]


def matches(issue: dict[str, Any], check: str, where: str, document: str | None) -> bool:
    """Whether a validator finding (check, where) is explained by this issue."""
    if issue.get("status") == "fixed" or issue.get("check") != check:
        return False
    if document and issue.get("document") != document:
        return False
    reg = issue.get("register")
    if reg and not where.upper().startswith(reg.upper() + " "):
        return False
    bits = issue.get("bits")
    if bits and f"[{bits}]" not in where:
        return False
    return True


# ---- feedback form ----------------------------------------------------------

def pages_text(page: int | list[int]) -> str:
    return ", ".join(str(p) for p in page) if isinstance(page, list) else str(page)


def form_url(document: str, part: str) -> str:
    """The feedback form with Literature Number and Part Number filled in.

    These are the only two fields the form reads from the URL (litnum, partnum).
    It clears Summary, Page Number(s), and Detailed description on load and has
    no parameter for Severity, so those are pasted by hand."""
    return f"{FEEDBACK_URL}?{urlencode({'litnum': document, 'partnum': part} if part else {'litnum': document})}"


def form_fields(issue: dict[str, Any], part: str) -> list[tuple[str, str]]:
    """(label, value) for each field of the feedback form, labeled as the form labels it."""
    return [
        ("Literature Number", issue["document"]),
        ("Part Number", part),
        ("Summary", issue["summary"]),
        ("Severity", issue["severity"]),
        ("Page Number(s)", pages_text(issue["page"])),
        ("Detailed description", issue["description"]),
    ]


def feedback_text(issue: dict[str, Any], part: str) -> str:
    *short, (desc_label, desc) = form_fields(issue, part)
    lines = [f"=== {issue['id']} ({issue['status']}) ===",
             f"{'Form:':22}{form_url(issue['document'], part)}"]
    lines += [f"{label + ':':22}{value}" for label, value in short]
    lines += [f"{desc_label}:"] + (desc.splitlines() or [""])
    return "\n".join(lines)


# ---- reporting ----------------------------------------------------------------

def mark_reported(manifest_path: Path, ids: list[str], when: date) -> list[str]:
    """Set each issue's status to "reported" and record the date it was filed.

    Returns one line per issue describing the change. Raises SystemExit(2) for an
    unknown id or an issue already marked fixed, before writing anything."""
    path = issues_path(manifest_path)
    data = json.loads(path.read_text()) if path.exists() else {"issues": []}
    by_id = {i.get("id"): i for i in data["issues"]}
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"unknown issue id(s): {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    fixed = [i for i in ids if by_id[i].get("status") == "fixed"]
    if fixed:
        print(f"already fixed, not changed: {', '.join(fixed)}", file=sys.stderr)
        raise SystemExit(2)

    record = {"date": when.isoformat()}
    lines = []
    for iid in dict.fromkeys(ids):
        issue = by_id[iid]
        before = issue.get("reported")
        # Rebuild the entry so "reported" sits right after "status".
        items = [(k, v) for k, v in issue.items() if k != "reported"]
        issue.clear()
        for k, v in items:
            issue[k] = "reported" if k == "status" else v
            if k == "status":
                issue["reported"] = dict(record)
        lines.append(f"{iid}: reported {record['date']}"
                     + (f" (was {json.dumps(before)})" if before else ""))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return lines


# ---- discovery ----------------------------------------------------------------

def discover(manifest_path: Path, pdf: Path) -> list[dict[str, Any]]:
    """Candidate issues: structural problems present in the PDF itself."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from extract_registers import extract
    from validate_registers import Report, check_structure

    ref = extract(pdf)
    doc = ref["document"].get("id", "")
    report = Report()
    check_structure(ref, report)
    pages = {r["offset"].upper(): r.get("source_page") for r in ref["registers"]}
    known = load_issues(manifest_path)

    out: list[dict[str, Any]] = []
    for f in report.findings:
        if f.severity != "error":
            continue
        m = re.match(r"([0-9A-Fa-f]+h) (.+?)(?: \[(\d+-\d+)\] (.+))?$", f.where)
        if not m:
            continue
        reg, reg_name, bits, field_name = m.groups()
        if any(matches(i, f.check, f.where, doc) for i in known + out):
            continue
        target = f"{reg_name} register (offset {reg})" + (f", bits {bits} {field_name}" if bits else "")
        out.append({
            "id": f"{reg}-{f.check}" + (f"-{bits}" if bits else ""),
            "document": doc,
            "page": pages.get(reg.upper()),
            "register": reg,
            **({"bits": bits} if bits else {}),
            "check": f.check,
            "severity": "S2-Major",
            "summary": f"{target}: {f.message}",
            "description": (f"The {target} description is internally inconsistent: {f.message}. "
                            "Please confirm the correct value and update the document."),
            "status": "open",
        })
    return out


# ---- review -------------------------------------------------------------------

def search_terms(issue: dict[str, Any]) -> dict[int, list[str]]:
    """Strings to box on each cited page. Without recorded evidence, the register heading."""
    terms: dict[int, list[str]] = {p: [] for p in issue_pages(issue)}
    for e in issue.get("evidence", []):
        terms.setdefault(e["page"], []).append(e["text"])
    if not issue.get("evidence") and issue.get("register"):
        for p in terms:
            terms[p].append(f"Offset = {issue['register']}")
    return terms


def locate(pdf, issue: dict[str, Any]) -> dict[int, list[tuple[str, list[dict]]]]:
    """For each cited page, every search term with the boxes where it appears.

    A page beyond the end of the document maps to None in place of a list."""
    found: dict[int, Any] = {}
    for pno, texts in search_terms(issue).items():
        if not 1 <= pno <= len(pdf.pages):
            found[pno] = None
            continue
        page = pdf.pages[pno - 1]
        found[pno] = [(t, page.search(t, regex=False)) for t in texts]
    return found


def _pdf_for(doc_id: str, docs: Path, override: Path | None, cache: dict) -> tuple[Path | None, str]:
    """The PDF to render for doc_id and a note when it is missing or a different revision."""
    if doc_id in cache:
        return cache[doc_id]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from extract_registers import document_info
    from fetch_document import find_local
    import pdfplumber

    if override:
        if not override.exists():
            result = (None, f"{override} does not exist.")
        else:
            with pdfplumber.open(override) as pdf:
                actual = document_info(pdf).get("id")
            note = "" if actual == doc_id else (
                f"The issue cites {doc_id}, but {override.name} is {actual or 'an unidentified document'}; "
                "page numbers may not line up.")
            result = (override, note)
    else:
        found = find_local(docs, doc_id) if docs.is_dir() else None
        result = (found, "" if found else (
            f"No PDF of {doc_id} in {docs}. fetch_document.py downloads only the latest "
            "revision; place an older one there by hand, or pass --pdf."))
    cache[doc_id] = result
    return result


CSS = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1d1d1f; --muted: #666; --line: #ddd;
        --card: #f6f6f7; --bad: #b3261e; --ok: #1e7b34; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161618; --fg: #e8e8ea; --muted: #a0a0a6; --line: #333; --card: #222225;
          --bad: #ff8a80; --ok: #7bd88f; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 -apple-system, system-ui, sans-serif;
       max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
header p, .meta, figcaption { color: var(--muted); }
nav a { margin-right: 1em; }
section { border-top: 1px solid var(--line); margin-top: 32px; padding-top: 8px; }
h2 code { font-size: 0.9em; }
.note { border-left: 4px solid var(--bad); padding: 4px 12px; background: var(--card); }
ul.evidence { list-style: none; padding-left: 0; }
.found { color: var(--ok); } .missing { color: var(--bad); font-weight: 600; }
figure { margin: 16px 0; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--line); background: #fff; }
pre { background: var(--card); padding: 12px; overflow-x: auto; white-space: pre-wrap; }
table.form { border-collapse: collapse; width: 100%; }
table.form th { text-align: left; vertical-align: top; padding: 8px 12px 8px 0; white-space: nowrap; width: 1%; }
table.form td { padding: 4px 0; }
table.form pre { margin: 0; padding: 8px; font: inherit; }
@media (max-width: 600px) { table.form th, table.form td { display: block; width: auto; } }
"""


def review(issues: list[dict[str, Any]], manifest_path: Path, part: str, out: Path,
           pdf_override: Path | None = None) -> Path:
    """Render each issue's cited pages with its evidence boxed; write out/index.html."""
    import pdfplumber

    out.mkdir(parents=True, exist_ok=True)
    docs = manifest_path.resolve().parent / "docs"
    cache: dict = {}
    esc = html.escape
    sections = []
    for issue in issues:
        iid = issue["id"]
        meta = " · ".join(f"{label}: {esc(issue[key])}" for label, key in
                          (("Status", "status"), ("Register", "register"), ("Bits", "bits"), ("Validator check", "check"))
                          if issue.get(key))
        body = [
            f'<section id="{esc(iid)}"><h2><code>{esc(iid)}</code></h2>',
            f'<p class="meta">{meta}</p>',
        ]
        url = form_url(issue["document"], part)
        rows = "".join(f"<tr><th scope=\"row\">{esc(label)}</th><td><pre>{esc(value)}</pre></td></tr>"
                       for label, value in form_fields(issue, part))
        body.append(f'<h3>Feedback form fields</h3><p><a href="{esc(url)}" target="_blank" rel="noopener">Open the form</a> '
                    "and copy each value into the field with the same label.</p>")
        body.append(f'<table class="form">{rows}</table>')
        body.append("<h3>Evidence</h3>")
        pdf_path, note = _pdf_for(issue["document"], docs, pdf_override, cache)
        if note:
            body.append(f'<p class="note">{esc(note)}</p>')
        if not issue.get("evidence"):
            body.append('<p class="note">No evidence text is recorded for this issue'
                        + (", so the register heading is boxed instead" if issue.get("register") else "")
                        + '. Add an "evidence" list to point the reviewer at the exact text.</p>')
        if pdf_path:
            with pdfplumber.open(pdf_path) as pdf:
                for pno, hits in locate(pdf, issue).items():
                    if hits is None:
                        body.append(f'<p class="note">Page {pno} is beyond the end of {esc(pdf_path.name)} '
                                    f"({len(pdf.pages)} pages).</p>")
                        continue
                    items = "".join(
                        f'<li class="{"found" if boxes else "missing"}">'
                        f'{"✓ found" if boxes else f"✗ not found on page {pno}"}'
                        f'{f" ({len(boxes)}×)" if len(boxes) > 1 else ""}: <code>{esc(text)}</code></li>'
                        for text, boxes in hits)
                    rects = [(b["x0"] - 2, b["top"] - 2, b["x1"] + 2, b["bottom"] + 2)
                             for _, boxes in hits for b in boxes]
                    img = pdf.pages[pno - 1].to_image(resolution=110)
                    if rects:
                        img.draw_rects(rects, stroke=(220, 0, 0), stroke_width=3, fill=(255, 0, 0, 40))
                    name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', iid)}-p{pno}.png"
                    img.save(out / name)
                    link = f"{pdf_path.resolve().as_uri()}#page={pno}"
                    body.append(
                        f'<ul class="evidence">{items}</ul>'
                        f'<figure><a href="{esc(link)}" target="_blank" rel="noopener"><img src="{esc(name)}" alt="Page {pno} of {esc(issue["document"])}"></a>'
                        f'<figcaption>Page {pno} of {esc(pdf_path.name)}. Select the image to open the PDF at this page.'
                        "</figcaption></figure>")
        body.append("</section>")
        sections.append("\n".join(body))

    nav = " ".join(f'<a href="#{esc(i["id"])}">{esc(i["id"])}</a>' for i in issues)
    page = out / "index.html"
    page.write_text(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(part)} document issues</title><style>{CSS}</style></head><body>"
        f"<header><h1>{esc(part)} document issues: review</h1>"
        f"<p>{len(issues)} issue(s), generated {date.today().isoformat()}. For each issue, check that "
        "the boxed text on the page says what the description claims before filing it. "
        "Edit known_document_issues.json for anything that does not hold up.</p>"
        f"<nav>{nav}</nav></header>\n" + "\n".join(sections) + "\n</body></html>\n")
    return page


# ---- driver -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "feedback", "discover", "review", "reported"):
        p = sub.add_parser(name)
        p.add_argument("manifest", type=Path)
        if name in ("feedback", "review"):
            p.add_argument("ids", nargs="*", help="issue ids (default: every open issue)")
        if name == "review":
            p.add_argument("--pdf", type=Path, help="document to render (default: found in docs/ by literature number)")
            p.add_argument("--out", type=Path, help="output folder (default: <device>/docs/review, gitignored)")
            p.add_argument("--open", action="store_true", help="open the page in the default browser")
        if name == "reported":
            p.add_argument("ids", nargs="+", help="ids of the issues you filed")
            p.add_argument("--date", type=date_arg, default=date.today(),
                           help="date filed, YYYY-MM-DD (default: today)")
        if name == "discover":
            p.add_argument("--pdf", type=Path, required=True)
            p.add_argument("--write", action="store_true", help=f"append candidates to {ISSUES_FILE}")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    part = manifest.get("device_name", "")
    issues = load_issues(args.manifest)
    bad = problems(issues)
    if bad:
        print(f"{issues_path(args.manifest)} is malformed:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2

    if args.cmd == "list":
        for i in issues:
            where = f"{i.get('register', '')} {i.get('bits', '')}".strip()
            print(f"{i['id']:24} {i['status']:9} {i['document']} p.{pages_text(i['page']):<6} "
                  f"{i['severity']:11} {where:10} {i['summary']}")
        return 0

    if args.cmd == "review":
        chosen = select(issues, args.ids)
        if not chosen:
            print("No open issues.")
            return 0
        out = args.out or args.manifest.resolve().parent / "docs" / "review"
        page = review(chosen, args.manifest, part, out, args.pdf)
        print(page)
        if args.open:
            webbrowser.open(page.as_uri())
        return 0

    if args.cmd == "reported":
        for line in mark_reported(args.manifest, args.ids, args.date):
            print(line)
        print(f"Updated {issues_path(args.manifest)}")
        return 0

    if args.cmd == "feedback":
        chosen = select(issues, args.ids)
        print("\n\n".join(feedback_text(i, part) for i in chosen) or "No open issues.")
        if chosen:
            print(f"\nAfter filing, record it: document_issues.py reported {args.manifest} ID")
        return 0

    candidates = discover(args.manifest, args.pdf)
    if not candidates:
        print("No unrecorded structural issues in the document.")
        return 0
    print(json.dumps(candidates, indent=2, ensure_ascii=False))
    if args.write:
        path = issues_path(args.manifest)
        data = json.loads(path.read_text()) if path.exists() else {"issues": []}
        data["issues"].extend(candidates)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"\nAppended {len(candidates)} issue(s) to {path}; edit summary, description, "
              "and severity before filing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
