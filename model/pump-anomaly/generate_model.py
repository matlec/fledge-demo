#!/usr/bin/env python3
"""Train the pump anomaly model on simulated normal operation and export it as ONNX.

The model learns how a healthy pump's values depend on its speed. For each step
of a window it compares the hydraulic, electrical and vibration values with the
values expected at that step's speed, so speed ramps are explained. It summarizes
the residuals per window (their means, and the spread of flow, discharge pressure
and vibration), scales each summary by its spread in the training data, and takes
the root mean square. Dividing by twice the value that 99.9% of normal training
windows stay below makes 1.0 the alarm threshold, with a margin for unseen data.
Temperatures lag load changes by minutes, so they are not used. Ground truth and
the setpoint are never inputs: the model sees only the nine measured values.

Training data comes from the simulator's own pump model with a fixed seed. The
fitted parameters are rounded to float32 with a fixed number of significant
digits, so the artifact doesn't depend on the platform's linear algebra library.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'simulator'))
from pump_simulator.model import MEASURED, RATED_SPEED, SCENARIOS, Pump, fleet_rng  # noqa: E402

WINDOW = 10  # steps of one second
SPEED = MEASURED.index('Speed')
VARIABLES = ('Flow', 'SuctionPressure', 'DischargePressure', 'MotorCurrent', 'MotorPower', 'BearingVibration')
VARIABLE_INDEX = tuple(MEASURED.index(name) for name in VARIABLES)
SPREAD_VARIABLES = tuple(VARIABLES.index(name) for name in ('Flow', 'DischargePressure', 'BearingVibration'))
FEATURES = tuple(f'mean:{name}' for name in VARIABLES) + tuple(f'std:{VARIABLES[i]}' for i in SPREAD_VARIABLES)
DEGREE = 3  # polynomial in speed / rated speed
QUANTILE = 0.999
MARGIN = 2.0  # threshold = MARGIN x the QUANTILE of normal training windows, for unseen normal data
SIGNIFICANT_DIGITS = 6
SPREAD_FLOOR = 1e-3  # avoids dividing by a spread that the training data happens to make tiny
OPSET = 13
IR_VERSION = 8
DEFAULT_SEED = 'pump-anomaly-1'
TOLERANCE = 1e-4  # agreement between the ONNX model in float32 and the float64 reference
EVALUATION_SEED = 'pump-anomaly-evaluation'
SEVERITIES = (0.25, 0.5, 1.0)
EVALUATION_SETPOINTS = (300, 600, 900, 1200, 1450, 1800)  # faults are evaluated from 300 rpm on
# Acceptance on held-out data: normal windows below 1.0, and fault windows at or
# above the gated severity at or above 1.0, once the window lies wholly after the
# fault's onset.
NORMAL_BELOW = 0.999
FAULT_ABOVE = 0.95
GATED_SEVERITY = 0.5


def simulate(seed):
    """Normal operation: steady setpoints, setpoint steps, stops and starts.

    Returns a list of runs, each an array of shape [steps, 9] of consecutive
    running steps. A stop splits a run, as it resets the scoring window.
    """
    runs = []

    def record(pump, seconds, current):
        for _ in range(seconds):
            values = pump.step(1.0)
            if values['Running']:
                current.append([values[name] for name in MEASURED])
            elif current:
                runs.append(np.array(current))
                current.clear()

    for setpoint in range(100, 1801, 100):
        pump = Pump(setpoint, fleet_rng(seed, f'steady-{setpoint}'))
        current = []
        record(pump, 900, current)
        runs.append(np.array(current))

    rng = np.random.default_rng(hash_seed(seed))
    for index in range(6):
        pump = Pump(1450, fleet_rng(seed, f'steps-{index}'))
        current = []
        for _ in range(40):
            if rng.random() < 0.1:
                pump.set_setpoint(0)
                record(pump, int(rng.integers(30, 300)), current)
            pump.set_setpoint(float(rng.integers(5, 19) * 100))
            record(pump, int(rng.integers(120, 900)), current)
        if current:
            runs.append(np.array(current))
    return [run for run in runs if len(run) >= WINDOW]


def hash_seed(seed):
    return int.from_bytes(hashlib.sha256(str(seed).encode()).digest()[:8], 'big')


def windows(run):
    """Every window of WINDOW consecutive steps in one run, shape [n, WINDOW, 9]."""
    return np.lib.stride_tricks.sliding_window_view(run, (WINDOW, run.shape[1]))[:, 0]


def design(speed):
    """Powers of speed / rated speed along a new last axis."""
    ratio = np.asarray(speed, dtype=np.float64) / RATED_SPEED
    return np.stack([ratio ** k for k in range(DEGREE + 1)], axis=-1)


def residuals(batch, coefficients):
    """Per-step deviation from the expected values at each step's speed, [n, WINDOW, variables]."""
    batch = np.asarray(batch, dtype=np.float64)
    expected = design(batch[..., SPEED]) @ coefficients.astype(np.float64)
    return batch[..., VARIABLE_INDEX] - expected


