# -*- coding: utf-8 -*-

"""Fledge north plugin that scores pump readings with an anomaly model.

It merges each pump's one-datapoint readings into steps, keeps a window of the
latest consecutive steps per pump, and sends the windows to a KServe v2
inference endpoint. Each score is posted as a reading of a separate asset (for
example Pump1Score) to a local HTTP south service, so Fledge stores it like any
other reading. A block counts as sent only after the endpoint and the ingest
accepted it; otherwise the north service keeps the block and sends it again.
"""

import copy
import datetime
import json
import logging
import math
import time
import urllib.error
import urllib.request

from fledge.common import logger

__license__ = "Apache 2.0"
__version__ = "1.0.2"

# The model's input order: the nine measured values of one step.
MEASURED = ('Speed', 'Flow', 'SuctionPressure', 'DischargePressure', 'MotorCurrent',
            'MotorPower', 'WindingTemperature', 'BearingVibration', 'BearingTemperature')
RUNNING = 'Running'
MAX_STEP_GAP = 1.5  # seconds between consecutive steps of one window
PENDING_SECONDS = 10  # an incomplete step older than this, relative to the pump's newest step, is dropped

_DEFAULT_CONFIG = {
    'plugin': {
        'description': 'Score pump readings with an anomaly model and ingest the scores',
        'type': 'string',
        'default': 'pump_inference',
        'readonly': 'true'
    },
    'assets': {
        'description': 'Comma-separated pump assets to score; other assets are ignored',
        'type': 'string',
        'default': 'Pump1,Pump2,Pump3',
        'order': '1',
        'displayName': 'Pump assets',
        'mandatory': 'true'
    },
    'engineUrl': {
        'description': 'Base URL of the KServe v2 inference endpoint',
        'type': 'string',
        'default': 'http://127.0.0.1:8001',
        'order': '2',
        'displayName': 'Inference endpoint',
        'mandatory': 'true'
    },
    'model': {
        'description': 'Model name on the inference endpoint',
        'type': 'string',
        'default': 'pump-anomaly',
        'order': '3',
        'displayName': 'Model',
        'mandatory': 'true'
    },
    'version': {
        'description': 'Model version on the inference endpoint',
        'type': 'string',
        'default': '1',
        'order': '4',
        'displayName': 'Model version',
        'mandatory': 'true'
    },
    'window': {
        'description': 'Steps per window; must match the model',
        'type': 'integer',
        'default': '10',
        'minimum': '2',
        'maximum': '300',
        'order': '5',
        'displayName': 'Window'
    },
    'maxBatch': {
        'description': 'Most windows per inference request',
        'type': 'integer',
        'default': '64',
        'minimum': '1',
        'maximum': '1024',
        'order': '6',
        'displayName': 'Maximum batch'
    },
    'ingestUrl': {
        'description': 'URL of the HTTP south service that ingests the scores',
        'type': 'string',
        'default': 'http://127.0.0.1:6683/scores',
        'order': '7',
        'displayName': 'Score ingest URL',
        'mandatory': 'true'
    },
    'scoreSuffix': {
        'description': 'Appended to the pump asset to name its score asset',
        'type': 'string',
        'default': 'Score',
        'order': '8',
        'displayName': 'Score asset suffix',
        'mandatory': 'true'
    },
    'timeout': {
        'description': 'Request timeout in seconds',
        'type': 'integer',
        'default': '10',
        'minimum': '1',
        'maximum': '120',
        'order': '9',
        'displayName': 'Timeout'
    },
    'source': {
        'description': 'Readings are the only supported source',
        'type': 'enumeration',
        'options': ['readings'],
        'default': 'readings',
        'order': '10',
        'displayName': 'Source'
    }
}

_LOGGER = logger.setup(__name__, level=logging.INFO)
# The north service retries a failed block about once a second; log a repeated
# failure at most this often.
_FAILURE_LOG_INTERVAL = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def plugin_info():
    return {
        'name': 'pump_inference',
        'version': __version__,
        'type': 'north',
        'interface': '1.0',
        'config': _DEFAULT_CONFIG
    }


def plugin_init(config):
    handle = copy.deepcopy(config)
    handle['_failures'] = _Failures()
    # Shared by every handle that keeps the windows, so that a block still in
    # flight during a reconfiguration commits its state where the next block reads.
    handle['_store'] = {'state': {}}
    return handle


def plugin_reconfigure(handle, new_config):
    new_handle = copy.deepcopy(new_config)
    new_handle['_failures'] = handle['_failures']
    # A window holds raw steps, which suit any model. Only a different window
    # length or a different set of pumps invalidates the collected windows.
    same = all(new_config[key]['value'] == handle[key]['value'] for key in ('window', 'assets'))
    new_handle['_store'] = handle['_store'] if same else {'state': {}}
    return new_handle


def plugin_shutdown(handle):
    pass


async def plugin_send(handle, payload, stream_id):
    """Score one block. Returns (sent, last id, count); the service uses the count."""
    last_id = payload[-1]['id'] if payload else 0
    settings = _Settings(handle)
    store = handle['_store']
    state = copy.deepcopy(store['state'])
    windows = collect(state, payload, settings)
    try:
        scores = []
        for start in range(0, len(windows), settings.max_batch):
            batch = windows[start:start + settings.max_batch]
            values = infer(settings, [w for _, _, w in batch])
            scores += [(asset, stamp, value) for (asset, stamp, _), value in zip(batch, values)]
        if scores:
            ingest(settings, scores)
    except _Failure as error:
        handle['_failures'].failed(str(error))
        return False, 0, 0
    handle['_failures'].succeeded()
    store['state'] = state
    return True, last_id, len(payload)


