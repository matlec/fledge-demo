"""Unit tests for the InfluxDB north plugin, against a fake InfluxDB write API."""
import asyncio
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sys
import threading
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'test-token-value'

# The plugin imports Fledge's logger module, which exists only in the image.
fledge_logger = types.ModuleType('fledge.common.logger')
fledge_logger.setup = lambda name, level=logging.INFO: logging.getLogger(name)
for name in ('fledge', 'fledge.common'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['fledge.common.logger'] = fledge_logger
sys.modules['fledge.common'].logger = fledge_logger

spec = importlib.util.spec_from_file_location('influxdb', ROOT / 'image/fledge/influxdb/influxdb.py')
influxdb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(influxdb)
influxdb._LOGGER.propagate = False  # assertLogs still captures; keeps test output clean

STAMP = '2026-09-29 12:00:00.123456+00:00'
NANOS = 1790683200123456000
MEASURED = ['Speed', 'Flow', 'SuctionPressure', 'DischargePressure', 'MotorCurrent',
            'MotorPower', 'WindingTemperature', 'BearingVibration', 'BearingTemperature']


class FakeInflux:
    """Answer each write with the next status in `statuses`, or with `respond(lines)`."""

    def __init__(self):
        self.requests = []
        self.statuses = []
        self.respond = None
        self.location = None  # sent with 3xx statuses
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length'])).decode()
                fake.requests.append((self.path, dict(self.headers), body))
                lines = body.split('\n')
                status = fake.respond(lines) if fake.respond else (fake.statuses.pop(0) if fake.statuses else 204)
                payload = b'' if status == 204 else json.dumps({'message': f'status {status}'}).encode()
                self.send_response(status)
                if fake.location and 300 <= status < 400:
                    self.send_header('Location', fake.location)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def config(url, **overrides):
    values = {'url': url, 'org': 'fledge-demo', 'bucket': 'pumps', 'measurement': 'fledge',
              'token': TOKEN, 'timeout': '2', **overrides}
    return {name: {'value': value} for name, value in values.items()}


def reading(id_, asset='Pump1', stamp=STAMP, **datapoints):
    return {'id': id_, 'asset_code': asset, 'user_ts': stamp, 'ts': stamp, 'reading': datapoints}


def step(first_id, asset='Pump1', stamp=STAMP):
    """One simulator step as s2opcua delivers it: one reading per measured Variable."""
    return [reading(first_id + i, asset, stamp, **{name: float(i)}) for i, name in enumerate(MEASURED)]


def send(handle, payload):
    return asyncio.run(influxdb.plugin_send(handle, payload, '1'))


class LineProtocolTests(unittest.TestCase):
    def test_step_becomes_one_point(self):
        lines = influxdb.to_lines(step(1), 'fledge')
        self.assertEqual(len(lines), 1)
        asset, stamp, line = lines[0]
        self.assertEqual((asset, stamp), ('Pump1', STAMP))
        self.assertTrue(line.startswith('fledge,asset=Pump1 '))
        self.assertTrue(line.endswith(f' {NANOS}'))
        self.assertEqual(line.count('='), 1 + len(MEASURED))

    def test_assets_and_timestamps_stay_separate(self):
        payload = step(1) + step(10, 'Pump2') + step(20, stamp='2026-09-29 12:00:01.123456+00:00')
        self.assertEqual(len(influxdb.to_lines(payload, 'fledge')), 3)

    def test_later_duplicate_datapoint_wins(self):
        payload = [reading(1, Speed=1.0), reading(2, Speed=2.0)]
        self.assertIn('Speed=2.0 ', influxdb.to_lines(payload, 'fledge')[0][2])

    def test_numbers_are_floats(self):
        line = influxdb.to_lines([reading(1, Running=1, Flag=True, Small=1e-05)], 'm')[0][2]
        self.assertIn('Running=1.0', line)
        self.assertIn('Flag=1.0', line)
        self.assertIn('Small=1e-05', line)
        self.assertNotRegex(line, r'=\d+i')

    def test_strings_and_escaping(self):
        line = influxdb.to_lines([reading(1, 'Pump 1,a=b', **{'Scen ario': 'bearing "wear"\\x'})], 'my m,x')[0][2]
        self.assertTrue(line.startswith('my\\ m\\,x,asset=Pump\\ 1\\,a\\=b '))
        self.assertIn('Scen\\ ario="bearing \\"wear\\"\\\\x"', line)

    def test_unsupported_values_are_skipped(self):
        lines = influxdb.to_lines([reading(1, Array=[1, 2], Nested={'a': 1}, Nan=float('nan'), Ok=1.5)], 'm')
        self.assertEqual(lines[0][2].split(' ')[1], 'Ok=1.5')
        self.assertEqual(influxdb.to_lines([reading(1, Array=[1, 2])], 'm'), [])

    def test_timestamp_without_zone_is_utc(self):
        line = influxdb.to_lines([reading(1, stamp='2026-09-29 12:00:00.123456', A=1.0)], 'm')[0][2]
        self.assertTrue(line.endswith(f' {NANOS}'))


class SendTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInflux()
        self.addCleanup(self.fake.close)
        self.handle = influxdb.plugin_init(config(self.fake.url))

    def test_accepted_block(self):
        payload = step(1) + step(10, 'Pump2')
        self.assertEqual(send(self.handle, payload), (True, 18, 18))
        path, headers, body = self.fake.requests[0]
        self.assertEqual(path, '/api/v2/write?org=fledge-demo&bucket=pumps&precision=ns')
        self.assertEqual(headers['Authorization'], 'Token ' + TOKEN)
        self.assertEqual(len(body.split('\n')), 2)

    def test_temporary_failures_keep_the_block(self):
        for status in (401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.fake.statuses = [status]
                self.assertEqual(send(self.handle, step(1)), (False, 0, 0))

    def test_redirect_is_refused_and_keeps_the_block(self):
        other = FakeInflux()
        self.addCleanup(other.close)
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                self.fake.statuses = [status]
                self.fake.location = other.url + '/api/v2/write'
                self.assertEqual(send(self.handle, step(1)), (False, 0, 0))
        self.assertEqual(other.requests, [])

    def test_unreachable_keeps_the_block(self):
        self.fake.close()
        self.assertEqual(send(self.handle, step(1)), (False, 0, 0))

    def test_timeout_keeps_the_block(self):
        import socket
        silent = socket.socket()
        silent.bind(('127.0.0.1', 0))
        silent.listen()
        self.addCleanup(silent.close)
        handle = influxdb.plugin_init(config(f'http://127.0.0.1:{silent.getsockname()[1]}', timeout='1'))
        self.assertEqual(send(handle, step(1)), (False, 0, 0))

    def test_too_large_block_is_written_line_by_line(self):
        self.fake.respond = lambda lines: 413 if len(lines) > 1 else 204
        self.assertEqual(send(self.handle, step(1) + step(10, 'Pump2')), (True, 18, 18))
        self.assertEqual(len(self.fake.requests), 3)

    def test_rejected_lines_are_dropped_and_the_rest_written(self):
        # 400 rejects the whole batch; written one at a time, only the bad line fails.
        written = []

        def respond(lines):
            if len(lines) > 1:
                return 400
            if 'asset=Bad' in lines[0]:
                return 422
            written.append(lines[0])
            return 204

        self.fake.respond = respond
        payload = step(1) + [reading(20, 'Bad', Speed=1.0)] + step(30, 'Pump2')
        with self.assertLogs(influxdb._LOGGER, 'WARNING') as logs:
            self.assertEqual(send(self.handle, payload), (True, 38, 19))
        self.assertEqual(len(written), 2)
        self.assertTrue(any('Bad' in line and STAMP in line for line in logs.output))

    def test_failure_while_isolating_keeps_the_block(self):
        self.fake.statuses = [400, 204, 503]
        self.assertEqual(send(self.handle, step(1) + step(10, 'Pump2')), (False, 0, 0))

    def test_empty_and_unsupported_blocks_are_sent(self):
        self.assertEqual(send(self.handle, []), (True, 0, 0))
        self.assertEqual(send(self.handle, [reading(5, Array=[1])]), (True, 5, 1))
        self.assertEqual(self.fake.requests, [])

    def test_token_never_logged(self):
        self.fake.statuses = [401, 400, 422, 204]
        with self.assertLogs(influxdb._LOGGER, 'DEBUG') as logs:
            send(self.handle, step(1))
            send(self.handle, step(1) + [reading(20, 'Bad', Speed=1.0)])
            send(self.handle, step(1))
        self.assertTrue(logs.output)
        self.assertFalse(any(TOKEN in line for line in logs.output))

    def test_repeated_failures_logged_at_intervals(self):
        now = [0.0]
        self.handle['_failures'].clock = lambda: now[0]
        with self.assertLogs(influxdb._LOGGER, 'INFO') as logs:
            for moment in (0, 1, 2, 61):
                now[0] = moment
                self.fake.statuses = [503]
                send(self.handle, step(1))
            send(self.handle, step(1))
        self.assertEqual(sum('write failed' in line for line in logs.output), 2)
        self.assertIn('failed 4 time(s)', [l for l in logs.output if 'write failed' in l][-1])
        self.assertTrue(any('resumed after 4' in line for line in logs.output))

    def test_reconfigure_uses_new_settings_and_keeps_failure_state(self):
        self.fake.statuses = [503]
        send(self.handle, step(1))
        handle = influxdb.plugin_reconfigure(self.handle, config(self.fake.url, token='new-token'))
        self.assertIs(handle['_failures'], self.handle['_failures'])
        send(handle, step(1))
        self.assertEqual(self.fake.requests[-1][1]['Authorization'], 'Token new-token')

    def test_source_checksums_are_current(self):
        import hashlib
        directory = ROOT / 'image/fledge/influxdb'
        for line in (directory / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), digest,
                             f'update image/fledge/influxdb/SHA256SUMS after changing {name}')

    def test_plugin_info(self):
        info = influxdb.plugin_info()
        self.assertEqual((info['name'], info['version'], info['type']), ('influxdb', '1.0.0', 'north'))
        self.assertEqual(info['config']['token']['type'], 'password')


if __name__ == '__main__':
    unittest.main()
