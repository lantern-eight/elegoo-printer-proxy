'''Tests for the shared chunked-upload session manager.

Covers concurrency edges of ChunkSessionManager that the proxy-level
tests don't reach: late chunks racing a finalizing session, and
shutdown discarding sessions while writers are active.
'''

from __future__ import annotations

import asyncio
import threading

import pytest

from src.cc1_upload import CC1UploadSession
from src.storage import GCodeStorage
from src.upload_session import ChunkSessionManager
from tests.conftest import make_gcode


class TestLateChunkAfterFinalize:
  @pytest.mark.asyncio
  async def test_waiting_chunk_dropped_once_session_finalized(self, tmp_path):
    '''A chunk queued on the session lock during finalize must not
    reopen the deleted temp file and archive a second copy.'''
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    manager = ChunkSessionManager()

    saves = []
    original_save = storage.save_gcode_file

    def counting_save(path, filename_hint=None):
      saves.append(path)
      return original_save(path, filename_hint=filename_hint)

    storage.save_gcode_file = counting_save

    data = make_gcode(input_filename_base='race')
    half = len(data) // 2
    entered_finalize = threading.Event()
    release_finalize = threading.Event()

    def factory():
      session = CC1UploadSession('race-uuid', len(data), storage)
      bound_finalize = session.finalize

      def slow_finalize(filename_hint=None):
        entered_finalize.set()
        assert release_finalize.wait(timeout=5)
        return bound_finalize(filename_hint=filename_hint)

      session.finalize = slow_finalize
      return session

    await manager.save_chunk('race-uuid', 0, data[:half], factory)
    assert 'race-uuid' in manager.sessions

    finalizing = asyncio.create_task(
      manager.save_chunk('race-uuid', half, data[half:], factory)
    )
    await asyncio.to_thread(entered_finalize.wait, 5)

    late = asyncio.create_task(
      manager.save_chunk('race-uuid', half, data[half:], factory)
    )
    # Let the late chunk find the session and block on its lock while
    # finalize is still in flight.
    await asyncio.sleep(0.05)
    release_finalize.set()

    await finalizing
    await late

    assert len(saves) == 1
    assert manager.sessions == {}
    assert not storage.temp_path('cc1_race-uuid').exists()


class TestDiscardAllSynchronization:
  @pytest.mark.asyncio
  async def test_discard_all_waits_for_active_writer(self, tmp_path):
    '''Shutdown must not delete a session's temp file while a chunk
    write is still in flight — the writer would recreate it after
    discard and leak it.'''
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    manager = ChunkSessionManager()

    in_write = threading.Event()
    release_write = threading.Event()

    def factory():
      session = CC1UploadSession('busy-uuid', 1000, storage)
      bound_write = session.write_chunk

      def slow_write(offset, data):
        in_write.set()
        assert release_write.wait(timeout=5)
        bound_write(offset, data)

      session.write_chunk = slow_write
      return session

    writer = asyncio.create_task(manager.save_chunk('busy-uuid', 0, b'X' * 10, factory))
    await asyncio.to_thread(in_write.wait, 5)

    discard = asyncio.create_task(manager.discard_all())
    await asyncio.sleep(0.05)
    assert not discard.done()

    release_write.set()
    await writer
    await discard

    assert manager.sessions == {}
    assert not storage.temp_path('cc1_busy-uuid').exists()
