"""Unit tests for the pump anomaly model generator."""
import hashlib
import importlib.util
import io
from pathlib import Path
import contextlib
import tempfile
import unittest
from unittest import mock

import numpy as np
import onnx
from onnx import numpy_helper

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('generate_model', ROOT / 'model/pump-anomaly/generate_model.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)

GROUND_TRUTH = {'Scenario', 'Severity', 'SpeedSetpoint', 'Running'}


def sha(result):
    return hashlib.sha256(result['data']).hexdigest()


class Generation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = generator.generate()

    def test_same_seed_gives_the_same_hash(self):
        self.assertEqual(sha(self.default), sha(generator.generate()))

    def test_changed_seed_gives_a_different_hash(self):
        self.assertNotEqual(sha(self.default), sha(generator.generate(seed='another-seed')))

    def test_contract(self):
        model = self.default['model']
        self.assertEqual([(o.domain, o.version) for o in model.opset_import], [('', generator.OPSET)])
        self.assertEqual(model.ir_version, generator.IR_VERSION)
        graph = model.graph
        self.assertEqual([i.name for i in graph.input], ['window'])
        self.assertEqual([o.name for o in graph.output], ['score'])
        window, score = graph.input[0].type.tensor_type, graph.output[0].type.tensor_type
        self.assertEqual(window.elem_type, onnx.TensorProto.FLOAT)
        self.assertEqual([d.dim_param or d.dim_value for d in window.shape.dim], ['N', 10, 9])
        self.assertEqual(score.elem_type, onnx.TensorProto.FLOAT)
        self.assertEqual([d.dim_param or d.dim_value for d in score.shape.dim], ['N'])

    def test_only_standard_operators(self):
        used = {node.op_type for node in self.default['model'].graph.node}
        self.assertEqual({node.domain for node in self.default['model'].graph.node}, {''})
        self.assertLessEqual(used, {'Gather', 'Unsqueeze', 'ReduceMean', 'Sub', 'Mul', 'Add', 'Div', 'Sqrt', 'Concat'})

    def test_no_ground_truth_input(self):
        # The model's only input holds the measured values, in the documented order.
        self.assertEqual(len(generator.MEASURED), 9)
        self.assertFalse(GROUND_TRUTH & set(generator.MEASURED))
        self.assertFalse(GROUND_TRUTH & {name.split(':')[1] for name in generator.FEATURES})
        # Training data has one column per measured value, and nothing else.
        runs = generator.simulate(generator.DEFAULT_SEED)
        self.assertEqual({run.shape[1] for run in runs}, {9})

    def test_reference_scores_separate_normal_from_faults(self):
        normal, bearing, cavitation = self.default['expected']
        self.assertLess(normal, 1.0)
        self.assertGreater(bearing, 1.0)
        self.assertGreater(cavitation, 1.0)

    def test_scores_scale_with_the_threshold(self):
        params = dict(self.default['params'], threshold=np.float32(2 * self.default['params']['threshold']))
        np.testing.assert_allclose(generator.score(self.default['inputs'], params), self.default['expected'] / 2)

    def test_parameters_are_rounded(self):
        for value in np.ravel(self.default['params']['coefficients']):
            self.assertEqual(float(value), float(np.float32(f'{float(value):.6g}')))


class Writing(unittest.TestCase):
    def run_main(self, directory, *args):
        output = Path(directory) / 'pump-anomaly'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            generator.main([str(output), *args])
        return output, out.getvalue()

    def test_writes_the_model_its_card_and_the_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            output, printed = self.run_main(directory)
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['README.md', 'model.onnx'])
            sha256 = hashlib.sha256((output / 'model.onnx').read_bytes()).hexdigest()
            self.assertIn(sha256, printed)
            card = (output / 'README.md').read_text()
            front_matter = card.split('---\n')[1]
            self.assertIn('license: apache-2.0', front_matter)
            self.assertIn('library_name: onnx', front_matter)
            self.assertIn(sha256, card)
            self.assertIn('| bearing-wear, severity 0.5 |', card)
            self.assertNotIn('{', card.split('## Check a served model')[0])  # every placeholder filled
            self.assertTrue((Path(directory) / 'pump-anomaly.reference.json').exists())

    def test_corrupted_model_is_not_written(self):
        original = generator.build

        def corrupted(params, name):
            model = original(params, name)
            for tensor in model.graph.initializer:
                if tensor.name == 'threshold':
                    tensor.CopyFrom(numpy_helper.from_array((numpy_helper.to_array(tensor) * 1.01).astype(np.float32), 'threshold'))
            return model

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(generator, 'build', corrupted):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(directory)
            self.assertIn('no model written', str(raised.exception))
            self.assertEqual(list(Path(directory).rglob('*')), [])

    def test_failed_evaluation_writes_nothing(self):
        real = generator.evaluate

        def failing(params):
            return dict(real(params), passed=False)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(generator, 'evaluate', failing):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(directory)
            self.assertIn('no model written', str(raised.exception))
            self.assertEqual(list(Path(directory).rglob('*')), [])


if __name__ == '__main__':
    unittest.main()
