"""Unit tests for the pump, sink, inference and bounded compare checks in verify-fledge.py."""
from datetime import datetime, timedelta, timezone
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_fledge', ROOT / 'scripts' / 'verify-fledge.py')
verify_fledge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_fledge)

VERIFICATION = json.loads((ROOT / 'demos/pump.json').read_text())['verification']
DATAPOINTS = VERIFICATION['datapoints']
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%d %H:%M:%S.%f')


def step(seconds_ago, names=DATAPOINTS):
    """One simulator step as Fledge stores it: one reading per datapoint, newest first."""
    return [{'timestamp': stamp(seconds_ago), 'reading': {name: 1.0}} for name in names]


def fleet(**overrides):
    rows = step(1) + step(2) + step(3)
    readings = {asset: rows for asset in VERIFICATION['assets']}
    readings.update(overrides)
    return readings


def check(readings):
    return verify_fledge.check_pump_readings(readings, DATAPOINTS, NOW, VERIFICATION['maxAgeSeconds'])


class PumpCheckTests(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(check(fleet()), [])

    def test_pass_with_partial_newest_step_and_control_readings(self):
        rows = step(0.5, DATAPOINTS[:4]) + [{'timestamp': stamp(0.7), 'reading': {'Scenario': 'normal'}}] + step(1.5)
        self.assertEqual(check(fleet(Pump2=rows)), [])

    def test_stale_reading(self):
        problems = check(fleet(Pump1=step(12) + step(13)))
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], 'Pump1')
        self.assertIn('12.0 s old', problems[0][1])

    def test_age_limit_is_exclusive(self):
        self.assertEqual(len(check(fleet(Pump1=step(10)))), 1)
        self.assertEqual(check(fleet(Pump1=step(9.9))), [])

    def test_missing_datapoint(self):
        without = [n for n in DATAPOINTS if n != 'BearingVibration']
        problems = check(fleet(Pump3=step(1, without) + step(2, without)))
        self.assertEqual(problems, [('Pump3', 'no step with all datapoints; missing BearingVibration')])

    def test_datapoints_split_across_steps(self):
        rows = step(1, DATAPOINTS[:5]) + step(2, DATAPOINTS[5:])
        problems = check(fleet(Pump3=rows))
        self.assertEqual(problems, [('Pump3', 'no step with all datapoints; datapoints have different timestamps')])

    def test_timestamp_without_fraction(self):
        rows = [{'timestamp': NOW.strftime('%Y-%m-%d %H:%M:%S'), 'reading': {n: 1.0}} for n in DATAPOINTS]
        self.assertEqual(check(fleet(Pump1=rows)), [])

    def test_unreadable_timestamp(self):
        rows = [{'timestamp': 'yesterday', 'reading': {n: 1.0}} for n in DATAPOINTS]
        self.assertEqual(check(fleet(Pump1=rows)), [('Pump1', "unreadable reading timestamp 'yesterday'")])

    def test_missing_asset(self):
        self.assertEqual(check(fleet(Pump2=[])), [('Pump2', 'no readings')])


SINK = json.loads((ROOT / 'demos/influxdb.json').read_text())['verification']


