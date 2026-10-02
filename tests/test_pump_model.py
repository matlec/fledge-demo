"""Unit tests for the pump physics model."""
from pathlib import Path
import statistics
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'simulator'))
from pump_simulator.model import MEASURED, SCENARIOS, Pump, fleet_rng  # noqa: E402


def run(pump, seconds):
    return [pump.step(1.0) for _ in range(seconds)]


def mean(samples, name):
    return statistics.fmean(s[name] for s in samples)


def faulty(scenario, severity, seconds=600, seed=7):
    pump = Pump(1450, fleet_rng(seed, 'Pump1'))
    pump.set_scenario(scenario)
    pump.set_severity(severity)
    return run(pump, seconds)


class SteadyState(unittest.TestCase):
    def test_all_measured_values_present(self):
        values = Pump(1450, fleet_rng(1, 'Pump1')).step(1.0)
        self.assertEqual(set(values), set(MEASURED) | {'Running'})
        self.assertIs(values['Running'], True)

    def test_constant_setpoint_is_stable_with_bounded_noise(self):
        samples = run(Pump(1450, fleet_rng(1, 'Pump1')), 300)
        first, last = samples[:150], samples[150:]
        for name in MEASURED:
            with self.subTest(name=name):
                level = mean(samples, name)
                self.assertGreater(level, 0)
                # No drift between the two halves, and noise within a few percent.
                self.assertAlmostEqual(mean(first, name), mean(last, name), delta=0.01 * level)
                self.assertLess(statistics.pstdev(s[name] for s in samples), 0.05 * level)

    def test_restart_state_is_normal_at_default_setpoint(self):
        pump = Pump(1200, fleet_rng(1, 'Pump2'))
        self.assertEqual((pump.scenario, pump.severity, pump.setpoint), ('normal', 0.0, 1200.0))


class LoadResponse(unittest.TestCase):
    def test_raised_setpoint_increases_and_settles_within_a_minute(self):
        pump = Pump(1200, fleet_rng(2, 'Pump1'))
        before = run(pump, 60)
        pump.set_setpoint(1600)
        run(pump, 60)
        after = run(pump, 60)
        for name in ('Speed', 'Flow', 'DischargePressure', 'MotorCurrent', 'MotorPower'):
            with self.subTest(name=name):
                self.assertGreater(mean(after, name), mean(before, name))
        self.assertAlmostEqual(mean(after, 'Speed'), 1600, delta=5)

    def test_affinity_laws(self):
        low, high = run(Pump(1000, fleet_rng(3, 'Pump1')), 120), run(Pump(1500, fleet_rng(3, 'Pump1')), 120)
        ratio = 1.5
        self.assertAlmostEqual(mean(high, 'Flow') / mean(low, 'Flow'), ratio, delta=0.03)
        rise = lambda s: mean(s, 'DischargePressure') - mean(s, 'SuctionPressure')
        self.assertAlmostEqual(rise(high) / rise(low), ratio ** 2, delta=0.05)
        self.assertAlmostEqual(mean(high, 'MotorPower') / mean(low, 'MotorPower'), ratio ** 3, delta=0.1)

    def test_monotonic_in_speed(self):
        levels = [run(Pump(s, fleet_rng(4, 'Pump1')), 60) for s in (600, 900, 1200, 1500, 1800)]
        for name in ('Flow', 'DischargePressure', 'MotorCurrent', 'MotorPower'):
            values = [mean(level, name) for level in levels]
            self.assertEqual(values, sorted(values), name)

    def test_vibration_stays_normal_after_load_change(self):
        pump = Pump(1200, fleet_rng(5, 'Pump1'))
        pump.set_setpoint(1600)
        samples = run(pump, 120)
        self.assertLess(max(s['BearingVibration'] for s in samples), 2.8)

    def test_temperatures_respond_with_delay(self):
        pump = Pump(900, fleet_rng(6, 'Pump1'))
        start = run(pump, 10)
        pump.set_setpoint(1600)
        soon = run(pump, 20)
        later = run(pump, 900)
        for name in ('WindingTemperature', 'BearingTemperature'):
            rise_soon = mean(soon[-5:], name) - mean(start, name)
            rise_total = mean(later[-60:], name) - mean(start, name)
            self.assertGreater(rise_total, 5, name)
            self.assertLess(rise_soon, 0.5 * rise_total, name)


