'''Tests for environment-based configuration loading.'''

from __future__ import annotations

import os
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import pytest

from src.config import Config


def _load_config(**env_overrides):
  '''Create a Config with a controlled environment.'''
  with patch.dict(os.environ, env_overrides, clear=True):
    return Config()


class TestConfigDefaults:
  def test_default_ports(self):
    config = _load_config()
    assert config.http_port == 80
    assert config.mqtt_port == 1883
    assert config.camera_port == 8080
    assert config.mqtt_ws_port == 9001
    assert config.ws_port == 3030

  def test_default_printer_type(self):
    config = _load_config()
    assert config.printer_type == 'auto'

  def test_default_retention_and_timeout(self):
    config = _load_config()
    assert config.retention_days == 90
    assert config.upload_timeout == 300

  def test_default_max_body_size(self):
    config = _load_config()
    assert config.max_body_size == 256 * 1024 * 1024

  def test_printer_ip_none_when_unset(self):
    config = _load_config()
    assert config.printer_ip is None

  def test_default_gcode_dir(self):
    config = _load_config()
    assert config.gcode_dir == '/data/gcode'

  def test_default_log_level(self):
    config = _load_config()
    assert config.log_level == 'INFO'

  def test_default_gcode_timezone(self):
    config = _load_config()
    assert config.gcode_timezone.key == 'UTC'

  def test_default_store_gcode_false(self):
    config = _load_config()
    assert config.store_gcode is False


class TestConfigOverrides:
  def test_all_values_from_env(self):
    config = _load_config(
      PRINTER_IP='10.0.0.50',
      PRINTER_TYPE='cc2',
      HTTP_PORT='8080',
      MQTT_PORT='1884',
      CAMERA_PORT='9090',
      MQTT_WS_PORT='9002',
      WS_PORT='3031',
      GCODE_DIR='/tmp/gcode',
      RETENTION_DAYS='60',
      UPLOAD_TIMEOUT='120',
      MAX_BODY_SIZE='536870912',
      STORE_GCODE='true',
      LOG_LEVEL='DEBUG',
      GCODE_TZ='America/New_York',
    )
    assert config.printer_ip == '10.0.0.50'
    assert config.printer_type == 'cc2'
    assert config.http_port == 8080
    assert config.mqtt_port == 1884
    assert config.camera_port == 9090
    assert config.mqtt_ws_port == 9002
    assert config.ws_port == 3031
    assert config.gcode_dir == '/tmp/gcode'
    assert config.retention_days == 60
    assert config.upload_timeout == 120
    assert config.max_body_size == 512 * 1024 * 1024
    assert config.store_gcode is True
    assert config.log_level == 'DEBUG'
    assert config.gcode_timezone.key == 'America/New_York'

  def test_retention_zero_disables_cleanup(self):
    config = _load_config(RETENTION_DAYS='0')
    assert config.retention_days == 0

  def test_partial_override(self):
    config = _load_config(PRINTER_IP='192.168.1.1')
    assert config.printer_ip == '192.168.1.1'
    assert config.http_port == 80  # still default

  @pytest.mark.parametrize('value', ['true', 'True', 'TRUE', '1', 'yes', 'Yes'])
  def test_store_gcode_truthy_values(self, value):
    config = _load_config(STORE_GCODE=value)
    assert config.store_gcode is True

  @pytest.mark.parametrize('value', ['false', 'False', '0', 'no', ''])
  def test_store_gcode_falsy_values(self, value):
    config = _load_config(STORE_GCODE=value)
    assert config.store_gcode is False


class TestConfigValidation:
  def test_non_numeric_port_raises(self):
    with pytest.raises(ValueError, match='invalid literal'):
      _load_config(HTTP_PORT='not_a_number')

  @pytest.mark.parametrize(
    'port_env', ['HTTP_PORT', 'MQTT_PORT', 'CAMERA_PORT', 'MQTT_WS_PORT', 'WS_PORT']
  )
  @pytest.mark.parametrize('value', ['0', '65536', '-1'])
  def test_port_out_of_range_raises(self, port_env, value):
    with pytest.raises(ValueError, match='must be in range 1-65535'):
      _load_config(**{port_env: value})

  def test_port_in_valid_range_accepts(self):
    config = _load_config(
      HTTP_PORT='1',
      MQTT_PORT='65535',
      CAMERA_PORT='443',
      MQTT_WS_PORT='9001',
    )
    assert config.http_port == 1
    assert config.mqtt_port == 65535
    assert config.camera_port == 443
    assert config.mqtt_ws_port == 9001

  def test_retention_days_negative_raises(self):
    with pytest.raises(ValueError, match='RETENTION_DAYS must be non-negative'):
      _load_config(RETENTION_DAYS='-1')

  def test_upload_timeout_negative_raises(self):
    with pytest.raises(ValueError, match='UPLOAD_TIMEOUT must be non-negative'):
      _load_config(UPLOAD_TIMEOUT='-1')

  def test_upload_timeout_zero_accepted(self):
    config = _load_config(UPLOAD_TIMEOUT='0')
    assert config.upload_timeout == 0

  def test_malformed_printer_ip_raises(self):
    with pytest.raises(ValueError, match='does not appear to be an IPv4 or IPv6'):
      _load_config(PRINTER_IP='not-an-ip')

  def test_invalid_ipv4_octet_raises(self):
    with pytest.raises(ValueError, match='does not appear to be an IPv4 or IPv6'):
      _load_config(PRINTER_IP='192.168.1.999')

  def test_printer_ip_normalized(self):
    config = _load_config(PRINTER_IP='FE80::1')
    assert config.printer_ip == 'fe80::1'

  def test_invalid_timezone_raises(self):
    with pytest.raises(ZoneInfoNotFoundError):
      _load_config(GCODE_TZ='Invalid/Zone')

  @pytest.mark.parametrize('value', ['cc1', 'cc2', 'auto'])
  def test_printer_type_valid_values(self, value):
    config = _load_config(PRINTER_TYPE=value)
    assert config.printer_type == value

  def test_printer_type_case_insensitive(self):
    config = _load_config(PRINTER_TYPE='CC2')
    assert config.printer_type == 'cc2'

  def test_printer_type_invalid_raises(self):
    with pytest.raises(ValueError, match='PRINTER_TYPE must be'):
      _load_config(PRINTER_TYPE='cc3')

  def test_frozen_cannot_mutate(self):
    config = _load_config(PRINTER_IP='10.0.0.1')
    with pytest.raises(AttributeError):
      config.printer_ip = 'changed'