def point(seconds_ago, names=SINK['datapoints']):
    return {'_time': (NOW - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            **{name: '1' for name in names}}


def sink(points):
    return verify_fledge.check_sink_points(points, SINK['datapoints'], NOW, SINK['maxAgeSeconds'])


class SinkCheckTests(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(sink({a: [point(1), point(2)] for a in SINK['assets']}), [])

    def test_stale_pump(self):
        points = {a: [point(1)] for a in SINK['assets']}
        points['Pump2'] = [point(31)]
        self.assertEqual(sink(points), [('Pump2', 'newest complete point is 31.0 s old (limit 10 s)')])

    def test_missing_field(self):
        incomplete = point(1, [n for n in SINK['datapoints'] if n != 'Scenario'])
        self.assertEqual(sink({'Pump1': [incomplete]}), [('Pump1', 'no point with all fields in the last 2 minutes')])
        self.assertEqual(sink({'Pump1': [incomplete, point(3)]}), [])

    def test_no_points(self):
        self.assertEqual(sink({'Pump3': []}), [('Pump3', 'no points in the last 2 minutes')])

    def test_flux_csv_and_lag_times(self):
        text = ',result,table,_time,Speed,Scenario\r\n,_result,0,2026-09-29T11:59:59.5Z,1450,normal\r\n\r\n'
        rows = verify_fledge.flux_rows(text)
        self.assertEqual(rows[0]['Scenario'], 'normal')
        lag = verify_fledge.parse_time('2026-09-29 12:00:00.000000') - verify_fledge.parse_time(rows[0]['_time'])
        self.assertEqual(lag.total_seconds(), 0.5)

    def test_sink_declaration_matches_pump_contract(self):
        pump = json.loads((ROOT / 'demos/pump.json').read_text())['verification']
        self.assertEqual((SINK['assets'], SINK['datapoints']), (pump['assets'], pump['datapoints']))



INFERENCE = json.loads((ROOT / 'demos/inference.json').read_text())['verification']


def pump(running=1, scores=((1, 0.2), (2, 0.3), (3, 0.1)), newest=0.5):
    return {'running': running, 'newest': stamp(newest) if newest is not None else None,
            'scores': [(stamp(age), value) for age, value in scores]}


def scores_check(pumps, expect=None):
    return verify_fledge.check_scores(pumps, NOW, INFERENCE['maxAgeSeconds'], expect)


class InferenceCheckTests(unittest.TestCase):
    def fleet(self, **overrides):
        pumps = {asset: pump() for asset in INFERENCE['assets']}
        pumps.update(overrides)
        return pumps

    def test_pass_reports_age_and_lag(self):
        problems, notes = scores_check(self.fleet())
        self.assertEqual(problems, [])
        self.assertIn(('Pump1', 'score age 1.0 s, lag behind its readings 0.5 s'), notes)

    def test_stale_running_pump(self):
        problems, _ = scores_check(self.fleet(Pump2=pump(scores=((45, 0.2),))))
        self.assertEqual(problems, [('Pump2', 'newest score is 45.0 s old (limit 30 s)')])

    def test_running_pump_without_scores(self):
        self.assertEqual(scores_check(self.fleet(Pump3=pump(scores=())))[0], [('Pump3', 'no scores')])

    def test_stopped_pump_is_not_stale(self):
        problems, notes = scores_check(self.fleet(Pump2=pump(running=0, scores=((600, 0.2),))))
        self.assertEqual(problems, [])
        self.assertIn(('Pump2', 'stopped; no scores expected'), notes)

    def test_pump_without_readings_fails(self):
        problems, _ = scores_check(self.fleet(Pump3=pump(running=None, scores=(), newest=None)))
        self.assertEqual(problems, [('Pump3', 'no recent readings with Running; check acquisition with demos/pump.json')])

    def test_expect_anomalous_passes(self):
        faulty = pump(scores=[(age, 3.0) for age in range(1, 40)])
        problems, notes = scores_check(self.fleet(Pump2=faulty), expect='Pump2')
        self.assertEqual(problems, [])
        self.assertIn(('Pump2', 'median score 3.00 over 60 s'), notes)

    def test_expect_anomalous_tolerates_isolated_false_alarms(self):
        others = pump(scores=[(age, 1.5 if age % 10 == 0 else 0.2) for age in range(1, 60)])
        faulty = pump(scores=[(age, 3.0) for age in range(1, 60)])
        self.assertEqual(scores_check(self.fleet(Pump1=others, Pump2=faulty), expect='Pump2')[0], [])

    def test_expect_anomalous_fails_for_a_normal_pump(self):
        problems, _ = scores_check(self.fleet(), expect='Pump2')
        self.assertEqual(problems, [('Pump2', 'median score 0.20 over 60 s is below 1.0')])

    def test_expect_anomalous_fails_when_another_pump_is_anomalous(self):
        faulty = pump(scores=[(age, 3.0) for age in range(1, 40)])
        problems, _ = scores_check(self.fleet(Pump1=faulty, Pump2=faulty), expect='Pump2')
        self.assertEqual(problems, [('Pump1', 'median score 3.00 over 60 s is not below 1.0')])

    def test_expect_anomalous_ignores_old_scores(self):
        old = pump(scores=[(1, 0.2)] + [(age, 3.0) for age in range(70, 100)])
        self.assertEqual(scores_check(self.fleet(Pump2=old), expect='Pump2')[0],
                         [('Pump2', 'median score 0.20 over 60 s is below 1.0')])

    def test_expect_unknown_or_stopped_pump(self):
        self.assertEqual(scores_check(self.fleet(), expect='Pump9')[0], [('Pump9', 'is not one of the checked pumps')])
        stopped = self.fleet(Pump2=pump(running=0))
        self.assertEqual(scores_check(stopped, expect='Pump2')[0], [('Pump2', 'is stopped, so it has no score to check')])

    def test_stopped_service_fails(self):
        readings = {asset: [{'timestamp': stamp(0.5), 'reading': {'Running': 1}}] for asset in INFERENCE['assets']}
        readings.update({asset + 'Score': [{'timestamp': stamp(1), 'reading': {'AnomalyScore': 0.2, 'Model': 'm/1'}}]
                         for asset in INFERENCE['assets']})

        def get(path):
            if path == '/schedule':
                return {'schedules': [{'name': 'pump-inference', 'enabled': True}, {'name': 'pump-scores', 'enabled': True}]}
            if path == '/service':
                return {'services': [{'name': 'pump-inference', 'status': 'running'}, {'name': 'pump-scores', 'status': 'shutdown'}]}
            return readings[path.split('/')[2].split('?')[0]]

        real_now = verify_fledge.datetime
        class Frozen(real_now):
            @classmethod
            def now(cls, tz=None):
                return NOW
        verify_fledge.datetime = Frozen
        self.addCleanup(setattr, verify_fledge, 'datetime', real_now)
        with self.assertRaises(RuntimeError) as raised, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            verify_fledge.verify_inference(get, 'pump-inference', INFERENCE, deadline=0)
        self.assertIn('pump-scores', str(raised.exception))
        self.assertIn('FAIL pump-scores: service is not enabled and running', err.getvalue())

    def test_declaration_matches_the_pump_contract(self):
        declaration = json.loads((ROOT / 'demos/inference.json').read_text())
        pump_assets = json.loads((ROOT / 'demos/pump.json').read_text())['verification']['assets']
        self.assertEqual(INFERENCE['assets'], pump_assets)
        north = [s for s in declaration['sources'] if s['name'] == INFERENCE['source']][0]
        south = [s for s in declaration['sources'] if s['name'] == INFERENCE['ingest']][0]
        self.assertEqual(north['config']['assets'].split(','), pump_assets)
        self.assertEqual(south['config']['host'], '127.0.0.1')
        self.assertEqual(north['config']['ingestUrl'], f'http://127.0.0.1:{south["config"]["port"]}/{south["config"]["uri"]}')
        self.assertEqual(declaration['sources'].index(south), 0)  # the ingest exists before scores are posted


def freeze(test):
    """Make verify-fledge.py's datetime.now() return NOW for the rest of the test."""
    real_now = verify_fledge.datetime
    class Frozen(real_now):
        @classmethod
        def now(cls, tz=None):
            return NOW
    verify_fledge.datetime = Frozen
    test.addCleanup(setattr, verify_fledge, 'datetime', real_now)


class FakeInfluxDB:
    """Stands in for OPENER: records each query and answers with CSV rows per asset."""

    def __init__(self, points, status=200):
        self.points, self.status, self.requests = points, status, []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, 'error', {}, None)
        query = json.loads(request.data)['query']
        asset = next(a for a in self.points if f'r.asset == "{a}"' in query)
        names = ['_time'] + SINK['datapoints']
        lines = [',result,table,' + ','.join(names)]
        lines += [',_result,0,' + ','.join(row.get(n, '') for n in names) for row in self.points[asset]]
        return io.BytesIO(('\r\n'.join(lines) + '\r\n\r\n').encode())


class SinkFlowTests(unittest.TestCase):
    TOKEN = 'read-token-1234'

    def run_sink(self, influxdb, services=(('influxdb-sink', True, 'running'),)):
        freeze(self)
        self.addCleanup(setattr, verify_fledge, 'OPENER', verify_fledge.OPENER)
        verify_fledge.OPENER = influxdb

        def get(path):
            if path == '/schedule':
                return {'schedules': [{'name': n, 'enabled': e} for n, e, _ in services]}
            if path == '/service':
                return {'services': [{'name': n, 'status': s} for n, _, s in services]}
            self.assertTrue(path.endswith('?limit=1'))
            return [{'timestamp': stamp(0.5), 'reading': {'Speed': 1.0}}]

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                verify_fledge.verify_sink(get, 'influxdb-sink', SINK, 'http://127.0.0.1:8086/', deadline=0)
            except RuntimeError as e:
                return out.getvalue(), err.getvalue(), e
        return out.getvalue(), err.getvalue(), None

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'FLEDGE_INFLUXDB_READ_TOKEN': self.TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pass_queries_each_asset_and_reports_lag(self):
        influxdb = FakeInfluxDB({a: [point(1), point(2)] for a in SINK['assets']})
        out, _, error = self.run_sink(influxdb)
        self.assertIsNone(error)
        self.assertIn('PASS: north service influxdb-sink running', out)
        self.assertIn('INFO Pump1: newest point 1.0 s old, InfluxDB lag 0.5 s', out)
        self.assertEqual(len(influxdb.requests), len(SINK['assets']))
        for asset, request in zip(SINK['assets'], influxdb.requests):
            self.assertEqual(request.full_url, 'http://127.0.0.1:8086/api/v2/query?org=fledge-demo')
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.get_header('Authorization'), 'Token ' + self.TOKEN)
            query = json.loads(request.data)['query']
            for part in ('from(bucket: "pumps")', 'range(start: -2m)', 'r._measurement == "fledge"',
                         f'r.asset == "{asset}"', 'limit(n: 5)'):
                self.assertIn(part, query)

    def test_stopped_service_and_stale_pump_are_reported_together(self):
        points = {a: [point(1)] for a in SINK['assets']}
        points['Pump2'] = [point(31)]
        _, err, error = self.run_sink(FakeInfluxDB(points), services=(('influxdb-sink', True, 'shutdown'),))
        self.assertEqual(str(error), 'InfluxDB check failed: Pump2, influxdb-sink')
        self.assertIn('FAIL Pump2: newest complete point is 31.0 s old (limit 10 s)', err)
        self.assertIn('FAIL influxdb-sink: north service is not enabled and running', err)

    def test_ages_are_reported_on_failure_too(self):
        points = {a: [point(1)] for a in SINK['assets']}
        points['Pump2'] = [point(31)]
        points['Pump3'] = []
        out, _, error = self.run_sink(FakeInfluxDB(points))
        self.assertIsNotNone(error)
        self.assertIn('INFO Pump2: newest point 31.0 s old, InfluxDB lag 30.5 s', out)
        self.assertNotIn('Pump3', out)

    def test_disabled_service_fails(self):
        points = {a: [point(1)] for a in SINK['assets']}
        _, _, error = self.run_sink(FakeInfluxDB(points), services=(('influxdb-sink', False, 'running'),))
        self.assertEqual(str(error), 'InfluxDB check failed: influxdb-sink')

    def test_rejected_query_does_not_show_the_token(self):
        out, err, error = self.run_sink(FakeInfluxDB({}, status=401))
        self.assertEqual(str(error), 'InfluxDB query failed (HTTP 401)')
        self.assertTrue(error.__suppress_context__)  # no traceback of the request
        self.assertNotIn(self.TOKEN, out + err + str(error))

    def test_missing_token_sends_no_query(self):
        influxdb = FakeInfluxDB({})
        with mock.patch.dict(os.environ, {'FLEDGE_INFLUXDB_READ_TOKEN': ''}):
            _, _, error = self.run_sink(influxdb)
        self.assertIn('FLEDGE_INFLUXDB_READ_TOKEN', str(error))
        self.assertEqual(influxdb.requests, [])