def features(batch, coefficients):
    """Window summaries of the residuals, in float64."""
    r = residuals(batch, coefficients)
    return np.concatenate([r.mean(axis=1), r.std(axis=1)[:, SPREAD_VARIABLES]], axis=1)


def rounded(values):
    return np.array([float(f'{float(v):.{SIGNIFICANT_DIGITS}g}') for v in np.ravel(values)],
                    dtype=np.float32).reshape(np.shape(values))


def fit(runs):
    """Fit the expected values, the feature centers and spreads and the threshold, rounded for export."""
    steps = np.concatenate(runs)
    coefficients = rounded(np.linalg.lstsq(design(steps[:, SPEED]), steps[:, VARIABLE_INDEX], rcond=None)[0])
    batch = np.concatenate([windows(run) for run in runs])
    values = features(batch, coefficients)
    params = {'coefficients': coefficients, 'centers': rounded(values.mean(axis=0)),
              'spreads': rounded(np.maximum(values.std(axis=0), SPREAD_FLOOR)), 'threshold': np.float32(1.0)}
    params['threshold'] = rounded([MARGIN * np.quantile(score(batch, params), QUANTILE)])[0]
    return params, len(batch)


def score(batch, params):
    """Reference computation of the model's output, in float64."""
    z = (features(batch, params['coefficients']) - params['centers'].astype(np.float64)) \
        / params['spreads'].astype(np.float64)
    return np.sqrt((z ** 2).mean(axis=1)) / float(params['threshold'])


def build(params, name):
    """The ONNX graph of score(), using only standard operators."""
    const = []

    def c(label, value, dtype=np.float32):
        const.append(numpy_helper.from_array(np.asarray(value, dtype=dtype), label))
        return label

    n = helper.make_node
    nodes = [
        n('Gather', ['window', c('speed_index', SPEED, np.int64)], ['speed'], axis=2),
        n('Unsqueeze', ['speed', c('last_axis', [2], np.int64)], ['speed_k']),
        n('Div', ['speed_k', c('rated_speed', RATED_SPEED)], ['s1']),
        n('Gather', ['window', c('variable_index', VARIABLE_INDEX, np.int64)], ['observed'], axis=2),
    ]
    expected = c('w0', params['coefficients'][0])
    power = 's1'
    for k in range(1, DEGREE + 1):
        if k > 1:
            nodes.append(n('Mul', [power, 's1'], [f's{k}']))
            power = f's{k}'
        nodes.append(n('Mul', [power, c(f'w{k}', params['coefficients'][k])], [f'term{k}']))
        nodes.append(n('Add', [expected, f'term{k}'], [f'expected{k}']))
        expected = f'expected{k}'
    nodes += [
        n('Sub', ['observed', expected], ['residual']),
        n('ReduceMean', ['residual'], ['mean'], axes=[1], keepdims=0),
        n('ReduceMean', ['residual'], ['mean_k'], axes=[1], keepdims=1),
        n('Sub', ['residual', 'mean_k'], ['dev']),
        n('Mul', ['dev', 'dev'], ['dev2']),
        n('ReduceMean', ['dev2'], ['var'], axes=[1], keepdims=0),
        n('Sqrt', ['var'], ['std']),
        n('Gather', ['std', c('spread_index', SPREAD_VARIABLES, np.int64)], ['std_sel'], axis=1),
        n('Concat', ['mean', 'std_sel'], ['values'], axis=1),
        n('Sub', ['values', c('centers', params['centers'])], ['centered']),
        n('Div', ['centered', c('spreads', params['spreads'])], ['z']),
        n('Mul', ['z', 'z'], ['z2']),
        n('ReduceMean', ['z2'], ['msq'], axes=[1], keepdims=0),
        n('Sqrt', ['msq'], ['rms']),
        n('Div', ['rms', c('threshold', params['threshold'])], ['score']),
    ]
    graph = helper.make_graph(
        nodes, name,
        [helper.make_tensor_value_info('window', TensorProto.FLOAT, ['N', WINDOW, len(MEASURED)])],
        [helper.make_tensor_value_info('score', TensorProto.FLOAT, ['N'])],
        initializer=const)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', OPSET)],
                              producer_name='fledge-pump-anomaly', ir_version=IR_VERSION)
    onnx.checker.check_model(model, full_check=True)
    return model


