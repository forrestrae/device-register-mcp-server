# Device Register Docs MCP Server

A **device-generic** MCP (Model Context Protocol) server that serves hardware
register documentation extracted from official manufacturer manuals. One server
binary (`server.py`) serves whichever device a `device.json` manifest selects —
add a new chip by adding a `devices/<name>/` folder, no server code changes.

Built to support development on the sibling
[TPS25751](https://github.com/forrestrae/TPS25751) and
[BQ25798](https://github.com/forrestrae/BQ25798) Arduino libraries, but the
server itself has no dependency on either — it's a standalone documentation tool.

## Configured Devices

| MCP server | Device | Registers | Tool prefix |
|---|---|---|---|
| `tps25751-docs` | TPS25751 USB-C PD controller | 27 | `mcp__tps25751-docs__*` |
| `bq25798-docs` | BQ25798 buck-boost charger | 57 | `mcp__bq25798-docs__*` |

Both run the same `server.py`, pointed at a different device via the
`DEVICE_CONFIG` environment variable.

## Layout

```
device-register-mcp-server/
  server.py              # generic server; device chosen via DEVICE_CONFIG
  requirements.txt
  devices/
    tps25751/  device.json + tps25751_registers_extracted.json + TPS25751-register-schema.json
    bq25798/   device.json + bq25798_registers_extracted.json  + BQ25798-register-schema.json
```

Each device's registers JSON is the single source of truth for that device,
validated at startup against the schema in the same folder (when `jsonschema`
is installed).

## Running the Server

```bash
# From this repo's root:
DEVICE_CONFIG=devices/tps25751/device.json uv run server.py
DEVICE_CONFIG=devices/bq25798/device.json  uv run server.py
```

`DEVICE_CONFIG` is resolved relative to `server.py`'s own directory, so it works
the same whether invoked from this repo directly or from a sibling library repo
(e.g. `uv run ../device-register-mcp-server/server.py`).

## Available Tools

Each configured device exposes three tools under its own `mcp__<server>__` prefix:

- **`search_register`** — fuzzy search by name, address, or keyword
- **`explain_bitfield`** — detailed bit-field lookup within a register
- **`get_register`** — exact, case-insensitive single-register lookup

See [`AGENTS.md`](AGENTS.md) for full tool/resource documentation, the JSON
schema contract, and how to onboard a new device.

## Adding a New Device

Onboarding a new chip means creating `devices/<name>/` (a `device.json` manifest,
a registers JSON, and a matching schema) and registering it as another MCP
server entry. The `/add-register-device` skill in this repo automates the whole
flow — see [`AGENTS.md`](AGENTS.md) for the manual procedure.

## Consumer Repos

Library repos that use this server point at it via a relative path, assuming
the sibling-checkout layout under a common `lib/`:

- **TPS25751**: `.mcp.json` → `uv run ../device-register-mcp-server/server.py`,
  `DEVICE_CONFIG=devices/tps25751/device.json`
- **BQ25798**: `.mcp.json` → `uv run ../device-register-mcp-server/server.py`,
  `DEVICE_CONFIG=devices/bq25798/device.json`

## License

Copyright (c) 2025-2026 Forrest Rae

Licensed under the **Apache License, Version 2.0**. You may use, copy, modify, and
redistribute this software, including for commercial purposes, subject to the terms
of the license. See [`LICENSE`](LICENSE) for full terms.
