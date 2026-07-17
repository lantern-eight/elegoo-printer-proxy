'''CC1 WebSocket + HTTP proxy on port 3030.

Handles three types of traffic on the same port:
  • WebSocket upgrade  → bidirectional message relay to printer:3030/websocket
  • POST /uploadFile/upload → capture gcode metadata, forward to printer
  • Everything else    → HTTP passthrough to printer:3030
'''

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .cc1_upload import CC1UploadCapture
from .discovery_relay import rewrite_mainboard_ip_str
from .http_proxy import forward_to_printer
from .storage import GCodeStorage

if TYPE_CHECKING:
  from .config import Config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# WebSocket + HTTP proxy
# ------------------------------------------------------------------


class WSProxy:
  def __init__(self, config: Config, storage: GCodeStorage) -> None:
    self._config = config
    self._storage = storage
    self._printer_ws_url = f'ws://{config.printer_ip}:{config.ws_port}/websocket'
    self._printer_http_url = f'http://{config.printer_ip}:{config.ws_port}'
    self._capture = CC1UploadCapture(storage, config.upload_timeout)
    self._client: aiohttp.ClientSession | None = None
    self._runner: web.AppRunner | None = None

  async def start(self) -> None:
    timeout = aiohttp.ClientTimeout(total=self._config.upload_timeout)
    self._client = aiohttp.ClientSession(timeout=timeout)

    app = web.Application(client_max_size=self._config.max_body_size)
    app.router.add_route('*', '/{path_info:.*}', self.handle_request)

    self._runner = web.AppRunner(app, access_log=None)
    await self._runner.setup()
    site = web.TCPSite(self._runner, '0.0.0.0', self._config.ws_port)
    await site.start()
    logger.info('WS proxy listening on :%d', self._config.ws_port)

  async def stop(self) -> None:
    if self._runner:
      await self._runner.cleanup()
    if self._client:
      await self._client.close()
    await self._capture.discard_all()

  def _rewrite_outbound(self, data: str) -> str:
    '''
    Rewrite MainboardIP in printer→slicer frames to the advertised IP.

    The slicer uses MainboardIP from printer payloads for follow-up
    operations (notably uploads); without the rewrite those bypass the
    proxy. No-op when ADVERTISE_IP is not configured.
    '''
    advertise_ip = self._config.advertise_ip
    printer_ip = self._config.printer_ip
    if not advertise_ip or not printer_ip or 'MainboardIP' not in data:
      return data
    return rewrite_mainboard_ip_str(data, printer_ip, advertise_ip)

  # ---- request routing ----

  async def handle_request(self, request: web.Request) -> web.StreamResponse:
    if request.headers.get('Upgrade', '').lower() == 'websocket':
      return await self._handle_ws(request)
    if request.method == 'POST' and request.path == '/uploadFile/upload':
      return await self._handle_upload(request)
    return await self._passthrough(request)

  # ---- WebSocket relay ----

  async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
    slicer_ws = web.WebSocketResponse()
    await slicer_ws.prepare(request)
    peer = request.remote
    logger.info('WS: new connection from %s', peer)

    try:
      async with self._client.ws_connect(self._printer_ws_url) as printer_ws:

        async def slicer_to_printer():
          async for message in slicer_ws:
            if message.type == aiohttp.WSMsgType.TEXT:
              await printer_ws.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
              await printer_ws.send_bytes(message.data)
            elif message.type in (
              aiohttp.WSMsgType.CLOSE,
              aiohttp.WSMsgType.ERROR,
            ):
              break

        async def printer_to_slicer():
          async for message in printer_ws:
            if message.type == aiohttp.WSMsgType.TEXT:
              await slicer_ws.send_str(self._rewrite_outbound(message.data))
            elif message.type == aiohttp.WSMsgType.BINARY:
              await slicer_ws.send_bytes(message.data)
            elif message.type in (
              aiohttp.WSMsgType.CLOSE,
              aiohttp.WSMsgType.ERROR,
            ):
              break

        done, pending = await asyncio.wait(
          [
            asyncio.create_task(slicer_to_printer()),
            asyncio.create_task(printer_to_slicer()),
          ],
          return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
          task.cancel()
        for task in done:
          if task.exception() and not isinstance(
            task.exception(), asyncio.CancelledError
          ):
            logger.debug('WS relay task error: %s', task.exception())

    except (aiohttp.ClientError, OSError, TimeoutError) as exception:
      logger.warning(
        'WS: cannot reach printer at %s: %s', self._printer_ws_url, exception
      )
    finally:
      if not slicer_ws.closed:
        await slicer_ws.close()
      logger.info('WS: connection from %s closed', peer)

    return slicer_ws

  # ---- CC1 upload capture ----

  async def _handle_upload(self, request: web.Request) -> web.Response:
    raw_body = await request.read()
    return await self._capture.handle(request, raw_body, self._forward_raw)

  # ---- HTTP passthrough ----

  async def _passthrough(self, request: web.Request) -> web.Response:
    body = await request.read() if request.can_read_body else None
    logger.debug(
      'Passthrough: %s %s (%d bytes in)',
      request.method,
      request.path_qs,
      len(body) if body else 0,
    )
    status, resp_body, resp_headers = await forward_to_printer(
      self._client,
      self._printer_http_url,
      request.method,
      request.path_qs,
      request.headers,
      body,
      config=self._config,
    )
    if status is None:
      return web.json_response({'error': 'printer_unreachable'}, status=502)
    return web.Response(status=status, body=resp_body, headers=resp_headers)

  async def _forward_raw(
    self,
    request: web.Request,
    raw_body: bytes,
  ) -> web.Response:
    '''Forward raw bytes to the printer, preserving content-type.'''
    status, resp_body, resp_headers = await forward_to_printer(
      self._client,
      self._printer_http_url,
      request.method,
      request.path_qs,
      request.headers,
      raw_body,
    )
    if status is None:
      return web.json_response({'error': 'printer_unreachable'}, status=502)
    return web.Response(
      status=status,
      body=resp_body,
      content_type=resp_headers.get('Content-Type', 'application/octet-stream'),
    )

  # ---- stale session reaper ----

  async def cleanup_stale_sessions(self) -> None:
    '''Periodically discard uploads that never completed.'''
    while True:
      await asyncio.sleep(60)
      await self._capture.cleanup_once()
