#!/usr/bin/env python3
"""
Device Documentation MCP Server

Generic MCP server for hardware device register documentation.
Device identity is driven by a device.json manifest (or DEVICE_CONFIG env var).
Defaults to TPS25751 when no manifest overrides are provided.

Dependencies are declared in pyproject.toml and pinned in uv.lock. Launch with
`uv --directory <this repo> run server.py` so uv uses this project's environment
regardless of the caller's working directory.
"""

import difflib
import json
import logging
import re
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
from mcp.server.mcpserver import MCPServer

# Configure logging to stderr to avoid interfering with stdio transport
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("device-docs-server")

# ---------------------------------------------------------------------------
# Config loader  (runs at import time so module globals are set before
# MCPServer decorators evaluate their URI f-strings)
# ---------------------------------------------------------------------------

_SERVER_DIR = Path(__file__).parent

DEFAULTS: Dict[str, Any] = {
    "server_name": "tps25751-docs",
    "device_name": "TPS25751",
    "uri_scheme": "tps25751",
    "class_prefix": "TPS25751",
    "registers_file": "tps25751_registers_extracted.json",
    "schema_file": "TPS25751-register-schema.json",
    "i2c_addresses": ["0x20", "0x21", "0x22", "0x23"],
    # Device-specific I2C protocol note shown in the config/i2c resource. Empty by
    # default so the generic server does not assert a protocol it cannot know; the
    # TPS25751 manifest sets this explicitly. Other devices may override it.
    "i2c_protocol": "",
}


def load_device_config() -> tuple[Dict[str, Any], Path]:
    """Load device configuration from manifest file.

    Returns (merged_config, manifest_dir) where manifest_dir is the directory
    containing the manifest file (or _SERVER_DIR if no manifest was found).
    """
    # Resolve manifest path from env var or default beside server.py
    env_path = os.environ.get("DEVICE_CONFIG")
    if env_path:
        manifest_path = Path(env_path)
        if not manifest_path.is_absolute():
            manifest_path = _SERVER_DIR / manifest_path
    else:
        # No DEVICE_CONFIG override: fall back to the TPS25751 device definition.
        manifest_path = _SERVER_DIR / "devices" / "tps25751" / "device.json"

    config = dict(DEFAULTS)
    manifest_dir = _SERVER_DIR

    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                overrides = json.load(f)
            config.update(overrides)
            manifest_dir = manifest_path.parent
            logger.info(
                f"Loaded device config from {manifest_path}: "
                f"device={config['device_name']}"
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"Failed to load device config from {manifest_path}: {e}. "
                "Using defaults."
            )
    else:
        logger.info(
            f"No manifest at {manifest_path}; using defaults "
            f"(device={config['device_name']})"
        )

    return config, manifest_dir


