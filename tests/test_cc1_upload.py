'''Tests for the shared CC1 chunked-upload capture.

The same capture runs behind both proxy listeners (port 80 and port 3030):
parse multipart chunks, forward via the listener's callable, and archive
only what the printer acknowledged.
'''

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from src.cc1_upload import CC1UploadCapture
from src.storage import GCodeStorage
from tests.conftest import build_cc1_multipart, make_gcode

OK_BODY = json.dumps({'code': '000000'}).encode()


def _capture(tmp_path) -> CC1UploadCapture:
  storage = GCodeStorage(str(tmp_path), retention_days=90)
  return CC1UploadCapture(storage, upload_timeout=300)


def _request(content_type: str = 'multipart/form-data') -> MagicMock:
  request = MagicMock()
  request.method = 'POST'
  request.path = '/uploadFile/upload'
  request.headers = {'Content-Type': content_type}
  # Mirror aiohttp semantics: the content_type property strips header
  # parameters, so the boundary is only available via request.headers.
  request.content_type = content_type.split(';')[0]
  return request


class _ForwardStub:
  '''Records forwarded chunks and returns a canned printer response.'''

  def __init__(self, status: int = 200, body: bytes = OK_BODY) -> None:
    self.status = status
    self.body = body
    self.calls: list[bytes] = []

  async def __call__(self, request, raw_body: bytes) -> web.Response:
    self.calls.append(raw_body)
    return web.Response(status=self.status, body=self.body)


# ------------------------------------------------------------------
# Route matching
# ------------------------------------------------------------------


class TestMatches:
  def test_matches_upload_post(self):
    assert CC1UploadCapture.matches(_request())

  def test_rejects_other_method(self):
    request = _request()
    request.method = 'GET'
    assert not CC1UploadCapture.matches(request)

  def test_rejects_other_path(self):
    request = _request()
    request.path = '/other'
    assert not CC1UploadCapture.matches(request)


# ------------------------------------------------------------------
# Capture behaviour
# ------------------------------------------------------------------


