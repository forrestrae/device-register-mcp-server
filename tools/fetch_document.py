#!/usr/bin/env python3
"""Find and download the latest revision of a device's source document.

A device opts in with a "document_url" in its device.json, such as TI's
literature link https://www.ti.com/lit/pdf/slvucr8. TI redirects that link to the
current revision's file (.../slvucr8b/slvucr8b.pdf), so the latest revision is
known from one HEAD request, without downloading the PDF.

Downloads are cached as devices/<device>/docs/<LITERATURE-ID>.pdf (gitignored;
TI documents are not redistributable). A PDF already in docs/ with the same
literature number is reused under whatever name it has.

Usage:
  uv run --group tools tools/fetch_document.py devices/tps25751/device.json --check
  uv run --group tools tools/fetch_document.py devices/tps25751/device.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

USER_AGENT = "device-register-tools/0.1"
TIMEOUT = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib re-issues a redirected HEAD as GET, which would download the PDF.
    # Stop at each redirect and follow it by hand instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve(url: str, max_hops: int = 5) -> str:
    """Follow redirects with HEAD requests and return the final URL."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(max_hops):
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=TIMEOUT):
                return url
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urljoin(url, e.headers["Location"])
                continue
            raise
    raise RuntimeError(f"too many redirects resolving {url}")


def literature_id(url: str) -> str | None:
    """'https://www.ti.com/lit/ug/slvucr8b/slvucr8b.pdf?ts=...' -> 'SLVUCR8B'."""
    name = Path(urlparse(url).path).name
    m = re.fullmatch(r"([a-z]{4}[a-z0-9]+)\.pdf", name, re.I)
    return m.group(1).upper() if m else None


def manifest_info(manifest_path: Path) -> tuple[dict, Path]:
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("document_url"):
        raise SystemExit(f"{manifest_path} has no document_url")
    return manifest, manifest_path.resolve().parent / "docs"


def latest(manifest_path: Path) -> tuple[str, str]:
    """Return (literature id, final PDF URL) of the latest revision."""
    manifest, _ = manifest_info(manifest_path)
    final = resolve(manifest["document_url"])
    doc_id = literature_id(final)
    if not doc_id:
        raise SystemExit(f"cannot read a literature number from {final}")
    return doc_id, final


def find_local(docs: Path, doc_id: str) -> Path | None:
    """A PDF already in docs/ whose literature number is doc_id, under any file name."""
    from extract_registers import document_info
    import pdfplumber

    for pdf_path in sorted(docs.glob("*.pdf")):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                if document_info(pdf).get("id") == doc_id:
                    return pdf_path
        except Exception:  # an unreadable file is simply not a match
            continue
    return None


def fetch(manifest_path: Path) -> Path:
    """Download the latest revision into docs/ unless it is already there."""
    _, docs = manifest_info(manifest_path)
    doc_id, final = latest(manifest_path)
    target = docs / f"{doc_id}.pdf"
    if target.exists():
        return target
    existing = find_local(docs, doc_id)
    if existing:
        return existing
    docs.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(final, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, \
            tempfile.NamedTemporaryFile(dir=docs, suffix=".part", delete=False) as tmp:
        if "pdf" not in resp.headers.get("Content-Type", ""):
            raise SystemExit(f"{final} did not return a PDF ({resp.headers.get('Content-Type')})")
        shutil.copyfileobj(resp, tmp)
    part = Path(tmp.name)
    if part.read_bytes()[:5] != b"%PDF-":
        part.unlink()
        raise SystemExit(f"{final} did not return a PDF")
    part.replace(target)
    return target


def registers_document_id(manifest_path: Path) -> str | None:
    manifest = json.loads(manifest_path.read_text())
    reg = manifest_path.resolve().parent / manifest.get("registers_file", "")
    if not reg.exists():
        return None
    return (json.loads(reg.read_text()).get("document") or {}).get("id")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path, help="the device's device.json")
    ap.add_argument("--check", action="store_true",
                    help="only report the latest revision and whether the registers JSON is current")
    args = ap.parse_args()

    doc_id, final = latest(args.manifest)
    current = registers_document_id(args.manifest)
    print(f"latest:  {doc_id}  {final.split('?')[0]}")
    print(f"current: {current or '(registers JSON has no document block)'}")
    stale = current != doc_id
    if stale:
        print("The registers JSON was not built from the latest revision.")
    if not args.check:
        print(f"pdf:     {fetch(args.manifest)}")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