def faulty_windows(seed, scenario, severity):
    """Windows wholly after a fault's onset, at several steady setpoints and across a setpoint step."""
    result = []
    for setpoint in EVALUATION_SETPOINTS:
        pump = Pump(setpoint, fleet_rng(seed, f'{scenario}-{severity}-{setpoint}'))
        for _ in range(300):
            pump.step(1.0)
        pump.set_scenario(scenario)
        pump.set_severity(severity)
        steps = [pump.step(1.0) for _ in range(600)]
        pump.set_setpoint(1800 if setpoint < 1200 else 700)
        steps += [pump.step(1.0) for _ in range(300)]
        run = np.array([[s[name] for name in MEASURED] for s in steps if s['Running']])
        result.append(windows(run))
    return np.concatenate(result)


def evaluate(params, seed=EVALUATION_SEED):
    """Score distributions on held-out data and whether they meet the acceptance thresholds."""
    normal = score(np.concatenate([windows(run) for run in simulate(seed)]), params)
    report = {'seed': seed, 'normal': {
        'windows': int(len(normal)), 'below_1': float((normal < 1.0).mean()),
        'quantiles': {q: round(float(np.quantile(normal, float(q))), 3) for q in ('0.5', '0.99', '0.999')},
        'min': round(float(normal.min()), 3), 'max': round(float(normal.max()), 3)}, 'faults': {}}
    passed = report['normal']['below_1'] >= NORMAL_BELOW
    for scenario in SCENARIOS[1:]:
        for severity in SEVERITIES:
            scores = score(faulty_windows(seed, scenario, severity), params)
            share = float((scores >= 1.0).mean())
            report['faults'][f'{scenario}@{severity}'] = {
                'at_or_above_1': round(share, 4), 'min': round(float(scores.min()), 3),
                'median': round(float(np.median(scores)), 3)}
            if severity >= GATED_SEVERITY:
                passed = passed and share >= FAULT_ABOVE
    report['passed'] = bool(passed)
    return report


def reference_windows(seed):
    """Three windows for engine checks: a normal pump, bearing wear and cavitation."""
    result = []
    for scenario in ('normal', 'bearing-wear', 'cavitation'):
        pump = Pump(1450, fleet_rng(seed, f'reference-{scenario}'))
        pump.set_scenario(scenario)
        pump.set_severity(0.0 if scenario == 'normal' else 0.8)
        steps = [pump.step(1.0) for _ in range(300)][-WINDOW:]
        result.append([[s[name] for name in MEASURED] for s in steps])
    return np.array(result, dtype=np.float32)


def check_onnx(data, inputs, expected):
    """Evaluate the serialized model with ONNX's reference runtime against score()."""
    from onnx.reference import ReferenceEvaluator
    got = ReferenceEvaluator(onnx.load_from_string(data)).run(None, {'window': inputs})[0]
    if got.shape != expected.shape or not np.allclose(got, expected, rtol=TOLERANCE, atol=TOLERANCE):
        raise SystemExit(f'model output {got} differs from the reference {expected}; no model written')


def generate(seed=DEFAULT_SEED, name='pump-anomaly'):
    """Train, export and check the model. Returns its bytes, parameters and reference data."""
    runs = simulate(seed)
    params, training_windows = fit(runs)
    model = build(params, name)
    data = model.SerializeToString()
    inputs = reference_windows(seed)
    expected = score(inputs, params)
    check_onnx(data, inputs, expected)
    return {'data': data, 'model': model, 'params': params, 'training_windows': training_windows,
            'inputs': inputs, 'expected': expected}


