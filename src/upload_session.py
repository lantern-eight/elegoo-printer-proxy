'''Base class for chunked G-code upload sessions (CC1 and CC2).'''

from __future__ import annotations

import asyncio
import time
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
  from pathlib import Path

  from .storage import GCodeStorage


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