class _Settings:
    def __init__(self, handle):
        self.assets = [a.strip() for a in handle['assets']['value'].split(',') if a.strip()]
        self.engine = handle['engineUrl']['value'].rstrip('/')
        self.model = handle['model']['value']
        self.version = str(handle['version']['value'])
        self.window = int(handle['window']['value'])
        self.max_batch = int(handle['maxBatch']['value'])
        self.ingest = handle['ingestUrl']['value']
        self.suffix = handle['scoreSuffix']['value']
        self.timeout = int(handle['timeout']['value'])


def collect(state, payload, settings):
    """Add a block's readings to the per-pump state; return the windows to score.

    State per pump: 'pending' maps a timestamp to the datapoints seen so far,
    'window' holds (moment, values) of the latest consecutive running steps, and
    'last' is the moment of the newest step already used. Returns a list of
    (asset, timestamp, window values) in timestamp order per pump.
    """
    unreadable = 0
    for reading in payload:
        asset = reading['asset_code']
        if asset not in settings.assets:
            continue
        if _moment(reading['user_ts']) is None:
            unreadable += 1
            continue
        pump = state.setdefault(asset, {'pending': {}, 'window': [], 'last': None})
        pump['pending'].setdefault(reading['user_ts'], {}).update(reading['reading'])
    if unreadable:
        _LOGGER.warning('Ignored %d readings with an unreadable timestamp', unreadable)

    windows = []
    for asset in settings.assets:
        pump = state.get(asset)
        if not pump or not pump['pending']:
            continue
        steps = sorted(((_moment(stamp), stamp, data) for stamp, data in pump['pending'].items()),
                       key=lambda step: step[0])
        newest = steps[-1][0]
        newest_complete = max((m for m, _, data in steps if _complete(data)), default=None)
        pending = {}
        for moment, stamp, data in steps:
            if pump['last'] is not None and moment <= pump['last']:
                continue  # already used, for example from a retried block
            if not _complete(data):
                if (newest_complete is None or moment > newest_complete) \
                        and (newest - moment).total_seconds() <= PENDING_SECONDS:
                    pending[stamp] = data
                continue
            pump['last'] = moment
            window = pump['window']
            if not data[RUNNING]:
                window.clear()
                continue
            if window and (moment - window[-1][0]).total_seconds() > MAX_STEP_GAP:
                window.clear()
            window.append((moment, [float(data[name]) for name in MEASURED]))
            del window[:-settings.window]
            if len(window) == settings.window:
                windows.append((asset, stamp, [values for _, values in window]))
        pump['pending'] = pending
    return windows


def _complete(data):
    running = data.get(RUNNING)
    if not isinstance(running, (bool, int, float)) or not math.isfinite(running):
        return False
    return all(isinstance(data.get(name), (int, float)) and not isinstance(data.get(name), bool)
               and math.isfinite(data[name]) for name in MEASURED)


def _moment(stamp):
    """The timestamp as an aware datetime, or None when it doesn't parse."""
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def infer(settings, windows):
    """Return one score per window, or raise _Failure."""
    body = {'inputs': [{'name': 'window', 'shape': [len(windows), settings.window, len(MEASURED)],
                        'datatype': 'FP32', 'data': [v for window in windows for step in window for v in step]}],
            'outputs': [{'name': 'score'}]}
    url = f'{settings.engine}/v2/models/{settings.model}/versions/{settings.version}/infer'
    response = _post(url, body, settings.timeout, 'inference')
    if not isinstance(response, dict):
        raise _Failure('inference endpoint answered with something other than a JSON object')
    if response.get('model_name') != settings.model or str(response.get('model_version')) != settings.version:
        raise _Failure(f'inference endpoint answered for model {response.get("model_name")!r} '
                       f'version {response.get("model_version")!r}')
    outputs = response.get('outputs')
    if not isinstance(outputs, list) or not all(isinstance(o, dict) for o in outputs):
        raise _Failure(f'inference endpoint returned an unexpected output for {len(windows)} windows')
    outputs = [o for o in outputs if o.get('name') == 'score']
    if len(outputs) != 1 or outputs[0].get('datatype') != 'FP32' or outputs[0].get('shape') != [len(windows)]:
        raise _Failure(f'inference endpoint returned an unexpected output for {len(windows)} windows')
    data = outputs[0].get('data')
    if not isinstance(data, list) or len(data) != len(windows) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in data):
        raise _Failure('inference endpoint returned a score that is not a finite number')
    return [float(v) for v in data]


def ingest(settings, scores):
    model = f'{settings.model}/{settings.version}'
    body = [{'timestamp': stamp, 'asset': asset + settings.suffix,
             'readings': {'AnomalyScore': value, 'Model': model}} for asset, stamp, value in scores]
    _post(settings.ingest, body, settings.timeout, 'score ingest')


def _post(url, body, timeout, what):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise _Failure(f'{what} rejected the request (HTTP {error.code})') from None
    except ValueError:
        raise _Failure(f'{what} answered with something other than JSON') from None
    except OSError as error:  # URLError, timeouts and refused connections
        raise _Failure(f'{what} unavailable ({type(error).__name__}: {getattr(error, "reason", error)})') from None


class _Failure(Exception):
    pass


class _Failures:
    """Log the first failure and then at most once per interval."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.count = 0
        self.logged = None

    def failed(self, message):
        self.count += 1
        now = self.clock()
        if self.logged is None or now - self.logged >= _FAILURE_LOG_INTERVAL:
            self.logged = now
            _LOGGER.error('Scoring failed %d time(s), the readings are kept for retry: %s', self.count, message)

    def succeeded(self):
        if self.count:
            _LOGGER.warning('Scoring resumed after %d failed attempt(s)', self.count)
        self.count = 0
        self.logged = None
