#!/usr/bin/env python3
"""Manage the demo's App Flows GitOps registration through the ZEDEDA REST API.

zcli 20.0.0 has no GitOps commands, so each subcommand calls one documented
endpoint under /api/v1/cluster/instances/kubernetes/ (API tags KubernetesSecrets
and KubernetesGitOps). Every request is printed to stderr as METHOD path.

The server and token come from zcli's configuration (run zcli configure and
zcli login first). The project is looked up from the cluster name, as zcli does.
Other settings come from the environment or .env; see .env.example.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request

API = '/api/v1/'
GITOPS = 'cluster/instances/kubernetes/'
ZCLI_CONFIG = Path.home() / '.config' / 'zededa' / 'zcli.json'
REQUIRED = ('ZEDEDA_CLUSTER', 'FLEDGE_GITOPS_REPOSITORY_URL')
DEFAULTS = {'FLEDGE_GITOPS_NAME': 'fledge-demo', 'FLEDGE_GITOPS_SECRET': 'fledge-demo-git',
            'FLEDGE_GITOPS_PATH': 'deploy/fledge'}
REDACT = {'sshPrivateKey', 'sshPublicKey', 'token', 'password', 'authorization'}


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure(f'redirect to {newurl} refused; check the zcli server setting')


def load_settings(path, environ=os.environ, zcli_config=ZCLI_CONFIG):
    """Settings from the environment, then .env read as data, then zcli's login."""
    values = dict(DEFAULTS)
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip().removeprefix('export ')
            if line and not line.startswith('#'):
                key, _, raw = line.partition('=')
                parts = shlex.split(raw, comments=True)
                values[key.strip()] = parts[0] if parts else ''
    values.update({key: environ[key] for key in (*REQUIRED, *DEFAULTS) if environ.get(key)})
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise Failure('set ' + ', '.join(missing) + ' in the environment or ' + str(path))
    try:
        login = json.loads(Path(zcli_config).read_text())
    except (OSError, ValueError):
        raise Failure(f'no zcli login in {zcli_config}; run zcli configure and zcli login') from None
    if not login.get('server') or not login.get('token'):
        raise Failure(f'{zcli_config} has no server or token; run zcli configure and zcli login')
    values['server'], values['token'] = login['server'], login['token']
    return values


