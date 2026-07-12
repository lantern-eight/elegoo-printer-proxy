# CC2 G-Code Capture Proxy

A lightweight local-network reverse proxy that sits between **ElegooSlicer** and an
**Elegoo Centauri Carbon 2** printer, transparently capturing every G-code file at
upload time.

## Why?

The CC2 with its Canvas (AMS) system reports only a *total* filament usage value over
MQTT. The **per-slot breakdown** (how much each spool contributed) exists only inside
the G-code file. There's no way to retrieve a file from the printer after it's been
sent.

This proxy solves that by saving a copy of every G-code file as it passes through,
enabling per-spool filament tracking with Home Assistant + Spoolman.

### Why not just download the file from the printer?

The CC2 stock firmware stores G-code at `/opt/usr/gcode` internally but exposes no
mechanism to retrieve file content over the network.

Flashing [OpenCentauri][opencentauri] firmware is an option that works. This proxy
solution is for anyone who may not want to flash a new firmware.

A reverse proxy is a straightforward solution: one IP change in the slicer, forward all
traffic to the printer, save a copy of the G-code file, set and forget. Because this
forwards all traffic to the printer, the slicer's Device page (controls, camera, file
list) works normally.

> **Note:** This is a workaround while Elegoo does not expose per-filament variables or
> allow G-code download from the printer. If Elegoo adds either of those in a future
> firmware or API, this proxy will no longer be needed.

## How It Works

```
ElegooSlicer ──HTTP PUT /upload──▶ Proxy ──forward──▶ CC2 Printer
                                    │
                                    ├── parse G-code head/tail (~68 KB)
                                    └── save JSON metadata to gcode-archive/
                                       (optionally keep full .gcode file)
```

### Upload Protocol

The slicer uses a **chunked upload** protocol: it splits the G-code file into many
small HTTP PUT requests, each carrying a `Content-Range` header (e.g.,
`bytes 0-262143/52428800`). The proxy accumulates chunks into a temp file and
finalizes when all bytes arrive. Each individual request body is only a few hundred
KB, so even a 500 MB file never requires loading the whole thing in one request.

In rare cases (small files, non-standard clients) the slicer may send the entire file
in a single PUT request with no `Content-Range` header. The proxy streams such
single-shot uploads to disk to avoid OOM on resource-constrained hosts.

The proxy runs four services:

| Port | Protocol | Behavior |
|------|----------|----------|
| 80   | HTTP     | Intercepts `PUT /upload`, saves metadata, forwards to printer. Also serves the REST API (`/api/*`). |
| 1883 | MQTT     | Transparent TCP pass-through (used by the slicer's C++ library) |
| 9001 | MQTT-WS  | Transparent TCP pass-through (MQTT over WebSocket, used by the Device page's JS) |
| 8080 | MJPEG    | Transparent TCP pass-through |

The slicer is pointed at the proxy IP instead of the printer. The four TCP relays
mean the slicer's Device page (controls, camera, file list, temperatures) works
normally. The CC2 printer exposes MQTT on two ports: 1883 (TCP, used by the
slicer's C++ elegoo-link library) and 9001 (WebSocket, used by the Device page's
bundled JavaScript). Both must be proxied for full functionality.

## Quick Start

```bash
# 1. Clone repo, cd to it
cd cc2-gcode-capture-proxy

# 2. Configure
cp .env.example .env
# Edit .env and set PRINTER_IP to the CC2's address

# 3. Run
docker compose up -d
```

Parsed metadata is written to the archive directory as lightweight JSON files. By
default, full `.gcode` files are **not** stored — only the JSON sidecar — keeping disk
usage minimal. Set `STORE_GCODE=true` to also keep the raw G-code files alongside.

With the default `docker-compose.yml`, the local `./gcode-archive/` folder is mounted
into the container at `/data/gcode`.

Layout:

```
gcode-archive/          ← on host (same as /data/gcode inside container)
├── 2026-03-06/
│   ├── 2026-03-06T19-16-22_CC2_benchy.json          ← always written
│   ├── 2026-03-06T19-16-22_CC2_benchy.gcode         ← only if STORE_GCODE=true
│   └── 2026-03-06T20-45-11_CC2_bracket.json
└── 2026-03-07/
    └── …
```

If permission errors writing to `gcode-archive` happen, create it before first run so
it's owned by the local user: `mkdir -p gcode-archive`. (If Docker creates it, it may
be root-owned.)

**Disk space note:** Even in JSON-only mode, the proxy needs enough free disk to hold
one temporary `.gcode` file at a time during upload (it streams chunks to a temp file,
parses the metadata, then deletes the temp file). Plan for at least as much free space
as the largest file the slicer might send.

## Configuration

All settings come from environment variables (or `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PRINTER_IP` | `192.168.1.100` | CC2 printer IP address |
| `HTTP_PORT` | `80` | Proxy HTTP listen port |
| `MQTT_PORT` | `1883` | Proxy MQTT listen port |
| `MQTT_WS_PORT` | `9001` | Proxy MQTT-over-WebSocket listen port (Device page JS) |
| `CAMERA_PORT` | `8080` | Proxy camera listen port |
| `GCODE_DIR` | `/data/gcode` | Archive directory (inside container) |
| `RETENTION_DAYS` | `90` | Auto-delete files older than this (0 = keep forever) |
| `GCODE_TZ` | `UTC` | IANA timezone for file timestamps and date directories (e.g. `America/New_York`) |
| `UPLOAD_TIMEOUT` | `300` | Seconds before an incomplete chunked upload is discarded |
| `MAX_BODY_SIZE` | `268435456` (256 MB) | Maximum request body size in bytes. Caps single-shot uploads to prevent OOM on resource-constrained hosts (e.g. Raspberry Pi). Chunked uploads send small requests regardless of total file size. Set to `0` to disable (not recommended). |
| `STORE_GCODE` | `false` | Keep full `.gcode` files alongside JSON metadata. When `false` (default), only lightweight JSON metadata is stored and the raw G-code is discarded after parsing. Set to `true` to archive the original files (requires more disk space). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Port Conflicts

The proxy must listen on ports 80, 1883, 9001, and 8080 — it masquerades as the printer,
and the slicer only accepts an IP address (no port override). All four ports are used:
HTTP for uploads, MQTT (1883) for the slicer library's status/controls, MQTT-WS (9001)
for the Device page's JavaScript, and 8080 for the camera stream. If something else on
the host already uses any of these ports, that service must be reconfigured or moved.

### Non-Root Container & Port 80

The container runs as a non-root user (`appuser`) for security. Binding to port 80
normally requires root on Linux, but the slicer expects port 80 and the printer
configuration cannot be changed. The solution is `CAP_NET_BIND_SERVICE`: the
`docker-compose.yml` adds this capability so the process can bind to privileged ports
without running as root. This is a minimal, well-understood privilege—common for web
servers and proxies—and avoids the risks of a full root container.

### Security

The proxy runs on the local network with no internet connections. There is no
authentication on the proxy, this is acceptable for LAN-only use where the proxy is not
exposed to the internet.

## Slicer Setup

1. Change the printer IP from the CC2's address to the proxy host IP.
1. The slicer connects through the proxy. Controls, camera, and file list work normally.
1. Every "Upload and Print" now saves a G-code copy automatically.

## REST API

The proxy exposes a lightweight REST API on the same HTTP port (80) for querying
captured G-code metadata. This is how the
[Elegoo Home Assistant integration][elegoo_homeassistant] retrieves per-slot filament
data.

### Endpoints

**`GET /api/filament?filename=CC2_benchy.gcode`**

Returns the most recent JSON metadata for the given original filename. The filename
should match what the slicer embedded in the G-code (e.g. `CC2_benchy.gcode`), or
the `X-File-Name` HTTP header from the upload if the G-code content did not contain
a filename.

```json
{
  "filename": "CC2_benchy.gcode",
  "slicer_version": "ElegooSlicer 1.3.2.9",
  "generated_at": "2026-01-01 at 12:00:00 UTC",
  "captured_at": "2026-01-01T12:00:00+00:00",
  "filament": {
    "per_slot_mm": [0.0, 0.0, 0.0, 500.5],
    "per_slot_cm3": [0.0, 0.0, 0.0, 1.1],
    "per_slot_grams": [0.0, 0.0, 0.0, 1.1],
    "per_slot_cost": [0.0, 0.0, 0.0, 0.05],
    "per_slot_density": [0.0, 0.0, 0.0, 1.24],
    "per_slot_diameter": [0.0, 0.0, 0.0, 1.75],
    "filament_names": ["", "", "", "ElegooPLA-Basic-White"],
    "total_grams": 1.50,
    "total_cost": 0.05,
    "total_filament_changes": 0,
    "total_layers": 300,
    "estimated_time": "1h 18m 10s"
  }
}
```

Returns `404` if no matching file has been captured, `400` if the `filename` query
parameter is missing.

**`GET /api/filament/latest`**

Returns the most recently captured metadata, regardless of filename. Returns `404` if
no files have been captured yet.

**`GET /api/health`**

Returns `{"status": "ok"}` with optional `printer_ip` and `printer_type` fields
when configured. Useful for checking proxy connectivity.

## Elegoo Home Assistant Integration

Home Assistant connects **directly** to the printer's MQTT broker. It does not go
through the proxy.

The HA integration queries the proxy's REST API to get per-slot filament data:

1. A print starts — HA learns the filename from the printer via MQTT.
2. HA calls `GET /api/filament?filename=<name>` on the proxy.
3. The proxy returns per-slot filament weight, type, and cost data.

**Alternative: shared filesystem.** If the proxy and HA run on the same host (or share
a network mount), the HA integration can also read JSON metadata files directly from
the `gcode-archive/` directory. The REST API is the recommended default as it works
across hosts with no filesystem setup.

## Running Without Docker

Requires [uv][uv]. The proxy must use ports 80, 1883, 9001, and 8080
for slicer compatibility (same as with Docker). Binding to port 80 typically requires
root on Unix:

```bash
export PRINTER_IP=192.168.1.100
export GCODE_DIR=./gcode-archive

sudo uv run python -m src.main
```

Docker is recommended. It avoids privilege requirements and handles port binding
cleanly.

## Development

Requires [uv][uv].

```bash
uv sync --group dev
```

### Linting & Formatting

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
uv run ruff check .          # lint
uv run ruff check . --fix    # lint + auto-fix
uv run ruff format .         # format
```

### Pre-commit Hooks

Install the hooks once so Ruff runs automatically on every commit:

```bash
uv run pre-commit install
```

### Tests

```bash
uv run pytest
```

### CI

GitHub Actions runs linting and tests on every push to `main` and on all pull
requests.

## Protocol Notes

For the full CC2 protocol reference — MQTT topics, registration flow, file detail
responses, and stock firmware capabilities see the open-source Elegoo repos:
[CentauriCarbon][centauricarbon], [ElegooSlicer][elegooslicer],
[elegoo-link][elegoo_link]. Also the [CC2_PROTOCOL.md][cc2_protocol] in the
[elegoo-homeassistant][elegoo_homeassistant] repo is a good reference.


## Releasing

Releases use git tags following [Semantic Versioning][semver] (`vMAJOR.MINOR.PATCH`).

Create the release manually at **Releases → Draft a new release** on GitHub, selecting
a tag. Use "Generate release notes" to auto-populate the changelog from merged PRs.

Or manually:

1. **Update the version** in `pyproject.toml`, then commit:

    ```toml
    version = "1.1.0"
    ```

1. **Create an annotated tag**:

    ```bash
    git tag -a v1.1.0 -m "v1.1.0"
    ```

1. **Push the commit and tag**:

    ```bash
    git push origin main --tags
    ```

1. **Create a GitHub Release** from the tag (requires [GitHub CLI](https://cli.github.com/)):

    ```bash
    gh release create v1.1.0 --generate-notes
    ```


## License

See [LICENSE](LICENSE).


[opencentauri]: https://docs.opencentauri.cc/
[uv]: https://docs.astral.sh/uv/
[cc2_protocol]: https://github.com/danielcherubini/elegoo-homeassistant/blob/main/docs/CC2_PROTOCOL.md
[centauricarbon]: https://github.com/elegooofficial/CentauriCarbon
[elegooslicer]: https://github.com/ELEGOO-3D/ElegooSlicer
[elegoo_link]: https://github.com/ELEGOO-3D/elegoo-link
[elegoo_homeassistant]: https://github.com/danielcherubini/elegoo-homeassistant
[semver]: https://semver.org/
