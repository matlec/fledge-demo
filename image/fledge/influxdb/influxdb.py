# -*- coding: utf-8 -*-

"""Fledge north plugin that writes readings to InfluxDB 2 as line protocol.

A block counts as sent only after InfluxDB has accepted it. Temporary failures and
rejected credentials return 0, so the north service keeps the block and sends it
again. Readings with the same asset and user timestamp become one point.
"""

import copy
import datetime
import time
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request

from fledge.common import logger

__license__ = "Apache 2.0"
__version__ = "1.0.0"

_DEFAULT_CONFIG = {
    'plugin': {
        'description': 'Write readings to InfluxDB 2 as line protocol',
        'type': 'string',
        'default': 'influxdb',
        'readonly': 'true'
    },
    'url': {
        'description': 'InfluxDB base URL',
        'type': 'string',
        'default': 'http://influxdb:8086',
        'order': '1',
        'displayName': 'URL',
        'mandatory': 'true'
    },
    'org': {
        'description': 'InfluxDB organization',
        'type': 'string',
        'default': 'fledge',
        'order': '2',
        'displayName': 'Organization',
        'mandatory': 'true'
    },
    'bucket': {
        'description': 'InfluxDB bucket',
        'type': 'string',
        'default': 'fledge',
        'order': '3',
        'displayName': 'Bucket',
        'mandatory': 'true'
    },
    'measurement': {
        'description': 'Measurement name; the asset becomes the asset tag',
        'type': 'string',
        'default': 'fledge',
        'order': '4',
        'displayName': 'Measurement',
        'mandatory': 'true'
    },
    'token': {
        'description': 'InfluxDB API token with write access to the bucket',
        'type': 'password',
        'default': '',
        'order': '5',
        'displayName': 'Token'
    },
    'timeout': {
        'description': 'Request timeout in seconds',
        'type': 'integer',
        'default': '10',
        'minimum': '1',
        'maximum': '120',
        'order': '6',
        'displayName': 'Timeout'
    },
    'source': {
        'description': 'Readings are the only supported source',
        'type': 'enumeration',
        'options': ['readings'],
        'default': 'readings',
        'order': '7',
        'displayName': 'Source'
    }
}

_LOGGER = logger.setup(__name__, level=logging.INFO)

# Statuses that reject the data itself; resending the same lines cannot succeed.
# InfluxDB answers 400 when a line doesn't parse (nothing is written) and 422 for
# a partial write (the valid lines are written). Every other failure keeps the
# block for the north service to send again.
_REJECT = {400, 413, 422}
# The north service retries a failed block about once a second; log a repeated
# failure at most this often.
_FAILURE_LOG_INTERVAL = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would send the token to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def plugin_info():
    return {
        'name': 'influxdb',
        'version': __version__,
        'type': 'north',
        'interface': '1.0',
        'config': _DEFAULT_CONFIG
    }


def plugin_init(config):
    handle = copy.deepcopy(config)
    handle['_failures'] = _Failures()
    return handle


def plugin_reconfigure(handle, new_config):
    new_handle = copy.deepcopy(new_config)
    new_handle['_failures'] = handle['_failures']
    return new_handle


def plugin_shutdown(handle):
    pass


async def plugin_send(handle, payload, stream_id):
    """Send one block. Returns (sent, last id, count); the service uses the count."""
    last_id = payload[-1]['id'] if payload else 0
    lines = to_lines(payload, handle['measurement']['value'])
    if not lines:
        return True, last_id, len(payload)
    writer = _Writer(handle)
    failures = handle['_failures']
    status, message = writer.write(lines)
    if status == 204:
        failures.succeeded()
        return True, last_id, len(payload)
    if status in _REJECT:
        try:
            dropped = writer.write_each(lines)
        except _Temporary as error:
            failures.failed(*error.args)
            return False, 0, 0
        failures.succeeded()
        _LOGGER.warning('InfluxDB rejected %d of %d points (HTTP %s: %s)', len(dropped), len(lines), status, message)
        for asset, stamp, reason in dropped:
            _LOGGER.warning('Dropped point of asset %s at %s: %s', asset, stamp, reason)
        return True, last_id, len(payload)
    failures.failed(status, message)
    return False, 0, 0


