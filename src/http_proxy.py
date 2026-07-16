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
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .cc1_upload import CC1UploadCapture
from .config import Config
from .discovery_relay import rewrite_mainboard_ip
from .storage import GCodeStorage

if TYPE_CHECKING:
  from .gcode_parser import GCodeMetadata

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
# Chunked-upload session tracker
# ------------------------------------------------------------------


class _UploadSession:
  '''Accumulates chunks for a single multi-PUT upload.'''

  def __init__(self, total_size: int, storage: GCodeStorage) -> None:
    self.total_size = total_size
    self.upload_id = f'{total_size}_{uuid.uuid4().hex[:12]}'
    self.bytes_written = 0
    self.created = time.monotonic()
    self.lock = asyncio.Lock()
    self._path = storage.temp_path(self.upload_id)
    self._storage = storage
    self._fh = None

  def _close_and_cleanup(self) -> None:
    '''Shared close logic for finalize/discard.'''
    if self._fh is not None:
      self._fh.close()
      self._fh = None

  def write_chunk(self, offset: int, data: bytes | Path) -> None:
    '''Append *data* at *offset*. *data* may be raw bytes or a Path to stream from.'''
    if self._fh is None:
      self._fh = open(self._path, 'wb')  # noqa: SIM115
    self._fh.seek(offset)
    if isinstance(data, Path):
      written = 0
      with open(data, 'rb') as source:
        while block := source.read(_STREAM_CHUNK_SIZE):
          self._fh.write(block)
          written += len(block)
      data_length = written
    else:
      self._fh.write(data)
      data_length = len(data)
    self._fh.flush()
    self.bytes_written = max(self.bytes_written, offset + data_length)

  @property
  def complete(self) -> bool:
    return self.bytes_written >= self.total_size

  def finalize(self, filename_hint: str | None = None) -> tuple[Path, GCodeMetadata]:
    '''Close, persist to the archive, clean up temp.  Returns *(path, meta)*.'''
    self._close_and_cleanup()
    return self._storage.save_gcode_file(self._path, filename_hint=filename_hint)

  def discard(self) -> None:
    self._close_and_cleanup()
    self._storage.cleanup_temp(self.upload_id)


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
    self._sessions: dict[tuple[str | None, int], _UploadSession] = {}
    # Lock ordering: always acquire self._lock before session.lock.
    # Both _save and cleanup_stale_sessions rely on this invariant
    # to avoid deadlocks.
    self._lock = asyncio.Lock()
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
    for session in self._sessions.values():
      await asyncio.to_thread(session.discard)
    self._sessions.clear()
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

  def _session_key(self, total: int, headers) -> tuple[str | None, int]:
    '''Session key: (filename, total) when X-File-Name present, else (None, total).'''
    return (self._filename_hint(headers), total)

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
      session_key = self._session_key(total, headers)
      logger.info(
        'Upload chunk: bytes %d–%d/%d (%.1f%%)',
        start,
        end,
        total,
        (end + 1) / total * 100,
      )

      async with self._lock:
        session = self._sessions.get(session_key)
        if session is None or start == 0:
          if session:
            await session.lock.acquire()
            try:
              await asyncio.to_thread(session.discard)
            finally:
              session.lock.release()
          session = _UploadSession(total, self._storage)
          self._sessions[session_key] = session
        await session.lock.acquire()

      try:
        await asyncio.to_thread(session.write_chunk, start, body)
        is_complete = session.complete
      finally:
        session.lock.release()

      if is_complete:
        try:
          path, _meta = await asyncio.to_thread(
            session.finalize, filename_hint=filename_hint
          )
          logger.info('Chunked upload complete: %s', path.name)
        except Exception:
          logger.exception('Failed to finalize chunked upload')
        finally:
          async with self._lock:
            if self._sessions.get(session_key) is session:
              del self._sessions[session_key]

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
    forwarded_headers = {
      header_name: header_value
      for header_name, header_value in headers.items()
      if header_name.lower() not in HOP_BY_HOP and header_name.lower() != 'host'
    }
    forwarded_headers['Host'] = self._config.printer_ip

    file_handle = None
    try:
      if isinstance(body, Path):
        file_handle = body.open('rb')  # noqa: SIM115
      async with self._client.request(
        method,
        f'{self._printer}{path_qs}',
        headers=forwarded_headers,
        data=file_handle if file_handle is not None else body,
      ) as response:
        resp_body = await response.read()
        resp_headers = {
          header_name: header_value
          for header_name, header_value in response.headers.items()
          if header_name.lower() not in HOP_BY_HOP
        }
        # Port-80 payloads (device info via the printer web UI) can carry
        # MainboardIP; the slicer trusts it for follow-up operations, so it
        # must advertise the proxy. Body size may change — drop the stale
        # Content-Length and let aiohttp recompute it.
        if self._config.advertise_ip and resp_body and b'MainboardIP' in resp_body:
          resp_body = rewrite_mainboard_ip(
            resp_body,
            self._config.printer_ip or '',
            self._config.advertise_ip,
          )
          resp_headers = {
            header_name: header_value
            for header_name, header_value in resp_headers.items()
            if header_name.lower() != 'content-length'
          }
        return response.status, resp_body, resp_headers
    except (TimeoutError, aiohttp.ClientError) as exception:
      logger.error('Printer unreachable: %s', exception)
      return None, None, None
    finally:
      if file_handle is not None:
        file_handle.close()

  # ---- stale session reaper ----

  async def cleanup_stale_sessions(self) -> None:
    '''Periodically discard uploads that never completed.'''
    while True:
      await asyncio.sleep(60)
      cutoff = time.monotonic() - self._config.upload_timeout
      async with self._lock:
        stale = [
          session_key
          for session_key, session in self._sessions.items()
          if session.created < cutoff
        ]
        for session_key in stale:
          session = self._sessions[session_key]
          async with session.lock:
            await asyncio.to_thread(session.discard)
          del self._sessions[session_key]
          logger.warning('Discarded stale upload session (key=%r)', session_key)
      await self._cc1_capture.cleanup_once()
