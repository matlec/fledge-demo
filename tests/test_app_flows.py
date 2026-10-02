"""Offline tests for the App Flows REST helper, using a fake HTTP opener."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('app_flows', ROOT / 'scripts' / 'app-flows.py')
app_flows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_flows)

TOKEN = 'private-test-token'
SETTINGS = {
    'server': 'tenant.example.invalid', 'token': TOKEN, 'ZEDEDA_CLUSTER': 'factory-edge',
    'FLEDGE_GITOPS_REPOSITORY_URL': 'ssh://git@github.com/example/fledge.git',
    'FLEDGE_GITOPS_NAME': 'fledge-demo', 'FLEDGE_GITOPS_SECRET': 'fledge-demo-git',
    'FLEDGE_GITOPS_PATH': 'deploy/fledge',
}
REPO = {'id': 'repo-1', 'name': 'fledge-demo', 'description': 'kept marker', 'data': {
    'repoDetails': {'url': SETTINGS['FLEDGE_GITOPS_REPOSITORY_URL'], 'commit': 'a' * 40,
                    'pathList': ['deploy/fledge']},
    'target': {'type': 'GIT_REPO_TARGET_TYPE_CLUSTER', 'value': 'factory-edge'}},
    'status': {'display': {'readyBundleDeployments': '1/1'},
               'conditions': [{'type': 'Ready', 'status': 'True', 'message': 'uses ' + TOKEN}]}}


CLUSTER = {'name': 'factory-edge', 'id': 'cluster-123', 'projectId': 'project-456'}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    """Returns queued responses and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url,
                              json.loads(request.data) if request.data else None))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(json.dumps(value).encode())


def http_error(code, body):
    return urllib.error.HTTPError('https://tenant.example.invalid', code, 'error', {},
                                  io.BytesIO(json.dumps(body).encode()))