def read_server_version() -> str:
    """Return the project version from pyproject.toml beside server.py.

    The project is not installed as a package (tool.uv.package = false), so
    importlib.metadata cannot see it; pyproject.toml is the single version source.
    """
    try:
        with open(_SERVER_DIR / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        logger.warning(f"Could not read version from pyproject.toml: {e}")
        return "0.0.0+unknown"


SERVER_VERSION: str = read_server_version()

_CONFIG, _MANIFEST_DIR = load_device_config()

# Module globals set from config — these are referenced in f-string decorators below
SERVER_NAME: str = _CONFIG["server_name"]
DEVICE_NAME: str = _CONFIG["device_name"]
URI_SCHEME: str = _CONFIG["uri_scheme"]
CLASS_PREFIX: str = _CONFIG["class_prefix"]
I2C_ADDRESSES: List[str] = _CONFIG["i2c_addresses"]
I2C_PROTOCOL: str = _CONFIG.get("i2c_protocol", "")

# Resolve file paths relative to manifest directory
REGISTERS_PATH: Path = (_MANIFEST_DIR / _CONFIG["registers_file"]).resolve()
SCHEMA_PATH: Path = (_MANIFEST_DIR / _CONFIG["schema_file"]).resolve()

logger.info(
    f"Server: {SERVER_NAME} v{SERVER_VERSION} | Device: {DEVICE_NAME} | "
    f"Registers: {REGISTERS_PATH}"
)

# ---------------------------------------------------------------------------
# MCPServer instance: must be created after SERVER_NAME is set
# ---------------------------------------------------------------------------

mcp = MCPServer(SERVER_NAME, version=SERVER_VERSION)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_registers_from_json() -> Dict[str, Any]:
    """Load register definitions from the configured registers JSON file."""
    try:
        with open(REGISTERS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Register JSON file not found: {REGISTERS_PATH}")
        return {"registers": []}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse register JSON: {e}")
        return {"registers": []}

    # Schema validation (warn-only, so a schema drift never takes the server down)
    if SCHEMA_PATH.exists():
        try:
            with open(SCHEMA_PATH) as f:
                schema = json.load(f)
            jsonschema.validate(instance=data, schema=schema)
            logger.info(f"Register data validated against schema {SCHEMA_PATH}")
        except jsonschema.ValidationError as ve:
            logger.warning(
                f"Register data failed schema validation: {ve.message}. "
                "Continuing with unvalidated data."
            )
        except (json.JSONDecodeError, OSError, jsonschema.SchemaError) as e:
            logger.warning(f"Could not validate against {SCHEMA_PATH}: {e}")
    else:
        logger.info(f"Schema file not found at {SCHEMA_PATH} — validation skipped.")

    return data


def convert_hex_offset_to_address(offset: str) -> str:
    """Convert hex offset format (e.g., '3h') to standard address format (e.g., '0x03')."""
    return f"0x{int(offset.rstrip('h'), 16):02X}"


def format_bit_field_description(field: Dict[str, Any]) -> str:
    """Format a field definition into a human-readable string."""
    bit_field = field.get("bit_field", "")
    name = field.get("name", "")
    description = field.get("description", "")
    access_type = field.get("access_type", "")

    if "-" in bit_field:
        high, low = bit_field.split("-")
        bit_desc = f"bit {high}" if high == low else f"bits {high}:{low}"
    else:
        bit_desc = f"bit {bit_field}"

    reset = field.get("reset", "")
    result = f"{name} ({bit_desc})"
    tags = [t for t in (access_type, f"reset {reset}" if reset else "") if t]
    if tags:
        result += f" [{', '.join(tags)}]"
    if description:
        result += f": {description}"
    if field.get("values"):
        result += "\n    Values:"
        for value, value_desc in field["values"].items():
            result += f"\n      {value}: {value_desc}"

    return result


def convert_registers_to_legacy_format(register_data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert JSON register format to the internal format expected by the server."""
    legacy_registers = {}
    for register in register_data.get("registers", []):
        reg_name = register_key(register.get("name", ""))
        address = convert_hex_offset_to_address(register.get("offset", "0h"))
        raw_fields = register.get("fields", [])
        fields = [format_bit_field_description(f) for f in raw_fields]
        legacy_registers[reg_name] = {
            "address": address,
            "description": register.get("description", ""),
            "fields": fields,
            "field_names": [f.get("name", "") for f in raw_fields],
            "width": register.get("width", 8),
            "reset": register.get("reset", "0h"),
            "source_page": register.get("source_page"),
        }
    return legacy_registers


def format_source(document: Dict[str, Any] | None, page: int | None) -> str | None:
    """Describe where a register's data came from, e.g.
    'SLVUCR8B (Rev. B, 2026-08), page 15'. None when nothing is recorded."""
    parts = []
    if document and document.get("id"):
        meta = []
        if document.get("revision"):
            meta.append(f"Rev. {document['revision']}")
        if document.get("date"):
            meta.append(document["date"])
        parts.append(document["id"] + (f" ({', '.join(meta)})" if meta else ""))
    if page:
        parts.append(f"page {page}")
    return ", ".join(parts) or None


def source_line(reg_info: Dict[str, Any]) -> str:
    """'Source: <doc>, page N' plus a newline, or '' when nothing is recorded."""
    source = format_source(DOCUMENT, reg_info.get("source_page"))
    return f"Source: {source}\n" if source else ""


def register_key(name: str) -> str:
    """Normalize a register name the way REGISTERS is keyed ('Power Path Status' -> 'POWER_PATH_STATUS')."""
    return name.strip().upper().replace(" ", "_")


def _norm_words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def closest_fields(query: str, names: List[str], limit: int = 5, cutoff: float = 0.75) -> List[int]:
    """Indexes of field names that approximately match `query`.

    Tolerates the document's own misspellings ("Capabalities", "Missmatch") by
    comparing the query against every same-length run of words in each name.
    """
    q = _norm_words(query)
    if not q:
        return []
    n = len(q.split())
    scored = []
    for i, name in enumerate(names):
        words = _norm_words(name).split()
        windows = [" ".join(words[j:j + n]) for j in range(max(1, len(words) - n + 1))]
        best = max(difflib.SequenceMatcher(None, q, w).ratio() for w in windows)
        if best >= cutoff:
            scored.append((best, i))
    scored.sort(key=lambda t: -t[0])
    return [i for _, i in scored[:limit]]


# Load register data at startup
_REGISTER_DATA = load_registers_from_json()
REGISTERS = convert_registers_to_legacy_format(_REGISTER_DATA)
# Literature number, revision and date the register data was transcribed from.
DOCUMENT: Dict[str, Any] | None = _REGISTER_DATA.get("document")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource(f"{URI_SCHEME}://registers")
def registers_index_resource() -> str:
    """Index of all registers: name, address, and first line of description."""
    source = format_source(DOCUMENT, None)
    lines = [f"{DEVICE_NAME} Register Index" + (f" (source: {source})" if source else "") + "\n"]
    for reg_name, reg_info in REGISTERS.items():
        first_line = reg_info["description"].split("\n")[0].split(".")[0].strip()
        lines.append(f"  {reg_name} ({reg_info['address']}, {reg_info['width']} bits): {first_line}")
    return "\n".join(lines)


@mcp.resource(f"{URI_SCHEME}://register/{{reg_name}}")
def register_resource(reg_name: str) -> str:
    """Device register documentation resource."""
    key = register_key(reg_name)
    if key not in REGISTERS:
        available = ", ".join(REGISTERS.keys())
        return f"Unknown register: {reg_name}. Available registers: {available}"

    reg_info = REGISTERS[key]
    # Derive class and variable names from CLASS_PREFIX + normalized key
    title_key = key.title().replace("_", "")
    class_name = f"{CLASS_PREFIX}{title_key}"
    var_name = key.lower().replace("_", "")

    fields_text = "\n".join(f"  • {f}" for f in reg_info["fields"])
    return f"""{DEVICE_NAME} {key} Register (Address: {reg_info['address']})

{source_line(reg_info)}Description: {reg_info['description']}

Bit Fields:
{fields_text}

Usage in Code:
```cpp
{class_name} {var_name};
if ({var_name}.readFromChip(device)) {{
    {var_name}.debugPrint();
}}
```

Library Class: {class_name}
Header File: {class_name}.h
"""


@mcp.resource(f"{URI_SCHEME}://config/i2c")
def i2c_config_resource() -> str:
    """Device I2C configuration and protocol details."""
    addrs = "\n".join(
        f"  • Address {i}: {addr}" for i, addr in enumerate(I2C_ADDRESSES)
    )
    first_addr = I2C_ADDRESSES[0] if I2C_ADDRESSES else "0x00"

    # Protocol note is manifest-driven: only assert a protocol the manifest declares,
    # so the generic server doesn't fabricate one for an arbitrary device.
    protocol_section = (
        f"I2C Protocol:\n{I2C_PROTOCOL}\n"
        if I2C_PROTOCOL
        else "I2C Protocol:\nSee the device datasheet for the register read/write protocol.\n"
    )

    return f"""{DEVICE_NAME} I2C Configuration

Default I2C Addresses:
{addrs}

{protocol_section}
Example Usage (illustrative — use this device's actual library classes):
```cpp
#include "{CLASS_PREFIX}.h"

{CLASS_PREFIX} device(Wire, {first_addr});

void setup() {{
    Wire.begin();
    device.begin();
}}
```
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_register(query: str) -> str:
    """Search for device register information by name, address, or keyword.

    Args:
        query: Register name, address (e.g. 0x1A), or search term (e.g. "interrupt")
    """
    q = query.upper()
    results: List[str] = []

    for reg_name, reg_info in REGISTERS.items():
        if (q in reg_name
                or q in reg_info["address"].upper()
                or q.lower() in reg_info["description"].lower()):
            fields_text = "\n".join(f"  • {f}" for f in reg_info["fields"])
            results.append(
                f"**{reg_name} Register (Address: {reg_info['address']})**\n"
                f"Width: {reg_info['width']} bits, Reset: {reg_info['reset']}\n"
                f"{source_line(reg_info)}"
                f"{reg_info['description']}\n\n"
                f"Bit Fields:\n{fields_text}"
            )

    if not results:
        return f"No registers found matching query: {query}"

    return (
        f"{DEVICE_NAME} Register Search Results for '{query}':\n\n"
        + "\n\n---\n\n".join(results)
    )


@mcp.tool()
def explain_bitfield(register: str, field: str) -> str:
    """Get detailed information about a specific bit field within a register.

    Args:
        register: Register name (e.g. STATUS, MODE, POWER_PATH_STATUS)
        field: Bit field name or part of the name (e.g. "Connection State", "Mode")
    """
    key = register_key(register)
    if key not in REGISTERS:
        available = ", ".join(REGISTERS.keys())
        return f"Unknown register: {register}. Available registers: {available}"

    reg_info = REGISTERS[key]
    matching = [f for f in reg_info["fields"] if field.lower() in f.lower()]
    note = ""

    if not matching:
        close = closest_fields(field, reg_info["field_names"])
        if not close:
            names = sorted({n for n in reg_info["field_names"] if n.upper() != "RESERVED"})
            return (
                f"Field '{field}' not found in {key} register.\n"
                f"Field names ({len(names)}, RESERVED omitted):\n"
                + "\n".join(f"  • {n}" for n in names)
            )
        matching = [reg_info["fields"][i] for i in close]
        note = (f"No exact match for '{field}'; closest field names follow "
                f"(the document's own spelling may differ).\n\n")

    matching_text = "\n".join(f"  • {f}" for f in matching)
    return (
        f"{note}"
        f"**{key} Register — {field}**\n\n"
        f"Register Address: {reg_info['address']}\n"
        f"{source_line(reg_info)}"
        f"Register Description: {reg_info['description']}\n\n"
        f"Matching Fields:\n{matching_text}"
    )


@mcp.tool()
def list_registers() -> str:
    """List every register with its address, width, reset value and source page.

    One call gives the register map: use it to check sizes or find a name
    before calling get_register.
    """
    source = format_source(DOCUMENT, None)
    lines = [f"{DEVICE_NAME} registers ({len(REGISTERS)})" + (f", source: {source}" if source else "")]
    for reg_name, reg_info in sorted(REGISTERS.items(), key=lambda kv: int(kv[1]["address"], 16)):
        page = f", p.{reg_info['source_page']}" if reg_info.get("source_page") else ""
        lines.append(
            f"  {reg_info['address']}  {reg_name}  {reg_info['width']} bits"
            f" ({-(-reg_info['width'] // 8)} bytes), reset {reg_info['reset']}{page}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_register(name: str) -> str:
    """Exact lookup of a register by name (case-insensitive).

    Returns structured output: address, width, reset, the source document
    revision and page when recorded, description, and all bit fields.
    If not found, lists available register names.

    Args:
        name: Register name (e.g. "STATUS", "Mode", "POWER_PATH_STATUS")
    """
    key = register_key(name)
    if key not in REGISTERS:
        available = sorted(REGISTERS.keys())
        available_list = "\n".join(f"  • {r}" for r in available)
        return (
            f"Register '{name}' not found.\n\n"
            f"Available registers ({len(available)}):\n{available_list}"
        )

    reg_info = REGISTERS[key]
    fields_text = "\n".join(f"  • {f}" for f in reg_info["fields"])
    return (
        f"**{key} Register**\n\n"
        f"Address: {reg_info['address']}\n"
        f"Width: {reg_info['width']} bits\n"
        f"Reset: {reg_info['reset']}\n"
        f"{source_line(reg_info)}"
        f"Description: {reg_info['description']}\n\n"
        f"Bit Fields:\n{fields_text}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
