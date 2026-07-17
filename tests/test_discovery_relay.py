'''Tests for the CC1 UDP discovery relay and MainboardIP rewriting.'''

from __future__ import annotations

import asyncio
import json

import pytest

from src.discovery_relay import DiscoveryRelayProtocol, rewrite_mainboard_ip

PRINTER_IP = '192.168.1.100'
ADVERTISE_IP = '192.168.1.102'

DISCOVERY_REPLY = {
  'Id': 'abc123',
  'Data': {
    'Name': 'Centauri Carbon',
    'MachineName': 'Centauri Carbon',
    'BrandName': 'ELEGOO',
    'MainboardIP': PRINTER_IP,
    'MainboardID': '000000000000000000000000000000',
    'ProtocolVersion': 'V3.0.0',
    'FirmwareVersion': 'V1.4.46',
  },
}


class TestRewriteMainboardIp:
  def test_rewrites_mainboard_ip(self):
    payload = json.dumps(DISCOVERY_REPLY).encode()
    result = json.loads(rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP))
    assert result['Data']['MainboardIP'] == ADVERTISE_IP

  def test_leaves_other_fields_untouched(self):
    payload = json.dumps(DISCOVERY_REPLY).encode()
    result = json.loads(rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP))
    assert result['Data']['MainboardID'] == '000000000000000000000000000000'
    assert result['Data']['Name'] == 'Centauri Carbon'

  def test_leaves_embedded_urls_alone(self):
    # The video stream URL embeds the printer address; it must keep
    # pointing at the printer because the proxy does not relay video.
    message = {
      'Data': {
        'MainboardIP': PRINTER_IP,
        'VideoUrl': f'http://{PRINTER_IP}:3031/video',
      },
    }
    payload = json.dumps(message).encode()
    result = json.loads(rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP))
    assert result['Data']['MainboardIP'] == ADVERTISE_IP
    assert result['Data']['VideoUrl'] == f'http://{PRINTER_IP}:3031/video'

  def test_rewrites_nested_structures(self):
    message = {
      'printers': [
        {'MainboardIP': PRINTER_IP},
        {'MainboardIP': '192.168.1.99'},
      ],
    }
    payload = json.dumps(message).encode()
    result = json.loads(rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP))
    assert result['printers'][0]['MainboardIP'] == ADVERTISE_IP
    assert result['printers'][1]['MainboardIP'] == '192.168.1.99'

  def test_mismatched_value_untouched(self):
    message = {'Data': {'MainboardIP': '10.0.0.9'}}
    payload = json.dumps(message).encode()
    assert rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP) == payload

  def test_non_json_passthrough(self):
    payload = b'M99999'
    assert rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP) == payload

  def test_invalid_utf8_passthrough(self):
    payload = b'\xff\xfe\x00'
    assert rewrite_mainboard_ip(payload, PRINTER_IP, ADVERTISE_IP) == payload


class _FakePrinterProtocol(asyncio.DatagramProtocol):
  '''Answers any probe with a canned discovery reply.'''

  def __init__(self):
    self.received: list[bytes] = []
    self.transport: asyncio.DatagramTransport | None = None

  def connection_made(self, transport):
    self.transport = transport

  def datagram_received(self, data, addr):
    self.received.append(data)
    assert self.transport is not None
    self.transport.sendto(json.dumps(DISCOVERY_REPLY).encode(), addr)


class _ClientProtocol(asyncio.DatagramProtocol):
  '''Sends one probe and captures the reply.'''

  def __init__(self, future: asyncio.Future):
    self.future = future

  def datagram_received(self, data, addr):
    if not self.future.done():
      self.future.set_result(data)


class TestDiscoveryRelayProtocol:
  @pytest.mark.asyncio
  async def test_relays_and_rewrites(self):
    loop = asyncio.get_running_loop()

    fake_printer = _FakePrinterProtocol()
    printer_transport, _ = await loop.create_datagram_endpoint(
      lambda: fake_printer, local_addr=('127.0.0.1', 0)
    )
    printer_addr = printer_transport.get_extra_info('sockname')

    relay_transport, _ = await loop.create_datagram_endpoint(
      lambda: DiscoveryRelayProtocol(
        printer_addr=printer_addr,
        printer_ip=PRINTER_IP,
        advertise_ip=ADVERTISE_IP,
      ),
      local_addr=('127.0.0.1', 0),
    )
    relay_addr = relay_transport.get_extra_info('sockname')

    reply_future: asyncio.Future[bytes] = loop.create_future()
    client_transport, _ = await loop.create_datagram_endpoint(
      lambda: _ClientProtocol(reply_future),
      remote_addr=relay_addr,
    )

    try:
      client_transport.sendto(b'M99999')
      reply = await asyncio.wait_for(reply_future, timeout=3)
    finally:
      client_transport.close()
      relay_transport.close()
      printer_transport.close()

    assert fake_printer.received == [b'M99999']
    parsed = json.loads(reply)
    assert parsed['Data']['MainboardIP'] == ADVERTISE_IP
    assert parsed['Data']['FirmwareVersion'] == 'V1.4.46'
