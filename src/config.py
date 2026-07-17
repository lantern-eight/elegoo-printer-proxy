'''Configuration loaded from environment variables.'''

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _parse_timezone() -> ZoneInfo:
  name = (os.getenv('GCODE_TZ') or 'UTC').strip() or 'UTC'
  return ZoneInfo(name)


def _parse_optional_ip(env_var: str) -> str | None:
  '''Parse an IPv4 address from *env_var*, or None when unset/blank.

  IPv4 only: consumers interpolate this value into URLs, Host headers,
  and SDCP MainboardIP payloads without IPv6 bracket handling.
  '''
  raw = os.getenv(env_var)
  if not raw or not raw.strip():
    return None
  try:
    return str(ipaddress.IPv4Address(raw.strip()))
  except ipaddress.AddressValueError:
    raise ValueError(
      f'{env_var} must be an IPv4 address, got {raw.strip()!r}'
    ) from None


def _parse_printer_type() -> str:
  raw = (os.getenv('PRINTER_TYPE') or 'auto').strip().lower()
  if raw not in ('cc1', 'cc2', 'auto'):
    raise ValueError(f"PRINTER_TYPE must be 'cc1', 'cc2', or 'auto', got '{raw}'")
  return raw


def _parse_port(env_var: str, default: int) -> int:
  value = int(os.getenv(env_var, str(default)))
  if not 1 <= value <= 65535:
    raise ValueError(f'{env_var} must be in range 1-65535, got {value}')
  return value


def _parse_non_negative_int(env_var: str, default: str) -> int:
  value = int(os.getenv(env_var, default))
  if value < 0:
    raise ValueError(f'{env_var} must be non-negative, got {value}')
  return value


@dataclass(frozen=True)
class Config:
  printer_ip: str | None = field(
    default_factory=lambda: _parse_optional_ip('PRINTER_IP')
  )
  printer_type: str = field(default_factory=_parse_printer_type)

  http_port: int = field(
    default_factory=lambda: _parse_port('HTTP_PORT', 80),
  )
  mqtt_port: int = field(
    default_factory=lambda: _parse_port('MQTT_PORT', 1883),
  )
  camera_port: int = field(
    default_factory=lambda: _parse_port('CAMERA_PORT', 8080),
  )
  mqtt_ws_port: int = field(
    default_factory=lambda: _parse_port('MQTT_WS_PORT', 9001),
  )
  ws_port: int = field(
    default_factory=lambda: _parse_port('WS_PORT', 3030),
  )
  discovery_port: int = field(
    default_factory=lambda: _parse_port('DISCOVERY_PORT', 3000),
  )

  # LAN address the proxy is reachable on (the per-printer host IP).
  # Required for CC1 slicer redirection: discovery replies and WS frames
  # advertise this address as MainboardIP so uploads route through the
  # proxy instead of going straight to the printer.
  advertise_ip: str | None = field(
    default_factory=lambda: _parse_optional_ip('ADVERTISE_IP')
  )

  gcode_dir: str = field(default_factory=lambda: os.getenv('GCODE_DIR', '/data/gcode'))
  retention_days: int = field(
    default_factory=lambda: _parse_non_negative_int('RETENTION_DAYS', '90'),
  )
  gcode_timezone: ZoneInfo = field(default_factory=_parse_timezone)

  # Seconds before an incomplete chunked upload is discarded
  upload_timeout: int = field(
    default_factory=lambda: _parse_non_negative_int('UPLOAD_TIMEOUT', '300'),
  )

  # Maximum request body size in bytes (0 = unlimited).
  # Must exceed the largest single-shot G-code upload the slicer may send.
  # Default 256 MB is sufficient for typical G-code; raise via env for huge prints.
  max_body_size: int = field(
    default_factory=lambda: int(os.getenv('MAX_BODY_SIZE', str(256 * 1024 * 1024))),
  )

  store_gcode: bool = field(
    default_factory=lambda: (
      os.getenv('STORE_GCODE', 'false').lower() in ('true', '1', 'yes')
    ),
  )

  log_level: str = field(default_factory=lambda: os.getenv('LOG_LEVEL', 'INFO'))
