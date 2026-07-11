'''Auto-detect printer type by sending UDP discovery probes.'''

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_CC2_DISCOVERY_PORT = 52700
_CC1_DISCOVERY_PORT = 3000
_CC2_DISCOVERY_MSG = json.dumps({'id': 0, 'method': 7000}).encode()
_CC1_DISCOVERY_MSG = b'M99999'


class _UDPProbe(asyncio.DatagramProtocol):
  '''One-shot UDP probe that resolves a future on first response.'''

  def __init__(self, future: asyncio.Future) -> None:
    self._future = future

  def datagram_received(self, data: bytes, addr: tuple) -> None:
    if not self._future.done():
      self._future.set_result(data)

  def error_received(self, exception: Exception) -> None:
    if not self._future.done():
      self._future.set_exception(exception)


async def _probe_udp(
  ip: str,
  port: int,
  message: bytes,
  probe_timeout: float,
) -> bytes | None:
  '''Send a UDP datagram and wait for a response. Returns None on timeout.'''
  loop = asyncio.get_running_loop()
  future = loop.create_future()

  transport, _ = await loop.create_datagram_endpoint(
    lambda: _UDPProbe(future),
    remote_addr=(ip, port),
  )
  try:
    transport.sendto(message)
    return await asyncio.wait_for(future, timeout=probe_timeout)
  except (TimeoutError, OSError):
    return None
  finally:
    transport.close()


async def detect_printer_type(ip: str, probe_timeout: float = 3.0) -> str:
  '''Probe a printer to determine its type. Returns 'cc1' or 'cc2'.'''
  response = await _probe_udp(
    ip, _CC2_DISCOVERY_PORT, _CC2_DISCOVERY_MSG, probe_timeout
  )
  if response is not None:
    logger.info('CC2 discovery response from %s on port %d', ip, _CC2_DISCOVERY_PORT)
    return 'cc2'

  response = await _probe_udp(
    ip, _CC1_DISCOVERY_PORT, _CC1_DISCOVERY_MSG, probe_timeout
  )
  if response is not None:
    logger.info('CC1 discovery response from %s on port %d', ip, _CC1_DISCOVERY_PORT)
    return 'cc1'

  raise RuntimeError(
    f'Could not detect printer type at {ip}. '
    f'No response on UDP:{_CC2_DISCOVERY_PORT} (CC2) or UDP:{_CC1_DISCOVERY_PORT} (CC1). '
    f'Set PRINTER_TYPE=cc1 or PRINTER_TYPE=cc2 to skip auto-detection.'
  )
