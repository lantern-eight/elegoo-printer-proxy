'''Shared CC1 (SDCP) chunked-upload capture.

ElegooSlicer uploads CC1 G-code as a sequence of multipart POSTs to
/uploadFile/upload (1 MB chunks with Uuid/Offset/TotalSize fields). The
printer serves this endpoint on BOTH of its HTTP ports — 3030 (SDCP) and
80 (web UI) — and current slicer builds upload via port 80, so both proxy
listeners must intercept the same path. Each listener passes its own
port-faithful ``forward`` callable; this module parses chunks, forwards
them, and archives the assembled file once the printer has accepted the
final chunk.
'''

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
  from collections.abc import Awaitable, Callable
  from pathlib import Path

  from .storage import GCodeStorage

  ForwardFn = Callable[[web.Request, bytes], Awaitable[web.Response]]

logger = logging.getLogger(__name__)

UPLOAD_PATH = '/uploadFile/upload'


class CC1UploadSession:
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
    self._temp_key = f'cc1_{safe_uuid}'
    self._path = storage.temp_path(self._temp_key)
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
    self._storage.cleanup_temp(self._temp_key)


def parse_multipart_bytes(raw_body: bytes, content_type: str) -> dict | None:
  '''Parse CC1 multipart upload form from raw bytes.'''
  try:
    boundary = None
    for segment in content_type.split(';'):
      segment = segment.strip()
      if segment.lower().startswith('boundary='):
        boundary = segment[len('boundary=') :].strip('"')
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


class CC1UploadCapture:
  '''Chunk assembly + archival, shared by the port-80 and port-3030 proxies.'''

  def __init__(self, storage: GCodeStorage, upload_timeout: float) -> None:
    self._storage = storage
    self._upload_timeout = upload_timeout
    self._sessions: dict[str, CC1UploadSession] = {}
    self._lock = asyncio.Lock()

  @property
  def sessions(self) -> dict[str, CC1UploadSession]:
    return self._sessions

  @staticmethod
  def matches(request: web.Request) -> bool:
    return request.method == 'POST' and request.path == UPLOAD_PATH

  async def handle(
    self,
    request: web.Request,
    raw_body: bytes,
    forward: ForwardFn,
  ) -> web.Response:
    '''Forward an upload chunk to the printer, saving a copy on success.'''
    # request.content_type strips header parameters — including the
    # multipart boundary — so the parser must see the raw header value.
    content_type = request.headers.get('Content-Type', '')
    fields = parse_multipart_bytes(raw_body, content_type)
    if fields is None:
      return await forward(request, raw_body)

    upload_uuid = fields.get('Uuid', '')
    try:
      offset = int(fields.get('Offset', '0'))
      total_size = int(fields.get('TotalSize', '0'))
    except (ValueError, TypeError):
      logger.warning('CC1 upload: invalid Offset/TotalSize, forwarding raw')
      return await forward(request, raw_body)
    if not upload_uuid or total_size <= 0:
      logger.warning('CC1 upload: missing Uuid or TotalSize, forwarding raw')
      return await forward(request, raw_body)

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

    printer_response = await forward(request, raw_body)

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
      if session is None and offset != 0:
        logger.warning(
          'CC1 upload: chunk at offset %d with no session (proxy restart?), skipping capture',
          offset,
        )
        return
      if session is None or offset == 0:
        if session is not None:
          async with session.lock:
            await asyncio.to_thread(session.discard)
        session = CC1UploadSession(upload_uuid, total_size, file_md5, self._storage)
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

  async def cleanup_once(self) -> int:
    '''Single pass: discard uploads older than upload_timeout. Returns count.'''
    cutoff = time.monotonic() - self._upload_timeout
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

  async def discard_all(self) -> None:
    for session in self._sessions.values():
      await asyncio.to_thread(session.discard)
    self._sessions.clear()
