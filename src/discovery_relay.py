'''UDP discovery relay for CC1 (SDCP) printers.

ElegooSlicer discovers printers with an M99999 probe on UDP:3000 and uses
the MainboardIP field of the reply for follow-up operations — including
file uploads. A proxy that relays replies verbatim therefore gets bypassed
for exactly the traffic it exists to capture: the slicer connects its
WebSocket to the proxy but uploads straight to the printer.

This relay forwards probes to the real printer and rewrites MainboardIP in
the reply to the proxy's advertised address, so the slicer keeps every
operation on the proxy.

The video stream URL is intentionally NOT rewritten, the printer serves
video directly to clients. The proxy does not relay the video port (see README).
'''

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from .detect import _probe_udp

if TYPE_CHECKING:
  from .config import Config

logger = logging.getLogger(__name__)

# The printer always listens for discovery on this port (protocol-fixed);
# the proxy's own listen port is config.discovery_port.
PRINTER_DISCOVERY_PORT = 3000

_PRINTER_REPLY_TIMEOUT = 2.0


def rewrite_mainboard_ip(payload: bytes, printer_ip: str, advertise_ip: str) -> bytes:
  '''
  Replace MainboardIP values equal to printer_ip with advertise_ip.

  Returns the payload unchanged when it is not valid JSON or contains no
  matching field. Only exact MainboardIP fields are touched — strings that
  merely embed the printer address (e.g. the video stream URL) are left
  alone so direct-to-printer streams keep working.
  '''
  try:
    data = json.loads(payload.decode('utf-8'))
  except (UnicodeDecodeError, json.JSONDecodeError):
    return payload
  if not _rewrite_in_place(data, printer_ip, advertise_ip):
    return payload
  return json.dumps(data).encode('utf-8')


def _rewrite_in_place(node: Any, printer_ip: str, advertise_ip: str) -> bool:
  changed = False
  if isinstance(node, dict):
    for key, value in node.items():
      if key == 'MainboardIP' and value == printer_ip:
        node[key] = advertise_ip
        changed = True
      else:
        changed = _rewrite_in_place(value, printer_ip, advertise_ip) or changed
  elif isinstance(node, list):
    for item in node:
      changed = _rewrite_in_place(item, printer_ip, advertise_ip) or changed
  return changed


class DiscoveryRelayProtocol(asyncio.DatagramProtocol):
  '''Accepts discovery probes and answers with rewritten printer replies.'''

  def __init__(
    self,
    printer_addr: tuple[str, int],
    printer_ip: str,
    advertise_ip: str,
  ) -> None:
    self._printer_addr = printer_addr
    self._printer_ip = printer_ip
    self._advertise_ip = advertise_ip
    self._transport: asyncio.DatagramTransport | None = None
    self._tasks: set[asyncio.Task] = set()
    # Clients probe repeatedly (the slicer re-scans while its device page
    # is open); log each client's first redirect at INFO, the rest at
    # DEBUG so discovery chatter can't drown upload-capture log lines.
    self._seen_clients: set[str] = set()

  def connection_made(self, transport: asyncio.BaseTransport) -> None:
    self._transport = transport  # type: ignore[assignment]

  def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
    task = asyncio.create_task(self._relay(data, addr))
    self._tasks.add(task)
    task.add_done_callback(self._tasks.discard)

  async def _relay(self, data: bytes, addr: tuple[str, int]) -> None:
    reply = await _probe_udp(
      self._printer_addr[0],
      self._printer_addr[1],
      data,
      _PRINTER_REPLY_TIMEOUT,
    )
    if reply is None:
      logger.debug('Discovery: no printer reply for probe from %s', addr)
      return

    rewritten = rewrite_mainboard_ip(reply, self._printer_ip, self._advertise_ip)
    if self._transport is None:
      return
    self._transport.sendto(rewritten, addr)
    client_ip = addr[0]
    if client_ip not in self._seen_clients:
      self._seen_clients.add(client_ip)
      logger.info(
        'Discovery: answering %s (MainboardIP -> %s)', client_ip, self._advertise_ip
      )
    else:
      logger.debug(
        'Discovery: answered %s (MainboardIP -> %s)', client_ip, self._advertise_ip
      )


async def start_discovery_relay(config: Config) -> asyncio.DatagramTransport:
  '''Start the UDP discovery relay. Returns its transport for shutdown.'''
  loop = asyncio.get_running_loop()
  transport, _protocol = await loop.create_datagram_endpoint(
    lambda: DiscoveryRelayProtocol(
      printer_addr=(config.printer_ip or '', PRINTER_DISCOVERY_PORT),
      printer_ip=config.printer_ip or '',
      advertise_ip=config.advertise_ip or '',
    ),
    local_addr=('0.0.0.0', config.discovery_port),
  )
  logger.info(
    'Discovery relay listening on UDP :%d -> %s (advertising %s)',
    config.discovery_port,
    config.printer_ip,
    config.advertise_ip,
  )
  return transport
