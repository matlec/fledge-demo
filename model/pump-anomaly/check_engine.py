#!/usr/bin/env python3
"""Check a served pump anomaly model against the generator's reference windows.

Sends the reference windows in one KServe v2 inference request and requires the
served model's identity, one FP32 `score` output of shape [N] and every score
within the reference tolerance. Uses only the Python standard library.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import urllib.error
import urllib.request

TIMEOUT = 10  # seconds for the whole request


def infer(endpoint, model, version, reference):
    body = {'inputs': [{'name': reference['input'], 'shape': reference['shape'], 'datatype': 'FP32',
                        'data': reference['data']}],
            'outputs': [{'name': reference['output']}]}
    request = urllib.request.Request(
        f'{endpoint.rstrip("/")}/v2/models/{model}/versions/{version}/infer',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def check(response, model, version, reference):
    """Return a list of problems with the response; empty when it passes."""
    if response.get('model_name') != model or str(response.get('model_version')) != str(version):
        return [f'response is from model {response.get("model_name")!r} version {response.get("model_version")!r}']
    outputs = [o for o in response.get('outputs', []) if o.get('name') == reference['output']]
    if len(outputs) != 1:
        return [f'expected one output named {reference["output"]!r}']
    output = outputs[0]
    expected = reference['expected']
    if output.get('datatype') != 'FP32' or output.get('shape') != [len(expected)]:
        return [f'output is {output.get("datatype")} {output.get("shape")}, expected FP32 [{len(expected)}]']
    data = output.get('data')
    if not isinstance(data, list) or len(data) != len(expected):
        return [f'expected {len(expected)} scores, got {len(data) if isinstance(data, list) else "none"}']
    problems = []
    for index, (got, want) in enumerate(zip(data, expected)):
        tolerance = reference['tolerance'] * (1 + abs(want))
        if not isinstance(got, (int, float)) or not math.isfinite(got) or abs(got - want) > tolerance:
            problems.append(f'window {index}: score {got!r}, expected {want:.6f}')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--endpoint', required=True, help='engine base URL, for example http://127.0.0.1:18001')
    parser.add_argument('--model', required=True)
    parser.add_argument('--version', default='1')
    parser.add_argument('--reference', type=Path, required=True, help='the generator\'s .reference.json')
    args = parser.parse_args(argv)
    reference = json.loads(args.reference.read_text())
    try:
        response = infer(args.endpoint, args.model, args.version, reference)
    except urllib.error.HTTPError as error:
        print(f'engine rejected the request: HTTP {error.code}', file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError):
        print('endpoint unavailable or request timed out', file=sys.stderr)
        return 1
    except ValueError:
        print('engine response is not JSON', file=sys.stderr)
        return 1
    problems = check(response, args.model, args.version, reference)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        scores = next(o for o in response['outputs'] if o.get('name') == reference['output'])['data']
        print(json.dumps({'model': args.model, 'version': str(args.version), 'scores': [round(v, 6) for v in scores]}))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
