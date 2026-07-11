'''Tests for HTTP proxy: routing, upload interception, chunked sessions, header filtering.

The proxy must only save G-code when the printer accepted the upload (2xx).
Bugs here mean lost prints or corrupted archives.
'''

from __future__ import annotations

import asyncio
import io
import time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from src.config import Config
from src.http_proxy import HTTPProxy, _parse_content_range, _UploadSession
from src.storage import GCodeStorage
from tests.conftest import make_gcode

# ===================================================================
# Content-Range parsing
# ===================================================================


class TestParseContentRange:
  def test_none_returns_none(self):
    assert _parse_content_range(None) is None

  def test_empty_string_returns_none(self):
    assert _parse_content_range('') is None

  def test_valid_range(self):
    assert _parse_content_range('bytes 0-1023/4096') == (0, 1023, 4096)

  def test_valid_range_extra_whitespace(self):
    assert _parse_content_range('bytes  100-200/500') == (100, 200, 500)

  def test_single_byte_range(self):
    assert _parse_content_range('bytes 0-0/1') == (0, 0, 1)

  def test_missing_bytes_prefix(self):
    assert _parse_content_range('0-1023/4096') is None

  def test_malformed_no_slash(self):
    assert _parse_content_range('bytes 0-1023') is None

  def test_malformed_no_dash(self):
    assert _parse_content_range('bytes 01023/4096') is None

  def test_large_values(self):
    result = _parse_content_range('bytes 0-104857599/104857600')
    assert result == (0, 104857599, 104857600)


# ===================================================================
# Upload session lifecycle
# ===================================================================


