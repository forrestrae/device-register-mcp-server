#!/usr/bin/env python3
"""Summarize the differences between two registers JSON files.

Use it to review a regenerated registers JSON before replacing the current one,
or to see what changed between two revisions of a document (run
extract_registers.py on each and diff the results).

Registers are matched by offset and fields by bit range. Prose changes are
counted rather than printed unless --prose is given.

Usage:
  uv run --group tools tools/diff_registers.py OLD.json NEW.json [--prose]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_registers import bits, hex_value, norm_name, norm_prose, similarity  # noqa: E402

PROSE_SAME = 0.97


def _doc(d: dict) -> str:
    doc = d.get("document") or {}
    return f"{doc.get('id', '(no document block)')} {doc.get('date', '')}".strip()


def diff(old: dict, new: dict, show_prose: bool) -> list[str]:
    out = [f"old: {_doc(old)}", f"new: {_doc(new)}", ""]
    a = {hex_value(r["offset"]): r for r in old.get("registers", [])}
    b = {hex_value(r["offset"]): r for r in new.get("registers", [])}

    added = [b[k] for k in sorted(set(b) - set(a))]
    removed = [a[k] for k in sorted(set(a) - set(b))]
    if added:
        out.append(f"Registers added ({len(added)}):")
        out += [f"  + {r['offset']:>4} {r['name']} ({r['width']} bits, {len(r['fields'])} fields)" for r in added]
    if removed:
        out.append(f"Registers removed ({len(removed)}):")
        out += [f"  - {r['offset']:>4} {r['name']}" for r in removed]

    prose_changes = 0
    changed_regs = 0
    for k in sorted(set(a) & set(b)):
        ra, rb = a[k], b[k]
        lines: list[str] = []
        if norm_name(ra["name"]) != norm_name(rb["name"]):
            lines.append(f"name: {ra['name']!r} -> {rb['name']!r}")
        if ra.get("width") != rb.get("width"):
            lines.append(f"width: {ra.get('width')} -> {rb.get('width')}")
        if hex_value(ra.get("reset", "")) != hex_value(rb.get("reset", "")):
            lines.append(f"reset: {ra.get('reset')} -> {rb.get('reset')}")
        if similarity(norm_prose(ra.get("description", "")), norm_prose(rb.get("description", ""))) < PROSE_SAME:
            prose_changes += 1
            if show_prose:
                lines.append(f"description: {ra.get('description', '')!r}\n      -> {rb.get('description', '')!r}")

        fa = {bits(f["bit_field"]): f for f in ra.get("fields", [])}
        fb = {bits(f["bit_field"]): f for f in rb.get("fields", [])}
        for rng in sorted(set(fa) | set(fb), reverse=True):
            label = f"[{rng[0]}-{rng[1]}]"
            f, g = fa.get(rng), fb.get(rng)
            if f is None:
                lines.append(f"+ field {label} {g['name']} {g.get('access_type')} reset {g.get('reset')}")
                continue
            if g is None:
                lines.append(f"- field {label} {f['name']}")
                continue
            parts = []
            if norm_name(f["name"]) != norm_name(g["name"]):
                parts.append(f"name {f['name']!r} -> {g['name']!r}")
            if f.get("access_type") != g.get("access_type"):
                parts.append(f"access {f.get('access_type')} -> {g.get('access_type')}")
            if hex_value(f.get("reset", "")) != hex_value(g.get("reset", "")):
                parts.append(f"reset {f.get('reset')} -> {g.get('reset')}")
            va = {hex_value(x): y for x, y in (f.get("values") or {}).items()}
            vb = {hex_value(x): y for x, y in (g.get("values") or {}).items()}
            plus = sorted(set(vb) - set(va), key=lambda v: -1 if v is None else v)
            minus = sorted(set(va) - set(vb), key=lambda v: -1 if v is None else v)
            if plus:
                parts.append("values added " + ", ".join(f"{v:X}h={vb[v]!r}" for v in plus))
            if minus:
                parts.append("values removed " + ", ".join(f"{v:X}h" for v in minus))
            reworded = [v for v in set(va) & set(vb)
                        if similarity(norm_prose(va[v]), norm_prose(vb[v])) < PROSE_SAME]
            if reworded:
                parts.append("values reworded " + ", ".join(f"{v:X}h" for v in sorted(reworded)))
            if similarity(norm_prose(f.get("description", "")), norm_prose(g.get("description", ""))) < PROSE_SAME:
                prose_changes += 1
                if show_prose:
                    parts.append(f"description {f.get('description', '')!r} -> {g.get('description', '')!r}")
            if parts:
                lines.append(f"~ field {label} {g['name']}: " + "; ".join(parts))
        if lines:
            changed_regs += 1
            out.append(f"\n{rb['offset']} {rb['name']}" + (f" (p.{rb['source_page']})" if rb.get("source_page") else ""))
            out += [f"    {line}" for line in lines]

    out.append(f"\n{len(added)} added, {len(removed)} removed, {changed_regs} changed; "
               f"{prose_changes} description(s) reworded" + ("" if show_prose else " (use --prose to show)"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--prose", action="store_true", help="print reworded descriptions")
    args = ap.parse_args()
    lines = diff(json.loads(args.old.read_text()), json.loads(args.new.read_text()), args.prose)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
