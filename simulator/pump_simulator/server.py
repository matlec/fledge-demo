"""Secure OPC UA server for the simulated pump fleet.

The server offers one endpoint: SignAndEncrypt with Basic256Sha256 and anonymous
users. Clients whose certificates the demo CA signed get full access; other
clients with a valid client certificate can browse and read but not write. The
certificate directory holds pump-simulator.der (server certificate),
pump-simulator.pem (its key), demo-ca.der and optionally demo-ca-crl.der.
"""
import argparse
import asyncio
import dataclasses
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import model

NAMESPACE = 'urn:fledge-demo:pumps'
DEFAULT_SETPOINTS = {'Pump1': 1450.0, 'Pump2': 1200.0, 'Pump3': 1600.0}
PERIOD = 1.0  # s
CONTROLS = ('SpeedSetpoint', 'Scenario', 'Severity')
UNITS = {
    'SpeedSetpoint': 'rpm', 'Severity': '0.0 to 1.0', 'Speed': 'rpm', 'Flow': 'm³/h',
    'SuctionPressure': 'bar', 'DischargePressure': 'bar', 'MotorCurrent': 'A', 'MotorPower': 'kW',
    'WindingTemperature': '°C', 'BearingVibration': 'mm/s RMS', 'BearingTemperature': '°C',
}
FILES = {
    'server certificate': 'pump-simulator.der',
    'server key': 'pump-simulator.pem',
    'CA certificate': 'demo-ca.der',
}
CRL_FILE = 'demo-ca-crl.der'

log = logging.getLogger('pump-simulator')


class StartupError(Exception):
    pass


@dataclasses.dataclass
class Credentials:
    certificate: Path
    key: Path
    ca: Path
    crl: Path | None
    application_uri: str


def check_credentials(directory):
    """Check the server's certificate files before serving anything.

    Diagnostics name the missing or unusable item, never file content.
    """
    paths = {item: directory / name for item, name in FILES.items()}
    for item, path in paths.items():
        if not os.access(path, os.R_OK) or not path.is_file():
            raise StartupError(f'missing or unreadable {item}: {path}')
    try:
        certificate = x509.load_der_x509_certificate(paths['server certificate'].read_bytes())
    except ValueError:
        raise StartupError(f'unusable server certificate (DER expected): {paths["server certificate"]}') from None
    try:
        key = serialization.load_pem_private_key(paths['server key'].read_bytes(), password=None)
    except (ValueError, TypeError):
        raise StartupError(f'unusable server key (unencrypted PEM expected): {paths["server key"]}') from None
    try:
        ca = x509.load_der_x509_certificate(paths['CA certificate'].read_bytes())
    except ValueError:
        raise StartupError(f'unusable CA certificate (DER expected): {paths["CA certificate"]}') from None
    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise StartupError('server key does not match the server certificate')
    try:
        ca.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes,
                               padding.PKCS1v15(), certificate.signature_hash_algorithm)
    except Exception:
        raise StartupError('server certificate is not signed by the CA certificate') from None
    try:
        uris = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
            .get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        uris = []
    if len(uris) != 1:
        raise StartupError('server certificate needs exactly one subjectAltName URI (the application URI)')
    crl = directory / CRL_FILE
    return Credentials(paths['server certificate'], paths['server key'], paths['CA certificate'],
                       crl if crl.is_file() else None, uris[0])


def channel_user_manager(trust):
    """Give full access to clients the demo CA signed, read-only access to other clients.

    Roles come from the secure channel's client certificate. asyncua accepts
    sessions on the unencrypted discovery channel, which has no certificate, so a
    session without one is rejected here.
    """
    from asyncua.crypto.permission_rules import User, UserRole

    class ChannelUserManager:
        def get_user(self, iserver, username=None, password=None, certificate=None):
            if username is not None or not certificate:
                return None
            try:
                peer = x509.load_der_x509_certificate(certificate)
            except ValueError:
                return None
            if trust.is_trusted(peer):
                if trust.validate(peer):
                    return User(role=UserRole.User)
                log.warning('rejected session from a revoked or expired client certificate of the demo CA')
                return None
            uri = application_uri(peer)
            log.info('read-only session for untrusted client %s', uri)
            return User(role=UserRole.Anonymous, name=uri)

    return ChannelUserManager()


