#!/usr/bin/env python3
"""Exercise the rendered package against real Fledge in isolated Docker resources.
Requires Docker, Helm, Python 3 and PyYAML. No cluster or external writes.
Build fledge-bundle:test with
--build-arg 'PLUGIN_PACKAGES=fledge-south-sinusoid=3.1.0 fledge-south-s2opcua=3.1.0'
and fledge-bundle:no-sinusoid with
--build-arg 'PLUGIN_PACKAGES=fledge-south-s2opcua=3.1.0' from image/fledge.
An unconfigured build has no optional plugins and cannot run these demo checks.
"""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest.mock
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get('FLEDGE_TEST_IMAGE', 'fledge-bundle:test')
ALTERNATE = os.environ.get('FLEDGE_TEST_ALTERNATE', 'fledge-bundle:no-sinusoid')
PREFIX = 'fledge-test-' + uuid.uuid4().hex[:10]
CONTAINERS = []
PASSWORDS = []


def run(*args, check=True, timeout=180, env=None):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env)
    for password in PASSWORDS:
        if password and password in result.stdout:
            raise AssertionError('credential disclosure detected; output suppressed')
    if check and result.returncode:
        raise AssertionError('command failed: ' + ' '.join(args[:4]) + '\n' + result.stdout[-2000:])
    return result


def log(message):
    print(message, flush=True)


def state(name):
    return json.loads(run('docker', 'inspect', name, '--format', '{{json .State}}').stdout)


def probe(name, mode):
    return run('docker', 'exec', name, 'python3', '/opt/fledge/runtime.py', mode, check=False, timeout=20)


def wait_ready(name):
    start = time.monotonic()
    while time.monotonic() - start < 180:
        if not state(name)['Running']:
            raise AssertionError('runtime exited during startup: ' + run('docker', 'logs', name).stdout[-2000:])
        if probe(name, 'readiness').returncode == 0:
            log(f'PASS {name}: ready after {time.monotonic()-start:.1f}s')
            return
        time.sleep(2)
    raise AssertionError('readiness deadline: ' + run('docker', 'logs', name).stdout[-2000:])


def stop(name, success):
    start = time.monotonic()
    run('docker', 'stop', '--timeout', '60', name, timeout=70)
    s = state(name)
    logs = run('docker', 'logs', name).stdout
    assert not s['OOMKilled'] and s['ExitCode'] == (0 if success else 1), (s['ExitCode'], logs[-1600:])
    if success:
        assert 'controlled shutdown completed' in logs
        assert 'Executing controlled shutdown' in logs
        assert 'Shutdown scheduled for Storage' in logs
        assert 'Stopping the Fledge Core event loop' in logs
        assert 'KILLED Microservice' not in logs and 'Killing it...' not in logs
    log(f'PASS {name}: stop exit={s["ExitCode"]} in {time.monotonic()-start:.2f}s')


def start(name, directory, credentials, volume, pause_shutdown=False):
    CONTAINERS.append(name)
    command = ['/opt/fledge/runtime.py', 'run']
    if pause_shutdown:
        command = ['-c', """import sys,time
sys.path.insert(0,'/opt/fledge')
import runtime
original=runtime.api
paused=False
def api(method,path,*args,**kwargs):
    global paused
    if method=='PUT' and path=='/shutdown' and not paused:
        paused=True
        print('TEST: shutdown token obtained',flush=True)
        time.sleep(5)
    return original(method,path,*args,**kwargs)
runtime.api=api
sys.argv=['runtime.py','run']
sys.exit(runtime.main())
"""]
    run('docker', 'run', '-d', '--name', name, '--label', 'fledge.test=' + PREFIX,
        '--memory', '1g', '--cpus', '1',
        '--mount', f'type=volume,src={volume},dst=/usr/local/fledge/data,volume-nocopy',
        '--mount', f'type=bind,src={credentials},dst=/run/fledge-credentials,readonly',
        '--entrypoint', 'python3', '-p', '127.0.0.1::8081', IMAGE, *command)


def admin_env(directory):
    """Host scripts take the credentials that the container reads from its mounted Secret."""
    credentials = directory/'credentials'
    return {**os.environ, 'FLEDGE_ADMIN_USERNAME': (credentials/'username').read_text().strip(),
            'FLEDGE_ADMIN_PASSWORD': (credentials/'password').read_text().rstrip('\r\n')}


