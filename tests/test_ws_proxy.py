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

from src.cc1_upload import CC1UploadSession, parse_multipart_bytes
from src.config import Config
from src.storage import GCodeStorage
from src.ws_proxy import WSProxy
from tests.conftest import build_cc1_multipart as _build_multipart
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
    'discovery_port': 3000,
    'advertise_ip': None,
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


# ------------------------------------------------------------------
# CC1 upload session lifecycle
# ------------------------------------------------------------------


class TestCC1UploadSession:
  def test_write_first_chunk(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = CC1UploadSession('test-uuid', 100, 'md5hash', storage)

    session.write_chunk(0, b'A' * 50)
    assert session.bytes_written == 50
    assert not session.complete

  def test_complete_when_all_bytes_written(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='test')
    session = CC1UploadSession('test-uuid', len(data), 'md5hash', storage)

    session.write_chunk(0, data)
    assert session.complete

  def test_bytes_written_tracks_high_water_mark(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = CC1UploadSession('test-uuid', 1000, 'md5hash', storage)

    session.write_chunk(500, b'X' * 100)
    assert session.bytes_written == 600

    session.write_chunk(0, b'Y' * 200)
    assert session.bytes_written == 600

  def test_finalize_saves_and_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='finalized')
    session = CC1UploadSession('test-uuid', len(data), 'md5hash', storage)
    session.write_chunk(0, data)

    json_path, metadata = session.finalize()
    assert json_path.exists()
    assert json_path.suffix == '.json'
    assert not storage.temp_path('cc1_test-uuid').exists()

  def test_discard_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = CC1UploadSession('test-uuid', 100, 'md5hash', storage)
    session.write_chunk(0, b'X' * 50)

    session.discard()
    assert not storage.temp_path('cc1_test-uuid').exists()


# ------------------------------------------------------------------
# Multipart parsing
# ------------------------------------------------------------------


class TestMultipartParsing:
  def test_parses_cc1_upload_form(self):
    file_data = b'G28\nG1 X10 Y10\n'
    body, content_type = _build_multipart(
      uuid='abc-123',
      offset=0,
      total_size=len(file_data),
      file_data=file_data,
      filename='test.gcode',
    )

    fields = parse_multipart_bytes(body, content_type)
    assert fields is not None
    assert fields['Uuid'] == 'abc-123'
    assert fields['Offset'] == '0'
    assert fields['TotalSize'] == str(len(file_data))
    assert fields['file_data'] == file_data
    assert fields['filename'] == 'test.gcode'

  def test_returns_none_for_non_multipart(self):
    fields = parse_multipart_bytes(b'plain body', 'application/octet-stream')
    assert fields is None

  def test_returns_none_for_empty_body(self):
    fields = parse_multipart_bytes(b'', 'multipart/form-data; boundary=---xyz')
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
# MainboardIP rewrite on port-3030 passthrough responses
# ------------------------------------------------------------------


def _mock_aiohttp_client(status=200, body=b'ok', headers=None):
  cm = MagicMock()
  response = MagicMock()
  response.status = status
  response.read = AsyncMock(return_value=body)
  response.headers = headers or {}
  cm.__aenter__ = AsyncMock(return_value=response)
  cm.__aexit__ = AsyncMock(return_value=False)
  client = MagicMock()
  client.request = MagicMock(return_value=cm)
  return client


class TestPassthroughMainboardIPRewrite:
  @pytest.mark.asyncio
  async def test_response_rewritten_when_advertising(self, tmp_path):
    config = _make_config(tmp_path, advertise_ip='192.168.1.200')
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    printer_payload = b'{"Data": {"MainboardIP": "192.168.1.100"}}'
    proxy._client = _mock_aiohttp_client(body=printer_payload)

    request = MagicMock()
    request.method = 'GET'
    request.path_qs = '/info'
    request.headers = MagicMock()
    request.headers.items.return_value = []
    request.can_read_body = False
    request.read = AsyncMock(return_value=b'')

    response = await proxy._passthrough(request)

    assert response.status == 200
    assert b'192.168.1.200' in response.body
    assert b'192.168.1.100' not in response.body

  @pytest.mark.asyncio
  async def test_response_untouched_without_advertise_ip(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)
    printer_payload = b'{"Data": {"MainboardIP": "192.168.1.100"}}'
    proxy._client = _mock_aiohttp_client(body=printer_payload)

    request = MagicMock()
    request.method = 'GET'
    request.path_qs = '/info'
    request.headers = MagicMock()
    request.headers.items.return_value = []
    request.can_read_body = False
    request.read = AsyncMock(return_value=b'')

    response = await proxy._passthrough(request)

    assert response.status == 200
    assert response.body == printer_payload


# ------------------------------------------------------------------
# Stale session cleanup
# ------------------------------------------------------------------


class TestWSProxyStaleCleanup:
  @pytest.mark.asyncio
  async def test_stale_sessions_discarded(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    stale = CC1UploadSession('stale-uuid', 1000, 'md5', storage)
    stale.created = time.monotonic() - 600
    proxy._capture.sessions['stale-uuid'] = stale

    fresh = CC1UploadSession('fresh-uuid', 2000, 'md5', storage)
    proxy._capture.sessions['fresh-uuid'] = fresh

    removed = await proxy._capture.cleanup_once()

    assert removed == 1
    assert 'stale-uuid' not in proxy._capture.sessions
    assert 'fresh-uuid' in proxy._capture.sessions

  @pytest.mark.asyncio
  async def test_fresh_sessions_survive(self, tmp_path):
    config = _make_config(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    proxy = WSProxy(config, storage)

    fresh = CC1UploadSession('fresh-uuid', 1000, 'md5', storage)
    proxy._capture.sessions['fresh-uuid'] = fresh

    removed = await proxy._capture.cleanup_once()

    assert removed == 0
    assert 'fresh-uuid' in proxy._capture.sessions


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

    session_1 = CC1UploadSession('uuid-1', 100, 'md5', storage)
    session_2 = CC1UploadSession('uuid-2', 200, 'md5', storage)
    proxy._capture.sessions['uuid-1'] = session_1
    proxy._capture.sessions['uuid-2'] = session_2

    await proxy.stop()

    assert len(proxy._capture.sessions) == 0
    assert not storage.temp_path('cc1_uuid-1').exists()
    assert not storage.temp_path('cc1_uuid-2').exists()