def application_uri(certificate):
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return next(iter(names.get_values_for_type(x509.UniformResourceIdentifier)), '(no URI)')
    except x509.ExtensionNotFound:
        return '(no URI)'


def read_only_for_unknown_clients():
    """Permission ruleset: the anonymous role may do everything a user may, except write and call."""
    from asyncua import ua
    from asyncua.crypto.permission_rules import ADMIN_TYPES, USER_TYPES, PermissionRuleset, UserRole

    user = {ua.NodeId(t) for t in USER_TYPES}
    blocked = {ua.NodeId(ua.ObjectIds.WriteRequest_Encoding_DefaultBinary),
               ua.NodeId(ua.ObjectIds.CallRequest_Encoding_DefaultBinary)}
    allowed = {UserRole.Admin: user | {ua.NodeId(t) for t in ADMIN_TYPES},
               UserRole.User: user,
               UserRole.Anonymous: user - blocked}

    class ReadOnlyForUnknownClients(PermissionRuleset):
        def check_validity(self, user, action_type_id, body):
            return action_type_id in allowed[user.role]

    return ReadOnlyForUnknownClients()


class Fleet:
    """The pumps, their OPC UA nodes and the control write validation."""

    def __init__(self, seed):
        self.pumps = {name: model.Pump(setpoint, model.fleet_rng(seed, name))
                      for name, setpoint in DEFAULT_SETPOINTS.items()}
        self.nodes = {}  # pump name -> browse name -> node
        self.controls = {}  # node id -> (pump name, browse name)

    async def build(self, server, idx):
        from asyncua import ua

        for name, pump in self.pumps.items():
            obj = await server.nodes.objects.add_object(ua.NodeId(name, idx), ua.QualifiedName(name, idx))
            initial = {
                'SpeedSetpoint': (pump.setpoint, ua.VariantType.Double),
                'Scenario': (pump.scenario, ua.VariantType.String),
                'Severity': (pump.severity, ua.VariantType.Double),
                'Running': (pump.speed > 0, ua.VariantType.Boolean),
                **{m: (0.0, ua.VariantType.Double) for m in model.MEASURED},
            }
            self.nodes[name] = {}
            for browse, (value, vtype) in initial.items():
                node = await obj.add_variable(ua.NodeId(f'{name}.{browse}', idx), ua.QualifiedName(browse, idx),
                                              value, varianttype=vtype)
                if browse in UNITS:
                    await node.write_attribute(ua.AttributeIds.Description,
                                               ua.DataValue(ua.Variant(ua.LocalizedText(UNITS[browse]))))
                if browse in CONTROLS:
                    await node.set_writable()
                    self.controls[node.nodeid] = (name, browse)
                self.nodes[name][browse] = node

    def install_write_validation(self, server):
        """Reject invalid control values with BadOutOfRange and apply valid ones."""
        from asyncua import ua

        service = server.iserver.attribute_service
        original = service.write
        checks = {'SpeedSetpoint': model.check_setpoint, 'Scenario': model.check_scenario,
                  'Severity': model.check_severity}
        setters = {'SpeedSetpoint': 'set_setpoint', 'Scenario': 'set_scenario', 'Severity': 'set_severity'}

        async def write(params, user=None):
            results = []
            for item in params.NodesToWrite:
                target = self.controls.get(item.NodeId) if item.AttributeId == ua.AttributeIds.Value else None
                value = None
                if target is not None:
                    variant = item.Value.Value if item.Value else None
                    try:
                        value = checks[target[1]](variant.Value if variant is not None else None)
                    except (TypeError, ValueError):
                        log.info('rejected %s %s = %r', target[0], target[1],
                                 variant.Value if variant is not None else None)
                        results.append(ua.StatusCode(ua.StatusCodes.BadOutOfRange))
                        continue
                single = ua.WriteParameters(NodesToWrite=[item])
                status = (await (original(single, user=user) if user is not None else original(single)))[0]
                if target is not None and status.is_good():
                    getattr(self.pumps[target[0]], setters[target[1]])(value)
                    log.info('%s %s = %r', target[0], target[1], value)
                results.append(status)
            return results

        service.write = write

    async def step(self, server):
        from asyncua import ua

        now = datetime.now(timezone.utc)
        for name, pump in self.pumps.items():
            values = pump.step(PERIOD)
            nodes = self.nodes[name]
            for browse in model.MEASURED:
                await server.write_attribute_value(nodes[browse].nodeid, ua.DataValue(
                    ua.Variant(round(values[browse], 3), ua.VariantType.Double),
                    SourceTimestamp=now, ServerTimestamp=now))
            # Republish the controls and state every step, so that each step's
            # record carries its own ground truth.
            current = {'SpeedSetpoint': (pump.setpoint, ua.VariantType.Double),
                       'Scenario': (pump.scenario, ua.VariantType.String),
                       'Severity': (pump.severity, ua.VariantType.Double),
                       'Running': (values['Running'], ua.VariantType.Boolean)}
            for browse, (value, vtype) in current.items():
                await server.write_attribute_value(nodes[browse].nodeid, ua.DataValue(
                    ua.Variant(value, vtype), SourceTimestamp=now, ServerTimestamp=now))