def verify(name, directory, compare=False):
    port = run('docker', 'port', name, '8081/tcp').stdout.strip()
    args = ['python3', str(ROOT/'scripts/verify-fledge.py'), '--api', 'http://' + port,
            '--compare' if compare else '--snapshot', str(directory/'snapshot.json')]
    result = run(*args, env=admin_env(directory))
    assert 'PASS:' in result.stdout
    log('PASS reading/authentication/source verification' + (' and exact retained readings' if compare else ''))


def signal_core(name, sig):
    code = ('import sys,os,signal;sys.path.insert(0,"/opt/fledge");import runtime;'
            f'[os.kill(p,signal.{sig}) for p,k in runtime.processes().items() if k=="core"]')
    run('docker', 'exec', name, 'python3', '-c', code)


def wait_live(name):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        assert state(name)['Running'], run('docker', 'logs', name).stdout[-2000:]
        if probe(name, 'startup').returncode == 0:
            return
        time.sleep(1)
    raise AssertionError('startup deadline')


def setup(name, directory, command, config=None, success=True):
    port = run('docker', 'port', name, '8081/tcp').stdout.strip()
    args = ['python3', str(ROOT/'scripts/setup-fledge.py'), command,
            '--api', 'http://' + port]
    if command == 'configure':
        args += ['--config', str(config or ROOT/'demos/sinusoid.json')]
    result = run(*args, check=False, env=admin_env(directory))
    assert (result.returncode == 0) == success, result.stdout
    return result


