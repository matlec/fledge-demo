#!/usr/bin/env python3
"""Read-only verification of the sinusoid or pump demo acquisition, the pump demo's InfluxDB, or its inference.

Admin credentials come from FLEDGE_ADMIN_USERNAME and FLEDGE_ADMIN_PASSWORD,
never command arguments. The InfluxDB check reads InfluxDB with the read-only token
in FLEDGE_INFLUXDB_READ_TOKEN. Each API call is printed once to stderr.
"""
import csv
import io
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def check_pump_readings(readings, datapoints, now, max_age):
    """Return (asset, problem) pairs; an empty list means every asset passed.

    readings maps each pump asset to its latest readings. The newest step that
    carries every measured datapoint under one user timestamp must be fresh.
    """
    problems = []
    for asset, rows in readings.items():
        if not rows:
            problems.append((asset, 'no readings'))
            continue
        steps = defaultdict(set)
        for row in rows:
            steps[row['timestamp']].update(row['reading'])
        complete = [ts for ts, names in steps.items() if set(datapoints) <= names]
        if not complete:
            missing = sorted(set(datapoints) - set().union(*steps.values()))
            problems.append((asset, 'no step with all datapoints; ' + (
                'missing ' + ', '.join(missing) if missing else 'datapoints have different timestamps')))
            continue
        newest = max(complete)
        try:
            stamp = datetime.fromisoformat(newest).replace(tzinfo=timezone.utc)
        except ValueError:
            problems.append((asset, f'unreadable reading timestamp {newest!r}'))
            continue
        age = (now - stamp).total_seconds()
        if age >= max_age:
            problems.append((asset, f'newest complete step is {age:.1f} s old (limit {max_age} s)'))
    return problems


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so that no token is sent to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def parse_time(stamp):
    """Parse Fledge ('YYYY-MM-DD HH:MM:SS.ffffff') and InfluxDB (RFC 3339) timestamps as UTC."""
    moment = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def check_sink_points(points, datapoints, now, max_age):
    """Return (asset, problem) pairs for InfluxDB points; an empty list means every asset passed.

    points maps each asset to its newest points, as dicts of field name to value
    with the point time under '_time'.
    """
    problems = []
    for asset, rows in points.items():
        complete = [row['_time'] for row in rows if all(row.get(name, '') != '' for name in datapoints)]
        if not complete:
            problems.append((asset, 'no point with all fields in the last 2 minutes' if rows else 'no points in the last 2 minutes'))
            continue
        age = (now - max(parse_time(stamp) for stamp in complete)).total_seconds()
        if age >= max_age:
            problems.append((asset, f'newest complete point is {age:.1f} s old (limit {max_age} s)'))
    return problems


def flux_rows(text):
    """Rows of an annotated-less InfluxDB CSV response, as dicts."""
    return [row for row in csv.DictReader(io.StringIO(text)) if row.get('_time')]


def verify_sink(get, name, verification, influx_url, deadline):
    """Check the north service, then each asset's newest points in InfluxDB, and report the lag."""
    token = os.environ.get('FLEDGE_INFLUXDB_READ_TOKEN', '')
    if not token:
        raise RuntimeError('set FLEDGE_INFLUXDB_READ_TOKEN to the read-only InfluxDB token')
    assets, datapoints = verification['assets'], verification['datapoints']
    max_age = verification.get('maxAgeSeconds', 10)
    query_url = influx_url.rstrip('/') + '/api/v2/query?' + urllib.parse.urlencode({'org': verification['org']})
    print('POST /api/v2/query', file=sys.stderr)

    def newest(asset):
        flux = (f'from(bucket: {json.dumps(verification["bucket"])}) |> range(start: -2m) '
                f'|> filter(fn: (r) => r._measurement == {json.dumps(verification["measurement"])} '
                f'and r.asset == {json.dumps(asset)}) '
                '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
                '|> group() |> sort(columns: ["_time"], desc: true) |> limit(n: 5)')
        request = urllib.request.Request(query_url, method='POST', data=json.dumps(
            {'query': flux, 'dialect': {'annotations': [], 'header': True}}).encode(), headers={
            'Authorization': 'Token ' + token, 'Content-Type': 'application/json', 'Accept': 'application/csv'})
        try:
            with OPENER.open(request, timeout=10) as response:
                return flux_rows(response.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'InfluxDB query failed (HTTP {e.code})') from None

    def running():
        schedule = [s for s in get('/schedule')['schedules'] if s['name'] == name]
        return len(schedule) == 1 and schedule[0]['enabled'] and any(
            s['name'] == name and s['status'] == 'running' for s in get('/service')['services'])

    # Wait for fresh points, but report a stopped north service and stale pumps together.
    while True:
        points = {a: newest(a) for a in assets}
        now = datetime.now(timezone.utc)
        problems = check_sink_points(points, datapoints, now, max_age)
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not running():
        problems.append((name, 'north service is not enabled and running'))
    for asset, problem in problems:
        print(f'FAIL {asset}: {problem}', file=sys.stderr)
    for asset in assets:
        if not points[asset]:
            continue
        newest_point = parse_time(points[asset][0]['_time'])
        note = f'newest point {(now - newest_point).total_seconds():.1f} s old'
        rows = get('/asset/' + urllib.parse.quote(asset, safe='') + '?limit=1')
        if rows:
            note += f', InfluxDB lag {max((parse_time(rows[0]["timestamp"]) - newest_point).total_seconds(), 0):.1f} s'
        print(f'INFO {asset}: {note}')
    if problems:
        raise RuntimeError('InfluxDB check failed: ' + ', '.join(sorted({subject for subject, _ in problems})))
    print(f'PASS: north service {name} running; {", ".join(assets)} each have a point with all '
          f'{len(datapoints)} fields in InfluxDB within {max_age} s')


