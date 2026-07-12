'''Tests for the CC1 WebSocket + upload proxy.

Covers request routing, upload capture with multipart form parsing,
session lifecycle, and WebSocket relay wiring.
'''

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web

from src.config import Config
from src.storage import GCodeStorage
from src.ws_proxy import WSProxy, _CC1UploadSession
from tests.conftest import make_gcode

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_config(tmp_path, **overrides) -> Config:
  config = Config.__new__(Config)
  defaults = {
    'printer_ip': '192.168.1.100',
    'printer_type': 'cc1',
    'http_port': 80,
    'mqtt_port': 1883,
    'camera_port': 8080,
    'mqtt_ws_port': 9001,
    'ws_port': 3030,
    'gcode_dir': str(tmp_path),
    'retention_days': 90,
    'gcode_timezone': ZoneInfo('UTC'),
    'upload_timeout': 300,
    'max_body_size': 256 * 1024 * 1024,
    'store_gcode': False,
    'log_level': 'WARNING',
  }
  defaults.update(overrides)
  for key, value in defaults.items():
    object.__setattr__(config, key, value)
  return config


def _build_multipart(
  uuid: str,
  offset: int,
  total_size: int,
  file_data: bytes,
  filename: str = 'test.gcode',
  md5: str = 'abc123',
) -> tuple[bytes, str]:
  '''Build a CC1-style multipart form body and return (body, content_type).'''
  boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
  parts = []

  fields = {
    'Check': '1',
    'S-File-MD5': md5,
    'Offset': str(offset),
    'Uuid': uuid,
    'TotalSize': str(total_size),
  }

  for field_name, field_value in fields.items():
    parts.append(
      f'------{boundary}\r\n'
      f'Content-Disposition: form-data; name="{field_name}"\r\n'
      f'\r\n'
      f'{field_value}\r\n'
    )

  parts.append(
    f'------{boundary}\r\n'
    f'Content-Disposition: form-data; name="File"; filename="{filename}"\r\n'
    f'Content-Type: application/octet-stream\r\n'
    f'\r\n'
  )
  file_part_header = ''.join(parts).encode()
  file_part_footer = f'\r\n------{boundary}--\r\n'.encode()

  body = file_part_header + file_data + file_part_footer
  content_type = f'multipart/form-data; boundary=----{boundary}'
  return body, content_type


# ------------------------------------------------------------------
# CC1 upload session lifecycle
# ------------------------------------------------------------------


