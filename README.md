# Elegoo Printer Proxy

A lightweight local-network reverse proxy that sits between **ElegooSlicer** and
**Elegoo Centauri Carbon** printers, transparently capturing every G-code file at
upload time. Supports any mix of **CC1** (Centauri Carbon) and **CC2**
(Centauri Carbon 2) printers — one proxy instance per printer.

## Why?

Printers's Canvas (AMS) systems report only a *total* filament usage value to network
clients. The **per-slot breakdown** (how much each spool contributed) exists only inside
the G-code file, and printers don't expose a way to retrieve a file after it's been
sent.

This proxy solves that by saving a copy of every G-code file as it passes through,
enabling per-spool filament tracking with Home Assistant + Spoolman.

### Why not just download the file from the printer?

Stock firmware stores G-code internally (e.g. `/opt/usr/gcode` on the CC2) but
exposes no mechanism to retrieve file content over the network.

Flashing [OpenCentauri][opencentauri] firmware is an option that works. This proxy
solution is for anyone who may not want to flash a new firmware.

A reverse proxy is a straightforward solution: one IP change in the slicer, forward
all traffic to the printer, save a copy of the G-code file, set and forget. Because
this forwards all traffic to the printer, the slicer's Device page (controls, camera,
file list) works normally.

> **Note:** This is a workaround while Elegoo does not expose per-filament variables
> or allow G-code download from the printer. If Elegoo adds either of those in a
> future firmware or API, this proxy will no longer be needed.

## How It Works

```
ElegooSlicer ──upload──▶ Proxy ──forward──▶ Printer
                           │
                           ├── parse G-code head/tail (~68 KB)
                           └── save JSON metadata to gcode-archive/
                              (optionally keep full .gcode file)
```

Each printer type speaks a different protocol, so the proxy starts different
services depending on `PRINTER_TYPE`:

**CC2** (MQTT-based):