CARD = """\
---
license: apache-2.0
library_name: onnx
tags:
- anomaly-detection
- condition-monitoring
- pumps
- fledge
- openvino
- edge-ai
- zededa
---

# Pump anomaly score

Scores the condition of a simulated centrifugal pump from a window of its
measured values, for the Fledge on ZEDEDA pump demo. Fledge sends each pump's
latest {window} one-second steps to the model and stores the returned score next
to the readings. A score of **1.0 or more is anomalous**.

The model learns how a healthy pump's values depend on its speed. For each step,
it compares flow, suction and discharge pressure, motor current, motor power and
bearing vibration with the values expected at that step's speed, summarizes the
differences over the window, and scales the result so that 1.0 is twice the
value that 99.9% of normal training windows stay below.

- **Input:** `window`, FP32, shape `[N, {window}, 9]`: N windows of {window} steps of
  {order}.
- **Output:** `score`, FP32, shape `[N]`, one score per window.
- **Format:** ONNX opset {opset}, IR {ir}, standard operators only, for OpenVINO
  Model Server on the CPU.
- **Training:** {training_windows} windows of normal operation from the demo's
  simulator model, seed `{seed}`. No ground truth, setpoint or temperature is used.

## Evaluation

On held-out simulated operation (seed `{eval_seed}`), at speeds from 300 to
1800 rpm. The generator fails unless at least 99.9% of normal windows score below
1.0 and at least 95% of the windows of each fault at severity 0.5 or more score
1.0 or more.

| Operation | Windows at or above 1.0 | Lowest score | Median score |
|-----------|------------------------:|-------------:|-------------:|
{rows}

## Limits

- It shows that a pump departs from normal behavior, not which fault it has.
- It is trained on the simulator's own physics and is not validated for real
  pumps.
- Weak faults can stay below 1.0. Impeller wear lowers flow in proportion to
  speed, so below 300 rpm even severe impeller wear may stay below 1.0.
- A pump needs {window} consecutive running steps for a score.

## Files

- `model.onnx`: the model, SHA256 `{sha256}`.

## Check a served model

The generator also writes reference windows with their expected scores.
`check_engine.py` in the demo repository sends them to a KServe v2 endpoint and
compares the result:

```sh
python3 model/pump-anomaly/check_engine.py --endpoint http://127.0.0.1:18001 \\
  --model {name} --version <version> --reference <output>.reference.json
```
"""


def model_card(result, report, seed, name, sha256):
    normal = report['normal']
    rows = [f"| normal | {1 - normal['below_1']:.1%} | {normal['min']:.2f} "
            f"| {normal['quantiles']['0.5']:.2f} |"]
    for label, fault in report['faults'].items():
        scenario, severity = label.split('@')
        rows.append(f"| {scenario}, severity {severity} | {fault['at_or_above_1']:.1%} | {fault['min']:.2f} "
                    f"| {fault['median']:.2f} |")
    return CARD.format(window=WINDOW, order=', '.join(f'`{m}`' for m in MEASURED), opset=OPSET,
                       ir=result['model'].ir_version, training_windows=result['training_windows'], seed=seed,
                       eval_seed=report['seed'], rows='\n'.join(rows), sha256=sha256, name=name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('output', type=Path,
                        help='directory to write model.onnx and its README.md card to; the reference '
                             'windows go next to it, in <output>.reference.json')
    parser.add_argument('--seed', default=DEFAULT_SEED)
    parser.add_argument('--name', default='pump-anomaly')
    args = parser.parse_args(argv)

    result = generate(args.seed, args.name)
    data, params, inputs, expected = result['data'], result['params'], result['inputs'], result['expected']
    report = evaluate(params)
    if not report['passed']:
        print(json.dumps({'evaluation': report}, indent=2))
        raise SystemExit('the model does not meet the evaluation thresholds; no model written')
    sha256 = hashlib.sha256(data).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'model.onnx').write_bytes(data)
    (args.output / 'README.md').write_text(model_card(result, report, args.seed, args.name, sha256))
    reference = {'input': 'window', 'output': 'score', 'shape': list(inputs.shape),
                 'data': inputs.ravel().tolist(), 'expected': expected.tolist(), 'tolerance': TOLERANCE}
    reference_path = args.output.parent / (args.output.name + '.reference.json')
    reference_path.write_text(json.dumps(reference) + '\n')
    print(json.dumps({
        'model': str(args.output / 'model.onnx'), 'card': str(args.output / 'README.md'),
        'reference': str(reference_path), 'sha256': sha256,
        'opset': OPSET, 'ir_version': result['model'].ir_version, 'seed': args.seed,
        'input': {'name': 'window', 'datatype': 'FP32', 'shape': ['N', WINDOW, len(MEASURED)],
                  'order': list(MEASURED)},
        'output': {'name': 'score', 'datatype': 'FP32', 'shape': ['N']},
        'features': list(FEATURES), 'training_windows': int(result['training_windows']),
        'threshold': float(params['threshold']), 'reference_scores': [round(v, 4) for v in expected.tolist()],
        'evaluation': report,
    }, indent=2))


if __name__ == '__main__':
    main()