async def serve(credentials, port, seed, stop):
    from asyncua import Server, ua
    from asyncua.crypto.truststore import TrustStore
    from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions

    # asyncua's TrustStore loads every *.der/*.pem file of a location.
    with tempfile.TemporaryDirectory(prefix='pump-simulator-') as scratch:
        trusted, revoked = Path(scratch, 'trusted'), Path(scratch, 'crl')
        trusted.mkdir()
        revoked.mkdir()
        (trusted / 'demo-ca.der').write_bytes(credentials.ca.read_bytes())
        if credentials.crl:
            (revoked / CRL_FILE).write_bytes(credentials.crl.read_bytes())
        trust = TrustStore([trusted], [revoked])
        await trust.load()

        server = Server(user_manager=channel_user_manager(trust))
        await server.init()
        server.set_endpoint(f'opc.tcp://0.0.0.0:{port}')
        server.set_server_name('Pump simulator')
        await server.set_application_uri(credentials.application_uri)
        server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt],
                                   permission_ruleset=read_only_for_unknown_clients())
        server.set_identity_tokens([ua.AnonymousIdentityToken])
        await server.load_certificate(str(credentials.certificate))
        await server.load_private_key(str(credentials.key))
        # No trust check here: unknown clients get a read-only session (channel_user_manager).
        server.set_certificate_validator(CertificateValidator(
            CertificateValidatorOptions.EXT_VALIDATION | CertificateValidatorOptions.PEER_CLIENT, trust))

        idx = await server.register_namespace(NAMESPACE)
        if idx != 2:
            raise StartupError(f'namespace index {idx} instead of 2')
        fleet = Fleet(seed)
        await fleet.build(server, idx)
        fleet.install_write_validation(server)
        await fleet.step(server)

        async with server:  # The port opens here, after certificates and address space.
            log.info('serving opc.tcp://0.0.0.0:%d (SignAndEncrypt, Basic256Sha256, anonymous), '
                     'application URI %s, seed %s', port, credentials.application_uri, seed)
            loop = asyncio.get_running_loop()
            deadline = loop.time()
            while not stop.is_set():
                deadline += PERIOD
                if deadline < loop.time() - PERIOD:  # Resume after a stall without a burst.
                    deadline = loop.time()
                try:
                    await asyncio.wait_for(stop.wait(), max(0.0, deadline - loop.time()))
                except asyncio.TimeoutError:
                    await fleet.step(server)
            log.info('stopping')
    log.info('stopped')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--cert-dir', type=Path,
                        default=Path(os.environ.get('PUMP_SIMULATOR_CERT_DIR', '/etc/pump-simulator/server')))
    parser.add_argument('--port', type=int, default=4840)
    parser.add_argument('--seed', default=os.environ.get('PUMP_SIMULATOR_SEED', '0'))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    log.setLevel(logging.INFO)
    logging.getLogger('asyncua.crypto.uacrypto').setLevel(logging.ERROR)  # pod host name is not in the SAN
    try:
        credentials = check_credentials(args.cert_dir)
    except StartupError as error:
        log.error('%s', error)
        return 1

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(credentials, args.port, args.seed, stop)

    try:
        asyncio.run(run())
    except StartupError as error:
        log.error('%s', error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