class AppFlowsTests(unittest.TestCase):
    def run_with(self, responses, action):
        opener = FakeOpener(responses)
        stderr = io.StringIO()
        with patch.object(app_flows.urllib.request, 'build_opener', return_value=opener), \
                contextlib.redirect_stderr(stderr):
            flows = app_flows.AppFlows(dict(SETTINGS))
            try:
                result = action(flows)
            except app_flows.Failure as error:
                result = error
        return result, opener.requests, stderr.getvalue()

    def test_apply_creates_when_absent(self):
        result, requests, stderr = self.run_with(
            [CLUSTER, {'gitrepos': [], 'totalCount': 0}, {'objectId': 'repo-new'}],
            lambda f: f.apply('b' * 40))
        self.assertEqual(result['result'], 'created')
        self.assertEqual([r[0] for r in requests], ['GET', 'GET', 'POST'])
        self.assertTrue(requests[0][1].endswith('/api/v1/zks/instances/name/factory-edge'))
        self.assertIn('projectId=project-456', requests[1][1])
        body = requests[2][2]
        self.assertEqual(body['data']['repoDetails']['commit'], 'b' * 40)
        self.assertEqual(body['data']['target']['value'], 'factory-edge')
        self.assertEqual(body['data']['projectId'], 'project-456')
        self.assertFalse(body['data']['auth']['insecureSkipTlsVerify'])
        self.assertIn('POST /api/v1/cluster/instances/kubernetes/gitrepos', stderr)

    def test_apply_updates_existing_and_shows_target_first(self):
        result, requests, stderr = self.run_with(
            [CLUSTER, {'gitrepos': [{'id': 'repo-1', 'name': 'fledge-demo'}]}, REPO, {}],
            lambda f: f.apply('c' * 40))
        self.assertEqual(result['result'], 'updated')
        self.assertEqual(requests[3][0], 'PUT')
        self.assertTrue(requests[3][1].endswith('/cluster/instances/kubernetes/gitrepos/id/repo-1'))
        self.assertEqual(requests[3][2]['metadata']['description'], 'kept marker')
        self.assertLess(stderr.index('Updating existing registration'), stderr.index('PUT '))
        self.assertIn('"id": "repo-1"', stderr)

    def test_duplicate_names_refused(self):
        rows = [{'id': 'a', 'name': 'fledge-demo'}, {'id': 'b', 'name': 'fledge-demo'}]
        result, requests, _ = self.run_with([CLUSTER, {'gitrepos': rows}], lambda f: f.apply('d' * 40))
        self.assertIsInstance(result, app_flows.Failure)
        self.assertIn('a, b', str(result))
        self.assertEqual(len(requests), 2)

    def test_paginated_list_refused(self):
        result, _, _ = self.run_with([CLUSTER, {'gitrepos': [], 'totalCount': 3}], lambda f: f.status())
        self.assertIsInstance(result, app_flows.Failure)

    def test_invalid_revision_refused_before_requests(self):
        for revision in ('main', 'a' * 39, 'A' * 40):
            result, requests, _ = self.run_with([], lambda f: f.apply(revision))
            self.assertIsInstance(result, app_flows.Failure)
            self.assertEqual(requests, [])

    def test_status_passes_platform_status_without_verdict(self):
        result, _, _ = self.run_with(
            [CLUSTER, {'gitrepos': [{'id': 'repo-1', 'name': 'fledge-demo'}]}, REPO], lambda f: f.status())
        self.assertEqual(result['status']['display'], {'readyBundleDeployments': '1/1'})
        self.assertNotIn('applicationReady', json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_platform_error_shown_with_redaction(self):
        body = {'error': [{'ec': 'zMsgErrorPermission', 'details': 'denied for ' + TOKEN}],
                'data': {'sshPrivateKey': 'PRIVATE-KEY-MATERIAL'}}
        result, _, _ = self.run_with([CLUSTER, http_error(403, body)], lambda f: f.status())
        message = str(result)
        self.assertIn('HTTP 403', message)
        self.assertIn('zMsgErrorPermission', message)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn('PRIVATE-KEY-MATERIAL', message)

    def test_error_payload_with_http_200_fails(self):
        result, _, _ = self.run_with([CLUSTER, {'error': [{'ec': 'zMsgErrorNotFound'}], 'httpStatusCode': 404}],
                                     lambda f: f.status())
        self.assertIsInstance(result, app_flows.Failure)
        self.assertIn('zMsgErrorNotFound', str(result))

    def test_secret_create_and_existing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            private, public = Path(directory, 'key'), Path(directory, 'key.pub')
            private.write_text('PRIVATE-KEY-MATERIAL')
            public.write_text('ssh-ed25519 AAAA')
            result, requests, stderr = self.run_with(
                [CLUSTER, {'secrets': []}, {'objectId': 'secret-1'}],
                lambda f: f.secret_create(private, public))
            self.assertEqual(result, {'secret': 'fledge-demo-git', 'id': 'secret-1', 'result': 'created'})
            self.assertEqual(requests[2][2]['metadata']['type'], 'SECRET_TYPE_SSH')
            self.assertNotIn('PRIVATE-KEY-MATERIAL', stderr)
            existing = {'id': 'secret-1', 'metadata': {'name': 'fledge-demo-git'}}
            result, requests, _ = self.run_with(
                [CLUSTER, {'secrets': [existing]}, existing], lambda f: f.secret_create(private, public))
            self.assertEqual(result['result'], 'already exists; not changed')
            self.assertEqual([r[0] for r in requests], ['GET', 'GET', 'GET'])

    def test_delete_requires_confirmation(self):
        rows = {'gitrepos': [{'id': 'repo-1', 'name': 'fledge-demo'}]}
        with patch('builtins.input', return_value='other'):
            result, requests, stderr = self.run_with([CLUSTER, rows, REPO], lambda f: f.delete(False))
        self.assertIsInstance(result, app_flows.Failure)
        self.assertNotIn('DELETE', [r[0] for r in requests])
        self.assertIn('Deleting registration', stderr)
        result, requests, _ = self.run_with([CLUSTER, rows, REPO, {}], lambda f: f.delete(True))
        self.assertEqual(result['result'], 'deletion requested')
        self.assertEqual(requests[-1][0], 'DELETE')

    def test_redirect_refused(self):
        with self.assertRaises(app_flows.Failure):
            app_flows.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://elsewhere.invalid/')

    def test_settings_from_environment_env_file_and_zcli_login(self):
        with tempfile.TemporaryDirectory() as directory:
            env, login = Path(directory, '.env'), Path(directory, 'zcli.json')
            env.write_text('ZEDEDA_CLUSTER=from-file\nFLEDGE_GITOPS_REPOSITORY_URL=ssh://git@github.com/a/b.git\n')
            login.write_text(json.dumps({'server': 'tenant.example.invalid', 'token': TOKEN}))
            values = app_flows.load_settings(env, {'ZEDEDA_CLUSTER': 'from-environment'}, login)
            self.assertEqual(values['ZEDEDA_CLUSTER'], 'from-environment')
            self.assertEqual(values['FLEDGE_GITOPS_NAME'], 'fledge-demo')
            self.assertEqual((values['server'], values['token']), ('tenant.example.invalid', TOKEN))
            with self.assertRaises(app_flows.Failure) as caught:
                app_flows.load_settings(Path(directory, 'absent.env'), {}, login)
            self.assertIn('ZEDEDA_CLUSTER', str(caught.exception))
            login.write_text(json.dumps({'server': 'tenant.example.invalid'}))
            with self.assertRaises(app_flows.Failure) as caught:
                app_flows.load_settings(env, {}, login)
            self.assertIn('zcli login', str(caught.exception))
            with self.assertRaises(app_flows.Failure):
                app_flows.load_settings(env, {}, Path(directory, 'missing.json'))

    def test_cluster_without_project_refused(self):
        result, requests, _ = self.run_with([{'name': 'factory-edge'}], lambda f: f.status())
        self.assertIsInstance(result, app_flows.Failure)
        self.assertEqual(len(requests), 1)

if __name__ == '__main__':
    unittest.main()
