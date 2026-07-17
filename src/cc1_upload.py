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

import json
import logging
import re
from typing import TYPE_CHECKING

from aiohttp import web

from .upload_session import BaseUploadSession, ChunkSessionManager

if TYPE_CHECKING:
  from collections.abc import Awaitable, Callable

  from .storage import GCodeStorage

  ForwardFn = Callable[[web.Request, bytes], Awaitable[web.Response]]

logger = logging.getLogger(__name__)

UPLOAD_PATH = '/uploadFile/upload'


class CC1UploadSession(BaseUploadSession):
  '''Accumulates multipart chunks for a single CC1 gcode upload.'''

  def __init__(
    self,
    uuid: str,
    total_size: int,
    storage: GCodeStorage,
  ) -> None:
    safe_uuid = re.sub(r'[^a-zA-Z0-9\-]', '', uuid)
    super().__init__(total_size, f'cc1_{safe_uuid}', storage)
    self.uuid = uuid


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
    self._manager = ChunkSessionManager()

  @property
  def sessions(self) -> dict[str, CC1UploadSession]:
    return self._manager.sessions

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
      await self._manager.save_chunk(
        upload_uuid,
        offset,
        file_data,
        lambda: CC1UploadSession(upload_uuid, total_size, self._storage),
        filename_hint=filename,
      )
    except Exception:
      logger.exception('Failed to save CC1 upload chunk')

    return printer_response

  async def cleanup_once(self) -> int:
    return await self._manager.cleanup(self._upload_timeout)

  async def discard_all(self) -> None:
    await self._manager.discard_all()
