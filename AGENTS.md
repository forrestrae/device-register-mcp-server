# Device Register Documentation MCP Server

This is a **device-generic** MCP server that provides register documentation for
hardware devices, extracted from their official manufacturer manuals. One server binary
(`server.py`) serves whichever device a `device.json` manifest selects.

## Configured devices

Two devices are defined and exposed as two MCP servers (both run this same `server.py`
with a different `DEVICE_CONFIG`):

| MCP server | Device | Tool prefix |
|---|---|---|
| `tps25751-docs` | TPS25751 USB-C PD controller | `mcp__tps25751-docs__*` |
| `bq25798-docs` | BQ25798 buck-boost charger | `mcp__bq25798-docs__*` |

**Use `tps25751-docs`** for the host PD controller's own registers, and **`bq25798-docs`**
for the downstream charger reached over the TPS25751's I2Cc bus. Both expose the same
four tools and the same resource URIs (under each device's own `uri_scheme`).

## Layout

```
device-register-mcp-server/
  server.py              # generic server; device chosen via DEVICE_CONFIG
  pyproject.toml         # uv project: dependencies; version reported as serverInfo.version
  uv.lock                # pinned dependency versions
  devices/
    tps25751/  device.json + tps25751_registers_extracted.json + TPS25751-register-schema.json
    bq25798/   device.json + bq25798_registers_extracted.json  + BQ25798-register-schema.json
    <device>/docs/         # source PDFs; gitignored, not redistributable
  tools/
    extract_registers.py   # PDF text layer -> registers JSON (reference extraction)
    validate_registers.py  # structural checks; with --pdf or --fetch, field-by-field comparison
    diff_registers.py      # summarize changes between two registers JSON files
    fetch_document.py      # find and download the latest revision via document_url
    document_issues.py     # record errors in the source document; review them; print TI feedback form text
  tests/                   # offline pytest suite over every device (uv run --group test pytest)
```

Each device's registers JSON is the single source of truth for that device, validated
at startup against the schema in the same folder.

## Available Tools

Each server exposes four tools (shown below for `tps25751-docs`; the `bq25798-docs`
server exposes the identical tools under the `mcp__bq25798-docs__` prefix).

### `mcp__tps25751-docs__search_register`

Search for registers by name, address, or keyword.

**Parameters:**
- `query` (string): Register name, address, or search term

**Examples:**
```
search_register("MODE")           # Find MODE register
search_register("0x1A")          # Find register at address 0x1A
search_register("interrupt")     # Find all interrupt-related registers
search_register("boot")          # Find boot-related registers
```

**Returns:**
- Register name and address
- Full description from TI manual
- All bit fields with positions, access types, and descriptions
- Enumerated values where applicable

### `mcp__tps25751-docs__explain_bitfield`

Get detailed information about specific bit fields within a register.

**Parameters:**
- `register` (string): Register name (e.g., "STATUS", "MODE")
- `field` (string): Bit field name or part of the name

**Examples:**
```
explain_bitfield("STATUS", "Connection State")
explain_bitfield("MODE", "Mode")
explain_bitfield("INTERRUPT_EVENT_FOR_I2CT_IRQ", "Liquid Detection")
```

**Returns:**
- Register address, source document and page, and description
- Matching bit field(s) with:
  - Bit positions (e.g., "bits 3:1" or "bit 30")
  - Access type and field reset, e.g. `[R/W, reset 0h]`
  - Detailed description
  - Enumerated values (e.g., "0h = No connection", "1h = Port disabled")
- Register names are accepted with spaces or underscores, in any case.
- When no field contains the query, the closest field names are matched instead, so a
  search still finds fields the document misspells (revision B of the TPS25751 manual
  spells "Capabalities"). With no close match, the reply lists the field names only.

### `mcp__tps25751-docs__list_registers`

The register map in one call: every register's address, name, width in bits and bytes,
reset value, and source page, headed by the source document. Use it to check register
sizes or find a name before calling `get_register`.

**Parameters:** none

### `mcp__tps25751-docs__get_register`

Exact, case-insensitive lookup of a register by name. Unlike `search_register`
(which does fuzzy/keyword matching), this returns the single named register or
an error with the full list of available register names.

**Parameters:**
- `name` (string): Exact register name (spaces or underscores, any case)

**Examples:**
```
get_register("STATUS")
get_register("Power Path Status")
get_register("INTERRUPT_EVENT_FOR_I2CT_IRQ")
```

**Returns:**
- Address, width, reset value, description, and all bit fields for the named register
- Source: the document's literature number, revision, and date, plus the page where
  the register is described (also shown by `search_register`, `explain_bitfield`, and
  the `register/{reg_name}` resource) (for example `SLVUCR8B (Rev. B, 2026-08), page 19`), when
  the registers JSON records them. Cite it when a register fact goes into code or a
  debugging conclusion.
- If not found: list of all available register names

## Usage Guidelines

### When to Use the MCP Server

Take register addresses, bit positions, access types, and enumerated values from
these MCP tools, not from memory or older copies of the manual. A wrong offset or bit
position compiles cleanly and fails only on hardware, so verify each one with the tools
before you use it in code or in a debugging conclusion.

### Typical Workflow

**Adding support for a new register:**

1. **Search for the register** using `mcp__tps25751-docs__search_register` with
   `query = "REGISTER_NAME"`. This gives you the address, description, and all fields.

2. **Fetch the exact register** using `mcp__tps25751-docs__get_register` with
   `name = "REGISTER_NAME"` for a structured single-register view.

3. **Understand specific fields** using `mcp__tps25751-docs__explain_bitfield` with
   `register = "REGISTER_NAME"` and `field = "field_name"`. This shows bit positions,
   access types, and enumerated values.

4. Implement the register class using the information from steps 1-3.

## Data Format

### JSON Schema

Registers follow this structure:
```json
{
  "offset": "3h",           // Hex offset with 'h' suffix
  "name": "Mode",
  "width": 32,              // Width in bits
  "reset": "00000000h",     // Reset value
  "description": "...",
  "fields": [
    {
      "bit_field": "31-0",
      "name": "Mode",
      "access_type": "R",   // R, R/W, or W
      "reset": "0h",
      "description": "...",
      "values": {           // Optional enumerated values
        "APP ": "Application mode",
        "BOOT": "Boot mode"
      }
    }
  ]
}
```

## Available Resources

In addition to the tools, the server exposes MCP resources queryable by URI.

### `tps25751://registers`

Index of all registers: name, address, and first line of description. Useful for
a quick overview without running a search query.

### `tps25751://register/{reg_name}`

Full register documentation for a single register (same content as `get_register`
but accessed as a resource rather than a tool call).

Example: `tps25751://register/STATUS`

### `tps25751://config/i2c`

I2C address list, protocol description, and a generic usage code snippet for the
configured device.

## Server Implementation

The MCP server (`server.py`) dynamically loads register data at startup:

1. Reads the configured registers JSON file (from `device.json` / `DEVICE_CONFIG`)
2. Validates the JSON against the device's schema using `jsonschema` (logs a
   WARNING on failure but does not crash; skips validation if no schema file exists)