def client(name, directory):
    import importlib.util
    spec = importlib.util.spec_from_file_location('setup_fledge', ROOT/'scripts/setup-fledge.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    port = run('docker', 'port', name, '8081/tcp').stdout.strip()
    with unittest.mock.patch.dict(os.environ, admin_env(directory)):
        c = module.Client('http://' + port)
    c.authenticate()
    return c, module


def main():
    global IMAGE
    run('docker', 'image', 'inspect', IMAGE)
    run('docker', 'image', 'inspect', ALTERNATE)
    digest = json.loads(run('docker', 'image', 'inspect', IMAGE).stdout)[0]['RepoDigests'][0].split('@')[1]
    for img in (IMAGE, ALTERNATE):
        identity = json.loads(run('docker', 'image', 'inspect', img).stdout)[0]['RepoDigests'][0].split('@')[1]
        args = ['--set', 'image.repository=example.test/fledge', '--set', 'image.digest=' + identity]
        run('helm', 'lint', str(ROOT/'deploy/fledge/chart'), *args)
        docs = list(yaml.safe_load_all(run('helm', 'template', 'fledge', str(ROOT/'deploy/fledge/chart'), *args).stdout))
        assert sorted(x['kind'] for x in docs) == ['Deployment', 'Service']
        spec = next(d for d in docs if d['kind']=='Deployment')['spec']
        assert spec['replicas']==1 and spec['strategy']=={'type':'Recreate'}
        assert not spec['template']['spec']['automountServiceAccountToken']
        assert next(d for d in docs if d['kind']=='Service')['spec']['type']=='ClusterIP'
    run(sys.executable, str(ROOT/'tests/test_chart.py'))
    assert run('helm','template','fledge',str(ROOT/'deploy/fledge/chart'),'--set','image.repository=','--set','image.digest=',check=False).returncode
    assert run('helm','template','fledge',str(ROOT/'deploy/fledge/chart'),'--set','image.repository=example.test/fledge','--set','image.digest=latest',check=False).returncode
    log('PASS chart: two image identities, invalid/missing identity, private exposure and prerequisite ownership')
    volumes = []
    def volume(suffix):
        value=PREFIX+'-'+suffix; volumes.append(value)
        run('docker','volume','create','--label','fledge.test='+PREFIX,value)
        return value
    with tempfile.TemporaryDirectory(prefix=PREFIX+'-') as td:
        directory=Path(td);credentials=directory/'credentials';credentials.mkdir(mode=0o700)
        empty=directory/'empty';empty.mkdir()
        password=secrets.token_urlsafe(24)+'aA1!';bad=secrets.token_urlsafe(24)+'bB2!';PASSWORDS.extend([password,bad])
        (credentials/'username').write_text('admin');(credentials/'password').touch(mode=0o600);(credentials/'password').write_text(password)
        try:
            data=volume('data')
            missing=PREFIX+'-missing';start(missing,directory,empty,data)
            deadline=time.monotonic()+10
            while state(missing)['Running'] and time.monotonic()<deadline:time.sleep(.2)
            assert state(missing)['ExitCode']==1
            log('PASS missing credentials fail before initialization')

            fresh=PREFIX+'-fresh';start(fresh,directory,credentials,data);wait_live(fresh)
            assert probe(fresh,'readiness').returncode==1
            assert probe(fresh,'liveness').returncode==0
            stop(fresh,True)
            log('PASS controlled shutdown before setup; live but unready')

            first=PREFIX+'-first';start(first,directory,credentials,data);wait_live(first)
            setup(first,directory,'initialize');setup(first,directory,'initialize');wait_ready(first)
            c,module=client(first,directory)
            assert not any(s['processName']=='south_c' for s in c.require('GET','/schedule')['schedules'])
            log('PASS repeated initialization and readiness with zero sources')
            setup(first,directory,'configure');time.sleep(3);verify(first,directory)
            setup(first,directory,'configure');verify(first,directory,True)
            log('PASS repeated configuration and exact readings')

            cfg=json.loads((ROOT/'demos/sinusoid.json').read_text())
            wrong=json.loads(json.dumps(cfg));wrong['sources'][0]['version']='0.0.0';badcfg=directory/'bad.json';badcfg.write_text(json.dumps(wrong))
            setup(first,directory,'configure',badcfg,success=False)
            assert probe(first,'readiness').returncode==0
            # Preserve an unrelated disabled source and an undeclared setting.
            c.require('POST','/service',{'name':'unrelated','type':'south','plugin':'sinusoid','enabled':False})
            original=c.require('GET','/category/unrelated')
            c.require('POST','/category/gitops-sinusoid/operatorNote',{'description':'Unowned field','type':'string','default':'keep-me'})
            # Setup keeps no records of its own: a rerun adds no configuration category.
            categories=lambda: {x['key'] for x in c.require('GET','/category')['categories']}
            before=categories()
            setup(first,directory,'configure')
            assert categories()==before
            assert c.require('GET','/category/unrelated')==original
            assert c.require('GET','/category/gitops-sinusoid')['operatorNote']['value']=='keep-me'
            # A declared name with the declared plugin is managed without a flag.
            managed=json.loads(json.dumps(cfg));managed['sources'][0]['name']='unrelated';managed.pop('retention');managed['sources'][0]['enabled']=False
            managed['sources'][0]['config']={'assetName':'managed-sinusoid'}
            managedfile=directory/'managed.json';managedfile.write_text(json.dumps(managed))
            setup(first,directory,'configure',managedfile)
            assert c.require('GET','/category/unrelated')['assetName']['value']==managed['sources'][0]['config']['assetName']
            assert len([s for s in c.require('GET','/schedule')['schedules'] if s['name']=='unrelated'])==1
            # A creation interrupted before settings and enablement is completed by a rerun.
            c.require('POST','/service',{'name':'resumed','type':'south','plugin':'sinusoid','enabled':False})
            resume=json.loads(json.dumps(managed));resume['sources'][0]['name']='resumed';resume['sources'][0]['enabled']=True
            managedfile.write_text(json.dumps(resume))
            setup(first,directory,'configure',managedfile);setup(first,directory,'configure',managedfile)
            resumed=[s for s in c.require('GET','/schedule')['schedules'] if s['name']=='resumed']
            assert len(resumed)==1 and resumed[0]['enabled']
            c.require('PUT','/schedule/disable',{'schedule_name':'resumed'})
            # Native plugin inventory/configuration and disabled creation need no server.
            plugins=c.require('GET','/plugins/installed')['plugins']
            assert len([p for p in plugins if p['name']=='s2opcua' and p['version']=='3.1.0'])==1
            c.require('POST','/service',{'name':'native-check','type':'south','plugin':'s2opcua','enabled':False})
            native=c.require('GET','/category/native-check');assert native['plugin']['value']=='s2opcua' and 'url' in native
            native_before=c.require('GET','/category/native-check')
            collision=json.loads(json.dumps(managed));collision['sources'][0]['name']='native-check';managedfile.write_text(json.dumps(collision))
            setup(first,directory,'configure',managedfile,success=False)
            assert c.require('GET','/category/native-check')==native_before
            c.require('PUT','/schedule/disable',{'schedule_name':'gitops-sinusoid'})
            assert probe(first,'readiness').returncode==0
            c.require('PUT','/schedule/enable',{'schedule_name':'gitops-sinusoid'})
            log('PASS plugin preflight, collision refusal, same-plugin management, interrupted-creation completion, no setup records, unrelated preservation and source-independent readiness')
            verify(first,directory)
            log('PASS OPC UA packaging: inventory, configuration and disabled source (no server, so no acquisition)')

            second=PREFIX+'-replacement';start(second,directory,credentials,data);time.sleep(3)
            run('docker','exec',second,'python3','-c','import sys;sys.path.insert(0,"/opt/fledge");import runtime;assert not runtime.processes()')
            stop(first,True);wait_ready(second);verify(second,directory,True)
            log('PASS competing-volume exclusion and replacement without setup')
            signal_core(second,'SIGSTOP');assert probe(second,'readiness').returncode!=0;assert probe(second,'liveness').returncode!=0
            signal_core(second,'SIGKILL');deadline=time.monotonic()+10
            while state(second)['Running'] and time.monotonic()<deadline:time.sleep(.2)
            assert state(second)['ExitCode']==1
            (credentials/'password').write_text(bad)
            mismatch=PREFIX+'-mismatch';start(mismatch,directory,credentials,data);wait_live(mismatch)
            assert probe(mismatch,'readiness').returncode==1 and probe(mismatch,'liveness').returncode==0
            setup(mismatch,directory,'configure',success=False);setup(mismatch,directory,'initialize',success=False)
            stop(mismatch,False)
            (credentials/'password').write_text(password)
            recovery=PREFIX+'-recovery';start(recovery,directory,credentials,data);wait_ready(recovery);verify(recovery,directory,True);stop(recovery,True)
            log('PASS core failure, retained credential mismatch without liveness loop, stop failure reported and recovery')

            standard=IMAGE;IMAGE=ALTERNATE
            alternate=PREFIX+'-alternate';start(alternate,directory,credentials,volume('alternate'));wait_live(alternate)
            setup(alternate,directory,'initialize');wait_ready(alternate)
            setup(alternate,directory,'configure',success=False)
            assert probe(alternate,'readiness').returncode==0
            stop(alternate,True);IMAGE=standard
            log('PASS bundle without sinusoid: healthy lifecycle and missing-plugin configuration refusal')

            # Password changes can complete before any readiness probe clears the marker.
            transition=PREFIX+'-transition';start(transition,directory,credentials,volume('transition'));wait_live(transition)
            setup(transition,directory,'initialize');stop(transition,True)
            racing=PREFIX+'-password-race'
            start(racing,directory,credentials,volume('password-race'),pause_shutdown=True)
            wait_live(racing)
            with ThreadPoolExecutor(max_workers=1) as executor:
                stopped=executor.submit(stop,racing,True)
                deadline=time.monotonic()+15
                while 'TEST: shutdown token obtained' not in run('docker','logs',racing).stdout:
                    assert time.monotonic()<deadline
                    time.sleep(.1)
                setup(racing,directory,'initialize')
                stopped.result()
            log('PASS initialization after shutdown token acquisition')
            early=PREFIX+'-early';start(early,directory,credentials,volume('early'));time.sleep(.2);stop(early,False)
            log('PASS password-transition shutdown and incomplete start reported as failure')
            for name in CONTAINERS:run('docker','logs',name)
            log('PASS test output and logs contain no generated credentials')
        finally:
            if sys.exc_info()[0]:
                for name in CONTAINERS:
                    print(run('docker','logs',name,check=False).stdout[-2000:])
            for name in CONTAINERS:run('docker','rm','-f','-v',name,check=False)
            for value in volumes:run('docker','volume','rm',value,check=False)
    log('PASS integration suite; dedicated resources removed')


if __name__=='__main__':
    main()