def sinusoid(seconds_ago, value):
    return {'reading': {'sinusoid': value}, 'timestamp': stamp(seconds_ago)}


class RetainedReadingsTests(unittest.TestCase):
    RECORDED = [sinusoid(0, 0.5), sinusoid(1, 0.25), sinusoid(2, 0.0)]

    def retained(self, rows):
        calls = []

        def get(path):
            calls.append(path)
            return rows
        try:
            verify_fledge.verify_retained(get, '/asset/gitops-sinusoid', self.RECORDED)
        except RuntimeError as e:
            return calls, e
        return calls, None

    def test_one_bounded_request_around_the_recorded_readings(self):
        calls, error = self.retained(self.RECORDED)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        path, query = calls[0].split('?')
        self.assertEqual(path, '/asset/gitops-sinusoid')
        # Readings from 2 s ago to now: the window ends 1 s after the newest and spans 3 s plus 2.
        self.assertEqual(urllib.parse.parse_qs(query), {
            'mostrecent': ['true'], 'seconds': ['5'], 'previous_ts': [stamp(-1)]})

    def test_more_retained_readings_pass(self):
        self.assertIsNone(self.retained([sinusoid(-1, 0.75)] + self.RECORDED + [sinusoid(3, -0.25)])[1])

    def test_missing_reading_fails(self):
        self.assertEqual(str(self.retained(self.RECORDED[:2])[1]), 'recorded readings missing or expired')

    def test_changed_value_fails(self):
        changed = self.RECORDED[:2] + [sinusoid(2, 0.001)]
        self.assertEqual(str(self.retained(changed)[1]), 'recorded readings missing or expired')


class RedirectTests(unittest.TestCase):
    def test_redirect_is_refused(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import urllib.error
        import urllib.request
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302 if self.path == '/start' else 200)
                self.send_header('Location', '/elsewhere')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/start', data=b'{}',
                                         method='POST', headers={'Authorization': 'Token secret'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            verify_fledge.OPENER.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(hits, ['/start'])


if __name__ == '__main__':
    unittest.main()
