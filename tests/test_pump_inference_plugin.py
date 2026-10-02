"""Unit tests for the pump inference north plugin, against fake inference and ingest servers."""
import asyncio
import datetime
import hashlib
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
PLUGIN = ROOT / 'image/fledge/pump-inference'

# The plugin imports Fledge's logger module, which exists only in the image.
fledge_logger = types.ModuleType('fledge.common.logger')
fledge_logger.setup = lambda name, level=logging.INFO: logging.getLogger(name)
for name in ('fledge', 'fledge.common'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['fledge.common.logger'] = fledge_logger
sys.modules['fledge.common'].logger = fledge_logger

spec = importlib.util.spec_from_file_location('pump_inference', PLUGIN / 'pump_inference.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
plugin._LOGGER.propagate = False
plugin._LOGGER.addHandler(logging.NullHandler())  # assertLogs still captures

START = datetime.datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=datetime.timezone.utc)


def stamp(second):
    return (START + datetime.timedelta(seconds=second)).isoformat(sep=' ')


class Fake:
    """A server that records JSON requests and answers with `respond(body)` -> (status, json)."""

    def __init__(self, respond):
        self.requests = []
        self.respond = respond
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.requests.append((self.path, body))
                status, answer = fake.respond(body)
                payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass  # the client timed out

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def engine_answer(body, model='pump-anomaly', version='1', score=lambda window: sum(window[-1]) / 100):
    """Score each window by its last step, so a test can tell windows apart."""
    request = body['inputs'][0]
    n, w, f = request['shape']
    data = request['data']
    windows = [[data[(i * w + j) * f:(i * w + j + 1) * f] for j in range(w)] for i in range(n)]
    return 200, {'model_name': model, 'model_version': version,
                 'outputs': [{'name': 'score', 'datatype': 'FP32', 'shape': [n], 'data': [score(x) for x in windows]}]}


def reading(id_, asset, second, **datapoints):
    return {'id': id_, 'asset_code': asset, 'user_ts': stamp(second), 'ts': stamp(second), 'reading': datapoints}


def step(first_id, second, asset='Pump1', running=1, speed=1450.0, skip=()):
    """One simulator step as s2opcua delivers it: one reading per Variable."""
    values = dict(zip(plugin.MEASURED, [speed] + [float(i) for i in range(1, 9)]))
    values.update(Running=running, Scenario='normal', Severity=0.0, SpeedSetpoint=speed)
    return [reading(first_id + i, asset, second, **{name: value})
            for i, (name, value) in enumerate(values.items()) if name not in skip]


def steps(seconds, asset='Pump1', first_id=1, **kwargs):
    payload = []
    for second in seconds:
        payload += step(first_id + len(payload), second, asset, **kwargs)
    return payload


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.engine = Fake(engine_answer)
        self.ingest = Fake(lambda body: (200, {'result': 'success'}))
        self.handle = plugin.plugin_init({name: {'value': value} for name, value in {
            'assets': 'Pump1,Pump2,Pump3', 'engineUrl': self.engine.url, 'model': 'pump-anomaly', 'version': '1',
            'window': '10', 'maxBatch': '64', 'ingestUrl': self.ingest.url + '/scores', 'scoreSuffix': 'Score',
            'timeout': '2'}.items()})

    def tearDown(self):
        self.engine.close()
        self.ingest.close()

    def send(self, payload):
        return asyncio.run(plugin.plugin_send(self.handle, payload, '1'))

    def scores(self):
        return [item for _, body in self.ingest.requests for item in body]

    def test_info(self):
        info = plugin.plugin_info()
        self.assertEqual((info['name'], info['type'], info['version']), ('pump_inference', 'north', '1.0.2'))

    def test_first_score_after_a_full_window(self):
        payload = steps(range(12))
        self.assertEqual(self.send(payload), (True, payload[-1]['id'], len(payload)))
        scores = self.scores()
        self.assertEqual([s['timestamp'] for s in scores], [stamp(9), stamp(10), stamp(11)])
        first = scores[0]
        self.assertEqual(first['asset'], 'Pump1Score')
        self.assertEqual(first['readings']['Model'], 'pump-anomaly/1')
        self.assertAlmostEqual(first['readings']['AnomalyScore'], (1450 + sum(range(1, 9))) / 100)
        self.assertEqual(len(self.engine.requests), 1)
        request = self.engine.requests[0]
        self.assertEqual(request[0], '/v2/models/pump-anomaly/versions/1/infer')
        self.assertEqual(request[1]['inputs'][0]['shape'], [3, 10, 9])

    def test_only_listed_assets_are_input(self):
        payload = steps(range(10)) + steps(range(10), asset='Pump1Score', first_id=500) \
            + steps(range(10), asset='gitops-sinusoid', first_id=900)
        self.send(payload)
        self.assertEqual({s['asset'] for s in self.scores()}, {'Pump1Score'})
        self.assertEqual(len(self.scores()), 1)

    def test_pumps_have_separate_windows(self):
        self.send(steps(range(10)) + steps(range(10), asset='Pump2', first_id=500))
        self.assertEqual(sorted(s['asset'] for s in self.scores()), ['Pump1Score', 'Pump2Score'])

    def test_step_split_across_blocks(self):
        payload = steps(range(10))
        self.send(payload[:-5])
        self.assertEqual(self.scores(), [])
        self.send(payload[-5:])
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9)])

    def test_retried_block_does_not_append_steps_twice(self):
        self.ingest.respond = lambda body: (503, b'{}')
        self.send(steps(range(10)))
        self.assertEqual(self.send(steps(range(10))), (False, 0, 0))  # still failing
        self.ingest.respond = lambda body: (200, {'result': 'success'})
        self.assertTrue(self.send(steps(range(10)))[0])
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9)] * 3)
        # The next step completes exactly one new window; the retries left no duplicate steps.
        self.ingest.requests.clear()
        self.send(steps([10], first_id=1000))
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(10)])

    def test_already_used_steps_are_ignored(self):
        self.send(steps(range(10)))
        self.ingest.requests.clear()
        self.send(steps(range(10)))  # the same readings again, for example after a resend
        self.assertEqual(self.scores(), [])

    def test_stopped_pump_gets_no_score_and_restarts_its_window(self):
        payload = steps(range(10)) + steps([10], first_id=500, running=0, speed=0.0) + steps(range(11, 21), first_id=600)
        self.send(payload)
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9), stamp(20)])

    def test_gap_restarts_the_window(self):
        self.send(steps(list(range(5)) + list(range(7, 17))))
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(16)])

    def test_incomplete_step_is_dropped_when_a_newer_step_completes(self):
        payload = steps(range(5)) + step(500, 5, skip=('Flow',)) + steps(range(6, 16), first_id=600)
        self.send(payload)
        # Step 5 is missing, so the window restarts after it: steps 6 to 15.
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(15)])
        self.assertEqual(self.handle['_store']['state']['Pump1']['pending'], {})

    def test_wrong_model_version_keeps_the_block(self):
        self.engine.respond = lambda body: engine_answer(body, version='2')
        with self.assertLogs(plugin._LOGGER, 'ERROR') as logs:
            self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
        self.assertIn("version '2'", logs.output[0])
        self.assertEqual(self.ingest.requests, [])

    def test_unexpected_shape_keeps_the_block(self):
        def answer(body):
            status, result = engine_answer(body)
            result['outputs'][0]['shape'] = [1, 1]
            return status, result
        self.engine.respond = answer
        self.assertEqual(self.send(steps(range(10))), (False, 0, 0))

    def test_score_that_is_not_finite_keeps_the_block(self):
        self.engine.respond = lambda body: (200, json.dumps(engine_answer(body, score=lambda w: float('nan'))[1]).encode())
        with self.assertLogs(plugin._LOGGER, 'ERROR') as logs:
            self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
        self.assertIn('not a finite number', logs.output[0])

    def test_engine_unavailable_keeps_the_block_and_recovers(self):
        self.handle['engineUrl']['value'] = 'http://127.0.0.1:9'
        with self.assertLogs(plugin._LOGGER, 'ERROR') as logs:
            self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
        self.assertIn('inference unavailable', logs.output[0])
        self.handle['engineUrl']['value'] = self.engine.url
        with self.assertLogs(plugin._LOGGER, 'WARNING') as logs:
            self.assertTrue(self.send(steps(range(10)))[0])
        self.assertIn('resumed after 1 failed', logs.output[0])
        self.assertEqual(len(self.scores()), 1)

    def test_engine_timeout(self):
        self.handle['timeout']['value'] = '1'
        release = threading.Event()

        def slow(body):
            release.wait(3)
            return engine_answer(body)
        self.engine.respond = slow
        try:
            self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
        finally:
            release.set()

    def test_ingest_rejection_keeps_the_block(self):
        self.ingest.respond = lambda body: (400, b'{}')
        with self.assertLogs(plugin._LOGGER, 'ERROR') as logs:
            self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
        self.assertIn('score ingest rejected the request (HTTP 400)', logs.output[0])

    def test_large_block_is_sent_in_batches(self):
        self.handle['maxBatch']['value'] = '4'
        self.send(steps(range(19)))
        self.assertEqual([body['inputs'][0]['shape'][0] for _, body in self.engine.requests], [4, 4, 2])
        self.assertEqual(len(self.ingest.requests), 1)
        self.assertEqual(len(self.scores()), 10)

    def test_repeated_failures_are_logged_once_per_interval(self):
        self.ingest.respond = lambda body: (503, b'{}')
        with self.assertLogs(plugin._LOGGER, 'ERROR') as logs:
            for _ in range(5):
                self.send(steps(range(10)))
        self.assertEqual(len(logs.output), 1)

    def reconfigured(self, **changes):
        config = {k: dict(v) for k, v in self.handle.items() if not k.startswith('_')}
        config.update({k: {'value': v} for k, v in changes.items()})
        return plugin.plugin_reconfigure(self.handle, config)

    def test_reconfigure_with_another_model_keeps_the_windows(self):
        self.send(steps(range(5)))
        self.assertEqual(len(self.reconfigured()['_store']['state']['Pump1']['window']), 5)
        self.assertEqual(len(self.reconfigured(model='other', version='2')['_store']['state']['Pump1']['window']), 5)

    def test_model_change_scores_the_next_full_window_without_warm_up(self):
        self.send(steps(range(9)))
        self.handle = self.reconfigured(version='2')
        self.engine.respond = lambda body: engine_answer(body, version='2')
        self.send(steps([9], first_id=1000))
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9)])

    def test_reconfigure_with_another_window_or_assets_drops_the_windows(self):
        self.send(steps(range(5)))
        self.assertEqual(self.reconfigured(window='20')['_store']['state'], {})
        self.assertEqual(self.reconfigured(assets='Pump1,Pump2')['_store']['state'], {})

    def test_block_in_flight_during_reconfigure_commits_where_the_next_block_reads(self):
        old = self.handle
        self.handle = self.reconfigured(version='2')
        self.engine.respond = lambda body: engine_answer(body, version='2')
        self.assertTrue(asyncio.run(plugin.plugin_send(old, steps(range(9)), '1'))[0])  # the old handle's send finishes
        self.send(steps([9], first_id=1000))
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9)])

    def test_engine_answer_that_is_not_an_object_keeps_the_block(self):
        for answer in ([], {'model_name': 'pump-anomaly', 'model_version': '1', 'outputs': {'score': 1}},
                       {'model_name': 'pump-anomaly', 'model_version': '1', 'outputs': ['score']}):
            self.engine.respond = lambda body, answer=answer: (200, answer)
            with self.assertLogs(plugin._LOGGER, 'ERROR'):
                self.assertEqual(self.send(steps(range(10))), (False, 0, 0))
            self.handle['_failures'].logged = None

    def test_unreadable_timestamp_is_ignored(self):
        payload = steps(range(10))
        payload.insert(3, reading(999, 'Pump1', 0) | {'user_ts': 'not a time'})
        with self.assertLogs(plugin._LOGGER, 'WARNING') as logs:
            self.assertTrue(self.send(payload)[0])
        self.assertIn('unreadable timestamp', logs.output[0])
        self.assertEqual([s['timestamp'] for s in self.scores()], [stamp(9)])

    def test_running_that_is_not_finite_is_incomplete(self):
        self.assertFalse(plugin._complete({**{name: 1.0 for name in plugin.MEASURED}, 'Running': float('nan')}))

    def test_empty_block(self):
        self.assertEqual(self.send([]), (True, 0, 0))
        self.assertEqual(self.engine.requests + self.ingest.requests, [])


class SourceTests(unittest.TestCase):
    def test_checksums(self):
        for line in (PLUGIN / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split()
            self.assertEqual(hashlib.sha256((PLUGIN / name).read_bytes()).hexdigest(), digest, name)

    def test_measured_order_matches_the_model(self):
        model = importlib.util.spec_from_file_location('pump_model', ROOT / 'simulator/pump_simulator/model.py')
        module = importlib.util.module_from_spec(model)
        model.loader.exec_module(module)
        self.assertEqual(plugin.MEASURED, module.MEASURED)


if __name__ == '__main__':
    unittest.main()
