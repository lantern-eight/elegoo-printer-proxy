'''Smart HTTP reverse-proxy on port 80 that captures G-code uploads.

Intercepts both upload protocols seen on this port:
  • PUT /upload            — CC2 chunked upload (Content-Range + offset response)
  • POST /uploadFile/upload — CC1 SDCP chunked upload (ElegooSlicer uploads
    via the printer's port-80 endpoint, not the :3030 one)

Every other request method/path is forwarded transparently to the printer.
'''

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from .cc1_upload import CC1UploadCapture
from .config import Config
from .discovery_relay import rewrite_mainboard_ip
from .storage import GCodeStorage
from .upload_session import BaseUploadSession, ChunkSessionManager

logger = logging.getLogger(__name__)

# Headers that must not be forwarded between hops.
HOP_BY_HOP = frozenset(
  {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
  }
)


# ------------------------------------------------------------------
# Shared proxy helper
# ------------------------------------------------------------------


async def forward_to_printer(
  client: aiohttp.ClientSession,
  printer_url: str,
  method: str,
  path_qs: str,
  headers,
  body=None,
  *,
  config: Config | None = None,
  extra_headers: dict[str, str] | None = None,
) -> tuple[int | None, bytes | None, dict | None]:
  '''Forward a request to the printer, returning *(status, body, headers)*.

  Filters hop-by-hop and Host headers from the request, optionally rewrites
  MainboardIP in the response (when *config.advertise_ip* is set), and
  strips Content-Length so aiohttp recomputes it from the body.

  Returns *(None, None, None)* when the printer is unreachable.
  '''
  fwd_headers = {
    k: v
    for k, v in headers.items()
    if (low := k.lower()) not in HOP_BY_HOP and low != 'host'
  }
  if extra_headers:
    fwd_headers.update(extra_headers)
  try:
    async with client.request(
      method,
      f'{printer_url}{path_qs}',
      headers=fwd_headers,
      data=body,
    ) as response:
      resp_body = await response.read()
      resp_headers = {
        k: v
        for k, v in response.headers.items()
        if (low := k.lower()) not in HOP_BY_HOP and low != 'content-length'
      }
      if config and config.advertise_ip and resp_body and b'MainboardIP' in resp_body:
        resp_body = rewrite_mainboard_ip(
          resp_body,
          config.printer_ip or '',
          config.advertise_ip,
        )
      return response.status, resp_body, resp_headers
  except (TimeoutError, aiohttp.ClientError) as exc:
    logger.error('Printer unreachable at %s: %s', printer_url, exc)
    return None, None, None


# ------------------------------------------------------------------
# Chunked-upload session tracker
# ------------------------------------------------------------------


class _UploadSession(BaseUploadSession):
  '''Accumulates chunks for a single multi-PUT upload.'''

  def __init__(self, total_size: int, storage: GCodeStorage) -> None:
    upload_id = f'{total_size}_{uuid.uuid4().hex[:12]}'
    super().__init__(total_size, upload_id, storage)
    self.upload_id = upload_id

  def write_chunk(self, offset: int, data: bytes | Path) -> None:
    '''Append *data* at *offset*. *data* may be raw bytes or a Path to stream from.'''
    if isinstance(data, Path):
      if self._fh is None:
        self._fh = open(self._path, 'wb')  # noqa: SIM115
      self._fh.seek(offset)
      written = 0
      with open(data, 'rb') as source:
        while block := source.read(_STREAM_CHUNK_SIZE):
          self._fh.write(block)
          written += len(block)
      self._fh.flush()
      self.bytes_written = max(self.bytes_written, offset + written)
    else:
      super().write_chunk(offset, data)


# ------------------------------------------------------------------
# Content-Range parser
# ------------------------------------------------------------------

_RE_RANGE = re.compile(r'bytes\s+(\d+)-(\d+)/(\d+)')

# Chunk size when streaming single-shot uploads to disk (64 KB)
_STREAM_CHUNK_SIZE = 64 * 1024


def _parse_content_range(header: str | None) -> tuple[int, int, int] | None:
  '''Return *(start, end, total)* or *None* when the header is absent/invalid.'''
  if not header:
    return None
  match = _RE_RANGE.match(header)
  if not match:
    return None
  return int(match.group(1)), int(match.group(2)), int(match.group(3))


# ------------------------------------------------------------------
# HTTP Proxy
# ------------------------------------------------------------------


