'''Tests for printer type auto-detection via UDP probes.'''

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.detect import detect_printer_type


class TestDetectPrinterType:
  @pytest.mark.asyncio
  async def test_cc2_detected_on_port_52700(self):
    async def mock_probe(ip, port, message, probe_timeout):
      if port == 52700:
        return b'{"id":0,"method":7000,"result":"ok"}'
      return None

    with patch('src.detect._probe_udp', side_effect=mock_probe):
      result = await detect_printer_type('192.168.1.100')
    assert result == 'cc2'

  @pytest.mark.asyncio
  async def test_cc1_detected_on_port_3000(self):
    async def mock_probe(ip, port, message, probe_timeout):
      if port == 3000:
        return b'ok M99999'
      return None

    with patch('src.detect._probe_udp', side_effect=mock_probe):
      result = await detect_printer_type('192.168.1.101')
    assert result == 'cc1'

  @pytest.mark.asyncio
  async def test_cc2_tried_first(self):
    '''CC2 probe runs before CC1, so if both respond, CC2 wins.'''
    call_order = []

    async def mock_probe(ip, port, message, probe_timeout):
      call_order.append(port)
      return b'response'

    with patch('src.detect._probe_udp', side_effect=mock_probe):
      result = await detect_printer_type('192.168.1.50')
    assert result == 'cc2'
    assert call_order[0] == 52700

  @pytest.mark.asyncio
  async def test_raises_when_neither_responds(self):
    async def mock_probe(ip, port, message, probe_timeout):
      return None

    with (
      patch('src.detect._probe_udp', side_effect=mock_probe),
      pytest.raises(RuntimeError, match='Could not detect printer type'),
    ):
      await detect_printer_type('192.168.1.200')

  @pytest.mark.asyncio
  async def test_error_message_includes_ip(self):
    async def mock_probe(ip, port, message, probe_timeout):
      return None

    with (
      patch('src.detect._probe_udp', side_effect=mock_probe),
      pytest.raises(RuntimeError, match='192.168.1.200'),
    ):
      await detect_printer_type('192.168.1.200')

  @pytest.mark.asyncio
  async def test_custom_timeout_passed_through(self):
    captured_timeouts = []

    async def mock_probe(ip, port, message, probe_timeout):
      captured_timeouts.append(probe_timeout)
      return None

    with (
      patch('src.detect._probe_udp', side_effect=mock_probe),
      pytest.raises(RuntimeError),
    ):
      await detect_printer_type('192.168.1.50', probe_timeout=5.0)
    assert all(t == 5.0 for t in captured_timeouts)