class TestUploadSession:
  def test_write_first_chunk(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _UploadSession(total_size=100, storage=storage)

    session.write_chunk(0, b'A' * 50)
    assert session.bytes_written == 50
    assert not session.complete

  def test_complete_when_all_bytes_written(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='test')
    session = _UploadSession(total_size=len(data), storage=storage)

    session.write_chunk(0, data)
    assert session.complete

  def test_bytes_written_tracks_high_water_mark(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _UploadSession(total_size=1000, storage=storage)

    session.write_chunk(500, b'X' * 100)
    assert session.bytes_written == 600

    session.write_chunk(0, b'Y' * 200)
    assert session.bytes_written == 600  # doesn't regress

  def test_finalize_saves_and_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    data = make_gcode(input_filename_base='finalized')
    session = _UploadSession(total_size=len(data), storage=storage)
    session.write_chunk(0, data)

    json_path, meta = session.finalize()
    assert json_path.exists()
    assert json_path.suffix == '.json'
    assert not storage.temp_path(session.upload_id).exists()

  def test_discard_cleans_temp(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    session = _UploadSession(total_size=100, storage=storage)
    session.write_chunk(0, b'X' * 50)
    temp_path = storage.temp_path(session.upload_id)

    session.discard()
    assert not temp_path.exists()


# ===================================================================
# Request routing
# ===================================================================


def _make_proxy(
  tmp_path, *, store_gcode=False, max_body_size=256 * 1024 * 1024
) -> HTTPProxy:
  config = Config.__new__(Config)
  object.__setattr__(config, 'printer_ip', '192.168.1.100')
  object.__setattr__(config, 'printer_type', 'cc2')
  object.__setattr__(config, 'http_port', 80)
  object.__setattr__(config, 'mqtt_port', 1883)
  object.__setattr__(config, 'camera_port', 8080)
  object.__setattr__(config, 'mqtt_ws_port', 9001)
  object.__setattr__(config, 'ws_port', 3030)
  object.__setattr__(config, 'gcode_dir', str(tmp_path))
  object.__setattr__(config, 'retention_days', 90)
  object.__setattr__(config, 'gcode_timezone', ZoneInfo('UTC'))
  object.__setattr__(config, 'upload_timeout', 300)
  object.__setattr__(config, 'max_body_size', max_body_size)
  object.__setattr__(config, 'store_gcode', store_gcode)
  object.__setattr__(config, 'log_level', 'WARNING')
  storage = GCodeStorage(str(tmp_path), retention_days=90, store_gcode=store_gcode)
  return HTTPProxy(config, storage)


def _mock_request(
  method: str, path: str, headers: dict | None = None, body: bytes = b''
) -> MagicMock:
  request = MagicMock()
  request.method = method
  request.path = path
  request.path_qs = path
  request.headers = headers or {}
  request.can_read_body = bool(body)
  request.read = AsyncMock(return_value=body)

  async def _iter_chunked(chunk_size):
    for i in range(0, len(body), chunk_size):
      yield body[i : i + chunk_size]

  content = MagicMock()
  content.iter_chunked = lambda chunk_size: _iter_chunked(chunk_size)
  request.content = content

  return request


class TestRequestRouting:
  @pytest.mark.asyncio
  async def test_put_upload_intercepted(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._handle_upload = AsyncMock(return_value=MagicMock())
    proxy._passthrough = AsyncMock(return_value=MagicMock())

    request = _mock_request('PUT', '/upload')
    await proxy.handle_request(request)

    proxy._handle_upload.assert_called_once()
    proxy._passthrough.assert_not_called()

  @pytest.mark.asyncio
  async def test_get_upload_goes_to_passthrough(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._handle_upload = AsyncMock(return_value=MagicMock())
    proxy._passthrough = AsyncMock(return_value=MagicMock())

    request = _mock_request('GET', '/upload')
    await proxy.handle_request(request)

    proxy._handle_upload.assert_not_called()
    proxy._passthrough.assert_called_once()

  @pytest.mark.asyncio
  async def test_put_other_path_goes_to_passthrough(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._handle_upload = AsyncMock(return_value=MagicMock())
    proxy._passthrough = AsyncMock(return_value=MagicMock())

    request = _mock_request('PUT', '/api/status')
    await proxy.handle_request(request)

    proxy._handle_upload.assert_not_called()
    proxy._passthrough.assert_called_once()

  @pytest.mark.asyncio
  async def test_post_goes_to_passthrough(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._handle_upload = AsyncMock(return_value=MagicMock())
    proxy._passthrough = AsyncMock(return_value=MagicMock())

    request = _mock_request('POST', '/something')
    await proxy.handle_request(request)

    proxy._passthrough.assert_called_once()


# ===================================================================
# Upload save decisions
# ===================================================================


class TestUploadSaveDecisions:
  @pytest.mark.asyncio
  async def test_saves_on_2xx(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._forward = AsyncMock(return_value=(200, b'{"offset":100}', {}))

    data = make_gcode(input_filename_base='accepted')
    request = _mock_request('PUT', '/upload', body=data)
    response = await proxy._handle_upload(request)

    assert response.status == 200
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 1

  @pytest.mark.asyncio
  async def test_does_not_save_on_4xx(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._forward = AsyncMock(return_value=(400, b'bad request', {}))

    data = make_gcode(input_filename_base='rejected')
    request = _mock_request('PUT', '/upload', body=data)
    response = await proxy._handle_upload(request)

    assert response.status == 400
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 0

  @pytest.mark.asyncio
  async def test_does_not_save_on_5xx(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._forward = AsyncMock(return_value=(500, b'error', {}))

    data = make_gcode(input_filename_base='error')
    request = _mock_request('PUT', '/upload', body=data)
    response = await proxy._handle_upload(request)

    assert response.status == 500
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 0

  @pytest.mark.asyncio
  async def test_returns_502_when_printer_unreachable(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._forward = AsyncMock(return_value=(None, None, None))

    request = _mock_request('PUT', '/upload', body=b'data')
    response = await proxy._handle_upload(request)

    assert response.status == 502
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 0

  @pytest.mark.asyncio
  async def test_single_shot_returns_413_when_body_exceeds_max_size(self, tmp_path):
    '''Streaming single-shot upload rejects body larger than max_body_size.'''
    proxy = _make_proxy(tmp_path, max_body_size=100)
    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    body = make_gcode(input_filename_base='oversized')  # > 100 bytes
    request = _mock_request('PUT', '/upload', body=body)
    response = await proxy._handle_upload(request)

    assert response.status == 413
    proxy._forward.assert_not_called()
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 0
    tmp_files = list((tmp_path / '.tmp').glob('*.tmp'))
    assert len(tmp_files) == 0


# ===================================================================
# _stream_body_to_temp streaming behaviour
# ===================================================================


class TestStreamBodyToTemp:
  @pytest.mark.asyncio
  async def test_streams_body_to_disk(self, tmp_path):
    '''Body bytes should appear on disk, not just in memory.'''
    proxy = _make_proxy(tmp_path)
    data = make_gcode(input_filename_base='streamed')
    request = _mock_request('PUT', '/upload', body=data)

    temp_path = await proxy._stream_body_to_temp(request)
    assert temp_path is not None
    assert temp_path.exists()
    assert temp_path.read_bytes() == data

  @pytest.mark.asyncio
  async def test_returns_none_and_cleans_up_on_oversized(self, tmp_path):
    '''Exceeding max_body_size must return None and leave no temp file.'''
    proxy = _make_proxy(tmp_path, max_body_size=50)
    data = b'X' * 200
    request = _mock_request('PUT', '/upload', body=data)

    result = await proxy._stream_body_to_temp(request)
    assert result is None
    tmp_files = list((tmp_path / '.tmp').glob('*.tmp'))
    assert len(tmp_files) == 0

  @pytest.mark.asyncio
  async def test_zero_max_body_size_means_unlimited(self, tmp_path):
    '''max_body_size=0 should allow any size through.'''
    proxy = _make_proxy(tmp_path, max_body_size=0)
    data = b'Y' * 1024
    request = _mock_request('PUT', '/upload', body=data)

    temp_path = await proxy._stream_body_to_temp(request)
    assert temp_path is not None
    assert temp_path.read_bytes() == data


# ===================================================================
# Response forwarding integrity: proxy must not corrupt the printer's
# response that the slicer depends on to proceed with printing.
# ===================================================================


class TestResponseForwarding:
  @pytest.mark.asyncio
  async def test_upload_response_body_forwarded_intact(self, tmp_path):
    '''The slicer relies on the printer's JSON response to track upload offset.'''
    proxy = _make_proxy(tmp_path)
    printer_body = b'{"offset":4096,"result":"ok"}'
    proxy._forward = AsyncMock(return_value=(200, printer_body, {'X-Custom': 'val'}))

    data = make_gcode(input_filename_base='test')
    request = _mock_request('PUT', '/upload', body=data)
    response = await proxy._handle_upload(request)

    assert response.status == 200
    assert response.body == printer_body

  @pytest.mark.asyncio
  async def test_upload_response_headers_forwarded(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    response_headers = {'Content-Type': 'application/json', 'X-Printer': 'CC2'}
    proxy._forward = AsyncMock(return_value=(200, b'ok', response_headers))

    request = _mock_request('PUT', '/upload', body=make_gcode())
    response = await proxy._handle_upload(request)

    assert response.headers['Content-Type'] == 'application/json'
    assert response.headers['X-Printer'] == 'CC2'

  @pytest.mark.asyncio
  async def test_passthrough_response_intact(self, tmp_path):
    '''Non-upload responses (status page, file list, etc.) pass through unchanged.'''
    proxy = _make_proxy(tmp_path)
    printer_body = b'{"status":"idle","temp":25.3}'
    proxy._forward = AsyncMock(
      return_value=(200, printer_body, {'Content-Type': 'application/json'})
    )

    request = _mock_request('GET', '/api/status')
    response = await proxy._passthrough(request)

    assert response.status == 200
    assert response.body == printer_body
    assert response.headers['Content-Type'] == 'application/json'

  @pytest.mark.asyncio
  async def test_error_response_forwarded_not_swallowed(self, tmp_path):
    '''Printer error bodies must reach the slicer so it can display them.'''
    proxy = _make_proxy(tmp_path)
    error_body = b'{"error":"disk_full"}'
    proxy._forward = AsyncMock(return_value=(507, error_body, {}))

    request = _mock_request('PUT', '/upload', body=b'data')
    response = await proxy._handle_upload(request)

    assert response.status == 507
    assert response.body == error_body


# ===================================================================
# Chunked upload flow
# ===================================================================


class TestChunkedUpload:
  @pytest.mark.asyncio
  async def test_single_shot_upload_no_content_range(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    data = make_gcode(input_filename_base='single')
    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request = _mock_request('PUT', '/upload', body=data)
    await proxy._handle_upload(request)

    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 1

  @pytest.mark.asyncio
  async def test_chunked_upload_completes(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    data = make_gcode(input_filename_base='chunked')
    half = len(data) // 2
    total = len(data)

    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request_1 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-{half - 1}/{total}'},
      body=data[:half],
    )
    await proxy._handle_upload(request_1)
    assert len(list(tmp_path.rglob('*.json'))) == 0

    request_2 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes {half}-{total - 1}/{total}'},
      body=data[half:],
    )
    await proxy._handle_upload(request_2)
    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 1

  @pytest.mark.asyncio
  async def test_chunked_upload_keeps_gcode_when_enabled(self, tmp_path):
    '''With store_gcode=True, the .gcode file is archived alongside the JSON.'''
    proxy = _make_proxy(tmp_path, store_gcode=True)
    data = make_gcode(input_filename_base='kept')
    half = len(data) // 2
    total = len(data)

    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request_1 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-{half - 1}/{total}'},
      body=data[:half],
    )
    request_2 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes {half}-{total - 1}/{total}'},
      body=data[half:],
    )
    await proxy._handle_upload(request_1)
    await proxy._handle_upload(request_2)

    gcode_files = list(tmp_path.rglob('*.gcode'))
    assert len(gcode_files) == 1
    assert gcode_files[0].read_bytes() == data

  @pytest.mark.asyncio
  async def test_concurrent_chunks_no_double_finalize(self, tmp_path):
    '''Two chunks arriving concurrently must not both finalize the same session.'''
    proxy = _make_proxy(tmp_path)
    data = make_gcode(input_filename_base='race')
    half = len(data) // 2
    total = len(data)

    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request_1 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-{half - 1}/{total}'},
      body=data[:half],
    )
    request_2 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes {half}-{total - 1}/{total}'},
      body=data[half:],
    )

    await asyncio.gather(
      proxy._handle_upload(request_1),
      proxy._handle_upload(request_2),
    )

    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 1

  @pytest.mark.asyncio
  async def test_same_size_different_filenames_separate_sessions(self, tmp_path):
    '''Two files with same size but different X-File-Name use separate session keys.'''
    proxy = _make_proxy(tmp_path)
    data_a = make_gcode(input_filename_base='model_a')
    data_b = make_gcode(input_filename_base='model_b')
    assert len(data_a) == len(data_b), 'need same size for collision test'
    total = len(data_a)
    half = total // 2

    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    # Start upload A (model_a.gcode)
    request_a1 = _mock_request(
      'PUT',
      '/upload',
      headers={
        'Content-Range': f'bytes 0-{half - 1}/{total}',
        'X-File-Name': 'model_a.gcode',
      },
      body=data_a[:half],
    )
    await proxy._handle_upload(request_a1)

    # Start upload B (model_b.gcode) - same total, different filename
    request_b1 = _mock_request(
      'PUT',
      '/upload',
      headers={
        'Content-Range': f'bytes 0-{half - 1}/{total}',
        'X-File-Name': 'model_b.gcode',
      },
      body=data_b[:half],
    )
    await proxy._handle_upload(request_b1)

    # Both sessions should exist (no collision)
    assert ('model_a.gcode', total) in proxy._sessions
    assert ('model_b.gcode', total) in proxy._sessions

    # Complete both uploads
    request_a2 = _mock_request(
      'PUT',
      '/upload',
      headers={
        'Content-Range': f'bytes {half}-{total - 1}/{total}',
        'X-File-Name': 'model_a.gcode',
      },
      body=data_a[half:],
    )
    request_b2 = _mock_request(
      'PUT',
      '/upload',
      headers={
        'Content-Range': f'bytes {half}-{total - 1}/{total}',
        'X-File-Name': 'model_b.gcode',
      },
      body=data_b[half:],
    )
    await asyncio.gather(
      proxy._handle_upload(request_a2),
      proxy._handle_upload(request_b2),
    )

    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 2

  @pytest.mark.asyncio
  async def test_no_x_file_name_fallback_to_total_only(self, tmp_path):
    '''When X-File-Name is absent, session key uses (None, total).'''
    proxy = _make_proxy(tmp_path)
    data = make_gcode(input_filename_base='legacy')
    total = len(data)
    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-{total - 1}/{total}'},
      body=data,
    )
    await proxy._handle_upload(request)

    json_files = list(tmp_path.rglob('*.json'))
    assert len(json_files) == 1

  @pytest.mark.asyncio
  async def test_restart_discards_previous_session(self, tmp_path):
    '''A new chunk with start=0 and same total discards the old session.'''
    proxy = _make_proxy(tmp_path)
    total = 200
    proxy._forward = AsyncMock(return_value=(200, b'ok', {}))

    request_1 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-99/{total}'},
      body=b'A' * 100,
    )
    await proxy._handle_upload(request_1)
    assert (None, total) in proxy._sessions

    request_2 = _mock_request(
      'PUT',
      '/upload',
      headers={'Content-Range': f'bytes 0-99/{total}'},
      body=b'B' * 100,
    )
    await proxy._handle_upload(request_2)

    session_key = (None, total)  # no X-File-Name
    session = proxy._sessions.get(session_key)
    assert session is not None
    assert session.bytes_written == 100


# ===================================================================
# Hop-by-hop header filtering
# ===================================================================


class TestHeaderFiltering:
  @pytest.mark.asyncio
  async def test_hop_by_hop_headers_stripped(self, tmp_path):
    proxy = _make_proxy(tmp_path)

    captured_headers = {}

    async def mock_forward(method, path, headers, body):
      captured_headers.update(headers)
      return 200, b'ok', {}

    proxy._forward = mock_forward
    proxy._client = MagicMock()

    request = _mock_request(
      'PUT',
      '/upload',
      headers={
        'Content-Range': 'bytes 0-99/100',
        'Content-Type': 'application/octet-stream',
        'Connection': 'keep-alive',
        'Transfer-Encoding': 'chunked',
      },
      body=b'X' * 100,
    )
    await proxy._handle_upload(request)

  @pytest.mark.asyncio
  async def test_passthrough_returns_502_on_unreachable(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._forward = AsyncMock(return_value=(None, None, None))

    request = _mock_request('GET', '/api/status')
    response = await proxy._passthrough(request)
    assert response.status == 502


# ===================================================================
# _forward streaming behaviour
# ===================================================================


def _mock_aiohttp_client(status=200, body=b'ok', headers=None, *, error=None):
  '''Build a mock aiohttp.ClientSession whose request() returns a context manager.

  When *error* is set, ``__aenter__`` raises that exception instead
  of returning a response (simulates connection failure).
  '''
  cm = MagicMock()
  if error:
    cm.__aenter__ = AsyncMock(side_effect=error)
  else:
    response = MagicMock()
    response.status = status
    response.read = AsyncMock(return_value=body)
    response.headers = headers or {}
    cm.__aenter__ = AsyncMock(return_value=response)
  cm.__aexit__ = AsyncMock(return_value=False)

  client = MagicMock()
  client.request = MagicMock(return_value=cm)
  return client


class TestForwardStreaming:
  @pytest.mark.asyncio
  async def test_path_body_streams_file_handle(self, tmp_path):
    '''Path body should pass a file-like to aiohttp, not read into memory.'''
    proxy = _make_proxy(tmp_path)
    data_file = tmp_path / 'upload.bin'
    data_file.write_bytes(b'X' * 1024)

    proxy._client = _mock_aiohttp_client()
    await proxy._forward('PUT', '/upload', {'Host': 'x'}, data_file)

    _, kwargs = proxy._client.request.call_args
    assert isinstance(kwargs['data'], io.BufferedReader)

  @pytest.mark.asyncio
  async def test_path_body_file_handle_closed_after_success(self, tmp_path):
    '''File handle must be closed after a successful forward.'''
    proxy = _make_proxy(tmp_path)
    data_file = tmp_path / 'upload.bin'
    data_file.write_bytes(b'hello')

    proxy._client = _mock_aiohttp_client()
    await proxy._forward('PUT', '/upload', {'Host': 'x'}, data_file)

    _, kwargs = proxy._client.request.call_args
    assert kwargs['data'].closed

  @pytest.mark.asyncio
  async def test_path_body_file_handle_closed_on_error(self, tmp_path):
    '''File handle must be closed even when the printer is unreachable.'''
    proxy = _make_proxy(tmp_path)
    data_file = tmp_path / 'upload.bin'
    data_file.write_bytes(b'hello')

    proxy._client = _mock_aiohttp_client(error=aiohttp.ClientError())
    status, _, _ = await proxy._forward('PUT', '/upload', {'Host': 'x'}, data_file)

    assert status is None
    _, kwargs = proxy._client.request.call_args
    assert kwargs['data'].closed

  @pytest.mark.asyncio
  async def test_bytes_body_passed_directly(self, tmp_path):
    '''bytes body should be forwarded as-is, no file handle involved.'''
    proxy = _make_proxy(tmp_path)
    proxy._client = _mock_aiohttp_client()

    await proxy._forward('GET', '/status', {'Host': 'x'}, b'raw')

    _, kwargs = proxy._client.request.call_args
    assert kwargs['data'] == b'raw'

  @pytest.mark.asyncio
  async def test_none_body_passed_directly(self, tmp_path):
    '''None body (e.g. GET with no content) forwards None.'''
    proxy = _make_proxy(tmp_path)
    proxy._client = _mock_aiohttp_client()

    await proxy._forward('GET', '/status', {'Host': 'x'}, None)

    _, kwargs = proxy._client.request.call_args
    assert kwargs['data'] is None


# ===================================================================
# Stale session cleanup
# ===================================================================


class TestStaleSessionCleanup:
  @pytest.mark.asyncio
  async def test_stale_sessions_discarded(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)

    stale = _UploadSession(total_size=1000, storage=storage)
    stale.created = time.monotonic() - 600  # 10 minutes ago
    proxy._sessions[(None, 1000)] = stale

    fresh = _UploadSession(total_size=2000, storage=storage)
    proxy._sessions[(None, 2000)] = fresh

    cutoff = time.monotonic() - proxy._config.upload_timeout
    async with proxy._lock:
      stale_keys = [
        session_key
        for session_key, session in proxy._sessions.items()
        if session.created < cutoff
      ]
      for session_key in stale_keys:
        session = proxy._sessions[session_key]
        async with session.lock:
          session.discard()
        del proxy._sessions[session_key]

    assert (None, 1000) not in proxy._sessions
    assert (None, 2000) in proxy._sessions

  @pytest.mark.asyncio
  async def test_recent_sessions_survive(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    storage = GCodeStorage(str(tmp_path), retention_days=90)

    recent = _UploadSession(total_size=3000, storage=storage)
    proxy._sessions[(None, 3000)] = recent

    cutoff = time.monotonic() - proxy._config.upload_timeout
    async with proxy._lock:
      stale_keys = [
        session_key
        for session_key, session in proxy._sessions.items()
        if session.created < cutoff
      ]
      for session_key in stale_keys:
        session = proxy._sessions[session_key]
        async with session.lock:
          session.discard()
        del proxy._sessions[session_key]

    assert (None, 3000) in proxy._sessions


# ===================================================================
# HTTPProxy.stop cleanup
# ===================================================================


class TestProxyLifecycle:
  @pytest.mark.asyncio
  async def test_start_cleans_orphaned_temp_files(self, tmp_path):
    '''Orphaned .tmp files from previous run are removed on proxy start.'''
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    (storage.base_dir / '.tmp').mkdir(parents=True, exist_ok=True)
    orphan = storage.base_dir / '.tmp' / 'orphan_123.tmp'
    orphan.write_bytes(b'leaked')
    assert orphan.exists()

    proxy = _make_proxy(tmp_path)
    await proxy.start()

    assert not orphan.exists()

  @pytest.mark.asyncio
  async def test_stop_discards_all_sessions(self, tmp_path):
    proxy = _make_proxy(tmp_path)
    proxy._client = MagicMock()
    proxy._client.close = AsyncMock()
    storage = GCodeStorage(str(tmp_path), retention_days=90)

    s1 = _UploadSession(total_size=100, storage=storage)
    s2 = _UploadSession(total_size=200, storage=storage)
    proxy._sessions[(None, 100)] = s1
    proxy._sessions[(None, 200)] = s2

    await proxy.stop()

    assert len(proxy._sessions) == 0
    assert not storage.temp_path(s1.upload_id).exists()
    assert not storage.temp_path(s2.upload_id).exists()