class _Failures:
    """Log the first failed write and then at most once per interval."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.count = 0
        self.logged = None

    def failed(self, status, message):
        self.count += 1
        now = self.clock()
        if self.logged is None or now - self.logged >= _FAILURE_LOG_INTERVAL:
            self.logged = now
            _LOGGER.error('InfluxDB write failed %d time(s), the readings are kept for retry (%s: %s)',
                          self.count, 'no response' if status is None else f'HTTP {status}', message)

    def succeeded(self):
        if self.count:
            _LOGGER.warning('InfluxDB writes resumed after %d failed attempt(s)', self.count)
        self.count = 0
        self.logged = None


class _Writer:
    def __init__(self, handle):
        base = handle['url']['value'].rstrip('/')
        query = urllib.parse.urlencode({'org': handle['org']['value'], 'bucket': handle['bucket']['value'],
                                        'precision': 'ns'})
        self.url = f'{base}/api/v2/write?{query}'
        self.token = handle['token']['value']
        self.timeout = int(handle['timeout']['value'])

    def write(self, lines):
        """Return (status, message); status is None when no response arrived."""
        data = '\n'.join(line for _, _, line in lines).encode()
        request = urllib.request.Request(self.url, data=data, method='POST', headers={
            'Authorization': 'Token ' + self.token, 'Content-Type': 'text/plain; charset=utf-8'})
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return response.status, ''
        except urllib.error.HTTPError as error:
            return error.code, _message(error)
        except (OSError, ValueError) as error:  # URLError, timeouts and refused connections
            return None, type(error).__name__ + ': ' + str(getattr(error, 'reason', error))

    def write_each(self, lines):
        """Write lines one at a time. Return the rejected ones; raise to keep the block otherwise."""
        dropped = []
        for asset, stamp, line in lines:
            status, message = self.write([(asset, stamp, line)])
            if status in _REJECT:
                dropped.append((asset, stamp, message))
            elif status != 204:
                # Something other than the data failed. Returning 0 would resend
                # the lines that were already written, which is harmless.
                raise _Temporary(status, message)
        return dropped


class _Temporary(Exception):
    pass


def _message(error):
    try:
        body = json.loads(error.read().decode(errors='replace') or '{}')
        return str(body.get('message', error.reason))
    except (ValueError, OSError):
        return str(error.reason)


def to_lines(payload, measurement):
    """Merge readings by asset and user timestamp and return (asset, stamp, line) tuples."""
    points = {}
    for reading in payload:
        key = (reading['asset_code'], reading['user_ts'])
        points.setdefault(key, {}).update(reading['reading'])
    lines = []
    skipped = 0
    for (asset, stamp), datapoints in points.items():
        fields = []
        for name, value in datapoints.items():
            encoded = _field_value(value)
            if encoded is None:
                skipped += 1
            else:
                fields.append(_escape(name, ',= ') + '=' + encoded)
        if fields:
            line = (f'{_escape(measurement, ", ")},asset={_escape(asset, ",= ")} '
                    f'{",".join(fields)} {_nanoseconds(stamp)}')
            lines.append((asset, stamp, line))
    if skipped:
        _LOGGER.debug('Skipped %d datapoints of unsupported types', skipped)
    return lines


def _field_value(value):
    if isinstance(value, (bool, int, float)):
        number = float(value)
        return repr(number) if math.isfinite(number) else None
    if isinstance(value, str):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return None


def _escape(text, characters):
    text = text.replace('\\', '\\\\')
    for character in characters:
        text = text.replace(character, '\\' + character)
    return text


def _nanoseconds(stamp):
    """Convert Fledge's 'YYYY-MM-DD HH:MM:SS.ffffff+00:00' to Unix nanoseconds."""
    moment = datetime.datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    delta = moment - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