3. Converts hex format ("3h" → "0x03")
4. Formats bit fields with ranges and access types
5. Builds searchable register dictionary
6. Serves data through MCP protocol

### Key Functions

- `load_device_config()`: Load manifest file and merge with defaults
- `load_registers_from_json()`: Load JSON file with optional schema validation
- `convert_hex_offset_to_address()`: Convert "3h" to "0x03" format
- `format_bit_field_description()`: Format fields with enumerated values
- `convert_registers_to_legacy_format()`: Transform JSON to server format

## Serving a Different Device

The server is fully device-generic. To serve registers for a different chip:

1. Create a `device.json` manifest (anywhere on disk) with these fields:

   | Field | Type | Description |
   |---|---|---|
   | `server_name` | string | MCP server name (e.g. `"bq25798-docs"`) |
   | `device_name` | string | Human-readable chip name (e.g. `"BQ25798"`) |
   | `uri_scheme` | string | URI prefix for resources (e.g. `"bq25798"`) |
   | `class_prefix` | string | C++ class prefix for code snippets (e.g. `"BQ25798"`) |
   | `registers_file` | string | Path to registers JSON, relative to the manifest |
   | `schema_file` | string | Path to JSON schema for validation, relative to the manifest |
   | `i2c_addresses` | array | List of I2C addresses as hex strings (e.g. `["0x6B"]`) |
   | `i2c_protocol` | string | Optional one-line I2C access note for the `config/i2c` resource; omit for a neutral "see datasheet" note |
   | `document_url` | string | Optional stable link to the latest source document, such as `https://www.ti.com/lit/pdf/slvucr8`; used by `tools/fetch_document.py` and `validate_registers.py --fetch`, ignored by the server |

   All fields are optional — missing keys fall back to the TPS25751 defaults.

   By convention each device gets its own folder `devices/<name>/` holding the
   manifest, registers JSON, and schema (all referenced by plain filename).

