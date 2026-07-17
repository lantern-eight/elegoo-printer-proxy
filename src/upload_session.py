'''Base class and session manager for chunked G-code uploads (CC1 and CC2).'''

from __future__ import annotations

import asyncio
import logging
import time
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
  from collections.abc import Callable
  from pathlib import Path

  from .storage import GCodeStorage

logger = logging.getLogger(__name__)


class BaseUploadSession:
  '''Accumulates chunks for a single multi-part upload.'''

  def __init__(self, total_size: int, temp_key: str, storage: GCodeStorage) -> None:
    self.total_size = total_size
    self.bytes_written = 0
    self.created = time.monotonic()
    self.lock = asyncio.Lock()
    self._temp_key = temp_key
    self._path = storage.temp_path(temp_key)
    self._storage = storage
    self._fh: IO[bytes] | None = None

  def write_chunk(self, offset: int, data: bytes) -> None:
    if self._fh is None:
      self._fh = open(self._path, 'wb')  # noqa: SIM115
    self._fh.seek(offset)
    self._fh.write(data)
    self._fh.flush()
    self.bytes_written = max(self.bytes_written, offset + len(data))

  @property
  def complete(self) -> bool:
    return self.bytes_written >= self.total_size

  def _close(self) -> None:
    if self._fh is not None:
      self._fh.close()
      self._fh = None

  def finalize(self, filename_hint: str | None = None) -> tuple[Path, ...]:
    self._close()
    return self._storage.save_gcode_file(self._path, filename_hint=filename_hint)

  def discard(self) -> None:
    self._close()
    self._storage.cleanup_temp(self._temp_key)


class ChunkSessionManager:
  '''Manages concurrent chunked-upload sessions with lock ordering.

  Encapsulates the concurrency protocol shared by CC1 and CC2 uploads:
  acquire global lock, lookup/create session, acquire session lock,
  release global lock, write chunk, finalize if complete, remove.
  '''

  def __init__(self) -> None:
    self.sessions: dict = {}
    self._lock = asyncio.Lock()

  async def save_chunk(
    self,
    key,
    offset: int,
    data,
    create_session: Callable[[], BaseUploadSession],
    *,
    filename_hint: str | None = None,
  ) -> None:
    async with self._lock:
      session = self.sessions.get(key)
      if session is None and offset != 0:
        logger.warning(
          'Chunk at offset %d with no session (late arrival?), skipping',
          offset,
        )
        return
      if session is None or offset == 0:
        if session is not None:
          async with session.lock:
            await asyncio.to_thread(session.discard)
        session = create_session()
        self.sessions[key] = session
      await session.lock.acquire()

    is_complete = False
    try:
      await asyncio.to_thread(session.write_chunk, offset, data)
      if session.complete:
        is_complete = True
        try:
          path, _meta = await asyncio.to_thread(
            session.finalize, filename_hint=filename_hint
          )
          logger.info('Upload complete: %s', path.name)
        except Exception:
          logger.exception('Failed to finalize upload')
    finally:
      session.lock.release()

    if is_complete:
      async with self._lock:
        if self.sessions.get(key) is session:
          del self.sessions[key]

  async def cleanup(self, max_age: float) -> int:
    '''Single pass: discard sessions older than *max_age* seconds. Returns count.'''
    cutoff = time.monotonic() - max_age
    async with self._lock:
      stale = [
        key for key, session in self.sessions.items() if session.created < cutoff
      ]
      for key in stale:
        session = self.sessions[key]
        async with session.lock:
          await asyncio.to_thread(session.discard)
        del self.sessions[key]
        logger.warning('Discarded stale upload session (key=%r)', key)
    return len(stale)

  async def discard_all(self) -> None:
    for session in self.sessions.values():
      await asyncio.to_thread(session.discard)
    self.sessions.clear()
