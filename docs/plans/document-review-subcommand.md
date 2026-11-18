# Plan: `review` subcommand for tools/document_issues.py

## Context

`document_issues.py` records errors in a manufacturer's document and prints them as TI
feedback-form text. Before anyone files one, a human should look at the cited page and
confirm the claim. Today that means opening the PDF, finding the page, and hunting for the
text by hand. The new `review` subcommand produces a local HTML page that shows each issue
next to an image of the cited page(s), with the offending text boxed in red. It only shows
evidence; it does not change `known_document_issues.json` (decided with the user).

Everything stays local: the PDFs and anything rendered from them are not redistributable,
so output goes under the gitignored `devices/<device>/docs/`.

## Design

### 1. New optional issue field: `evidence`

A list of `{"page": <int>, "text": "<exact string as it appears in the PDF>"}`. It tells
the reviewer, and the tool, what to look at on each page.

- `problems()` (`tools/document_issues.py:66`) validates it: a list of objects, `page` an
  int contained in the issue's `page` value, `text` a non-empty string.
- Add it to the field list in the module docstring.
- Populate it for the three existing issues, choosing strings from the PDF text layer
  during implementation (check each with `page.search()`):
  - `REG48-reset` (p.128): the `[Reset = ...]` heading text and the PN / DEV_REV reset
    cells in Table 7-70.
  - `REG2E-ADC_AVG-text` (p.113): `Running average (not available for)`.
  - `rev-B-misspellings`: `Capabalities` (pp. 19, 20, 21, 23), `Discover mode Completed`
    (21, 23), `Mitigaion` (25), `Capabality Missmatch` (39), `Capabality` (41),
    `poewr` (51).

When an issue has no `evidence`, fall back to boxing the register heading (search the
page for `Offset = <register>`), and say on the page that no evidence text is recorded.

### 2. Locating the PDF

Reuse `find_local(docs, doc_id)` from `tools/fetch_document.py:84` with the issue's
`document` (literature number), so a revision under any file name in `docs/` is found.
`--pdf PATH` overrides for a single document. If no file matches, the issue's section on
the page says which literature number is missing and that older revisions must be placed
in `docs/` by hand (`fetch_document.py` downloads only the latest).

### 3. Rendering

pdfplumber is already in the `tools` group and bundles pypdfium2, so no new dependency.
For each cited page (1-based physical page, matching `source_page` from
`extract_registers.py`):

- `page.search(text)` for every evidence string on that page; collect match boxes.
- `page.to_image(resolution=110).draw_rects(boxes, stroke="red", stroke_width=3,
  fill=(255, 0, 0, 40))`, saved as `<issue-id>-p<N>.png`.
- An evidence string with no match is listed on the page as **not found on page N**. This
  is itself a review signal: the claim may be stale or the string mistyped.

### 4. The HTML page

Written to `devices/<device>/docs/review/index.html` (or `--out DIR`), with plain inline
CSS that works in light and dark mode. Per issue:

- Heading: id, status, severity, document, register / bits.
- Summary and detailed description (HTML-escaped, line breaks kept).
- The annotated page image(s), each captioned with the page number and linked to
  `file://<pdf>#page=N` so the reviewer can open the full PDF at that page in the browser.
- Evidence strings and whether each was found.
- The exact form text from the existing `feedback_text()` (`document_issues.py:112`) in a
  `<pre>` block, so what is reviewed is what gets filed.

Then `webbrowser.open()` the file unless `--no-open` is given, and print its path.

### 5. CLI

```
document_issues.py review MANIFEST [ID ...] [--pdf PATH] [--out DIR] [--no-open]
```

Default selection matches `feedback`: every open issue. Factor the selection and
unknown-id check out of `main()` (`document_issues.py:200-205`) into a small `select()`
helper used by both subcommands. Put the rendering code in its own section
(`# ---- review ----`) in `document_issues.py`; import pdfplumber inside the function, as
`discover()` does, so `list` and `feedback` keep working without the tools group.

### 6. Documentation

- Module docstring: the `evidence` field and the `review` usage line.
- `CLAUDE.md`, "Known document issues": add the `review` command between `discover` and
  `feedback`, and say that review comes before filing. Same in README.md if it covers
  document_issues.py.

## Critical files

- `tools/document_issues.py`: `evidence` validation, `select()`, `review` section, CLI.
- `devices/bq25798/known_document_issues.json`, `devices/tps25751/known_document_issues.json`: add `evidence`.
- `tests/test_registers.py`: tests below.
- `CLAUDE.md` (and README.md if relevant).

Reused: `find_local()` (`tools/fetch_document.py:84`), `feedback_text()`, `pages_text()`,
`load_issues()`, `problems()` (`tools/document_issues.py`).

## Verification

1. `uv run --group test pytest`, with new tests:
   - `problems()` rejects malformed `evidence` (non-list, page outside the issue's pages,
     empty text) and the checked-in files pass.
   - When the issue's PDF is present in `docs/`, every evidence string is found on its
     page (skip otherwise, since the PDFs are not in git).
2. `uv run --group tools tools/document_issues.py review devices/bq25798/device.json`:
   the browser shows REG48 and REG2E with red boxes on pages 128 and 113.
3. Same for `devices/tps25751/device.json`: eight page images with every misspelling
   boxed; the file links open the PDF at the right page.
4. `review ... --pdf /nonexistent.pdf` and a manifest with no PDF in `docs/`: the page
   reports the missing document instead of crashing.
5. Confirm `git status` shows nothing under `devices/*/docs/`.
