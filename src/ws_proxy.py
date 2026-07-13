'''CC1 WebSocket + HTTP proxy on port 3030.

Handles three types of traffic on the same port:
  • WebSocket upgrade  → bidirectional message relay to printer:3030/websocket
  • POST /uploadFile/upload → capture gcode metadata, forward to printer
  • Everything else    → HTTP passthrough to printer:3030
'''

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .discovery_relay import rewrite_mainboard_ip
from .storage import GCodeStorage

if TYPE_CHECKING:
  from .config import Config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# CC1 chunked-upload session tracker
# ------------------------------------------------------------------


class _CC1UploadSession:
  '''Accumulates multipart chunks for a single CC1 gcode upload.'''

  def __init__(
    self,
    uuid: str,
    total_size: int,
    md5: str,
    storage: GCodeStorage,
  ) -> None:
    safe_uuid = re.sub(r'[^a-zA-Z0-9\-]', '', uuid)
    self.uuid = uuid
    self.total_size = total_size
    self.md5 = md5
    self.bytes_written = 0
    self.created = time.monotonic()
    self.lock = asyncio.Lock()
    self._path = storage.temp_path(f'cc1_{safe_uuid}')
    self._storage = storage
    self._fh = None

  def write_chunk(self, offset: int, data: bytes) -> None:
    if self._fh is None:
      self._fh = open(self._path, 'wb')  # noqa: SIM115
    self._fh.seek(offset)
    self._fh.write(data)
    self._fh.flush()
    # High-water mark; assumes CC1 sends chunks sequentially (no gaps).
    self.bytes_written = max(self.bytes_written, offset + len(data))

  @property
  def complete(self) -> bool:
    return self.bytes_written >= self.total_size

  def _close(self) -> None:
    if self._fh is not None:
      self._fh.close()
      self._fh = None

  def finalize(self, filename_hint: str | None = None) -> tuple[Path, dict]:
    self._close()
    return self._storage.save_gcode_file(self._path, filename_hint=filename_hint)

  def discard(self) -> None:
    self._close()
    self._storage.cleanup_temp(f'cc1_{self.uuid}')


# ------------------------------------------------------------------
# WebSocket + HTTP proxy
# ------------------------------------------------------------------


