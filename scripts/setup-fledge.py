#!/usr/bin/env python3
"""Explicit credential initialization and sequential, repeatable API configuration.

Admin credentials come from FLEDGE_ADMIN_USERNAME and FLEDGE_ADMIN_PASSWORD.
Certificates that a declaration references are read from --cert-dir or
FLEDGE_CERT_DIR, a directory outside the repository working directory.
A setting declared as {"env": "NAME"} takes its value from that environment
variable, so that declarations hold no secrets.
Each Fledge API call is printed once to stderr, without request bodies.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROCESSES = {'south': 'south_c', 'north': 'north_C'}


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials():
    """Admin credentials from the environment, filled from the Kubernetes Secret."""
    username = os.environ.get('FLEDGE_ADMIN_USERNAME', '')
    password = os.environ.get('FLEDGE_ADMIN_PASSWORD', '')
    if username != 'admin' or not password or password == 'fledge':
        raise Failure('set FLEDGE_ADMIN_USERNAME=admin and FLEDGE_ADMIN_PASSWORD from the admin Secret')
    return username, password


class Client:
    def __init__(self, api):
        url = urllib.parse.urlsplit(api)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise Failure('invalid private API URL')
        self.base = api.rstrip('/') + '/fledge'
        self.username, self.password = credentials()
        self.shown = set()
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, data=None, authenticated=True, body=None, content_type='application/json'):
        if (method, path) not in self.shown:  # Show each Fledge API call once; never bodies.
            self.shown.add((method, path))
            print(method, '/fledge' + path, file=sys.stderr)
        headers = {'Content-Type': content_type}
        if authenticated and self.token:
            headers['authorization'] = self.token
        if body is None and data is not None:
            body = json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, method=method, headers=headers, data=body)
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, None  # Never expose submitted data or response bodies.
        except (OSError, ValueError):
            raise Failure('API unavailable or invalid response') from None

    def require(self, method, path, data=None):
        status, response = self.request(method, path, data)
        if status != 200:
            raise Failure(f'{method} API operation failed (HTTP {status})')
        return response

    def upload_certificate(self, files):
        """Store certificate files through the certificate API, replacing existing ones."""
        boundary = uuid.uuid4().hex
        body = f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\n1\r\n'.encode()
        for field, (filename, content) in files.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n').encode() + content + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        status, _ = self.request('POST', '/certificate', body=body,
                                 content_type='multipart/form-data; boundary=' + boundary)
        if status != 200:
            raise Failure(f'certificate upload failed (HTTP {status})')

    def login(self, default=False):
        return self.request('POST', '/login', {'username': self.username,
                            'password': 'fledge' if default else self.password}, authenticated=False)

    def authenticate(self):
        status, result = self.login()
        if status != 200:
            raise Failure('managed authentication failed; preserve storage and check FLEDGE_ADMIN_PASSWORD against the admin Secret')
        self.token = result['token']
        return result


def initialize(client):
    status, result = client.login()
    if status != 200:
        status, result = client.login(default=True)
        if status != 200:
            raise Failure('initialization authentication failed; preserve storage and check FLEDGE_ADMIN_PASSWORD against the admin Secret')
        client.token = result['token']
        client.require('PUT', '/user/' + str(result['uid']) + '/password',
                       {'current_password': 'fledge', 'new_password': client.password})
    client.authenticate()
    if client.login(default=True)[0] not in (401, 403, 404):
        raise Failure('default credential rejection not confirmed')
    if client.request('GET', '/service', authenticated=False)[0] not in (401, 403):
        raise Failure('unauthenticated protected access was not rejected')
    print('PASS: managed credentials initialized; protected access verified')


def load_config(path):
    return load_config_data(json.loads(path.read_text()))


def load_config_data(cfg):
    if not isinstance(cfg, dict) or cfg.get('schemaVersion') != 1 or not isinstance(cfg.get('sources'), list):
        raise Failure('configuration requires schemaVersion 1 and a sources list')
    names = set()
    for source in cfg['sources']:
        name = source.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or name in names:
            raise Failure('source names must be unique valid identifiers')
        names.add(name)
        if source.get('type') not in PROCESSES or not source.get('plugin') or not source.get('version'):
            raise Failure('each source requires south or north type, plugin and expected version')
        if not isinstance(source.get('enabled'), bool):
            raise Failure('each source requires a boolean enabled setting')
        for field in ('config', 'advanced'):
            settings = source.get(field, {})
            if not isinstance(settings, dict) or not all(isinstance(v, str) or env_name(v) for v in settings.values()):
                raise Failure('configuration settings must be strings or {"env": NAME} references')
        if 'plugin' in source.get('config', {}):
            raise Failure('plugin identity cannot be changed through configuration')
    certificates = cfg.get('certificates', [])
    if not isinstance(certificates, list):
        raise Failure('certificates must be a list')
    names = set()
    for entry in certificates:
        name = entry.get('name', '') if isinstance(entry, dict) else ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or name in names:
            raise Failure('certificate names must be unique valid identifiers')
        names.add(name)
        if set(entry) - {'name', 'cert', 'key'} or entry.get('cert') != name + '.der' \
                or entry.get('key', name + '.pem') != name + '.pem':
            raise Failure(f'certificate {name} requires cert {name}.der and optionally key {name}.pem')
    if 'retention' in cfg:
        retention = cfg['retention']
        if not isinstance(retention, dict) or not retention or any(k not in ('age', 'size', 'retainUnsent') or not isinstance(v, str) for k, v in retention.items()):
            raise Failure('invalid declared retention settings')
    return cfg


def env_name(value):
    """The variable name of an {"env": NAME} setting, or None."""
    if isinstance(value, dict) and set(value) == {'env'} and isinstance(value['env'], str) \
            and re.fullmatch(r'[A-Z_][A-Z0-9_]*', value['env']):
        return value['env']
    return None


def resolve_settings(cfg):
    """Replace {"env": NAME} settings with their values before any change; never print them."""
    resolved = json.loads(json.dumps(cfg))
    for source in resolved['sources']:
        for field in ('config', 'advanced'):
            for key, value in source.get(field, {}).items():
                name = env_name(value)
                if name:
                    if not os.environ.get(name):
                        raise Failure(f'set {name} for setting {key} of {source["name"]}')
                    source[field][key] = os.environ[name]
    return resolved


def category(client, name):
    status, result = client.request('GET', '/category/' + urllib.parse.quote(name, safe=''))
    if status == 404:
        return None
    if status != 200:
        raise Failure(f'category preflight failed (HTTP {status})')
    return result


def read_certificates(cfg, cert_dir):
    """Read every referenced certificate file before any change; never print content."""
    if not cfg.get('certificates'):
        return []
    if cert_dir is None:
        raise Failure('the declaration references certificates; set --cert-dir or FLEDGE_CERT_DIR')
    directory = cert_dir.expanduser().resolve()
    if directory == ROOT or ROOT in directory.parents:
        raise Failure('refusing a certificate directory inside the repository working directory')
    loaded = []
    for entry in cfg['certificates']:
        files = {}
        for field in ('cert', 'key'):
            if field in entry:
                path = directory / entry[field]
                try:
                    files[field] = (entry[field], path.read_bytes())
                except OSError:
                    raise Failure(f'missing or unreadable certificate file: {path}') from None
        loaded.append((entry['name'], files))
    return loaded


def wait_stopped(client, name):
    deadline = time.monotonic() + 60
    while any(s['name'] == name and s['status'] == 'running' for s in client.require('GET', '/service')['services']):
        if time.monotonic() >= deadline:
            raise Failure(f'source {name} did not stop for its restart')
        time.sleep(1)


def configure(client, cfg, cert_dir=None, restart_sources=False):
    """Create missing declared sources and apply declared settings to existing ones."""
    certificates = read_certificates(cfg, cert_dir)
    cfg = resolve_settings(cfg)
    client.authenticate()  # This command never tries default credentials.
    plugins = client.require('GET', '/plugins/installed')['plugins']
    schedules = client.require('GET', '/schedule')['schedules']
    existing = {}
    # Complete all checks before the first change.
    for source in cfg['sources']:
        name = source['name']
        found = [p for p in plugins if p['name'] == source['plugin'] and p['type'] == source['type']]
        if len(found) != 1 or found[0]['version'] != source['version']:
            raise Failure('required plugin/version is unavailable; configuration not applied')
        matches = [s for s in schedules if s['name'] == name]
        cat = category(client, name)
        if len(matches) > 1 or (matches and matches[0]['processName'] != PROCESSES[source['type']]):
            raise Failure(f'source {name} belongs to a different service type or has duplicate schedules')
        if cat is not None and cat.get('plugin', {}).get('value') != source['plugin']:
            raise Failure(f'source {name} uses a different plugin; configuration not applied')
        if matches and cat is None:
            raise Failure(f'source {name} has a schedule but no configuration')
        existing[name] = (cat, matches)
    if 'retention' in cfg and category(client, 'PURGE_READ') is None:
        raise Failure('instance retention category unavailable')

    for name, files in certificates:
        client.upload_certificate(files)
    if certificates:
        print(f'PASS: {len(certificates)} declared certificates uploaded')

    for source in cfg['sources']:
        name = source['name']
        encoded = urllib.parse.quote(name, safe='')
        cat, matches = existing[name]
        if cat is None:
            client.require('POST', '/service', {'name': name, 'type': source['type'], 'plugin': source['plugin'],
                'enabled': False, 'config': {k: {'value': v} for k, v in source.get('config', {}).items()}})
        elif not matches:  # Completes a creation that was interrupted before its schedule.
            client.require('POST', '/schedule', {'type': 1, 'name': name, 'process_name': PROCESSES[source['type']],
                                                 'enabled': False, 'exclusive': True})
        if source.get('config'):
            client.require('PUT', '/category/' + encoded, source['config'])
        if source.get('advanced'):
            deadline = time.monotonic() + 60
            while category(client, name + 'Advanced') is None:
                if time.monotonic() >= deadline:
                    raise Failure('source advanced settings unavailable; retry configuration')
                time.sleep(1)
            client.require('PUT', '/category/' + encoded + 'Advanced', source['advanced'])
        client.require('PUT', '/schedule/' + ('enable' if source['enabled'] else 'disable'), {'schedule_name': name})
    if 'retention' in cfg:
        client.require('PUT', '/category/PURGE_READ', cfg['retention'])
    if restart_sources:
        # Sources read certificates when they start; restart those that use uploaded ones.
        uploaded = {name for name, _ in certificates}
        for source in cfg['sources']:
            if source['enabled'] and uploaded & set(source.get('config', {}).values()):
                client.require('PUT', '/schedule/disable', {'schedule_name': source['name']})
                wait_stopped(client, source['name'])
                client.require('PUT', '/schedule/enable', {'schedule_name': source['name']})
                print(f'PASS: restarted source {source["name"]} to load its certificates')
    print('PASS: declared configuration applied; run acquisition verification separately')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['initialize', 'configure'])
    parser.add_argument('--api', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--cert-dir', type=Path,
                        default=Path(os.environ['FLEDGE_CERT_DIR']) if os.environ.get('FLEDGE_CERT_DIR') else None,
                        help='directory of the certificate files a declaration references')
    parser.add_argument('--restart-sources', action='store_true',
                        help='restart enabled sources that use uploaded certificates')
    args = parser.parse_args()
    if args.command == 'configure' and args.config is None:
        parser.error('configure requires --config')
    if args.command == 'initialize' and (args.config or args.restart_sources):
        parser.error('--config and --restart-sources apply only to configure')
    cfg = load_config(args.config) if args.config else None
    client = Client(args.api)
    if args.command == 'initialize':
        initialize(client)
    else:
        configure(client, cfg, args.cert_dir, args.restart_sources)


if __name__ == '__main__':
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, TypeError):
        error = sys.exc_info()[1]
        print('FAIL: ' + (str(error) if isinstance(error, Failure) else 'invalid configuration or API response'), file=sys.stderr)
        sys.exit(1)
