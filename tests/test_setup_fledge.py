"""Offline tests for the setup helper's certificates, north services and secret settings, using a fake Fledge API."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_fledge', ROOT / 'scripts' / 'setup-fledge.py')
setup_fledge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_fledge)

PASSWORD = 'private-test-password-1!'
KEY = b'-----BEGIN PRIVATE KEY-----\nprivate-test-key-material\n-----END PRIVATE KEY-----\n'
DECLARATION = {
    'schemaVersion': 1,
    'certificates': [{'name': 'demo-ca', 'cert': 'demo-ca.der'},
                     {'name': 'fledge-opcua-client', 'cert': 'fledge-opcua-client.der',
                      'key': 'fledge-opcua-client.pem'}],
    'sources': [{'name': 'pump-opcua', 'type': 'south', 'plugin': 's2opcua', 'version': '3.1.0',
                 'config': {'url': 'opc.tcp://pump-simulator:4840', 'caCert': 'demo-ca',
                            'clientCert': 'fledge-opcua-client', 'clientKey': 'fledge-opcua-client'},
                 'enabled': True},
                {'name': 'plain', 'type': 'south', 'plugin': 's2opcua', 'version': '3.1.0',
                 'config': {'url': 'opc.tcp://other:4840'}, 'enabled': True}],
}


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeFledge:
    """Just enough of the Fledge API for configure; records mutating calls."""

    def __init__(self):
        self.calls = []
        self.uploads = []
        self.schedules = []
        self.categories = {}
        self.running = set()

    def open(self, request, timeout=None):
        method, path = request.get_method(), request.full_url.split('/fledge', 1)[1]
        body = request.data
        if method != 'GET' and path != '/login':
            self.calls.append((method, path))
        result = self.handle(method, path, body, request.headers)
        if isinstance(result, int):
            raise urllib.error.HTTPError(request.full_url, result, 'error', {}, io.BytesIO(b'{}'))
        return Response(json.dumps(result).encode())

    def handle(self, method, path, body, headers):
        if path == '/login':
            return {'token': 'token', 'uid': 1} if json.loads(body)['password'] == PASSWORD else 401
        if headers.get('Authorization') != 'token':
            return 401
        if path == '/plugins/installed':
            return {'plugins': [{'name': 's2opcua', 'type': 'south', 'version': '3.1.0'},
                                {'name': 'influxdb', 'type': 'north', 'version': '1.0.0'}]}
        if path == '/schedule' and method == 'POST':
            data = json.loads(body)
            self.schedules.append({'name': data['name'], 'processName': data['process_name']})
            return {}
        if path == '/schedule':
            return {'schedules': self.schedules}
        if path == '/service' and method == 'GET':
            return {'services': [{'name': n, 'status': 'running'} for n in self.running]}
        if path == '/service' and method == 'POST':
            data = json.loads(body)
            self.schedules.append({'name': data['name'],
                                   'processName': {'south': 'south_c', 'north': 'north_C'}[data['type']]})
            self.categories[data['name']] = {'plugin': {'value': data['plugin']}}
            return {}
        if path.startswith('/category/'):
            name = urllib.parse.unquote(path.split('/', 2)[2])
            if method == 'GET':
                return self.categories.get(name, 404) if name != 'PURGE_READ' else {}
            self.categories.setdefault(name, {}).update(json.loads(body))
            return {}
        if path == '/certificate':
            self.uploads.append((headers['Content-type'], body))
            return {}
        if path in ('/schedule/enable', '/schedule/disable'):
            name = json.loads(body)['schedule_name']
            (self.running.add if path.endswith('enable') else self.running.discard)(name)
            return {}
        return 404


class CertificateSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='setup-fledge-test-')
        self.addCleanup(tmp.cleanup)
        self.certs = Path(tmp.name)
        (self.certs / 'demo-ca.der').write_bytes(b'ca-der')
        (self.certs / 'fledge-opcua-client.der').write_bytes(b'client-der')
        (self.certs / 'fledge-opcua-client.pem').write_bytes(KEY)
        self.api = FakeFledge()

    def run_configure(self, cfg=None, cert_dir=None, restart=False):
        cfg = setup_fledge.load_config_data(copy.deepcopy(cfg or DECLARATION))
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'FLEDGE_ADMIN_USERNAME': 'admin', 'FLEDGE_ADMIN_PASSWORD': PASSWORD}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            client = setup_fledge.Client('http://127.0.0.1:8081')
            client.opener = self.api
            try:
                setup_fledge.configure(client, cfg, cert_dir or self.certs, restart)
            except setup_fledge.Failure as error:
                print('FAIL:', error, file=err)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn('private-test-key-material', output)
        self.assertNotIn('PRIVATE KEY', output)
        return output

    def test_uploads_before_sources_and_reports(self):
        output = self.run_configure()
        self.assertIn('PASS: 2 declared certificates uploaded', output)
        first_source_change = self.api.calls.index(('POST', '/service'))
        self.assertEqual(self.api.calls[:2], [('POST', '/certificate')] * 2)
        self.assertLess(1, first_source_change)
        content_type, body = self.api.uploads[1]
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="overwrite"\r\n\r\n1\r\n', body)
        self.assertIn(b'name="cert"; filename="fledge-opcua-client.der"', body)
        self.assertIn(b'name="key"; filename="fledge-opcua-client.pem"', body)
        self.assertIn(KEY, body)
        self.assertNotIn(b'name="key"', self.api.uploads[0][1])

    def test_repeated_run_does_not_restart(self):
        self.run_configure()
        self.api.calls.clear()
        output = self.run_configure()
        self.assertNotIn(('PUT', '/schedule/disable'), self.api.calls)
        self.assertEqual(self.api.calls.count(('POST', '/certificate')), 2)
        self.assertNotIn(('POST', '/service'), self.api.calls)
        self.assertNotIn('restarted', output)

    def test_restart_only_sources_using_certificates(self):
        self.run_configure()
        self.api.calls.clear()
        output = self.run_configure(restart=True)
        self.assertEqual(self.api.calls.count(('PUT', '/schedule/disable')), 1)
        self.assertIn('PASS: restarted source pump-opcua to load its certificates', output)
        self.assertNotIn('restarted source plain', output)
        disable = self.api.calls.index(('PUT', '/schedule/disable'))
        self.assertEqual(self.api.calls[disable + 1], ('PUT', '/schedule/enable'))
        self.assertIn('pump-opcua', self.api.running)

    def test_missing_file_changes_nothing(self):
        (self.certs / 'fledge-opcua-client.pem').unlink()
        output = self.run_configure()
        self.assertIn('missing or unreadable certificate file', output)
        self.assertIn('fledge-opcua-client.pem', output)
        self.assertEqual(self.api.calls, [])

    def test_directory_inside_repository_refused(self):
        output = self.run_configure(cert_dir=ROOT / 'demos')
        self.assertIn('inside the repository', output)
        self.assertEqual(self.api.calls, [])
        output = self.run_configure(cert_dir=ROOT)
        self.assertIn('inside the repository', output)

    def test_declaration_without_certificate_directory(self):
        with self.assertRaisesRegex(setup_fledge.Failure, 'FLEDGE_CERT_DIR'):
            setup_fledge.read_certificates(DECLARATION, None)
        self.assertEqual(setup_fledge.read_certificates({'sources': []}, None), [])

    def test_invalid_certificate_entries(self):
        for entries in ([{'name': 'a', 'cert': 'b.der'}], [{'name': 'a', 'cert': 'a.der', 'key': 'b.pem'}],
                        [{'name': 'a', 'cert': '../a.der'}], [{'name': '../a', 'cert': '../a.der'}],
                        [{'name': 'a', 'cert': 'a.der'}, {'name': 'a', 'cert': 'a.der'}],
                        [{'name': 'a', 'cert': 'a.der', 'path': '/etc'}], {'name': 'a'}):
            with self.subTest(entries=entries), self.assertRaises(setup_fledge.Failure):
                setup_fledge.load_config_data({**DECLARATION, 'certificates': entries})

    def test_failed_upload_stops_before_sources(self):
        self.api.handle_original = self.api.handle
        self.api.handle = lambda m, p, b, h: 500 if p == '/certificate' else self.api.handle_original(m, p, b, h)
        output = self.run_configure()
        self.assertIn('certificate upload failed (HTTP 500)', output)
        self.assertNotIn(('POST', '/service'), self.api.calls)


TOKEN = 'private-influx-token-value'
NORTH = {
    'schemaVersion': 1,
    'sources': [{'name': 'influxdb-sink', 'type': 'north', 'plugin': 'influxdb', 'version': '1.0.0',
                 'config': {'url': 'http://influxdb:8086', 'token': {'env': 'FLEDGE_INFLUXDB_TOKEN'}},
                 'enabled': True}],
}


class NorthSetupTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeFledge()

    def run_configure(self, cfg=None, env=None):
        environment = {'FLEDGE_ADMIN_USERNAME': 'admin', 'FLEDGE_ADMIN_PASSWORD': PASSWORD,
                       'FLEDGE_INFLUXDB_TOKEN': TOKEN, **(env or {})}
        out = io.StringIO()
        with patch.dict(os.environ, environment), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cfg = setup_fledge.load_config_data(copy.deepcopy(cfg or NORTH))
                client = setup_fledge.Client('http://127.0.0.1:8081')
                client.opener = self.api
                setup_fledge.configure(client, cfg)
            except setup_fledge.Failure as error:
                print('FAIL:', error)
        output = out.getvalue()
        self.assertNotIn(TOKEN, output)
        return output

    def test_creates_north_service_with_secret_from_environment(self):
        output = self.run_configure()
        self.assertIn('PASS: declared configuration applied', output)
        self.assertEqual(self.api.schedules, [{'name': 'influxdb-sink', 'processName': 'north_C'}])
        self.assertEqual(self.api.categories['influxdb-sink']['token'], TOKEN)
        self.assertIn('influxdb-sink', self.api.running)

    def test_repeated_run_keeps_one_service(self):
        self.run_configure()
        self.api.calls.clear()
        self.run_configure(env={'FLEDGE_INFLUXDB_TOKEN': 'rotated-token'})
        self.assertNotIn(('POST', '/service'), self.api.calls)
        self.assertEqual(len(self.api.schedules), 1)
        self.assertEqual(self.api.categories['influxdb-sink']['token'], 'rotated-token')

    def test_missing_or_empty_variable_changes_nothing(self):
        for value in (None, ''):
            with self.subTest(value=value):
                env = {} if value is None else {'FLEDGE_INFLUXDB_TOKEN': value}
                with patch.dict(os.environ, {}):
                    os.environ.pop('FLEDGE_INFLUXDB_TOKEN', None)
                    cfg = copy.deepcopy(NORTH)
                    output = io.StringIO()
                    with patch.dict(os.environ, {'FLEDGE_ADMIN_USERNAME': 'admin', 'FLEDGE_ADMIN_PASSWORD': PASSWORD,
                                                 **env}), contextlib.redirect_stdout(output):
                        client = setup_fledge.Client('http://127.0.0.1:8081')
                        client.opener = self.api
                        with self.assertRaisesRegex(setup_fledge.Failure, 'set FLEDGE_INFLUXDB_TOKEN for setting token'):
                            setup_fledge.configure(client, setup_fledge.load_config_data(cfg))
                self.assertEqual(self.api.calls, [])

    def test_name_collision_between_types(self):
        south = copy.deepcopy(DECLARATION['sources'][1])
        south['name'] = 'influxdb-sink'
        self.run_configure({'schemaVersion': 1, 'sources': [south]})
        self.api.calls.clear()
        output = self.run_configure()
        self.assertIn('belongs to a different service type', output)
        self.assertEqual(self.api.calls, [])
        other = FakeFledge()
        self.api = other
        self.run_configure()
        self.api.calls.clear()
        output = self.run_configure({'schemaVersion': 1, 'sources': [south]})
        self.assertIn('belongs to a different service type', output)
        self.assertEqual(self.api.calls, [])

    def test_interrupted_creation_uses_north_process(self):
        self.api.categories['influxdb-sink'] = {'plugin': {'value': 'influxdb'}}
        self.run_configure()
        self.assertIn(('POST', '/schedule'), self.api.calls)
        self.assertEqual(self.api.schedules, [{'name': 'influxdb-sink', 'processName': 'north_C'}])

    def test_invalid_settings_refused(self):
        for value in ({'env': 'lower'}, {'env': 'A', 'x': 1}, {'file': 'x'}, 5, ['A']):
            with self.subTest(value=value), self.assertRaises(setup_fledge.Failure):
                cfg = copy.deepcopy(NORTH)
                cfg['sources'][0]['config']['token'] = value
                setup_fledge.load_config_data(cfg)
        for kind in ('east', None):
            with self.subTest(kind=kind), self.assertRaises(setup_fledge.Failure):
                cfg = copy.deepcopy(NORTH)
                cfg['sources'][0]['type'] = kind
                setup_fledge.load_config_data(cfg)


if __name__ == '__main__':
    unittest.main()
