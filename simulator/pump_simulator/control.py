"""Show and change the simulated pumps over the secure OPC UA endpoint.

Run it in the simulator pod, for example:
  kubectl exec deploy/pump-simulator -- pump-control show
  kubectl exec deploy/pump-simulator -- pump-control set Pump2 --scenario bearing-wear --severity 0.8
"""
import argparse
import asyncio
import os
from pathlib import Path
import sys

from . import model

PUMPS = ('Pump1', 'Pump2', 'Pump3')
COLUMNS = (('Running', 'running', '{}'), ('SpeedSetpoint', 'setpoint', '{:.0f}'), ('Speed', 'rpm', '{:.0f}'),
           ('Flow', 'm3/h', '{:.1f}'), ('DischargePressure', 'bar', '{:.2f}'),
           ('MotorPower', 'kW', '{:.1f}'), ('BearingVibration', 'mm/s', '{:.2f}'),
           ('BearingTemperature', 'bearing C', '{:.1f}'), ('Scenario', 'scenario', '{}'),
           ('Severity', 'severity', '{:.2f}'))


class Failure(Exception):
    pass


def readable(path, item):
    if not path.is_file() or not os.access(path, os.R_OK):
        raise Failure(f'missing or unreadable {item}: {path}')
    return str(path)


def requested(args):
    """Validate the requested controls with the simulator's rules before connecting."""
    values = {}
    try:
        if args.scenario is not None:
            values['Scenario'] = model.check_scenario(args.scenario)
        if args.severity is not None:
            values['Severity'] = model.check_severity(args.severity)
        if args.setpoint is not None:
            values['SpeedSetpoint'] = model.check_setpoint(args.setpoint)
    except ValueError as error:
        raise Failure(f'invalid value: {error}; nothing changed') from None
    if not values:
        raise Failure('set needs --scenario, --severity or --setpoint')
    return values


async def connect(args):
    from asyncua import Client, ua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

    certificate = readable(args.cert_dir / 'pump-operator.der', 'operator certificate')
    key = readable(args.cert_dir / 'pump-operator.pem', 'operator key')
    server = readable(args.server_cert, 'server certificate')
    client = Client(args.url, timeout=10)
    client.application_uri = 'urn:fledge-demo:pump-operator'
    await client.set_security(SecurityPolicyBasic256Sha256, certificate, key, server_certificate=server,
                              mode=ua.MessageSecurityMode.SignAndEncrypt)
    try:
        await client.connect()
    except ua.UaStatusCodeError as error:
        raise Failure(f'server rejected the connection: {type(error).__name__}') from None
    except (OSError, asyncio.TimeoutError, ConnectionError):
        # A server whose certificate differs from --server-cert cannot decrypt the
        # request and drops the connection, which looks the same as no server.
        raise Failure(f'cannot connect to {args.url}: the server is not running, or its certificate '
                      f'differs from {args.server_cert}') from None
    return client


async def read(client, pump):
    from asyncua import ua

    names = [c[0] for c in COLUMNS]
    nodes = [client.get_node(ua.NodeId(f'{pump}.{name}', 2)) for name in names]
    return dict(zip(names, await client.read_values(nodes)))


async def write(client, pump, values):
    """Write the controls in one request; report each rejection with the server's status."""
    from asyncua import ua

    nodes = [client.get_node(ua.NodeId(f'{pump}.{name}', 2)) for name in values]
    variants = [ua.Variant(v, ua.VariantType.String if isinstance(v, str) else ua.VariantType.Double)
                for v in values.values()]
    try:
        results = await client.write_values(nodes, variants, raise_on_partial_error=False)
    except ua.uaerrors.BadUserAccessDenied:
        # Clients that the demo CA did not sign have read-only sessions.
        raise Failure(f'{pump} not changed: the server allows writes only from a certificate that the '
                      'demo CA signed') from None
    rejected = [f'{name}: {status.name}' for name, status in zip(values, results) if not status.is_good()]
    if rejected:
        raise Failure(f'{pump} rejected by the server: ' + ', '.join(rejected))


def table(rows):
    header = ['pump'] + [c[1] for c in COLUMNS]
    lines = [header] + [[pump] + [fmt.format(row[name]) for name, _, fmt in COLUMNS] for pump, row in rows.items()]
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    return '\n'.join('  '.join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines)


async def run(args):
    values = requested(args) if args.command == 'set' else None
    client = await connect(args)
    try:
        if values:
            await write(client, args.pump, values)
        pumps = [args.pump] if values else PUMPS
        print(table({pump: await read(client, pump) for pump in pumps}))
    finally:
        await client.disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pump-control', description=__doc__.splitlines()[0],
                                     epilog='Scenarios: ' + ', '.join(model.SCENARIOS))
    parser.add_argument('--url', default=os.environ.get('PUMP_CONTROL_URL', 'opc.tcp://localhost:4840'))
    parser.add_argument('--cert-dir', type=Path,
                        default=Path(os.environ.get('PUMP_CONTROL_CERT_DIR', '/etc/pump-simulator/operator')),
                        help='directory with pump-operator.der and pump-operator.pem')
    parser.add_argument('--server-cert', type=Path, default=Path(os.environ.get(
        'PUMP_CONTROL_SERVER_CERT', '/etc/pump-simulator/server/pump-simulator.der')))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help="show the pumps' controls and main values")
    change = commands.add_parser('set', help="change one pump's controls",
                                 epilog='Scenarios: ' + ', '.join(model.SCENARIOS))
    change.add_argument('pump', choices=PUMPS)
    change.add_argument('--scenario')
    change.add_argument('--severity', type=float)
    change.add_argument('--setpoint', type=float, help=f'rpm, 0 stops the pump, at most {model.MAX_SPEED:g}')
    args = parser.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.CRITICAL)  # asyncua logs connection details at warning level
    try:
        asyncio.run(run(args))
    except Failure as error:
        print(f'pump-control: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
