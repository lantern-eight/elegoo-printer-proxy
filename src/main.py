'''Elegoo Printer Proxy: entry point.

Starts different services based on printer type:

CC2 (Centauri Carbon 2):
  • HTTP reverse-proxy   (:80)   - intercepts PUT /upload, saves G-code copy
  • MQTT TCP relay        (:1883) - transparent pass-through
  • MQTT-WS TCP relay     (:9001) - transparent pass-through (WebSocket)
  • Camera TCP relay      (:8080) - transparent pass-through

CC1 (Centauri Carbon):
  • HTTP reverse-proxy   (:80)   - passthrough + REST API
  • WS+HTTP proxy        (:3030) - WebSocket relay + POST upload capture
  • Camera TCP relay      (:8080) - transparent pass-through

Also exposes a REST API on port 80 for querying captured G-code
metadata (GET /api/filament, /api/health).
'''

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiohttp import web

from .api import API
from .config import Config
from .http_proxy import HTTPProxy
from .storage import GCodeStorage
from .tcp_proxy import start_tcp_proxy

logger = logging.getLogger('elegoo_proxy')


def _setup_logging(level: str) -> None:
  logging.basicConfig(
    level=getattr(logging, level.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
  )


async def _run() -> None:
  config = Config()
  _setup_logging(config.log_level)

  if not config.printer_ip or not config.printer_ip.strip():
    raise SystemExit(
      'PRINTER_IP is required but not set. '
      "Set the PRINTER_IP environment variable to your printer's IP address."
    )

  printer_type = config.printer_type
  if printer_type == 'auto':
    from .detect import detect_printer_type

    delays = [5, 10, 20]
    for attempt, delay in enumerate(delays, 1):
      try:
        printer_type = await detect_printer_type(config.printer_ip)
        logger.info('Auto-detected printer type: %s', printer_type)
        break
      except RuntimeError:
        logger.warning(
          'Auto-detect attempt %d/%d failed, retrying in %ds…',
          attempt,
          len(delays),
          delay,
        )
        await asyncio.sleep(delay)
    else:
      printer_type = await detect_printer_type(config.printer_ip)

  logger.info('Elegoo Printer Proxy starting')
  logger.info('  Printer IP   : %s', config.printer_ip)
  logger.info('  Printer type : %s', printer_type)
  logger.info('  G-code dir   : %s', config.gcode_dir)
  logger.info('  Retention    : %d days', config.retention_days)
  logger.info('  Timezone     : %s', config.gcode_timezone.key)
  logger.info('  Store gcode  : %s', config.store_gcode)

  storage = GCodeStorage(
    config.gcode_dir,
    config.retention_days,
    store_gcode=config.store_gcode,
    tz=config.gcode_timezone,
  )

  removed = storage.cleanup_old_files()
  if removed:
    logger.info('Startup cleanup removed %d expired file(s)', removed)

  # --- HTTP proxy (port 80: smart for CC2, passthrough for CC1) + API ---
  http_proxy = HTTPProxy(config, storage)
  await http_proxy.start()

  app = web.Application(client_max_size=config.max_body_size)
  api = API(storage, config, printer_type)
  api.register_routes(app)
  app.router.add_route('*', '/{path_info:.*}', http_proxy.handle_request)

  runner = web.AppRunner(app, access_log=None)
  await runner.setup()
  site = web.TCPSite(runner, '0.0.0.0', config.http_port)
  await site.start()
  logger.info('HTTP proxy listening on :%d', config.http_port)

  # --- Type-specific services ---
  servers = []
  ws_proxy = None
  discovery_transport = None
  background_tasks = [
    asyncio.create_task(storage.periodic_cleanup()),
    asyncio.create_task(http_proxy.cleanup_stale_sessions()),
  ]

  if printer_type == 'cc2':
    servers.append(
      await start_tcp_proxy(config.mqtt_port, config.printer_ip, 1883, 'MQTT')
    )
    servers.append(
      await start_tcp_proxy(config.mqtt_ws_port, config.printer_ip, 9001, 'MQTT-WS')
    )
    servers.append(
      await start_tcp_proxy(config.camera_port, config.printer_ip, 8080, 'Camera')
    )

  elif printer_type == 'cc1':
    from .ws_proxy import WSProxy

    ws_proxy = WSProxy(config, storage)
    await ws_proxy.start()
    background_tasks.append(asyncio.create_task(ws_proxy.cleanup_stale_sessions()))
    servers.append(
      await start_tcp_proxy(config.camera_port, config.printer_ip, 8080, 'Camera')
    )
    if config.advertise_ip:
      from .discovery_relay import start_discovery_relay

      discovery_transport = await start_discovery_relay(config)
    else:
      logger.warning(
        'ADVERTISE_IP not set! Discovery relay disabled. The slicer will '
        'learn the real printer address and upload directly, bypassing '
        'gcode capture by the proxy.'
      )

  logger.info(
    'All services started — proxying %s to %s, API at /api/',
    printer_type.upper(),
    config.printer_ip,
  )

  # --- wait for shutdown signal ---
  stop = asyncio.Event()
  loop = asyncio.get_running_loop()
  for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, stop.set)
  await stop.wait()

  # --- teardown ---
  logger.info('Shutting down…')
  for task in background_tasks:
    task.cancel()
  for server in servers:
    server.close()
  if discovery_transport:
    discovery_transport.close()
  if ws_proxy:
    await ws_proxy.stop()
  await asyncio.gather(
    *[server.wait_closed() for server in servers],
    *background_tasks,
    return_exceptions=True,
  )
  await http_proxy.stop()
  await runner.cleanup()
  logger.info('Shutdown complete')


def main() -> None:
  with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(_run())


if __name__ == '__main__':
  main()
