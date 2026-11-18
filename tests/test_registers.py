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

from document_issues import load_issues, problems  # noqa: E402
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