class TestCC1UploadSession:
  def test_write_first_chunk(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _CC1UploadSession('test-uuid', 100, 'md5hash', storage)

    session.write_chunk(0, b'A' * 50)
    assert session.bytes_written == 50
    assert not session.complete

  def test_complete_when_all_bytes_written(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='test')
    session = _CC1UploadSession('test-uuid', len(data), 'md5hash', storage)

    session.write_chunk(0, data)
    assert session.complete

  def test_bytes_written_tracks_high_water_mark(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _CC1UploadSession('test-uuid', 1000, 'md5hash', storage)

    session.write_chunk(500, b'X' * 100)
    assert session.bytes_written == 600

    session.write_chunk(0, b'Y' * 200)
    assert session.bytes_written == 600

  def test_finalize_saves_and_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='finalized')
    session = _CC1UploadSession('test-uuid', len(data), 'md5hash', storage)
    session.write_chunk(0, data)

    json_path, metadata = session.finalize()
    assert json_path.exists()
    assert json_path.suffix == '.json'
    assert not storage.temp_path('cc1_test-uuid').exists()

  def test_discard_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _CC1UploadSession('test-uuid', 100, 'md5hash', storage)
    session.write_chunk(0, b'X' * 50)

    session.discard()
    assert not storage.temp_path('cc1_test-uuid').exists()


# ------------------------------------------------------------------
# Multipart parsing
# ------------------------------------------------------------------


class TestMultipartParsing:
  @pytest.mark.asyncio
  async def test_parses_cc1_upload_form(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    file_data = b'G28\nG1 X10 Y10\n'
    body, content_type = _build_multipart(
      uuid='abc-123',
      offset=0,
      total_size=len(file_data),
      file_data=file_data,
      filename='test.gcode',
    )

    fields = proxy._parse_multipart_bytes(body, content_type)
    assert fields is not None
    assert fields['Uuid'] == 'abc-123'
    assert fields['Offset'] == '0'
    assert fields['TotalSize'] == str(len(file_data))
    assert fields['file_data'] == file_data
    assert fields['filename'] == 'test.gcode'

  @pytest.mark.asyncio
  async def test_returns_none_for_non_multipart(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    fields = proxy._parse_multipart_bytes(b'plain body', 'application/octet-stream')
    assert fields is None

  @pytest.mark.asyncio
  async def test_returns_none_for_empty_body(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    fields = proxy._parse_multipart_bytes(b'', 'multipart/form-data; boundary=---xyz')
    assert fields is None


# ------------------------------------------------------------------
# Request routing
# ------------------------------------------------------------------


class TestWSProxyRouting:
  @pytest.mark.asyncio
  async def test_post_upload_intercepted(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    proxy._handle_upload = AsyncMock(return_value=web.Response(status=200))
    proxy._handle_ws = AsyncMock(return_value=web.WebSocketResponse())
    proxy._passthrough = AsyncMock(return_value=web.Response(status=200))

    request = MagicMock()
    request.method = 'POST'
    request.path = '/uploadFile/upload'
    request.headers = {}

    await proxy.handle_request(request)
    proxy._handle_upload.assert_called_once()
    proxy._passthrough.assert_not_called()

  @pytest.mark.asyncio
  async def test_websocket_upgrade_intercepted(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    proxy._handle_upload = AsyncMock(return_value=web.Response(status=200))
    proxy._handle_ws = AsyncMock(return_value=web.WebSocketResponse())
    proxy._passthrough = AsyncMock(return_value=web.Response(status=200))

    request = MagicMock()
    request.method = 'GET'
    request.path = '/websocket'
    request.headers = {'Upgrade': 'websocket'}

    await proxy.handle_request(request)
    proxy._handle_ws.assert_called_once()
    proxy._handle_upload.assert_not_called()

  @pytest.mark.asyncio
  async def test_other_requests_passthrough(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    proxy._handle_upload = AsyncMock(return_value=web.Response(status=200))
    proxy._handle_ws = AsyncMock(return_value=web.WebSocketResponse())
    proxy._passthrough = AsyncMock(return_value=web.Response(status=200))

    request = MagicMock()
    request.method = 'GET'
    request.path = '/status'
    request.headers = {}

    await proxy.handle_request(request)
    proxy._passthrough.assert_called_once()
    proxy._handle_upload.assert_not_called()
    proxy._handle_ws.assert_not_called()


# ------------------------------------------------------------------
# Stale session cleanup
# ------------------------------------------------------------------


class TestWSProxyStaleCleanup:
  @pytest.mark.asyncio
  async def test_stale_sessions_discarded(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    stale = _CC1UploadSession('stale-uuid', 1000, 'md5', storage)
    stale.created = time.monotonic() - 600
    proxy._sessions['stale-uuid'] = stale

    fresh = _CC1UploadSession('fresh-uuid', 2000, 'md5', storage)
    proxy._sessions['fresh-uuid'] = fresh

    removed = await proxy._cleanup_once()

    assert removed == 1
    assert 'stale-uuid' not in proxy._sessions
    assert 'fresh-uuid' in proxy._sessions

  @pytest.mark.asyncio
  async def test_fresh_sessions_survive(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    fresh = _CC1UploadSession('fresh-uuid', 1000, 'md5', storage)
    proxy._sessions['fresh-uuid'] = fresh

    removed = await proxy._cleanup_once()

    assert removed == 0
    assert 'fresh-uuid' in proxy._sessions


# ------------------------------------------------------------------
# Proxy lifecycle
# ------------------------------------------------------------------


class TestWSProxyLifecycle:
  @pytest.mark.asyncio
  async def test_stop_discards_all_sessions(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    proxy._client = MagicMock()
    proxy._client.close = AsyncMock()
    proxy._runner = MagicMock()
    proxy._runner.cleanup = AsyncMock()

    session_1 = _CC1UploadSession('uuid-1', 100, 'md5', storage)
    session_2 = _CC1UploadSession('uuid-2', 200, 'md5', storage)
    proxy._sessions['uuid-1'] = session_1
    proxy._sessions['uuid-2'] = session_2

    await proxy.stop()

    assert len(proxy._sessions) == 0
    assert not storage.temp_path('cc1_uuid-1').exists()
    assert not storage.temp_path('cc1_uuid-2').exists()