class Stop(unittest.TestCase):
    def test_zero_setpoint_stops_pump(self):
        pump = Pump(1450, fleet_rng(1, 'Pump1'))
        hot = run(pump, 10)
        pump.set_setpoint(0)
        samples = run(pump, 60)
        end = samples[-1]
        self.assertIs(end['Running'], False)
        for name in ('Speed', 'Flow', 'MotorCurrent', 'MotorPower', 'BearingVibration'):
            self.assertEqual(end[name], 0.0, name)
        self.assertAlmostEqual(end['DischargePressure'], end['SuctionPressure'], delta=1e-9)
        for name in ('WindingTemperature', 'BearingTemperature'):
            temps = [s[name] for s in samples]
            self.assertLess(temps[-1], mean(hot, name) - 3, name)
            self.assertGreater(temps[-1], 30, name)  # declines gradually, not at once


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.normal = faulty('normal', 0.0)

    def compare(self, samples, name, window=slice(-120, None)):
        return mean(samples[window], name) - mean(self.normal[window], name)

    def test_bearing_wear(self):
        samples = faulty('bearing-wear', 0.8)
        self.assertGreater(self.compare(samples, 'BearingVibration'), 2)
        self.assertGreater(self.compare(samples, 'BearingTemperature'), 10)
        # Vibration rises in the first step, at a low load too.
        low = Pump(700, fleet_rng(7, 'Pump1'))
        base = low.step(1.0)['BearingVibration']
        low.set_scenario('bearing-wear'); low.set_severity(0.8)
        self.assertGreater(low.step(1.0)['BearingVibration'], base + 2)

    def test_cavitation(self):
        samples = faulty('cavitation', 0.8)
        self.assertLess(self.compare(samples, 'SuctionPressure'), -0.5)
        for name in ('Flow', 'DischargePressure'):
            spread = statistics.pstdev(s[name] for s in samples)
            self.assertGreater(spread, 3 * statistics.pstdev(s[name] for s in self.normal), name)
        vibration = [s['BearingVibration'] for s in samples]
        self.assertGreater(max(vibration), max(s['BearingVibration'] for s in self.normal) + 2)
        self.assertLess(statistics.median(vibration), max(vibration) - 1)  # intermittent peaks

    def test_impeller_wear(self):
        samples = faulty('impeller-wear', 0.8)
        for name in ('Flow', 'DischargePressure', 'MotorCurrent'):
            self.assertLess(self.compare(samples, name), 0, name)
        self.assertAlmostEqual(self.compare(samples, 'Speed'), 0, delta=2)

    def test_severity_zero_equals_normal(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertEqual(faulty(scenario, 0.0, seconds=120), self.normal[:120])

    def test_higher_severity_stronger_effect(self):
        weak, strong = faulty('bearing-wear', 0.3), faulty('bearing-wear', 0.9)
        self.assertLess(self.compare(weak, 'BearingVibration'), self.compare(strong, 'BearingVibration'))

    def test_clear_fault_returns_to_normal(self):
        pump = Pump(1450, fleet_rng(7, 'Pump1'))
        pump.set_scenario('bearing-wear'); pump.set_severity(1.0)
        run(pump, 600)
        pump.set_scenario('normal')
        cleared = run(pump, 1200)[-120:]
        for name in MEASURED:
            level = mean(self.normal[-120:], name)
            self.assertAlmostEqual(mean(cleared, name), level, delta=0.03 * level, msg=name)

    def test_invalid_controls_keep_state(self):
        pump = Pump(1450, fleet_rng(7, 'Pump1'))
        pump.set_scenario('cavitation'); pump.set_severity(0.5)
        for call, value in ((pump.set_scenario, 'overheating'), (pump.set_severity, 1.5),
                            (pump.set_severity, -0.1), (pump.set_setpoint, -1), (pump.set_setpoint, 5000)):
            with self.assertRaises(ValueError):
                call(value)
        self.assertEqual((pump.scenario, pump.severity, pump.setpoint), ('cavitation', 0.5, 1450.0))


class Randomness(unittest.TestCase):
    def test_same_seed_reproduces(self):
        a = run(Pump(1450, fleet_rng(11, 'Pump1')), 50)
        b = run(Pump(1450, fleet_rng(11, 'Pump1')), 50)
        self.assertEqual(a, b)

    def test_other_seed_differs(self):
        a = run(Pump(1450, fleet_rng(11, 'Pump1')), 50)
        b = run(Pump(1450, fleet_rng(12, 'Pump1')), 50)
        self.assertNotEqual(a, b)

    def test_pumps_have_independent_generators(self):
        a = run(Pump(1450, fleet_rng(11, 'Pump1')), 200)
        b = run(Pump(1450, fleet_rng(11, 'Pump2')), 200)
        speeds = [(x['Speed'], y['Speed']) for x, y in zip(a, b)]
        self.assertNotEqual(a, b)
        self.assertLess(abs(statistics.correlation(*zip(*speeds))), 0.3)


if __name__ == '__main__':
    unittest.main()
