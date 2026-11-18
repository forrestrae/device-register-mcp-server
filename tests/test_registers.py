"""Offline checks over every device's registers JSON and the server built from it.

Run with: uv run --group test pytest

No PDF or network is needed. Each registers JSON must pass its schema and the
structural checks in tools/validate_registers.py with no findings other than
ones explained by the device's known_document_issues.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from document_issues import load_issues, mark_reported, problems  # noqa: E402
from validate_registers import Report, apply_known_issues, check_schema, check_structure  # noqa: E402

MANIFESTS = sorted(ROOT.glob("devices/*/device.json"))


def _ids(paths):
    return [p.parent.name for p in paths]


@pytest.fixture(params=MANIFESTS, ids=_ids(MANIFESTS))
def device(request):
    manifest_path = request.param
    manifest = json.loads(manifest_path.read_text())
    data = json.loads((manifest_path.parent / manifest["registers_file"]).read_text())
    return manifest_path, manifest, data


def test_devices_found():
    assert MANIFESTS, "no devices/*/device.json manifests found"


def test_registers_json_is_clean(device):
    manifest_path, manifest, data = device
    report = Report()
    check_schema(data, manifest_path.parent / manifest["schema_file"], report)
    check_structure(data, report)
    apply_known_issues(report, data, load_issues(manifest_path), compared_pdf=False)
    unexplained = [f for f in report.findings if not f.message.startswith("known document issue")]
    assert not unexplained, "\n".join(f"{f.severity} [{f.check}] {f.where}: {f.message}" for f in unexplained)


def test_known_issues_well_formed(device):
    manifest_path, _, data = device
    issues = load_issues(manifest_path)
    assert not problems(issues)
    offsets = {r["offset"].upper() for r in data["registers"]}
    doc = (data.get("document") or {}).get("id")
    for issue in issues:
        if issue.get("register") and issue["document"] == doc:
            assert issue["register"].upper() in offsets, f"{issue['id']}: no register {issue['register']}"


BASE_ISSUE = {"id": "x", "document": "D", "page": [3, 4], "check": "text", "severity": "S3-Minor",
              "summary": "s", "description": "d", "status": "open"}


@pytest.mark.parametrize("evidence, expected", [
    ([{"page": 3, "text": "Capabalities"}], None),
    ([], "non-empty list"),
    ("Capabalities", "non-empty list"),
    (["Capabalities"], "must be an object"),
    ([{"page": 5, "text": "Capabalities"}], "page must be one of"),
    ([{"page": "3", "text": "Capabalities"}], "page must be one of"),
    ([{"page": 3, "text": "  "}], "text must be a non-empty string"),
])
def test_evidence_validation(evidence, expected):
    found = problems([{**BASE_ISSUE, "evidence": evidence}])
    if expected is None:
        assert not found
    else:
        assert any(expected in p for p in found), found


@pytest.mark.parametrize("change, expected", [
    ({}, None),
    ({"description": "Line one.\nLine two, with \"quotes\" and 3h -> 19h."}, None),
    ({"summary": "Reset ≥ 19h"}, "plain ASCII only"),
    ({"description": "Use “curly” quotes"}, "plain ASCII only"),
    ({"description": "The <b>heading</b> says 0h"}, "rejects as markup"),
    ({"summary": "Calls alert(1)"}, "rejects as markup"),
    ({"page": list(range(100, 120))}, "holds 50"),
])
def test_feedback_form_rules(change, expected):
    found = problems([{**BASE_ISSUE, **change}])
    if expected is None:
        assert not found
    else:
        assert any(expected in p for p in found), found


@pytest.mark.parametrize("change, expected", [
    ({"status": "reported", "reported": {"date": "2026-11-18"}}, None),
    ({"status": "fixed", "reported": {"date": "2026-11-18"}}, None),
    ({"status": "open", "reported": {"date": "2026-11-18"}}, "status is still open"),
    ({"status": "reported", "reported": "2026-11-18"}, "must be an object"),
    ({"status": "reported", "reported": {"date": "11/18/2026"}}, "YYYY-MM-DD"),
    ({"status": "reported", "reported": {"date": "2026-02-30"}}, "YYYY-MM-DD"),
    ({"status": "reported", "reported": {"date": "2026-11-18", "reference": "x"}}, "unknown key"),
])
def test_reported_validation(change, expected):
    found = problems([{**BASE_ISSUE, **change}])
    if expected is None:
        assert not found
    else:
        assert any(expected in p for p in found), found


def _issue_file(tmp_path, *issues):
    (tmp_path / "known_document_issues.json").write_text(json.dumps({"issues": list(issues)}))
    return tmp_path / "device.json"


def test_mark_reported(tmp_path):
    from datetime import date
    manifest = _issue_file(tmp_path, {**BASE_ISSUE, "id": "a"}, {**BASE_ISSUE, "id": "b"})
    mark_reported(manifest, ["a"], date(2026, 11, 18))
    mark_reported(manifest, ["b"], date(2026, 11, 17))
    a, b = load_issues(manifest)
    assert a["status"] == "reported" and a["reported"] == {"date": "2026-11-18"}
    assert b["reported"] == {"date": "2026-11-17"}
    assert list(a).index("reported") == list(a).index("status") + 1
    assert not problems([a, b])


@pytest.mark.parametrize("status, ids", [("open", ["missing"]), ("fixed", ["a"])])
def test_mark_reported_refuses(tmp_path, status, ids):
    from datetime import date
    manifest = _issue_file(tmp_path, {**BASE_ISSUE, "id": "a", "status": status})
    before = (tmp_path / "known_document_issues.json").read_text()
    with pytest.raises(SystemExit):
        mark_reported(manifest, ids, date(2026, 11, 18))
    assert (tmp_path / "known_document_issues.json").read_text() == before


def test_evidence_found_in_pdf(device):
    """Every recorded evidence string appears on its page, when the cited PDF is at hand."""
    pdfplumber = pytest.importorskip("pdfplumber", reason="needs the tools group")
    from document_issues import locate
    from fetch_document import find_local

    manifest_path = device[0]
    issues = [i for i in load_issues(manifest_path) if i.get("evidence")]
    docs = manifest_path.parent / "docs"
    checked = 0
    for doc_id in {i["document"] for i in issues}:
        pdf_path = find_local(docs, doc_id) if docs.is_dir() else None
        if not pdf_path:
            continue
        with pdfplumber.open(pdf_path) as pdf:
            for issue in (i for i in issues if i["document"] == doc_id):
                for pno, hits in locate(pdf, issue).items():
                    assert hits is not None, f"{issue['id']}: page {pno} is beyond the document"
                    missing = [t for t, boxes in hits if not boxes]
                    assert not missing, f"{issue['id']}: not on page {pno}: {missing}"
                    checked += 1
    if not checked:
        pytest.skip("no cited PDF in docs/ (TI documents are not in git)")


def test_document_recorded(device):
    _, _, data = device
    doc = data.get("document") or {}
    assert doc.get("id") and "revision" in doc, "registers JSON should record its source document"
    assert all(r.get("source_page") for r in data["registers"]), "every register should have source_page"


SERVER_PROBE = r"""
import json, server
regs = json.loads(open(server.REGISTERS_PATH).read())["registers"]
first = regs[0]
spaced = first["name"]
named = [f["name"] for f in first["fields"] if f["name"].upper() != "RESERVED"] or [first["fields"][0]["name"]]
print(json.dumps({
    "count": len(server.REGISTERS),
    "expected": len(regs),
    "list": server.list_registers(),
    "get": server.get_register(spaced),
    "explain": server.explain_bitfield(spaced, named[0]),
    "search": server.search_register(server.REGISTERS[server.register_key(spaced)]["address"]),
}))
"""


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_ids(MANIFESTS))
def test_server_serves_device(manifest_path):
    env = dict(os.environ, DEVICE_CONFIG=str(manifest_path))
    out = subprocess.run([sys.executable, "-c", SERVER_PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    result = json.loads(out.strip().splitlines()[-1])
    assert result["count"] == result["expected"]
    assert result["list"].count("\n") == result["expected"]
    for key in ("get", "explain", "search"):
        assert "Source:" in result[key], f"{key} output lacks the Source line"
    assert "not found" not in result["explain"].lower(), "explain_bitfield rejected a register name with spaces"
