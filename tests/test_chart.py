"""Render chart variants without a container daemon or cluster."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = str(ROOT / 'deploy/fledge/chart')
UI = {'ui.enabled': 'true', 'ui.image.repository': 'example.test/fledge-gui',
      'ui.image.digest': 'sha256:' + 'a' * 64}


SIMULATOR = str(ROOT / 'deploy/pump-demo/chart')
SIMULATOR_IMAGE = {'simulator.image.digest': 'sha256:' + 'b' * 64}


def command(verb, values, chart=CHART, release='demo'):
    args = ['helm', verb]
    if verb == 'template':
        args.append(release)
    args.append(chart)
    for key, value in values.items():
        args += ['--set', f'{key}={value}']
    return subprocess.run(args, text=True, capture_output=True)


def render(values, **kwargs):
    result = command('template', values, **kwargs)
    assert result.returncode == 0, result.stderr
    return {(d['kind'], d['metadata']['name']): d for d in
            yaml.safe_load_all(result.stdout) if d}


class ChartTests(unittest.TestCase):
    def test_variants_preserve_core(self):
        baseline = render({})
        variants = [{}, UI, {'service.type': 'NodePort'},
                    {**UI, 'service.type': 'NodePort', 'ui.service.type': 'NodePort'},
                    {**UI, 'service.type': 'NodePort', 'ui.service.type': 'NodePort',
                     'service.nodePort': 31081, 'ui.service.nodePort': 31080},
                    {**UI, 'ui.service.type': 'NodePort'},
                    {'ui.service.type': 'NodePort', 'service.type': 'NodePort',
                     'ui.service.nodePort': 30081},
                    {**UI, 'imagePullSecrets[0].name': 'registry-pull'}]
        for values in variants:
            with self.subTest(values=values):
                lint = command('lint', values)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
                docs = render(values)
                enabled = values.get('ui.enabled') == 'true'
                self.assertEqual(set(docs), set(baseline) | (
                    {('Deployment', 'demo-ui'), ('Service', 'demo-ui')} if enabled else set()))
                core = docs['Deployment', 'demo']['spec']['template']
                expected = baseline['Deployment', 'demo']['spec']['template']
                if 'imagePullSecrets[0].name' not in values:
                    self.assertEqual(core, expected)
                for (kind, name), doc in docs.items():
                    spec = doc['spec']
                    if kind == 'Service':
                        prefix = 'ui.service' if name.endswith('-ui') else 'service'
                        nodeport = values.get(prefix + '.type') == 'NodePort'
                        self.assertEqual(spec['type'], 'NodePort' if nodeport else 'ClusterIP')
                        self.assertNotIn('publishNotReadyAddresses', spec)
                        port = spec['ports'][0]
                        self.assertEqual(port['port'], 8080 if name.endswith('-ui') else 8081)
                        self.assertEqual(port['targetPort'], 'http' if name.endswith('-ui') else 'api')
                        if nodeport:
                            self.assertEqual(port['nodePort'], values.get(prefix + '.nodePort', 30080 if name.endswith('-ui') else 30081))
                        else:
                            self.assertNotIn('nodePort', port)
                        self.assertEqual(spec['selector'], docs['Deployment', name]['spec']['selector']['matchLabels'])
                    else:
                        pod = spec['template']['spec']
                        self.assertFalse(pod['automountServiceAccountToken'])
                        self.assertFalse(pod.get('hostNetwork', False))
                        for container in pod['containers']:
                            self.assertTrue(all('hostPort' not in p for p in container['ports']))
                if enabled:
                    ui = docs['Deployment', 'demo-ui']['spec']
                    self.assertNotEqual(ui['selector'], docs['Deployment', 'demo']['spec']['selector'])
                    pod = ui['template']['spec']
                    self.assertNotIn('volumes', pod)
                    container = pod['containers'][0]
                    self.assertNotIn('volumeMounts', container)
                    self.assertNotIn('env', container)
                    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
                        self.assertEqual(container[probe]['httpGet'], {'path': '/fledge.html', 'port': 'http'})
                    if 'imagePullSecrets[0].name' in values:
                        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry-pull'}])

    def test_invalid_values(self):
        invalid = [({'image.repository': ''}, 'image'),
                   ({'image.digest': ''}, 'image'),
                   ({'image.digest': 'latest'}, 'image'),
                   ({'ui.enabled': 'true'}, 'ui'),
                   ({**UI, 'ui.image.repository': ''}, 'ui'),
                   ({**UI, 'ui.image.repository': 'Repo:latest'}, 'ui'),
                   ({**UI, 'ui.image.digest': ''}, 'ui'),
                   ({**UI, 'ui.image.digest': 'latest'}, 'ui'),
                   ({**UI, 'ui.image': 'null'}, 'ui'),
                   ({**UI, 'service.type': 'NodePort', 'ui.service.type': 'NodePort',
                     'ui.service.nodePort': 30081}, 'must be distinct')]
        for prefix in ('service', 'ui.service'):
            for value in (29999, 32768, 30080.5, 'invalid'):
                invalid.append(({**UI, prefix + '.nodePort': value}, prefix.split('.')[0]))
            invalid.append(({**UI, prefix + '.type': 'LoadBalancer'}, prefix.split('.')[0]))
        for values, diagnostic in invalid:
            with self.subTest(values=values):
                result = command('template', values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)



class PumpSimulatorChartTests(unittest.TestCase):
    """The simulator part of the pump demo chart."""
    def render(self, values):
        return render(values, chart=SIMULATOR, release='pump-demo')

    def test_render(self):
        for values in (SIMULATOR_IMAGE, {**SIMULATOR_IMAGE, 'imagePullSecrets[0].name': 'registry-pull'}):
            with self.subTest(values=values):
                lint = command('lint', values, chart=SIMULATOR)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
                docs = self.render(values)
                # The server certificate names the Service, whatever the release name.
                self.assertIn(('Service', 'pump-simulator'), docs)
                service = docs['Service', 'pump-simulator']['spec']
                self.assertEqual(service['type'], 'ClusterIP')
                self.assertEqual(service['ports'], [{'name': 'opcua', 'port': 4840, 'targetPort': 'opcua'}])
                deployment = docs['Deployment', 'pump-simulator']['spec']
                self.assertEqual(service['selector'], deployment['selector']['matchLabels'])
                self.assertEqual((deployment['replicas'], deployment['strategy']), (1, {'type': 'Recreate'}))
                pod = deployment['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertFalse(pod.get('hostNetwork', False))
                self.assertEqual(pod['terminationGracePeriodSeconds'], 15)
                self.assertFalse(pod['enableServiceLinks'])
                self.assertTrue(pod['securityContext']['runAsNonRoot'])
                self.assertEqual(pod['securityContext']['runAsUser'], 10001)
                container = pod['containers'][0]
                self.assertEqual(container['image'], 'matthiasl381/pump-simulator@sha256:' + 'b' * 64)
                self.assertEqual(container['ports'], [{'name': 'opcua', 'containerPort': 4840}])
                self.assertEqual(container['securityContext'], {'allowPrivilegeEscalation': False,
                                 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}})
                for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
                    self.assertEqual(container[probe]['tcpSocket'], {'port': 'opcua'})
                volumes = {v['name']: v for v in pod['volumes']}
                self.assertEqual(volumes['server']['secret']['secretName'], 'pump-simulator-server')
                self.assertEqual(volumes['operator']['secret']['secretName'], 'pump-simulator-operator')
                self.assertNotIn('persistentVolumeClaim', str(volumes))
                mounts = {m['mountPath']: m for m in container['volumeMounts']}
                self.assertTrue(mounts['/etc/pump-simulator/server']['readOnly'])
                self.assertTrue(mounts['/etc/pump-simulator/operator']['readOnly'])
                if 'imagePullSecrets[0].name' in values:
                    self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry-pull'}])

    def test_invalid_values(self):
        invalid = [({'simulator.image.digest': ''}, 'digest'),
                   ({'simulator.image.digest': 'latest'}, 'digest'),
                   ({**SIMULATOR_IMAGE, 'simulator.image.repository': ''}, 'repository'),
                   ({**SIMULATOR_IMAGE, 'simulator.serverSecret': ''}, 'serverSecret'),
                   ({**SIMULATOR_IMAGE, 'simulator.operatorSecret': ''}, 'operatorSecret'),
                   ({**SIMULATOR_IMAGE, 'simulator.service.type': 'NodePort'}, 'service'),
                   ({**SIMULATOR_IMAGE, 'persistence.enabled': 'true'}, 'persistence')]
        for values, diagnostic in invalid:
            with self.subTest(values=values):
                result = command('template', values, chart=SIMULATOR, release='pump-demo')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)

    def test_fleet_bundle(self):
        fleet = yaml.safe_load((ROOT / 'deploy/pump-demo/fleet.yaml').read_text())
        self.assertEqual(fleet['defaultNamespace'], 'fledge-demo')
        self.assertEqual(fleet['helm']['releaseName'], 'pump-demo')
        self.assertEqual(fleet['helm']['values'], {'grafana': {'service': {'type': 'NodePort', 'nodePort': 30083}}})
        values = {'grafana.service.type': 'NodePort', 'grafana.service.nodePort': 30083, **SIMULATOR_IMAGE}
        docs = render(values, chart=SIMULATOR, release='pump-demo')
        self.assertEqual(docs['Service', 'grafana']['spec']['ports'][0]['nodePort'], 30083)
        self.assertEqual(docs['Service', 'influxdb']['spec']['type'], 'ClusterIP')
        self.assertEqual(fleet['helm']['chart'], './chart')


SINK = SIMULATOR


class PumpDashboardChartTests(unittest.TestCase):
    """The InfluxDB and Grafana part of the pump demo chart."""
    def render(self, values=None, chart=SINK):
        return render(values or {}, chart=chart, release='pump-demo')

    def test_render(self):
        for values in ({}, {'imagePullSecrets[0].name': 'registry-pull'}):
            with self.subTest(values=values):
                lint = command('lint', values, chart=SINK)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
                docs = self.render(values)
                # No Secret, namespace, volume claim or node exposure is generated.
                self.assertEqual(set(docs), {('Deployment', 'pump-simulator'), ('Service', 'pump-simulator'),
                                             ('Deployment', 'influxdb'), ('Service', 'influxdb'),
                                             ('Deployment', 'grafana'), ('Service', 'grafana'),
                                             ('ConfigMap', 'grafana-provisioning'), ('ConfigMap', 'grafana-dashboards')})
                for name, port in (('influxdb', 8086), ('grafana', 3000)):
                    service = docs['Service', name]['spec']
                    self.assertEqual(service['type'], 'ClusterIP')
                    self.assertEqual(service['ports'], [{'name': 'http', 'port': port, 'targetPort': 'http'}])
                    deployment = docs['Deployment', name]['spec']
                    self.assertEqual(service['selector'], deployment['selector']['matchLabels'])
                    pod = deployment['template']['spec']
                    self.assertFalse(pod['automountServiceAccountToken'])
                    self.assertFalse(pod['enableServiceLinks'])
                    self.assertTrue(pod['securityContext']['runAsNonRoot'])
                    container = pod['containers'][0]
                    self.assertRegex(container['image'], r'@sha256:[a-f0-9]{64}$')
                    self.assertEqual(container['securityContext'], {'allowPrivilegeEscalation': False,
                                     'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}})
                    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
                        self.assertEqual(container[probe]['httpGet']['port'], 'http')
                    if values:
                        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry-pull'}])
                influx = docs['Deployment', 'influxdb']['spec']
                self.assertEqual(influx['strategy'], {'type': 'Recreate'})
                volumes = {v['name']: v for v in influx['template']['spec']['volumes']}
                self.assertEqual(volumes['data']['persistentVolumeClaim']['claimName'], 'influxdb-data')
                env = {e['name']: e for e in influx['template']['spec']['containers'][0]['env']}
                for key in ('USERNAME', 'PASSWORD', 'ADMIN_TOKEN'):
                    self.assertEqual(env['DOCKER_INFLUXDB_INIT_' + key]['valueFrom']['secretKeyRef']['name'], 'influxdb-admin')
                self.assertEqual(env['DOCKER_INFLUXDB_INIT_RETENTION']['value'], '7d')
                grafana = docs['Deployment', 'grafana']['spec']['template']['spec']
                env = {e['name']: e for e in grafana['containers'][0]['env']}
                self.assertEqual(env['GF_SECURITY_ADMIN_PASSWORD']['valueFrom']['secretKeyRef']['name'], 'grafana-admin')
                self.assertEqual(env['INFLUXDB_READ_TOKEN']['valueFrom']['secretKeyRef']['name'], 'grafana-influxdb')
                self.assertEqual(env['GF_AUTH_ANONYMOUS_ENABLED']['value'], 'false')
                self.assertEqual((env['GF_DATABASE_WAL']['value'], env['GF_DATABASE_TRANSACTION_RETRIES']['value']),
                                 ('true', '20'))
                self.assertNotIn('persistentVolumeClaim', str(grafana['volumes']))

    def test_provisioning(self):
        docs = self.render()
        provisioning = docs['ConfigMap', 'grafana-provisioning']['data']
        source = yaml.safe_load(provisioning['datasource.yaml'])['datasources'][0]
        self.assertEqual((source['uid'], source['url'], source['jsonData']['version']),
                         ('influxdb', 'http://influxdb:8086', 'Flux'))
        self.assertEqual(source['secureJsonData']['token'], '$INFLUXDB_READ_TOKEN')
        dashboard = json.loads(docs['ConfigMap', 'grafana-dashboards']['data']['pumps.json'])
        queries = []

        def walk(panels):
            for panel in panels:
                walk(panel.get('panels', []))
                for target in panel.get('targets', []):
                    self.assertEqual(target['datasource']['uid'], 'influxdb')
                    queries.append(target['query'])
        walk(dashboard['panels'])
        self.assertGreater(len(queries), 10)
        self.assertTrue(all('from(bucket: "pumps")' in q for q in queries))
        # InfluxDB can push a regex tag filter down to storage, but not contains().
        pump, score = 'r.asset =~ /^${pump:regex}$/', 'r.asset =~ /^(${pump:regex})Score$/'
        self.assertTrue(all((pump in q or score in q) and 'contains(' not in q for q in queries))
        self.assertTrue(any(score in q and '"AnomalyScore"' in q for q in queries))
        self.assertTrue(all('-7d' not in q for q in queries))
        titles = [p['title'] for p in dashboard['panels']]
        for title in ('Data age', 'Scenario (ground truth)', 'Severity (ground truth)', 'Condition monitoring',
                      'Score age', 'Anomaly score and severity'):
            self.assertIn(title, titles)

    def test_accepts_fleet_global_values(self):
        # Fleet adds global.fleet.clusterLabels to every chart's values.
        self.render({'global.fleet.clusterLabels.name': 'edge'})

    def test_nodeport_option(self):
        service = self.render({'grafana.service.type': 'NodePort', 'grafana.service.nodePort': 31083})
        self.assertEqual(service['Service', 'grafana']['spec']['ports'][0]['nodePort'], 31083)
        self.assertEqual(service['Service', 'influxdb']['spec']['type'], 'ClusterIP')

    def test_dashboard_change_replaces_only_grafana(self):
        with tempfile.TemporaryDirectory() as tmp:
            chart = Path(tmp) / 'chart'
            shutil.copytree(SINK, chart)
            dashboard = chart / 'files/pumps.json'
            dashboard.write_text(dashboard.read_text().replace('"title": "Pumps"', '"title": "Pumps 2"'))
            before, after = self.render(), self.render(chart=str(chart))
        self.assertEqual(before['Deployment', 'influxdb'], after['Deployment', 'influxdb'])
        self.assertEqual(before['Deployment', 'pump-simulator'], after['Deployment', 'pump-simulator'])
        self.assertNotEqual(before['Deployment', 'grafana']['spec']['template']['metadata']['annotations'],
                            after['Deployment', 'grafana']['spec']['template']['metadata']['annotations'])

    def test_invalid_values(self):
        invalid = [({'influxdb.image.digest': ''}, 'digest'),
                   ({'grafana.image.digest': 'latest'}, 'digest'),
                   ({'influxdb.adminSecret': ''}, 'adminSecret'),
                   ({'grafana.tokenSecret': ''}, 'tokenSecret'),
                   ({'influxdb.retention': 'forever'}, 'retention'),
                   ({'grafana.service.type': 'LoadBalancer'}, 'type'),
                   ({'persistence.enabled': 'true'}, 'persistence')]
        for values, diagnostic in invalid:
            with self.subTest(values=values):
                result = command('template', values, chart=SINK, release='pump-demo')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)



if __name__ == '__main__':
    unittest.main()