class HTTPProxy:
  def __init__(self, config: Config, storage: GCodeStorage) -> None:
    self._config = config
    self._storage = storage
    self._printer = f'http://{config.printer_ip}'
    self._manager = ChunkSessionManager()
    self._sessions = self._manager.sessions
    self._cc1_capture = CC1UploadCapture(storage, config.upload_timeout)
    self._client: aiohttp.ClientSession | None = None

  async def start(self) -> None:
    removed = self._storage.cleanup_orphaned_temp_files()
    if removed:
      logger.info('Cleaned %d orphaned temp file(s) from previous run', removed)
    timeout = aiohttp.ClientTimeout(total=self._config.upload_timeout)
    self._client = aiohttp.ClientSession(timeout=timeout)

  async def stop(self) -> None:
    if self._client:
      await self._client.close()
    await self._manager.discard_all()
    await self._cc1_capture.discard_all()

  # ---- aiohttp request handler (catch-all) ----

  async def handle_request(self, request: web.Request) -> web.Response:
    if request.method == 'PUT' and request.path == '/upload':
      return await self._handle_upload(request)
    if self._cc1_capture.matches(request):
      return await self._handle_cc1_upload(request)
    return await self._passthrough(request)

  # ---- CC1 upload interception (SDCP chunked POST on port 80) ----

  async def _handle_cc1_upload(self, request: web.Request) -> web.Response:
    raw_body = await request.read()
    return await self._cc1_capture.handle(request, raw_body, self._forward_cc1_chunk)

  async def _forward_cc1_chunk(
    self,
    request: web.Request,
    raw_body: bytes,
  ) -> web.Response:
    '''Port-faithful forward of one upload chunk to the printer's port 80.'''
    status, resp_body, resp_headers = await self._forward(
      request.method,
      request.path_qs,
      request.headers,
      raw_body,
    )
    if status is None:
      return web.json_response({'error': 'printer_unreachable'}, status=502)
    return web.Response(status=status, body=resp_body, headers=resp_headers)

  # ---- upload interception ----

  async def _handle_upload(self, request: web.Request) -> web.Response:
    content_range = _parse_content_range(request.headers.get('Content-Range'))

    temp_file = await self._stream_body_to_temp(request)
    if temp_file is None:
      return web.json_response({'error': 'body_too_large'}, status=413)

    try:
      resp_status, resp_body, resp_headers = await self._forward(
        'PUT',
        '/upload',
        request.headers,
        temp_file,
      )
      if resp_status is None:
        return web.json_response(
          {'error': 'printer_unreachable'},
          status=502,
        )

      if 200 <= resp_status < 300:
        await self._save(content_range, temp_file, request.headers)

      return web.Response(status=resp_status, body=resp_body, headers=resp_headers)
    finally:
      temp_file.unlink(missing_ok=True)

  async def _stream_body_to_temp(self, request: web.Request) -> Path | None:
    '''Stream request body to a temp file. Returns path or None if size exceeded.'''
    temp_id = f'body_{uuid.uuid4().hex[:12]}'
    temp_path = self._storage.temp_path(temp_id)
    max_size = self._config.max_body_size
    total = 0

    with temp_path.open('wb') as fh:
      async for chunk in request.content.iter_chunked(_STREAM_CHUNK_SIZE):
        total += len(chunk)
        if max_size and total > max_size:
          temp_path.unlink(missing_ok=True)
          return None
        await asyncio.to_thread(fh.write, chunk)

    return temp_path

  @staticmethod
  def _session_key(total: int, filename_hint: str | None) -> tuple[str | None, int]:
    '''Session key: (filename, total) when X-File-Name present, else (None, total).'''
    return (filename_hint, total)

  @staticmethod
  def _filename_hint(headers) -> str | None:
    '''Extract the upload filename from request headers, if present.'''
    name = headers.get('X-File-Name') or headers.get('x-file-name')
    return name.strip() if name else None

  async def _save(
    self,
    content_range: tuple[int, int, int] | None,
    body: Path,
    headers,
  ) -> None:
    '''Write body to archive (single-shot or chunked).'''
    filename_hint = self._filename_hint(headers)
    try:
      if content_range is None:
        await asyncio.to_thread(
          self._storage.save_gcode_file,
          body,
          filename_hint=filename_hint,
        )
        return

      start, end, total = content_range
      session_key = self._session_key(total, filename_hint)
      logger.info(
        'Upload chunk: bytes %d–%d/%d (%.1f%%)',
        start,
        end,
        total,
        (end + 1) / total * 100,
      )

      await self._manager.save_chunk(
        session_key,
        start,
        body,
        lambda: _UploadSession(total, self._storage),
        filename_hint=filename_hint,
      )

    except Exception:
      logger.exception('Failed to save G-code')

  # ---- transparent passthrough ----

  async def _passthrough(self, request: web.Request) -> web.Response:
    body = await request.read() if request.can_read_body else None
    logger.debug(
      'Passthrough: %s %s (%d bytes in)',
      request.method,
      request.path_qs,
      len(body) if body else 0,
    )
    status, resp_body, headers = await self._forward(
      request.method,
      request.path_qs,
      request.headers,
      body,
    )
    if status is None:
      return web.json_response(
        {'error': 'printer_unreachable'},
        status=502,
      )
    return web.Response(status=status, body=resp_body, headers=headers)

  # ---- low-level forward ----

  async def _forward(
    self,
    method: str,
    path_qs: str,
    headers: dict,
    body: bytes | Path | None,
  ) -> tuple[int | None, bytes | None, dict | None]:
    file_handle = None
    try:
      data = body
      if isinstance(body, Path):
        file_handle = body.open('rb')  # noqa: SIM115
        data = file_handle
      return await forward_to_printer(
        self._client,
        self._printer,
        method,
        path_qs,
        headers,
        data,
        config=self._config,
        extra_headers={'Host': self._config.printer_ip},
      )
    finally:
      if file_handle is not None:
        file_handle.close()

  # ---- stale session reaper ----

  async def cleanup_stale_sessions(self) -> None:
    '''Periodically discard uploads that never completed.'''
    while True:
      await asyncio.sleep(60)
      await self._manager.cleanup(self._config.upload_timeout)
      await self._cc1_capture.cleanup_once()