2. Create a schema-valid `<device>_registers.json` and its schema beside the manifest.

3. Point the server at your manifest via the `DEVICE_CONFIG` env var (resolved relative
   to `server.py`'s directory when not absolute):

   ```bash
   DEVICE_CONFIG=devices/<name>/device.json uv run server.py
   ```

4. Register it as an MCP server by adding an entry to `.mcp.json` (and `.claude/settings.json`
   permissions) — see the `tps25751-docs` / `bq25798-docs` entries as templates. The
   `/add-register-device` skill automates this whole flow.

### Example: BQ25798

The BQ25798 device lives at `devices/bq25798/` (manifest + registers JSON +
`BQ25798-register-schema.json`) and is served as the `bq25798-docs` MCP server:

```bash
# From the device-register-mcp-server/ directory:
DEVICE_CONFIG=devices/bq25798/device.json uv run server.py
```

Each device uses its **own** schema (the TPS25751 and BQ25798 schemas differ — the
BQ25798 one additionally allows `"X"` resets, `reset_sources`, and `encoding` metadata).

## Maintenance

### Updating Register Data

For a born-digital TI document, regenerate the registers JSON from the PDF rather than
editing it by hand, then review the diff before replacing the current file.

1. Check whether TI has published a newer revision than the one the JSON records:

   ```bash
   uv run --group tools tools/fetch_document.py devices/tps25751/device.json --check
   ```

   It prints the latest literature number and exits 1 when the registers JSON is behind.

2. Download the latest revision to `devices/tps25751/docs/<LITERATURE-ID>.pdf`:

   ```bash
   uv run --group tools tools/fetch_document.py devices/tps25751/device.json
   ```

3. Extract a candidate registers JSON:

   ```bash
   uv run --group tools tools/extract_registers.py devices/tps25751/docs/<LITERATURE-ID>.pdf \
       --manifest devices/tps25751/device.json -o /tmp/tps25751_candidate.json
   ```

4. Review what changed against the current file:

   ```bash
   uv run --group tools tools/diff_registers.py devices/tps25751/tps25751_registers_extracted.json \
       /tmp/tps25751_candidate.json
   ```

5. Copy the candidate over `devices/tps25751/tps25751_registers_extracted.json`, run the
   validator described in the next section, and restart the MCP server, which loads the
   JSON at startup. No code changes are needed in `server.py`.

Register names come straight from the document, so a renamed register changes its
`get_register` key. The TPS25751 revision B manual renamed several registers, for
example `Interrupt Event for I2C1` to `Interrupt Event for I2Ct_IRQ`.

### Validating register data

`tools/validate_registers.py` checks a device's registers JSON against its source PDF.
Run it after any edit to a registers JSON, and before relying on the data for a new
document revision. The PDFs are gitignored, so they are fetched or placed under
`devices/<device>/docs/` locally.

```bash
uv run --group tools tools/validate_registers.py devices/tps25751/device.json --fetch
```

`--fetch` compares against the latest revision from the manifest's `document_url`,
downloading it once to `docs/`. Pass `--pdf <path>` instead to compare against a
specific local revision.

The validator reads the PDF text layer with pdfplumber, so the reference is exact for
born-digital TI documents and needs no OCR or LLM. It parses TI's per-register template:
the `<Name> Register (Offset = ...) [Reset = ...]` heading, the Bit / Field / Type /
Reset / [Notes] / Description table, and the register summary table.

It reports errors for differences the JSON introduced: a missing or extra register,
or a mismatched width, reset, bit range, field name, access type, enumerated value,
reset source, or encoding. It reports warnings for prose that differs after
normalization and for template text leaked into descriptions. Inconsistencies that
exist in the source document itself, such as overlapping RESERVED rows or a register
reset that disagrees with its field resets, are reported as warnings marked "same in
source document". The exit code is 1 when any error remains.

Without `--pdf`, only the schema and structural checks run, and source-document
inconsistencies cannot be told apart from JSON errors.

`tools/extract_registers.py` produces the reference extraction on its own, which is
useful for diffing two revisions of a document:

```bash
uv run --group tools tools/extract_registers.py <doc.pdf> -o <out.json>
```

Both schemas accept an optional top-level `document` block (`id`, `revision`, `date`,
`title`) and a per-register `source_page`, so a registers JSON can record the
literature number and revision it was transcribed from, for example `SLVUCR8B`.

### Known document issues

Manufacturer documents contain their own errors: the BQ25798 datasheet gives REG48 a
reset of `0h` while its field table composes to `19h`, and the TPS25751 revision B
manual misspells several field names. A faithful registers JSON inherits them, so each
device records them in `devices/<device>/known_document_issues.json`. The validator and
the tests report a finding that matches an open or reported issue as a warning rather
than an error, which keeps the offline check meaningful without the PDF. A recorded
issue that no longer reproduces is flagged so it can be marked `fixed`.

`tools/document_issues.py` manages the file:

```bash
uv run --group tools tools/document_issues.py list devices/bq25798/device.json
uv run --group tools tools/document_issues.py discover devices/bq25798/device.json --pdf <doc.pdf>
uv run --group tools tools/document_issues.py review devices/bq25798/device.json --open
uv run --group tools tools/document_issues.py feedback devices/bq25798/device.json
```

`discover` drafts entries for structural problems found in the PDF itself (`--write`
appends them; edit the summary and description before filing).

Review each issue against the document before filing it. `review` renders every cited
page with the issue's `evidence` strings boxed in red and writes a local HTML page that
shows each image beside the issue's description and the exact form text, then prints
its path. Add `--open` to open it in the default browser. Each image links to the PDF
at that page. An `evidence` entry is `{"page": N, "text": "..."}`,
with the text copied exactly from the PDF; a string not found on its page is flagged,
which usually means the claim or the string is wrong. The PDF is located in
`devices/<device>/docs/` by the issue's literature number (`--pdf` overrides), and the
output goes to `devices/<device>/docs/review/`, which is gitignored because TI documents
are not redistributable. With the PDF present, the tests check that every evidence
string is on its page. `review` only displays; correct the issue file by hand.

`feedback` prints each open issue as the fields of TI's Technical Documents:
Documentation Feedback form, with a link that fills in Literature Number and Part
Number; the review page lists the same link at the top. The form reads no other URL
parameters and clears Summary, Page Number(s), and Detailed description on load, so
those, and Severity, are pasted by hand. It also rejects text that is not plain ASCII,
text that looks like markup such as `<...>`, and page lists over 50 characters, so the
issue file is checked against those rules whenever it is loaded. The form has a CAPTCHA
, so filing is manual. After filing, set the issue's `status` to `reported` and add a 
`reported` object with the date and any reference TI provides.

### Tests

```bash
uv run --group test pytest
```

The suite runs the schema and structural checks on every device's registers JSON
offline and fails on any finding not explained by a known document issue. It also
starts the server for each device and checks that all registers are served and that
lookups by spaced name return a source line. Run it after any edit to a registers JSON.

### Adding New Registers

Add new register entries to the JSON file following the schema:
- Use hex format with 'h' suffix (e.g., "3h")
- Include all required fields: offset, name, width, reset, fields
- Specify access_type for each field: "R", "R/W", or "W"
- Add enumerated values where applicable

## Integration with Main Project

The main TPS25751 Arduino/PlatformIO project uses this MCP server for:
- Looking up register information during development
- Verifying register implementations against the TI manual
- Understanding bit field meanings for debugging

Consumer library repos document their own use of this server in their own instruction files.