class AppFlows:
    def __init__(self, settings):
        self.s = settings
        self.base = 'https://' + settings['server'].removeprefix('https://').rstrip('/')
        self._project = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def redact(self, value):
        if isinstance(value, dict):
            return {k: '<redacted>' if k in REDACT else self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            return value.replace(self.s['token'], '<redacted>')
        return value

    def request(self, method, path, payload=None):
        print(method, API + path, file=sys.stderr)
        request = urllib.request.Request(
            self.base + API + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self.s['token'],
                     'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors='replace')
            try:
                body = json.dumps(self.redact(json.loads(body)), indent=2)
            except ValueError:
                body = self.redact(body)
            raise Failure(f'{method} {path} returned HTTP {error.code}:\n{body[:4000]}') from None
        except urllib.error.URLError as error:
            raise Failure(f'{method} {path} failed: {error.reason}') from None
        except ValueError:
            raise Failure(f'{method} {path} returned a response that is not JSON') from None
        if not isinstance(result, dict):
            raise Failure(f'{method} {path} returned an unexpected response')
        # Some failures arrive as HTTP 200 with an error payload.
        errors = [e for e in result.get('error') or [] if e.get('ec') not in ('zMsgErrorNone', 'zMsgSucess')]
        if errors or int(result.get('httpStatusCode', 200)) >= 400:
            raise Failure(f'{method} {path} failed:\n' + json.dumps(self.redact(result), indent=2)[:4000])
        return result

    @property
    def project(self):
        """The cluster's project, looked up by name as zcli does."""
        if self._project is None:
            name = urllib.parse.quote(self.s['ZEDEDA_CLUSTER'], safe='')
            self._project = self.request('GET', f'zks/instances/name/{name}').get('projectId')
            if not self._project:
                raise Failure(f"cluster {self.s['ZEDEDA_CLUSTER']} has no project in the response")
        return self._project

    def find(self, kind, name):
        """Look up a resource by name within the project; there is no local state."""
        project = urllib.parse.quote(self.project, safe='')
        result = self.request('GET', f'{GITOPS}{kind}?projectId={project}')
        rows = result.get(kind) or []
        if result.get('totalCount', len(rows)) > len(rows):
            raise Failure(f'{kind} list is paginated; cannot check {name} is unique')
        matches = [r for r in rows if r.get('name', r.get('metadata', {}).get('name')) == name]
        if len(matches) > 1:
            raise Failure(f'{len(matches)} {kind} named {name}: ' + ', '.join(r['id'] for r in matches))
        return self.request('GET', f"{GITOPS}{kind}/id/{urllib.parse.quote(matches[0]['id'], safe='')}") if matches else None

    def describe(self, repo):
        details = repo.get('data', {}).get('repoDetails', {})
        return {'name': repo.get('name'), 'id': repo.get('id'), 'url': details.get('url'),
                'path': details.get('pathList'), 'revision': details.get('commit'),
                'target': repo.get('data', {}).get('target')}

    def secret_create(self, private_key, public_key):
        name = self.s['FLEDGE_GITOPS_SECRET']
        existing = self.find('secrets', name)
        if existing:
            # The API has no secret update or delete operation.
            return {'secret': name, 'id': existing['id'], 'result': 'already exists; not changed'}
        result = self.request('POST', GITOPS + 'secrets', {
            'metadata': {'name': name, 'type': 'SECRET_TYPE_SSH', 'projectId': self.project},
            'data': {'sshPrivateKey': base64.b64encode(Path(private_key).read_bytes()).decode(),
                     'sshPublicKey': base64.b64encode(Path(public_key).read_bytes()).decode()}})
        return {'secret': name, 'id': result.get('objectId'), 'result': 'created'}

    def apply(self, revision):
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise Failure('--revision must be a full 40-character commit SHA')
        name = self.s['FLEDGE_GITOPS_NAME']
        payload = {'metadata': {'name': name}, 'data': {
            'projectId': self.project,
            'repoDetails': {'url': self.s['FLEDGE_GITOPS_REPOSITORY_URL'], 'commit': revision,
                            'pathList': [self.s['FLEDGE_GITOPS_PATH']]},
            'auth': {'git': {'secret': {'type': 'SECRET_TYPE_SSH', 'name': self.s['FLEDGE_GITOPS_SECRET']}},
                     'insecureSkipTlsVerify': False},
            'target': {'type': 'GIT_REPO_TARGET_TYPE_CLUSTER', 'value': self.s['ZEDEDA_CLUSTER']}}}
        existing = self.find('gitrepos', name)
        if existing is None:
            result = self.request('POST', GITOPS + 'gitrepos', payload)
            return {'registration': name, 'id': result.get('objectId'), 'result': 'created', 'revision': revision}
        print('Updating existing registration:', json.dumps(self.describe(existing)), file=sys.stderr)
        payload['metadata']['description'] = existing.get('description', '')
        self.request('PUT', f"{GITOPS}gitrepos/id/{urllib.parse.quote(existing['id'], safe='')}", payload)
        return {'registration': name, 'id': existing['id'], 'result': 'updated', 'revision': revision}

    def status(self):
        repo = self.find('gitrepos', self.s['FLEDGE_GITOPS_NAME'])
        if repo is None:
            return {'registration': self.s['FLEDGE_GITOPS_NAME'], 'result': 'not found'}
        return {**self.describe(repo), 'status': self.redact(repo.get('status', {}))}

    def delete(self, confirmed):
        repo = self.find('gitrepos', self.s['FLEDGE_GITOPS_NAME'])
        if repo is None:
            return {'registration': self.s['FLEDGE_GITOPS_NAME'], 'result': 'not found'}
        print('Deleting registration:', json.dumps(self.describe(repo)), file=sys.stderr)
        print('App Flows removes the workloads it deployed; operator-owned PVC and Secret stay.', file=sys.stderr)
        if not confirmed and input(f"Type {repo['name']} to confirm: ") != repo['name']:
            raise Failure('not confirmed; nothing deleted')
        self.request('DELETE', f"{GITOPS}gitrepos/id/{urllib.parse.quote(repo['id'], safe='')}")
        return {'registration': repo['name'], 'id': repo['id'], 'result': 'deletion requested'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', default='.env')
    commands = parser.add_subparsers(dest='command', required=True)
    secret = commands.add_parser('secret-create', help='POST secrets: create the SSH secret App Flows uses for Git')
    secret.add_argument('--private-key', required=True)
    secret.add_argument('--public-key', required=True)
    apply = commands.add_parser('apply', help='POST or PUT gitrepos: register or update the revision')
    apply.add_argument('--revision', required=True)
    commands.add_parser('status', help='GET gitrepos: show the registration and its platform status')
    delete = commands.add_parser('delete', help='DELETE gitrepos: remove the registration')
    delete.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
    args = parser.parse_args()
    try:
        flows = AppFlows(load_settings(args.env_file))
        if args.command == 'secret-create':
            result = flows.secret_create(args.private_key, args.public_key)
        elif args.command == 'apply':
            result = flows.apply(args.revision)
        elif args.command == 'status':
            result = flows.status()
        else:
            result = flows.delete(args.yes)
    except (Failure, OSError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
