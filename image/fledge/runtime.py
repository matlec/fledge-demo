"""Source-independent health probes and PID 1 lifecycle for the bundled image."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATA = Path(os.environ.get('FLEDGE_DATA', '/usr/local/fledge/data'))
STATE = Path('/run/fledge-demo')
CREDS = Path(os.environ.get('FLEDGE_DEMO_CREDENTIALS', '/run/fledge-credentials'))
BASE = 'http://127.0.0.1:8081/fledge'
STOP_SECONDS = 45
STOPPING = False
EXITS = {}


class Failure(Exception):
    pass


def log(message):
    print('[fledge-demo] ' + message, flush=True)


def api(method, path, data=None, token=None, timeout=3):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['authorization'] = token
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        # Do not echo response bodies: they can contain submitted credential values.
        return error.code, None
    except (OSError, ValueError):
        raise Failure('API unavailable or invalid response') from None


def require(method, path, data=None, token=None):
    status, value = api(method, path, data, token)
    if status != 200:
        raise Failure(f'{method} {path.split("?")[0]} failed (HTTP {status})')
    return value


def credentials():
    try:
        username = (CREDS / 'username').read_text().strip()
        password = (CREDS / 'password').read_text().rstrip('\r\n')
    except OSError:
        raise Failure('required credential files missing or unreadable') from None
    if username != 'admin' or not password or password == 'fledge':
        raise Failure('credentials require admin and a non-default password')
    return username, password


def login(username, password):
    return api('POST', '/login', {'username': username, 'password': password})


def authenticate():
    status, auth = login(*credentials())
    if status != 200:
        raise Failure(f'managed authentication failed (HTTP {status}); preserve storage and correct the secret')
    return auth['token']


def healthy(token):
    services = require('GET', '/service', token=token)['services']
    return all(any(s.get('type') == kind and s.get('status') == 'running'
                   for s in services) for kind in ('Core', 'Storage'))


def responsive():
    # Authentication is intentionally independent of restart decisions. A replaced
    # Secret or pending first setup must leave the API available for repair.
    return ({'core', 'storage'}.issubset(set(processes().values())) and
            require('GET', '/ping').get('uptime', -1) != -1)


def probe(mode):
    if not responsive():
        return 1
    if mode == 'readiness':
        token = authenticate()
        if not healthy(token):
            return 1
        if api('GET', '/service')[0] not in (401, 403):
            raise Failure('protected API permits unauthenticated access')
        (DATA / '.gitops-initializing').unlink(missing_ok=True)
    return 0


def shutdown_token():
    status, auth = login(*credentials())
    if status == 200:
        (DATA / '.gitops-initializing').unlink(missing_ok=True)
        return auth['token']
    if (DATA / '.gitops-initializing').is_file():
        status, auth = login('admin', 'fledge')
        if status == 200:
            return auth['token']
        # Initialization may have changed the password between the two requests.
        status, auth = login(*credentials())
        if status == 200:
            (DATA / '.gitops-initializing').unlink(missing_ok=True)
            return auth['token']
    raise Failure('shutdown authentication failed; preserve storage and correct the secret')


def processes():
    found = {}
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            cmd = path.read_bytes().split(b'\0')
            # Core also invokes service binaries for plugin metadata. Those
            # subprocesses are reaped by Core and are not supervised daemons.
            status = (path.parent / 'status').read_text().splitlines()
            if not any(line.split() == ['PPid:', '1'] for line in status):
                continue
            if any(arg in (b'--plugin', b'--readingsplugin', b'--info') for arg in cmd):
                continue
            for kind in ('core', 'storage', 'south', 'north'):
                target = ('fledge.services.' + kind).encode()
                if any(arg == target or arg.endswith(b'/' + target) for arg in cmd):
                    found[int(path.parent.name)] = kind
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def reap():
    try:
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if not pid:
                break
            EXITS[pid] = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        pass


def shutdown():
    (STATE / 'ready').unlink(missing_ok=True)
    start = time.monotonic()
    expected = processes()
    if not {'core', 'storage'}.issubset(set(expected.values())):
        raise Failure('controlled shutdown unavailable: core/storage already lost')
    token = shutdown_token()
    status, reply = api('PUT', '/shutdown', token=token)
    if status in (401, 403):
        # External initialization can invalidate a fresh default token between
        # login and shutdown. Retry once with managed credentials only.
        status, reply = api('PUT', '/shutdown', token=authenticate())
    if status != 200:
        raise Failure(f'shutdown request failed (HTTP {status})')
    if reply.get('message') != 'Fledge shutdown has been scheduled. Wait for few seconds for process cleanup.':
        raise Failure('shutdown was not acknowledged')
    log('authenticated controlled shutdown acknowledged')
    while time.monotonic() - start < STOP_SECONDS:
        reap()
        if not processes():
            time.sleep(1.2)  # Drain rsyslog before PID 1 exits.
            reap()
            if any(EXITS.get(pid) != 0 for pid in expected):
                raise Failure('missing or unsuccessful service exit during shutdown')
            log(f'core/storage/acquisition exited 0; controlled shutdown completed in {time.monotonic()-start:.2f}s')
            return
        time.sleep(.2)
    raise Failure('controlled shutdown exceeded 45 seconds')


def supervise():
    global STOPPING
    def stop(signum, frame):
        global STOPPING
        STOPPING = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    credentials()  # Fail before creating a default-account database if the secret is missing.
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / 'ready').unlink(missing_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    # Hold a kernel lock for this PID 1 lifetime: Recreate alone does not prevent
    # ReplicaSet overlap after manual pod deletion. A second pod waits before startup.
    lock = (DATA / '.gitops.lock').open('a')
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if STOPPING:
                raise Failure('termination while waiting for exclusive data ownership')
            time.sleep(.2)
    if not any(p.name not in ('lost+found', '.gitops.lock') for p in DATA.iterdir()):
        (DATA / '.gitops-initializing').touch(mode=0o600)
    (DATA / 'var/run/fledge.core.pid').unlink(missing_ok=True)
    Path('/run/rsyslogd.pid').unlink(missing_ok=True)
    subprocess.run(['touch', '/var/log/syslog'], check=True)
    subprocess.run(['chmod', '640', '/var/log/syslog'], check=True)
    subprocess.run(['chown', 'syslog:adm', '/var/log/syslog'], check=True)
    subprocess.run(['/usr/sbin/rsyslogd', '-f', '/etc/rsyslog.conf'], check=True)
    subprocess.Popen(['tail', '-F', '/var/log/syslog'])
    child = subprocess.Popen(['/usr/local/fledge/bin/fledge', 'start'])
    deadline = time.monotonic() + 150
    while child.poll() is None:
        if STOPPING:
            raise Failure('termination during incomplete startup')
        if time.monotonic() > deadline:
            raise Failure('core startup exceeded 150 seconds')
        time.sleep(.2)
    if child.returncode:
        raise Failure('fledge start failed')
    while not STOPPING:
        reap()
        if 'core' not in processes().values():
            raise Failure('core process lost')
        time.sleep(.2)
    shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['run', 'startup', 'readiness', 'liveness'])
    args = parser.parse_args()
    try:
        if args.mode == 'run':
            supervise()
        else:
            return probe(args.mode)
        return 0
    except (Failure, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        # Only our controlled messages are safe to print; raw exceptions may embed data.
        log('ERROR ' + (str(error) if isinstance(error, Failure) else 'runtime operation failed'))
        return 1


if __name__ == '__main__':
    sys.exit(main())
