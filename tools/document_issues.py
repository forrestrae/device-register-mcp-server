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
  status       "open", "reported", or "fixed"
  reported     optional {"date": "YYYY-MM-DD", "reference": "..."} once filed

Usage:
  uv run --group tools tools/document_issues.py list devices/bq25798/device.json
  uv run --group tools tools/document_issues.py feedback devices/bq25798/device.json [ID ...]
  uv run --group tools tools/document_issues.py discover devices/bq25798/device.json --pdf DOC.pdf [--write]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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
    return out


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


def feedback_text(issue: dict[str, Any], part: str) -> str:
    url = f"{FEEDBACK_URL}?{urlencode({'litnum': issue['document'], 'partnum': part})}"
    lines = [
        f"=== {issue['id']} ({issue['status']}) ===",
        f"Form:                 {url}",
        f"Literature Number:    {issue['document']}",
        f"Part Number:          {part}",
        f"Summary:              {issue['summary']}",
        f"Severity:             {issue['severity']}",
        f"Page Number(s):       {pages_text(issue['page'])}",
        "Detailed description:",
    ]
    lines += issue["description"].splitlines() or [""]
    return "\n".join(lines)


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


# ---- driver -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "feedback", "discover"):
        p = sub.add_parser(name)
        p.add_argument("manifest", type=Path)
        if name == "feedback":
            p.add_argument("ids", nargs="*", help="issue ids (default: every open issue)")
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

    if args.cmd == "feedback":
        chosen = [i for i in issues if (i["id"] in args.ids if args.ids else i["status"] == "open")]
        missing = set(args.ids) - {i["id"] for i in issues}
        if missing:
            print(f"unknown issue id(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
        print("\n\n".join(feedback_text(i, part) for i in chosen) or "No open issues.")
        if chosen:
            print("\nAfter filing, set each issue's status to \"reported\" and add "
                  "\"reported\": {\"date\": ..., \"reference\": ...}.")
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
