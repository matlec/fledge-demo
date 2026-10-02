"""Local integration tests for the pump simulator's OPC UA server.

Creates two certificate sets with scripts/make-opcua-certs.sh in a temporary
directory (the second one plays a foreign CA), starts the server in a
subprocess and connects with asyncua clients. Requires asyncua, bash and openssl.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'simulator'))
from pump_simulator.model import MEASURED  # noqa: E402

PUMPS = ('Pump1', 'Pump2', 'Pump3')
VARIABLES = {
    'SpeedSetpoint': (ua.VariantType.Double, True), 'Scenario': (ua.VariantType.String, True),
    'Severity': (ua.VariantType.Double, True), 'Running': (ua.VariantType.Boolean, False),
    **{name: (ua.VariantType.Double, False) for name in MEASURED},
}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(('127.0.0.1', port)) == 0


def start_server(cert_dir, port):
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'simulator')}
    return subprocess.Popen([sys.executable, '-m', 'pump_simulator.server', '--cert-dir', str(cert_dir),
                             '--port', str(port), '--seed', '1'],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def make_certs(directory):
    subprocess.run(['bash', str(ROOT / 'scripts/make-opcua-certs.sh'), str(directory)],
                   check=True, stdout=subprocess.DEVNULL)


def make_self_signed(directory, name, expired=False):
    """A client certificate as typical OPC UA clients create it: self-signed, URI urn:fledge-demo:<name>."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = (now - datetime.timedelta(days=30), now - datetime.timedelta(days=1)) if expired else \
        (now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=1))
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(start).not_valid_after(end)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.UniformResourceIdentifier(f'urn:fledge-demo:{name}'), x509.DNSName('localhost')]),
                       critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True,
                                                key_encipherment=True, data_encipherment=True,
                                                key_agreement=False, key_cert_sign=False, crl_sign=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                   .sign(key, hashes.SHA256()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{name}.der').write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    (directory / f'{name}.pem').write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class NoSessionCertificateClient(Client):
    """Omits the client certificate from CreateSession, which asyncua then does not validate."""

    async def create_session(self):
        send = self.uaclient.create_session

        async def without_certificate(params):
            params.ClientCertificate = None
            return await send(params)

        self.uaclient.create_session = without_certificate
        try:
            return await super().create_session()
        finally:
            self.uaclient.create_session = send


class ServerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='pump-server-test-')
        cls.certs = Path(cls.tmp.name, 'a')
        cls.foreign = Path(cls.tmp.name, 'b')
        make_certs(cls.certs)
        make_certs(cls.foreign)
        cls.own = Path(cls.tmp.name, 'own')
        make_self_signed(cls.own, 'self-signed')
        make_self_signed(cls.own, 'expired', expired=True)
        cls.port = free_port()
        cls.url = f'opc.tcp://localhost:{cls.port}'
        cls.server = start_server(cls.certs, cls.port)
        deadline = time.monotonic() + 30
        while not port_open(cls.port):
            if cls.server.poll() is not None or time.monotonic() > deadline:
                cls.server.kill()
                raise AssertionError('server did not start: ' + cls.server.communicate()[0])
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.send_signal(signal.SIGTERM)
        try:
            output, _ = cls.server.communicate(timeout=10)
            code = cls.server.returncode
        except subprocess.TimeoutExpired:
            cls.server.kill()
            output, code = cls.server.communicate()[0], 'no exit within 10 s'
        finally:
            cls.tmp.cleanup()
        assert code == 0, output
        assert 'PRIVATE KEY' not in output
        assert 'read-only session for untrusted client urn:fledge-demo:self-signed' in output, output

    async def client(self, cert_dir=None, name='pump-operator', cls=Client):
        cert_dir = cert_dir or self.certs
        client = cls(self.url, timeout=5)
        client.application_uri = f'urn:fledge-demo:{name}'
        await client.set_security(SecurityPolicyBasic256Sha256, str(cert_dir / f'{name}.der'),
                                  str(cert_dir / f'{name}.pem'),
                                  server_certificate=str(self.certs / 'pump-simulator.der'),
                                  mode=ua.MessageSecurityMode.SignAndEncrypt)
        return client

    async def node(self, client, pump, variable):
        return client.get_node(ua.NodeId(f'{pump}.{variable}', 2))

    async def test_browse_pump_fleet(self):
        async with await self.client() as client:
            objects = await client.nodes.objects.get_children()
            names = [await o.read_browse_name() for o in objects]
            self.assertEqual(sorted(n.Name for n in names if n.NamespaceIndex == 2), list(PUMPS))
            for pump in PUMPS:
                obj = client.get_node(ua.NodeId(pump, 2))
                children = {}
                for child in await obj.get_children():
                    children[(await child.read_browse_name()).Name] = child
                self.assertEqual(set(children), set(VARIABLES), pump)
                for name, (vtype, writable) in VARIABLES.items():
                    node = children[name]
                    self.assertEqual(node.nodeid, ua.NodeId(f'{pump}.{name}', 2))
                    self.assertEqual(await node.read_node_class(), ua.NodeClass.Variable)
                    self.assertEqual(await node.read_data_type_as_variant_type(), vtype, name)
                    access = (await node.read_attribute(ua.AttributeIds.UserAccessLevel)).Value.Value
                    self.assertTrue(access & ua.AccessLevel.CurrentRead.mask, name)
                    self.assertEqual(bool(access & ua.AccessLevel.CurrentWrite.mask), writable, name)
                    self.assertEqual(await node.get_children(), [], name)  # no properties below
            self.assertEqual(await (await self.node(client, 'Pump2', 'Scenario')).read_value(), 'normal')
            self.assertEqual(await (await self.node(client, 'Pump2', 'SpeedSetpoint')).read_value(), 1200.0)

    async def test_step_timestamps(self):
        # Every step republishes the controls and Running with the measured values.
        STEP = list(MEASURED) + ['SpeedSetpoint', 'Scenario', 'Severity', 'Running']
        received = []

        class Handler:
            def datachange_notification(self, node, value, data):
                received.append((node.nodeid.Identifier, data.monitored_item.Value.SourceTimestamp,
                                 datetime.now(timezone.utc)))

        async with await self.client() as client:
            subscription = await client.create_subscription(200, Handler())
            nodes = [await self.node(client, 'Pump1', name) for name in STEP]
            # Like the Fledge source, request notifications on new timestamps: by
            # default a rounded value equal to the previous step's is not reported.
            # subscribe_data_change() takes no filter, hence the private helper.
            timestamps = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
            await subscription._subscribe(nodes, mfilter=timestamps)
            await asyncio.sleep(10.5)
            await subscription.delete()
        steps = defaultdict(set)
        for identifier, source, arrival in received:
            steps[source].add(identifier.split('.')[1])
            self.assertLess(abs((arrival - source).total_seconds()), 1.0)
        counts = defaultdict(int)
        for identifier, _, _ in received:
            counts[identifier] += 1
        for name in STEP:
            self.assertGreaterEqual(counts[f'Pump1.{name}'], 9, name)
            self.assertLessEqual(counts[f'Pump1.{name}'], 12, name)
        # Subscribing or deleting the subscription during a step's writes can cut
        # the first or last step; every step in between is complete.
        inner = sorted(steps)[1:-1]
        self.assertGreaterEqual(len(inner), 8)
        self.assertEqual([(ts, sorted(set(STEP) - steps[ts])) for ts in inner if steps[ts] != set(STEP)], [])

    async def test_control_writes(self):
        async with await self.client() as client:
            scenario = await self.node(client, 'Pump3', 'Scenario')
            severity = await self.node(client, 'Pump3', 'Severity')
            setpoint = await self.node(client, 'Pump3', 'SpeedSetpoint')
            vibration = await self.node(client, 'Pump3', 'BearingVibration')
            invalid = ((scenario, ua.Variant('overheating', ua.VariantType.String)),
                       (severity, ua.Variant(1.5, ua.VariantType.Double)),
                       (severity, ua.Variant(-0.1, ua.VariantType.Double)),
                       (setpoint, ua.Variant(5000.0, ua.VariantType.Double)),
                       (setpoint, ua.Variant(float('nan'), ua.VariantType.Double)))
            for node, variant in invalid:
                with self.assertRaises(ua.uaerrors.BadOutOfRange):
                    await node.write_value(variant)
            with self.assertRaises(ua.uaerrors.BadTypeMismatch):
                await severity.write_value(ua.Variant(0.5, ua.VariantType.Float))
            with self.assertRaises(ua.UaStatusCodeError):
                await vibration.write_value(ua.Variant(0.0, ua.VariantType.Double))
            self.assertEqual((await scenario.read_value(), await severity.read_value(), await setpoint.read_value()),
                             ('normal', 0.0, 1600.0))

            before = await vibration.read_value()
            await scenario.write_value(ua.Variant('bearing-wear', ua.VariantType.String))
            await severity.write_value(ua.Variant(1.0, ua.VariantType.Double))
            await asyncio.sleep(2.2)
            try:
                self.assertEqual(await scenario.read_value(), 'bearing-wear')
                self.assertGreater(await vibration.read_value(), before + 3)
            finally:
                await scenario.write_value(ua.Variant('normal', ua.VariantType.String))
                await severity.write_value(ua.Variant(0.0, ua.VariantType.Double))

            await setpoint.write_value(ua.Variant(0.0, ua.VariantType.Double))
            try:
                await asyncio.sleep(2.2)
                self.assertLess(await (await self.node(client, 'Pump3', 'Speed')).read_value(), 1600 * 0.8)
            finally:
                await setpoint.write_value(ua.Variant(1600.0, ua.VariantType.Double))

    async def test_no_security_endpoint_offered(self):
        client = Client(self.url, timeout=5)
        with self.assertRaisesRegex(ua.UaError, 'No matching endpoints'):
            async with client:
                pass

    async def test_unencrypted_session_rejected(self):
        # asyncua always opens a None channel for discovery; a client that skips
        # its own endpoint check must still get no session and no data.
        client = Client(self.url, timeout=5)
        with patch.object(Client, 'find_endpoint', staticmethod(lambda endpoints, mode, uri: list(endpoints)[0])), \
                self.assertRaises(ua.uaerrors.BadUserAccessDenied):
            async with client:
                await client.get_node(ua.NodeId('Pump1.Speed', 2)).read_value()

    async def assert_read_only(self, client):
        async with client:
            pump = client.get_node(ua.NodeId('Pump1', 2))
            self.assertEqual(len(await pump.get_children()), len(VARIABLES))
            self.assertGreater(await (await self.node(client, 'Pump1', 'Speed')).read_value(), 0)
            changes = asyncio.Queue()

            class Handler:
                def datachange_notification(self, node, value, data):
                    changes.put_nowait(value)

            subscription = await client.create_subscription(200, Handler())
            await subscription.subscribe_data_change(await self.node(client, 'Pump1', 'Speed'))
            await asyncio.wait_for(changes.get(), 5)
            scenario = await self.node(client, 'Pump1', 'Scenario')
            with self.assertRaises(ua.uaerrors.BadUserAccessDenied):
                await scenario.write_value(ua.Variant('cavitation', ua.VariantType.String))
            self.assertEqual(await scenario.read_value(), 'normal')

    async def test_unknown_clients_read_only(self):
        # A typical client's own self-signed certificate, and one from another CA.
        await self.assert_read_only(await self.client(self.own, name='self-signed'))
        await self.assert_read_only(await self.client(self.foreign))

    async def test_untrusted_channel_without_session_certificate_read_only(self):
        # asyncua skips its validator when CreateSession carries no certificate;
        # the role still comes from the channel certificate.
        await self.assert_read_only(await self.client(self.foreign, cls=NoSessionCertificateClient))

    async def test_unusable_client_certificates_rejected(self):
        expired = await self.client(self.own, name='expired')
        with self.assertRaises(ua.UaStatusCodeError):
            async with expired:
                pass
        wrong_uri = await self.client(self.own, name='self-signed')
        wrong_uri.application_uri = 'urn:fledge-demo:someone-else'
        with self.assertRaises(ua.UaStatusCodeError):
            async with wrong_uri:
                pass

    def control(self, *args, cert_dir=None, server_cert=None):
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'simulator')}
        return subprocess.run([sys.executable, '-m', 'pump_simulator.control', '--url', self.url,
                               '--cert-dir', str(cert_dir or self.certs),
                               '--server-cert', str(server_cert or self.certs / 'pump-simulator.der'),
                               *args], env=env, capture_output=True, text=True, timeout=60)

    async def test_control_tool(self):
        shown = self.control('show')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual([line.split()[0] for line in shown.stdout.splitlines()], ['pump', *PUMPS])

        changed = self.control('set', 'Pump2', '--scenario', 'impeller-wear', '--severity', '0.9',
                               '--setpoint', '1400')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        row = changed.stdout.splitlines()[1].split()
        self.assertEqual((row[0], row[2], row[-2], row[-1]), ('Pump2', '1400', 'impeller-wear', '0.90'))
        try:
            await asyncio.sleep(2.2)
            async with await self.client() as client:
                speed = await (await self.node(client, 'Pump2', 'Speed')).read_value()
                flow = await (await self.node(client, 'Pump2', 'Flow')).read_value()
            self.assertGreater(speed, 1210)  # ramping from 1200 towards 1400
            self.assertLess(flow, 100 * speed / 1450 * 0.85)  # impeller wear lowers flow at this speed
        finally:
            restored = self.control('set', 'Pump2', '--scenario', 'normal', '--severity', '0', '--setpoint', '1200')
            self.assertEqual(restored.returncode, 0, restored.stderr)

        for args in (('--severity', '1.5'), ('--scenario', 'overheating'), ('--setpoint', '-1'),
                     ('--scenario', 'cavitation', '--severity', '2')):
            rejected = self.control('set', 'Pump2', *args)
            self.assertEqual(rejected.returncode, 1, args)
            self.assertIn('invalid value', rejected.stderr)
        async with await self.client() as client:
            self.assertEqual(await (await self.node(client, 'Pump2', 'Scenario')).read_value(), 'normal')
            self.assertEqual(await (await self.node(client, 'Pump2', 'Severity')).read_value(), 0.0)

        missing = self.control('show', cert_dir=Path(self.tmp.name, 'none'))
        self.assertEqual(missing.returncode, 1)
        self.assertIn('missing or unreadable operator certificate', missing.stderr)
        untrusted = self.control('show', cert_dir=self.foreign)
        self.assertEqual(untrusted.returncode, 0, untrusted.stderr)
        refused = self.control('set', 'Pump2', '--scenario', 'cavitation', cert_dir=self.foreign)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('allows writes only from a certificate that the demo CA signed', refused.stderr)
        mismatch = self.control('show', server_cert=self.foreign / 'pump-simulator.der')
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn('its certificate differs from', mismatch.stderr)

    async def test_control_reports_server_rejection(self):
        from pump_simulator import control
        async with await self.client() as client:
            with self.assertRaisesRegex(control.Failure, 'Severity: BadOutOfRange'):
                await control.write(client, 'Pump2', {'Severity': 7.0})


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pump-server-startup-')
        self.addCleanup(self.tmp.cleanup)
        self.certs = Path(self.tmp.name, 'a')
        make_certs(self.certs)

    def fails(self, cert_dir, message):
        port = free_port()
        server = start_server(cert_dir, port)
        try:
            output, _ = server.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            output = server.communicate()[0]
        code = server.returncode
        self.assertEqual(code, 1, output)
        self.assertIn(message, output)
        self.assertNotIn('PRIVATE KEY', output)
        self.assertNotIn(self.certs.joinpath('pump-simulator.pem').read_text().splitlines()[1], output)
        self.assertFalse(port_open(port))

    def variant(self, replace):
        """A copy of the certificate directory with files replaced (None removes one)."""
        directory = Path(self.tmp.name, 'variant')
        shutil.rmtree(directory, ignore_errors=True)
        shutil.copytree(self.certs, directory)
        for name, source in replace.items():
            if source is None:
                (directory / name).unlink()
            else:
                shutil.copy(source, directory / name)
        return directory

    def test_missing_items(self):
        self.fails(Path(self.tmp.name, 'empty'), 'missing or unreadable server certificate')
        self.fails(self.variant({'pump-simulator.pem': None}), 'missing or unreadable server key')
        self.fails(self.variant({'demo-ca.der': None}), 'missing or unreadable CA certificate')

    def test_ignores_kubernetes_service_link_variables(self):
        # A Service named pump-simulator defines these in pods with service links.
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'simulator'),
               'PUMP_SIMULATOR_PORT': 'tcp://10.0.0.1:4840', 'PUMP_SIMULATOR_SERVICE_PORT': '4840'}
        result = subprocess.run([sys.executable, '-m', 'pump_simulator.server', '--cert-dir',
                                 str(Path(self.tmp.name, 'empty'))], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('missing or unreadable server certificate', result.stderr)

    def test_unusable_items(self):
        self.fails(self.variant({'pump-simulator.pem': self.certs / 'pump-operator.pem'}),
                   'server key does not match the server certificate')
        foreign = Path(self.tmp.name, 'b')
        make_certs(foreign)
        self.fails(self.variant({'demo-ca.der': foreign / 'demo-ca.der'}), 'not signed by the CA certificate')
        self.fails(self.variant({'pump-simulator.der': self.certs / 'pump-simulator.pem'}), 'unusable server certificate')


if __name__ == '__main__':
    unittest.main()