ANOMALY_WINDOW = 60  # seconds of scores whose median --expect-anomalous checks


def check_scores(pumps, now, max_age, expect=None):
    """Return (problems, notes) for the pump scores; no problems means the check passed.

    pumps maps each pump asset to a dict with 'running' (its newest Running value,
    or None without recent readings, which fails), 'newest' (its newest reading's timestamp, or None) and 'scores' (its
    newest score readings as (timestamp, value), newest first). A stopped pump
    gets no scores and is not stale. With expect, that pump's median score over
    the last ANOMALY_WINDOW seconds must be at least 1.0 and every other running
    pump's median below 1.0.
    """
    problems, notes = [], []
    for asset, pump in pumps.items():
        scores = [(parse_time(stamp), value) for stamp, value in pump['scores']]
        if pump['running'] is None:
            problems.append((asset, 'no recent readings with Running; check acquisition with demos/pump.json'))
            continue
        if not pump['running']:
            notes.append((asset, 'stopped; no scores expected'))
            if asset == expect:
                problems.append((asset, 'is stopped, so it has no score to check'))
            continue
        if not scores:
            problems.append((asset, 'no scores'))
            continue
        newest = max(moment for moment, _ in scores)
        age = (now - newest).total_seconds()
        if age >= max_age:
            problems.append((asset, f'newest score is {age:.1f} s old (limit {max_age} s)'))
        if pump['newest']:
            notes.append((asset, f'score age {age:.1f} s, lag behind its readings '
                                 f'{max((parse_time(pump["newest"]) - newest).total_seconds(), 0):.1f} s'))
        if expect:
            recent = sorted(value for moment, value in scores if (now - moment).total_seconds() <= ANOMALY_WINDOW)
            if not recent:
                problems.append((asset, f'no scores in the last {ANOMALY_WINDOW} s'))
                continue
            median = recent[len(recent) // 2] if len(recent) % 2 else (recent[len(recent) // 2 - 1] + recent[len(recent) // 2]) / 2
            if asset == expect and median < 1.0:
                problems.append((asset, f'median score {median:.2f} over {ANOMALY_WINDOW} s is below 1.0'))
            elif asset != expect and median >= 1.0:
                problems.append((asset, f'median score {median:.2f} over {ANOMALY_WINDOW} s is not below 1.0'))
            else:
                notes.append((asset, f'median score {median:.2f} over {ANOMALY_WINDOW} s'))
    if expect and expect not in pumps:
        problems.append((expect, 'is not one of the checked pumps'))
    return problems, notes


def verify_inference(get, name, verification, deadline, expect=None):
    """Check both services, then each running pump's newest score readings in Fledge."""
    assets, suffix = verification['assets'], verification.get('scoreSuffix', 'Score')
    max_age = verification.get('maxAgeSeconds', 30)
    services = [name, verification['ingest']]

    def asset(path_asset, limit):
        return get('/asset/' + urllib.parse.quote(path_asset, safe='') + f'?limit={limit}')

    def snapshot():
        pumps = {}
        for pump in assets:
            readings = asset(pump, 40)  # about three steps of 13 one-datapoint readings
            running = next((r['reading']['Running'] for r in readings if 'Running' in r['reading']), None)
            scores = [(r['timestamp'], r['reading']['AnomalyScore']) for r in asset(pump + suffix, ANOMALY_WINDOW + 30)
                      if isinstance(r['reading'].get('AnomalyScore'), (int, float))]
            pumps[pump] = {'running': running, 'newest': readings[0]['timestamp'] if readings else None,
                           'scores': scores}
        return pumps

    def stopped():
        schedules = {s['name']: s['enabled'] for s in get('/schedule')['schedules']}
        statuses = {s['name']: s['status'] for s in get('/service')['services']}
        return [s for s in services if not schedules.get(s) or statuses.get(s) != 'running']

    while True:
        problems, notes = check_scores(snapshot(), datetime.now(timezone.utc), max_age, expect)
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(2)
    problems += [(service, 'service is not enabled and running') for service in stopped()]
    for subject, note in notes:
        print(f'INFO {subject}: {note}')
    for subject, problem in problems:
        print(f'FAIL {subject}: {problem}', file=sys.stderr)
    if problems:
        raise RuntimeError('inference check failed: ' + ', '.join(sorted({subject for subject, _ in problems})))
    print(f'PASS: services {", ".join(services)} running; every running pump has a score within {max_age} s'
          + (f'; {expect} is anomalous and the other running pumps are not' if expect else ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', required=True, help='Private API URL, e.g. http://127.0.0.1:8081')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'demos/sinusoid.json')
    parser.add_argument('--snapshot', type=Path, help='Write three exact readings and source settings')
    parser.add_argument('--compare', type=Path, help='Verify retained readings/settings and observe additional readings')
    parser.add_argument('--influxdb', default='http://127.0.0.1:8086',
                        help='InfluxDB URL for the InfluxDB check, e.g. through a port-forward')
    parser.add_argument('--expect-anomalous', metavar='PUMP',
                        help='inference check: require this pump to score as anomalous and the others not')
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    args.source = cfg['verification']['source']
    matches = [s for s in cfg['sources'] if s['name'] == args.source]
    if len(matches) != 1:
        raise RuntimeError('verification source must name exactly one configured source')
    source = matches[0]
    kind = {'sinusoid': 'sinusoid', 's2opcua': 'pump', 'influxdb': 'sink',
            'pump_inference': 'inference'}.get(source['plugin'])
    pump = kind == 'pump'
    if kind is None or kind != 'sinusoid' and 'assets' not in cfg['verification']:
        raise RuntimeError('this verifier supports the sinusoid, pump, InfluxDB and inference declarations')
    if kind != 'sinusoid' and (args.snapshot or args.compare):
        raise RuntimeError('--snapshot and --compare apply to the sinusoid demo')
    if kind != 'inference' and args.expect_anomalous:
        raise RuntimeError('--expect-anomalous applies to the inference declaration')
    version = source['version']
    base = args.api.rstrip('/') + '/fledge'
    shown = set()
    def request(method, path, data=None, token=None):
        if (method, path) not in shown:  # Show each Fledge API call once; never bodies.
            shown.add((method, path))
            print(method, '/fledge' + path, file=sys.stderr)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['authorization'] = token
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
        try:
            with OPENER.open(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None
    username = os.environ.get('FLEDGE_ADMIN_USERNAME', '')
    password = os.environ.get('FLEDGE_ADMIN_PASSWORD', '')
    if not username or not password:
        raise RuntimeError('set FLEDGE_ADMIN_USERNAME and FLEDGE_ADMIN_PASSWORD from the admin Secret')
    status, login = request('POST', '/login', {'username': username, 'password': password})
    if status != 200:
        raise RuntimeError(f'managed login failed (HTTP {status})')
    token = login['token']
    def get(path):
        code, data = request('GET', path, token=token)
        if code != 200:
            raise RuntimeError(f'API read failed (HTTP {code})')
        return data
    if request('GET', '/service')[0] not in (401, 403):
        raise RuntimeError('unauthenticated API access was not rejected')
    if request('POST', '/login', {'username': 'admin', 'password': 'fledge'})[0] not in (401, 403, 404):
        raise RuntimeError('default credentials were not rejected')
    plugins = [p for p in get('/plugins/installed')['plugins'] if p['name'] == source['plugin'] and p['type'] == source['type']]
    if len(plugins) != 1 or plugins[0]['version'] != version:
        raise RuntimeError(f'pinned {source["plugin"]} inventory mismatch')
    deadline = time.monotonic() + 120
    if kind == 'sink':
        return verify_sink(get, args.source, cfg['verification'], args.influxdb, deadline)
    if kind == 'inference':
        return verify_inference(get, args.source, cfg['verification'], deadline, args.expect_anomalous)
    schedule = [s for s in get('/schedule')['schedules'] if s['name'] == args.source]
    if len(schedule) != 1 or not schedule[0]['enabled']:
        raise RuntimeError('expected exactly one enabled source')
    while time.monotonic() < deadline:
        service = [s for s in get('/service')['services'] if s['name'] == args.source]
        if len(service) == 1 and service[0]['status'] == 'running':
            break
        time.sleep(1)
    else:
        raise RuntimeError('source is not running within the acquisition deadline')
    if pump:
        return verify_pump(get, args.source, cfg['verification'], deadline)
    args.asset = cfg['verification']['asset']
    category = get('/category/' + urllib.parse.quote(args.source, safe=''))
    if category['plugin']['value'] != 'sinusoid' or category['assetName']['value'] != args.asset:
        raise RuntimeError('source plugin/asset mismatch')
    config = {k: v['value'] for k, v in category.items()}
    path = '/asset/' + urllib.parse.quote(args.asset, safe='')
    previous = json.loads(args.compare.read_text()) if args.compare else None
    if previous and (previous['source'] != args.source or previous['asset'] != args.asset or previous['config'] != config):
        raise RuntimeError('snapshot source/settings mismatch')
    if previous:
        verify_retained(get, path, previous['readings'])
    # Verify new acquisition, not just three historical rows or a green ping.
    initial = get(path + '?limit=3')
    after = max((r['timestamp'] for r in initial), default='')
    while time.monotonic() < deadline:
        rows = get(path + '?limit=3')
        if len(rows) == 3 and all(r['timestamp'] > after for r in rows):
            break
        time.sleep(2)
    else:
        raise RuntimeError('fewer than three new acquired readings within 120 seconds')
    result = {'source': args.source, 'asset': args.asset, 'pluginVersion': version, 'config': config, 'readings': rows}
    if args.snapshot:
        args.snapshot.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if previous:
        print('PASS: exact stored readings and source settings retained; new acquisition observed')
    else:
        print('PASS: authentication, source identity and three new acquired readings')


def verify_retained(get, path, recorded):
    """Check that the recorded readings are still stored, exactly.

    One bounded request, however many readings are retained: a window of user
    timestamps around the recorded readings. Its end is one second past the
    newest one, because Fledge doesn't match the end exactly.
    """
    wanted = {json.dumps(r, sort_keys=True) for r in recorded}
    stamps = [parse_time(r['timestamp']) for r in recorded]
    end = max(stamps) + timedelta(seconds=1)
    span = int((end - min(stamps)).total_seconds()) + 2
    rows = get(path + '?' + urllib.parse.urlencode({'mostrecent': 'true', 'seconds': span,
                                                    'previous_ts': end.strftime('%Y-%m-%d %H:%M:%S.%f')}))
    wanted.difference_update(json.dumps(r, sort_keys=True) for r in rows)
    if wanted:
        raise RuntimeError('recorded readings missing or expired')


def verify_pump(get, name, verification, deadline):
    """Check the latest readings of each pump asset; bounded to one page per asset."""
    assets, datapoints = verification['assets'], verification['datapoints']
    max_age = verification.get('maxAgeSeconds', 10)
    limit = 3 * len(datapoints) + 10  # about three steps of one reading per datapoint
    while True:
        readings = {a: get('/asset/' + urllib.parse.quote(a, safe='') + f'?limit={limit}') for a in assets}
        problems = check_pump_readings(readings, datapoints, datetime.now(timezone.utc), max_age)
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(2)
    for asset, problem in problems:
        print(f'FAIL {asset}: {problem}', file=sys.stderr)
    if problems:
        raise RuntimeError(f'pump acquisition check failed for {len(problems)} of {len(assets)} assets')
    print(f'PASS: source {name} running; {", ".join(assets)} each have all {len(datapoints)} '
          f'datapoints in one step within {max_age} s')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        print('FAIL: ' + (str(e) if isinstance(e, RuntimeError) else 'API or snapshot unavailable/invalid'), file=sys.stderr)
        sys.exit(1)