class WSProxy:
  def __init__(self, config: Config, storage: GCodeStorage) -> None:
    self._config = config
    self._storage = storage
    self._printer_ws_url = f'ws://{config.printer_ip}:{config.ws_port}/websocket'
    self._printer_http_url = f'http://{config.printer_ip}:{config.ws_port}'
    self._sessions: dict[str, _CC1UploadSession] = {}
    self._lock = asyncio.Lock()
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
    for session in self._sessions.values():
      await asyncio.to_thread(session.discard)
    self._sessions.clear()

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
    rewritten = rewrite_mainboard_ip(data.encode('utf-8'), printer_ip, advertise_ip)
    return rewritten.decode('utf-8')

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

    fields = self._parse_multipart_bytes(raw_body, request.content_type)
    if fields is None:
      return await self._forward_raw(request, raw_body)

    upload_uuid = fields.get('Uuid', '')
    try:
      offset = int(fields.get('Offset', '0'))
      total_size = int(fields.get('TotalSize', '0'))
    except (ValueError, TypeError):
      logger.warning('CC1 upload: invalid Offset/TotalSize, forwarding raw')
      return await self._forward_raw(request, raw_body)
    file_md5 = fields.get('S-File-MD5', '')
    file_data = fields.get('file_data', b'')
    filename = fields.get('filename')

    logger.info(
      'CC1 upload chunk: uuid=%s offset=%d/%d (%d bytes)',
      upload_uuid[:8],
      offset,
      total_size,
      len(file_data),
    )

    printer_response = await self._forward_raw(request, raw_body)

    if printer_response.status != 200:
      return printer_response

    try:
      printer_body = printer_response.body
      if isinstance(printer_body, bytes):
        response_data = json.loads(printer_body)
        if response_data.get('code') != '000000':
          return printer_response
    except (json.JSONDecodeError, AttributeError, TypeError):
      pass

    try:
      await self._save_chunk(
        upload_uuid,
        offset,
        total_size,
        file_md5,
        file_data,
        filename,
      )
    except Exception:
      logger.exception('Failed to save CC1 upload chunk')

    return printer_response

  async def _save_chunk(
    self,
    upload_uuid: str,
    offset: int,
    total_size: int,
    file_md5: str,
    file_data: bytes,
    filename: str | None,
  ) -> None:
    # Acquire self._lock to look up / create the session, then acquire
    # session.lock before releasing self._lock (end of `async with`).
    # This avoids holding the global lock during the blocking write.
    async with self._lock:
      session = self._sessions.get(upload_uuid)
      if session is None or offset == 0:
        if session is not None:
          async with session.lock:
            await asyncio.to_thread(session.discard)
        session = _CC1UploadSession(upload_uuid, total_size, file_md5, self._storage)
        self._sessions[upload_uuid] = session
      await session.lock.acquire()

    is_complete = False
    try:
      await asyncio.to_thread(session.write_chunk, offset, file_data)
      if session.complete:
        is_complete = True
        try:
          path, _metadata = await asyncio.to_thread(
            session.finalize,
            filename_hint=filename,
          )
          logger.info('CC1 upload complete: %s', path.name)
        except Exception:
          logger.exception('Failed to finalize CC1 upload')
    finally:
      session.lock.release()

    if is_complete:
      async with self._lock:
        if self._sessions.get(upload_uuid) is session:
          del self._sessions[upload_uuid]

  # ---- multipart parsing ----

  def _parse_multipart_bytes(
    self,
    raw_body: bytes,
    content_type: str,
  ) -> dict | None:
    '''Parse CC1 multipart upload form from raw bytes.'''
    try:
      boundary = None
      for segment in content_type.split(';'):
        segment = segment.strip()
        if segment.startswith('boundary='):
          boundary = segment[len('boundary=') :]
          break

      if not boundary:
        return None

      fields = {}
      boundary_bytes = f'--{boundary}'.encode()
      parts = raw_body.split(boundary_bytes)

      for part in parts:
        if not part or part == b'--\r\n' or part == b'--':
          continue

        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
          continue

        header_section = part[:header_end].decode('utf-8', errors='ignore')
        body_section = part[header_end + 4 :]
        if body_section.endswith(b'\r\n'):
          body_section = body_section[:-2]

        name = None
        part_filename = None
        for header_line in header_section.split('\r\n'):
          if 'Content-Disposition' in header_line:
            for attr in header_line.split(';'):
              attr = attr.strip()
              if attr.startswith('name='):
                name = attr[5:].strip('"')
              elif attr.startswith('filename='):
                part_filename = attr[9:].strip('"')

        if name == 'File':
          fields['file_data'] = body_section
          if part_filename:
            fields['filename'] = part_filename
        elif name:
          fields[name] = body_section.decode('utf-8', errors='ignore')

      return fields if fields else None

    except Exception:
      logger.debug('Failed to parse multipart body', exc_info=True)
      return None

  # ---- HTTP passthrough ----

  async def _passthrough(self, request: web.Request) -> web.Response:
    body = await request.read() if request.can_read_body else None
    try:
      async with self._client.request(
        request.method,
        f'{self._printer_http_url}{request.path_qs}',
        headers={
          header_name: header_value
          for header_name, header_value in request.headers.items()
          if header_name.lower() not in ('host', 'connection', 'transfer-encoding')
        },
        data=body,
      ) as response:
        response_body = await response.read()
        response_headers = {
          header_name: header_value
          for header_name, header_value in response.headers.items()
          if header_name.lower() not in ('connection', 'transfer-encoding')
        }
        return web.Response(
          status=response.status,
          body=response_body,
          headers=response_headers,
        )
    except (TimeoutError, aiohttp.ClientError) as exception:
      logger.error('Printer unreachable at %s: %s', self._printer_http_url, exception)
      return web.json_response({'error': 'printer_unreachable'}, status=502)

  async def _forward_raw(
    self,
    request: web.Request,
    raw_body: bytes,
  ) -> web.Response:
    '''Forward raw bytes to the printer, preserving content-type.'''
    try:
      async with self._client.request(
        request.method,
        f'{self._printer_http_url}{request.path_qs}',
        headers={
          header_name: header_value
          for header_name, header_value in request.headers.items()
          if header_name.lower() not in ('host', 'connection', 'transfer-encoding')
        },
        data=raw_body,
      ) as response:
        response_body = await response.read()
        return web.Response(
          status=response.status,
          body=response_body,
          content_type=response.content_type,
        )
    except (TimeoutError, aiohttp.ClientError) as exception:
      logger.error('Printer unreachable at %s: %s', self._printer_http_url, exception)
      return web.json_response({'error': 'printer_unreachable'}, status=502)

  # ---- stale session reaper ----

  async def _cleanup_once(self) -> int:
    '''Single pass: discard uploads older than upload_timeout. Returns count.'''
    cutoff = time.monotonic() - self._config.upload_timeout
    async with self._lock:
      stale = [
        session_uuid
        for session_uuid, session in self._sessions.items()
        if session.created < cutoff
      ]
      for session_uuid in stale:
        session = self._sessions[session_uuid]
        async with session.lock:
          await asyncio.to_thread(session.discard)
        del self._sessions[session_uuid]
        logger.warning('Discarded stale CC1 upload session (uuid=%s)', session_uuid[:8])
    return len(stale)

  async def cleanup_stale_sessions(self) -> None:
    '''Periodically discard uploads that never completed.'''
    while True:
      await asyncio.sleep(60)
      await self._cleanup_once()