class TestCaptureHandle:
  @pytest.mark.asyncio
  async def test_single_chunk_upload_archived(self, tmp_path):
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='single')
    body, content_type = build_cc1_multipart(
      uuid='uuid-1',
      offset=0,
      total_size=len(data),
      file_data=data,
      filename='single.gcode',
    )

    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 200
    assert forward.calls == [body]
    assert not capture.sessions
    assert list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_chunks_assemble_across_requests(self, tmp_path):
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='chunked')
    half = len(data) // 2

    body, content_type = build_cc1_multipart(
      uuid='uuid-2', offset=0, total_size=len(data), file_data=data[:half]
    )
    await capture.handle(_request(content_type), body, forward)
    assert 'uuid-2' in capture.sessions
    assert not list(tmp_path.rglob('*.json'))

    body, content_type = build_cc1_multipart(
      uuid='uuid-2', offset=half, total_size=len(data), file_data=data[half:]
    )
    await capture.handle(_request(content_type), body, forward)

    assert not capture.sessions
    assert list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_printer_rejection_not_saved(self, tmp_path):
    capture = _capture(tmp_path)
    forward = _ForwardStub(status=500, body=b'boom')
    data = make_gcode(input_filename_base='rejected')
    body, content_type = build_cc1_multipart(
      uuid='uuid-3', offset=0, total_size=len(data), file_data=data
    )

    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 500
    assert not capture.sessions
    assert not list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_printer_error_code_not_saved(self, tmp_path):
    capture = _capture(tmp_path)
    forward = _ForwardStub(body=json.dumps({'code': '999999'}).encode())
    data = make_gcode(input_filename_base='errcode')
    body, content_type = build_cc1_multipart(
      uuid='uuid-4', offset=0, total_size=len(data), file_data=data
    )

    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 200
    assert not capture.sessions
    assert not list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_non_json_printer_response_still_saved(self, tmp_path):
    '''A 200 with an unparseable body counts as acceptance.'''
    capture = _capture(tmp_path)
    forward = _ForwardStub(body=b'OK')
    data = make_gcode(input_filename_base='nonjson')
    body, content_type = build_cc1_multipart(
      uuid='uuid-5', offset=0, total_size=len(data), file_data=data
    )

    await capture.handle(_request(content_type), body, forward)

    assert list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_missing_total_size_forwarded_raw(self, tmp_path):
    '''TotalSize=0 (or missing) must not create a capture session.'''
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    body, content_type = build_cc1_multipart(
      uuid='uuid-zero', offset=0, total_size=0, file_data=b'junk'
    )

    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 200
    assert forward.calls == [body]
    assert not capture.sessions
    assert not list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_empty_uuid_forwarded_raw(self, tmp_path):
    '''Empty Uuid must not create a capture session.'''
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='no_uuid')
    body, content_type = build_cc1_multipart(
      uuid='', offset=0, total_size=len(data), file_data=data
    )

    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 200
    assert forward.calls == [body]
    assert not capture.sessions
    assert not list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_non_multipart_forwarded_raw(self, tmp_path):
    capture = _capture(tmp_path)
    forward = _ForwardStub()

    response = await capture.handle(
      _request('application/octet-stream'), b'raw bytes', forward
    )

    assert response.status == 200
    assert forward.calls == [b'raw bytes']
    assert not capture.sessions

  @pytest.mark.asyncio
  async def test_real_aiohttp_request_parses_boundary(self, tmp_path):
    '''
    Regression: aiohttp's request.content_type strips the multipart
    boundary parameter; the capture must parse using the raw
    Content-Type header or every real upload silently degrades to
    an uncaptured passthrough.
    '''
    from aiohttp.test_utils import make_mocked_request

    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='real_request')
    body, content_type = build_cc1_multipart(
      uuid='uuid-real', offset=0, total_size=len(data), file_data=data
    )
    request = make_mocked_request(
      'POST', '/uploadFile/upload', headers={'Content-Type': content_type}
    )

    await capture.handle(request, body, forward)

    assert not capture.sessions
    assert list(tmp_path.rglob('*.json'))

  @pytest.mark.asyncio
  async def test_offset_zero_restarts_session(self, tmp_path):
    '''A retry from offset 0 replaces the stale partial session.'''
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='restart')

    body, content_type = build_cc1_multipart(
      uuid='uuid-6', offset=0, total_size=len(data) * 2, file_data=data
    )
    await capture.handle(_request(content_type), body, forward)
    first_session = capture.sessions['uuid-6']

    await capture.handle(_request(content_type), body, forward)

    assert capture.sessions['uuid-6'] is not first_session

  @pytest.mark.asyncio
  async def test_mid_upload_chunk_without_session_skips_capture(self, tmp_path):
    '''A chunk at offset > 0 with no session (e.g. proxy restart) must not
    create a corrupt archive with a zero-filled head.'''
    capture = _capture(tmp_path)
    forward = _ForwardStub()
    data = make_gcode(input_filename_base='orphan')

    body, content_type = build_cc1_multipart(
      uuid='uuid-orphan', offset=512, total_size=len(data), file_data=data
    )
    response = await capture.handle(_request(content_type), body, forward)

    assert response.status == 200
    assert forward.calls == [body]
    assert not capture.sessions
    assert not list(tmp_path.rglob('*.json'))


# ------------------------------------------------------------------
# Stale session cleanup
# ------------------------------------------------------------------


class TestCleanup:
  @pytest.mark.asyncio
  async def test_stale_sessions_discarded(self, tmp_path):
    import time

    from src.cc1_upload import CC1UploadSession

    storage = GCodeStorage(str(tmp_path), retention_days=90)
    capture = CC1UploadCapture(storage, upload_timeout=300)

    stale = CC1UploadSession('stale-uuid', 1000, 'md5', storage)
    stale.created = time.monotonic() - 600
    capture.sessions['stale-uuid'] = stale
    fresh = CC1UploadSession('fresh-uuid', 2000, 'md5', storage)
    capture.sessions['fresh-uuid'] = fresh

    removed = await capture.cleanup_once()

    assert removed == 1
    assert 'stale-uuid' not in capture.sessions
    assert 'fresh-uuid' in capture.sessions
