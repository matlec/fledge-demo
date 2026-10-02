"""Unit tests for the pump anomaly engine check, against a fake KServe v2 endpoint."""
import contextlib
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_engine', ROOT / 'model/pump-anomaly/check_engine.py')
check_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_engine)

REFERENCE = {'input': 'window', 'output': 'score', 'shape': [2, 10, 9], 'data': [0.0] * 180,
             'expected': [0.25, 4.0], 'tolerance': 1e-4}


class FakeEngine(BaseHTTPRequestHandler):
    respond = None  # (status, body or None) for each request
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeEngine.requests.append((self.path, body))
        status, payload = FakeEngine.respond
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        if payload is not None:
            self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    def log_message(self, *args):
        pass


def response(data=(0.25, 4.0), shape=None, datatype='FP32', model='pump-anomaly', version='1'):
    return {'model_name': model, 'model_version': version,
            'outputs': [{'name': 'score', 'datatype': datatype, 'shape': shape or [len(data)], 'data': list(data)}]}


class EngineCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeEngine)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f'http://127.0.0.1:{cls.server.server_port}'
        cls.directory = tempfile.TemporaryDirectory()
        cls.reference = Path(cls.directory.name) / 'model.reference.json'
        cls.reference.write_text(json.dumps(REFERENCE))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.directory.cleanup()

    def run_check(self, status, payload, endpoint=None):
        FakeEngine.respond, FakeEngine.requests = (status, payload), []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_engine.main(['--endpoint', endpoint or self.endpoint, '--model', 'pump-anomaly',
                                      '--version', '1', '--reference', str(self.reference)])
        return code, out.getvalue(), err.getvalue()

    def test_pass(self):
        code, out, _ = self.run_check(200, response((0.25001, 3.9999)))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['scores'], [0.25001, 3.9999])
        path, body = FakeEngine.requests[0]
        self.assertEqual(path, '/v2/models/pump-anomaly/versions/1/infer')
        self.assertEqual(body['inputs'][0]['name'], 'window')
        self.assertEqual(body['inputs'][0]['shape'], [2, 10, 9])
        self.assertEqual(body['inputs'][0]['datatype'], 'FP32')

    def test_wrong_value(self):
        code, _, err = self.run_check(200, response((0.25, 4.1)))
        self.assertEqual(code, 1)
        self.assertIn('window 1', err)

    def test_not_finite(self):
        code, _, err = self.run_check(200, json.dumps(response((0.25, float('nan')))).encode())
        self.assertEqual(code, 1)
        self.assertIn('score nan', err)

    def test_missing_or_short_data(self):
        for data in ([], [0.25]):
            code, _, err = self.run_check(200, response(data, shape=[2]))
            self.assertEqual(code, 1)
            self.assertIn('expected 2 scores', err)
        body = response()
        del body['outputs'][0]['data']
        code, _, err = self.run_check(200, body)
        self.assertEqual(code, 1)
        self.assertIn('got none', err)

    def test_score_output_after_another_output(self):
        body = response((0.25, 4.0))
        body['outputs'].insert(0, {'name': 'other', 'datatype': 'FP32', 'shape': [1], 'data': [9.0]})
        code, out, _ = self.run_check(200, body)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['scores'], [0.25, 4.0])

    def test_wrong_shape(self):
        code, _, err = self.run_check(200, response((0.25, 4.0), shape=[2, 1]))
        self.assertEqual(code, 1)
        self.assertIn('expected FP32 [2]', err)

    def test_wrong_datatype(self):
        code, _, _ = self.run_check(200, response(datatype='FP64'))
        self.assertEqual(code, 1)

    def test_other_model_version(self):
        code, _, err = self.run_check(200, response(version='2'))
        self.assertEqual(code, 1)
        self.assertIn("version '2'", err)

    def test_missing_output(self):
        code, _, err = self.run_check(200, {'model_name': 'pump-anomaly', 'model_version': '1', 'outputs': []})
        self.assertEqual(code, 1)
        self.assertIn('one output', err)

    def test_rejected_request(self):
        code, _, err = self.run_check(404, None)
        self.assertEqual(code, 1)
        self.assertIn('HTTP 404', err)

    def test_not_json(self):
        code, _, err = self.run_check(200, b'<html>')
        self.assertEqual(code, 1)
        self.assertIn('not JSON', err)

    def test_unavailable(self):
        code, _, err = self.run_check(200, None, endpoint='http://127.0.0.1:9')
        self.assertEqual(code, 1)
        self.assertIn('endpoint unavailable', err)


if __name__ == '__main__':
    unittest.main()