| Port | Protocol | Behavior |
|------|----------|----------|
| 80   | HTTP     | Intercepts `PUT /upload`, saves metadata, forwards to printer. Also serves the REST API (`/api/*`). |
| 1883 | MQTT     | Transparent TCP pass-through (used by the slicer's C++ library) |
| 9001 | MQTT-WS  | Transparent TCP pass-through (MQTT over WebSocket, used by the Device page's JS) |
| 8080 | MJPEG    | Transparent TCP pass-through (camera stream) |

The CC2 exposes MQTT on two ports: 1883 (TCP, used by the slicer's C++ elegoo-link
library) and 9001 (WebSocket, used by the Device page's bundled JavaScript). Both
must be proxied for full functionality.

**CC1** (WebSocket/SDCP-based):

| Port | Protocol | Behavior |
|------|----------|----------|
| 3030 | WS + HTTP | Relays the SDCP WebSocket (status, controls) and intercepts multipart `POST /uploadFile/upload` chunks. |
| 80   | HTTP     | Serves the REST API (`/api/*`), passes everything else through to the printer. |
| 8080 | MJPEG    | Transparent TCP pass-through. Closed on CC1 firmware V1.4.46 — kept for older/future firmware, harmless when unused. |

The CC1 uploads G-code as multipart form POSTs in 1 MB chunks over port 3030 — the
same port as its control WebSocket — so a single smart service handles both.

### CC2 Upload Protocol

The slicer uses a **chunked upload** protocol: it splits the G-code file into many
small HTTP PUT requests, each carrying a `Content-Range` header (e.g.,
`bytes 0-262143/52428800`). The proxy accumulates chunks into a temp file and
finalizes when all bytes arrive. Each individual request body is only a few hundred
KB, so even a 500 MB file never requires loading the whole thing in one request.

In rare cases (small files, non-standard clients) the slicer may send the entire file
in a single PUT request with no `Content-Range` header. The proxy streams such
single-shot uploads to disk to avoid OOM on resource-constrained hosts.

### CC1 Upload Protocol

Chunked as well, but as multipart form POSTs: each 1 MB chunk carries the upload's
UUID, byte offset, total size, and MD5. The proxy reassembles chunks by UUID,
forwards each request to the printer verbatim, and archives the file once the
printer confirms the final chunk.

## One IP per printer

**Every printer's proxy gets its own dedicated LAN IP.**

1. **The slicer only accepts an IP address** — there is no port override in its
   printer settings. The proxy must be reachable on the exact ports the printer
   protocol dictates.
2. **Connections carry no printer identity.** An incoming `PUT /upload` or MQTT
   connection says nothing about *which* CC2 it is destined for, so one listener
   can't route between two same-type printers.
3. Therefore two printers of the same type would collide on ports if their proxies
   shared an IP. Giving each proxy its own IP makes port collisions **structurally
   impossible** — for any number of printers, including future models that reuse
   the same ports.

Scaling to another printer is: add one IP, copy one compose service block, create
one env file. The IPs are cheap — secondary addresses on the Docker host's existing
network interface, no extra hardware or VMs.

Pick proxy IPs **outside your router's DHCP pool** (or reserve them) so the router
never hands them to another device. The host claims them directly, the router is
not asked.

## Quick Start

```bash
# 1. Clone repo, cd to it

# 2. Compose file — set one service per printer, with your chosen proxy IPs
cp docker-compose.example.yml docker-compose.yml
# Edit: proxy IPs in the port bindings, one service block per printer

# 3. Env files — one per printer
cp .env.example .env.cc2   # set PRINTER_IP=<real CC2 IP>, PRINTER_TYPE=cc2
cp .env.example .env.cc1   # set PRINTER_IP=<real CC1 IP>, PRINTER_TYPE=cc1

# 4. Give the host the proxy IPs (persistent across reboots)
sudo ./scripts/setup_macos_ips.sh          # macOS — reads IPs from docker-compose.yaml
# macOS also needs a one-time Docker Desktop setting — see
#   "macOS: enable privileged port mapping" below
# Linux equivalent: sudo ip addr add <proxy-ip>/24 dev eth0
#   (persist via your distro's network config, e.g. netplan/systemd-networkd)

# 5. Build and run
docker compose up -d --build

# 6. Verify each proxy from another machine
curl http://<proxy-ip>/api/health          # {"status": "ok", "printer_type": ...}
```

Then point the slicer at the proxy IPs (see [Slicer Setup](#slicer-setup)).

### What the macOS script does

Docker Desktop on macOS can't give containers their own LAN IPs (macvlan doesn't
escape its hidden VM), but it *can* bind published ports to a specific host IP.
So the host carries one extra IP per printer, and each container binds only its
printer's IP.

`scripts/setup_macos_ips.sh` creates those IPs as macOS **network services** — the
CLI equivalent of System Settings → Network → adding a service on the same
Ethernet port — using `networksetup`. Services persist across reboots (a plain
`ifconfig alias` would not) and show up in System Settings where they're easy to
inspect or remove. The script is idempotent: re-run it after adding a printer to
the compose file.

### macOS: Enable privileged port mapping (one-time)

macOS allows unprivileged processes to bind ports below 1024 **only on the
wildcard address** (`0.0.0.0`). Binding a *specific* IP to port 80 — exactly what
this deployment does — requires root, so Docker Desktop must delegate those binds
to its privileged helper (`com.docker.vmnetd`). Without the helper installed,
`docker compose up` fails with:

```
Error response from daemon: ports are not available: exposing port
TCP 192.168.x.x:80 -> 127.0.0.1:0: connecting to /var/run/com.docker.vmnetd.sock:
dial unix /var/run/com.docker.vmnetd.sock: connect: no such file or directory
```

Enable it once:

**Docker Desktop → Settings → Advanced → "Allow privileged port
mapping"**

Then Apply & restart (asks for an admin password to install the helper; the restart
briefly stops all containers on the host). The unprivileged ports (1883, 3030, 8080,
9001) never need this — only the `:80` bindings do.

`setup_macos_ips.sh` checks for this automatically: if the compose file binds a
privileged port and the helper is missing, the script exits with these
instructions. The script deliberately does not install the helper itself, it is
Docker-private machinery that Docker Desktop's settings flow and updater manage.

## Archive Layout

Parsed metadata is written per printer as lightweight JSON files. By default, full
`.gcode` files are **not** stored — only the JSON sidecar — keeping disk usage
minimal. Set `STORE_GCODE=true` to also keep the raw G-code files alongside.

```
gcode-archive/               ← on host
├── cc1/                     ← mounted at /data/gcode in the CC1 container
│   └── 2026-03-06/
│       └── 2026-03-06T19-16-22_benchy.json
└── cc2/                     ← mounted at /data/gcode in the CC2 container
    └── 2026-03-06/
        ├── 2026-03-06T19-16-22_CC2_benchy.json   ← always written
        └── 2026-03-06T19-16-22_CC2_benchy.gcode  ← only if STORE_GCODE=true
```

If permission errors writing to `gcode-archive` happen, create the per-printer
directories before first run so they're owned by the local user:
`mkdir -p gcode-archive/cc1 gcode-archive/cc2`. (If Docker creates them, they may
be root-owned.)

**Disk space note:** Even in JSON-only mode, the proxy needs enough free disk to
hold one temporary `.gcode` file at a time during upload (it streams chunks to a
temp file, parses the metadata, then deletes the temp file). Plan for at least as
much free space as the largest file the slicer might send.

## Upgrading from v1 (single-CC2 setup)

v1 ran one container with host-wide port bindings and a single `.env`. To upgrade:

```bash
# 1. Stop the old container
docker compose down

# 2. Split the config: the old .env becomes the CC2 env file
mv .env .env.cc2            # then add PRINTER_TYPE=cc2

# 3. Move the existing archive under the per-printer directory
mkdir -p gcode-archive/cc2
mv gcode-archive/20* gcode-archive/cc2/    # all date directories

# 4. New compose file with per-printer IPs (see Quick Start), then:
sudo ./scripts/setup_macos_ips.sh
docker compose up -d --build
```

Finally, update the slicer's CC2 entry from the old host IP to the CC2 proxy's new
dedicated IP.

## Configuration

Each container reads its settings from its env file (`.env.cc1` / `.env.cc2`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PRINTER_IP` | `192.168.1.100` | The real printer's IP address |
| `PRINTER_TYPE` | `auto` | `cc1`, `cc2`, or `auto` (probes the printer via UDP discovery at startup) |
| `HTTP_PORT` | `80` | Proxy HTTP listen port (REST API, CC2 uploads) |
| `MQTT_PORT` | `1883` | Proxy MQTT listen port (CC2) |
| `MQTT_WS_PORT` | `9001` | Proxy MQTT-over-WebSocket listen port (CC2 Device page JS) |
| `WS_PORT` | `3030` | Proxy WebSocket/SDCP listen port (CC1 control + uploads) |
| `CAMERA_PORT` | `8080` | Proxy camera listen port |
| `GCODE_DIR` | `/data/gcode` | Archive directory (inside container) |
| `RETENTION_DAYS` | `90` | Auto-delete files older than this (0 = keep forever) |
| `GCODE_TZ` | `UTC` | IANA timezone for file timestamps and date directories (e.g. `America/New_York`) |
| `UPLOAD_TIMEOUT` | `300` | Seconds before an incomplete chunked upload is discarded |
| `MAX_BODY_SIZE` | `268435456` (256 MB) | Maximum request body size in bytes. Caps single-shot uploads to prevent OOM on resource-constrained hosts. Chunked uploads send small requests regardless of total file size. Set to `0` to disable (not recommended). |
| `STORE_GCODE` | `false` | Keep full `.gcode` files alongside JSON metadata. When `false` (default), only lightweight JSON metadata is stored and the raw G-code is discarded after parsing. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Non-Root Container & Port 80

The container runs as a non-root user (`appuser`) for security. Binding to port 80
normally requires root on Linux, but the slicer expects port 80 and the printer
configuration cannot be changed. The solution is `CAP_NET_BIND_SERVICE`: the
`docker-compose.yml` adds this capability so the process can bind to privileged
ports without running as root. This is a minimal, well-understood privilege — common
for web servers and proxies — and avoids the risks of a full root container.

### Security

The proxy runs on the local network with no internet connections. There is no
authentication on the proxy, this is acceptable for LAN-only use where the proxy is not
exposed to the internet.

## Slicer Setup

- **CC2**: In the printer settings, change the printer IP from the CC2's real
  address to the CC2 proxy's IP. Controls, camera, and file list work normally.
- **CC1**: Add the printer by IP, using the CC1 proxy's IP instead of the real
  printer address. The slicer connects over SDCP (port 3030) through the proxy.
  (The proxy does not answer discovery broadcasts, so auto-discovery finds the
  real printer — add by IP instead.)

## REST API

Each proxy instance exposes a lightweight REST API on its own IP (HTTP port 80)
for querying captured G-code metadata — the same endpoints for both printer types.
This is how the [Elegoo Home Assistant integration][elegoo_homeassistant] retrieves
per-slot filament data.

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
when configured. Useful for checking proxy connectivity and confirming which
printer a proxy instance serves.

## Elegoo Home Assistant Integration

Home Assistant connects **directly** to each printer (MQTT for CC2, WebSocket for
CC1). It does not go through the proxy.

The HA integration queries each proxy's REST API to get per-slot filament data:

1. A print starts — HA learns the filename from the printer.
2. HA calls `GET /api/filament?filename=<name>` on that printer's proxy IP
   (the integration's `gcode_proxy_url` setting, one per printer).
3. The proxy returns per-slot filament weight, type, and cost data.

**Alternative: shared filesystem.** If the proxy and HA run on the same host (or share
a network mount), the HA integration can also read JSON metadata files directly from
the `gcode-archive/` directory. The REST API is the recommended default as it works
across hosts with no filesystem setup.

## Running Without Docker

Requires [uv][uv]. The proxy must use the standard printer ports for slicer
compatibility (same as with Docker). Binding to port 80 typically requires root on
Unix:

```bash
export PRINTER_IP=192.168.1.100
export PRINTER_TYPE=cc2
export GCODE_DIR=./gcode-archive

sudo uv run python -m src.main
```

Docker is recommended. It avoids privilege requirements, handles port binding
cleanly, and makes running one instance per printer straightforward.

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
[CentauriCarbon2][centauricarbon2], [ElegooSlicer][elegooslicer],
[elegoo-link][elegoo_link]. Also the [CC2_PROTOCOL.md][cc2_protocol] in the
[elegoo-homeassistant][elegoo_homeassistant] repo is a good reference.

The CC1 speaks SDCP (WebSocket JSON-RPC on port 3030, UDP discovery on port 3000), at
[CentauriCarbon][centauricarbon], and the [elegoo-homeassistant][elegoo_homeassistant]
repo documents it as well.

## Releasing

Releases use git tags following [Semantic Versioning][semver] (`vMAJOR.MINOR.PATCH`).

Create the release manually at **Releases → Draft a new release** on GitHub, selecting
a tag. Use "Generate release notes" to auto-populate the changelog from merged PRs.

Or manually:

1. **Update the version** in `pyproject.toml`, then commit:

    ```toml
    version = "2.0.0"
    ```

1. **Create an annotated tag**:

    ```bash
    git tag -a v2.0.0 -m "v2.0.0"
    ```

1. **Push the commit and tag**:

    ```bash
    git push origin main --tags
    ```

1. **Create a GitHub Release** from the tag (requires [GitHub CLI](https://cli.github.com/)):

    ```bash
    gh release create v2.0.0 --generate-notes
    ```


## License

See [LICENSE](LICENSE).


[opencentauri]: https://docs.opencentauri.cc/
[uv]: https://docs.astral.sh/uv/
[cc2_protocol]: https://github.com/danielcherubini/elegoo-homeassistant/blob/main/docs/CC2_PROTOCOL.md
[centauricarbon]: https://github.com/elegooofficial/CentauriCarbon
[centauricarbon2]: https://github.com/elegooofficial/CentauriCarbon2
[elegooslicer]: https://github.com/ELEGOO-3D/ElegooSlicer
[elegoo_link]: https://github.com/ELEGOO-3D/elegoo-link
[elegoo_homeassistant]: https://github.com/danielcherubini/elegoo-homeassistant
[semver]: https://semver.org/
