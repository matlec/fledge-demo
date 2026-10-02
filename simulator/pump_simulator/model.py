"""Illustrative centrifugal pump model without I/O.

Each pump runs against a fixed friction-only system curve, so its operating point
follows the affinity laws: flow grows with speed, head with speed squared and
hydraulic power with speed cubed. Speed follows the setpoint through a
first-order ramp; temperatures follow first-order lags. Fault scenarios change
model parameters in proportion to their severity. The values are plausible for a
15 kW water pump; they are not a validated pump model.
"""
from dataclasses import dataclass
import math
import random

SCENARIOS = ('normal', 'bearing-wear', 'cavitation', 'impeller-wear')
MEASURED = ('Speed', 'Flow', 'SuctionPressure', 'DischargePressure', 'MotorCurrent',
            'MotorPower', 'WindingTemperature', 'BearingVibration', 'BearingTemperature')
MAX_SPEED = 1800.0  # rpm, highest accepted setpoint

RATED_SPEED = 1450.0  # rpm
RATED_FLOW = 100.0  # m³/h at rated speed
RATED_HEAD = 40.0  # m at rated speed
SUCTION_PRESSURE = 1.5  # bar
PUMP_EFFICIENCY = 0.75
MOTOR_EFFICIENCY = 0.92
VOLTAGE = 400.0  # V, three-phase
POWER_FACTOR = 0.85
NO_LOAD_CURRENT = 3.0  # A
RATED_CURRENT = 30.0  # A, reference for winding heating
AMBIENT = 25.0  # °C
STOP_SPEED = 5.0  # rpm, below which a pump without setpoint stands still

SPEED_TAU = 5.0  # s
WINDING_TAU = 120.0  # s
BEARING_TAU = 180.0  # s

RHO_G = 1000 * 9.81  # water, N/m³
BAR = 1e5  # Pa


def fleet_rng(seed, name):
    """A generator for one pump, derived from the fleet seed and the pump name."""
    return random.Random(f'{seed}:{name}')


def check_setpoint(value):
    """Return a valid speed setpoint in rpm or raise ValueError."""
    value = float(value)
    if not 0.0 <= value <= MAX_SPEED:
        raise ValueError(f'setpoint must be between 0 and {MAX_SPEED:g} rpm')
    return value


def check_scenario(value):
    if value not in SCENARIOS:
        raise ValueError('scenario must be one of ' + ', '.join(SCENARIOS))
    return value


def check_severity(value):
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError('severity must be between 0.0 and 1.0')
    return value


@dataclass(frozen=True)
class Targets:
    speed: float
    flow: float
    head: float
    power: float
    current: float
    winding: float
    bearing: float


class Pump:
    """One pump's state. It starts in steady state at its setpoint, fault-free."""

    def __init__(self, setpoint, rng):
        self.rng = rng
        self.scenario = 'normal'
        self.severity = 0.0
        self.setpoint = check_setpoint(setpoint)
        self.speed = self.setpoint
        steady = self._targets()
        self.winding = steady.winding
        self.bearing = steady.bearing

    def set_setpoint(self, value):
        self.setpoint = check_setpoint(value)

    def set_scenario(self, value):
        self.scenario = check_scenario(value)

    def set_severity(self, value):
        self.severity = check_severity(value)

    def _effect(self, scenario):
        return self.severity if self.scenario == scenario else 0.0

    def _targets(self):
        """Noise-free values that the lagging quantities move towards."""
        ratio = self.speed / RATED_SPEED
        impeller = self._effect('impeller-wear')
        flow = RATED_FLOW * ratio * (1 - 0.25 * impeller)
        head = RATED_HEAD * ratio ** 2 * (1 - 0.3 * impeller)
        hydraulic = RHO_G * flow / 3600 * head  # W
        efficiency = PUMP_EFFICIENCY * (1 - 0.15 * impeller)
        power = hydraulic / efficiency / MOTOR_EFFICIENCY / 1000  # kW
        running = self.speed > 0
        current = (NO_LOAD_CURRENT + power * 1000 / (math.sqrt(3) * VOLTAGE * POWER_FACTOR)) if running else 0.0
        winding = AMBIENT + 55 * (current / RATED_CURRENT) ** 2
        bearing = AMBIENT + ((15 + 20 * ratio ** 2 + 30 * self._effect('bearing-wear')) if running else 0.0)
        return Targets(self.speed, flow, head, power, current, winding, bearing)

    def step(self, dt):
        """Advance by dt seconds and return the measured values and Running."""
        # Draw the same random numbers in every scenario, so that severity 0
        # reproduces the normal sequence exactly.
        n = [self.rng.gauss(0, 1) for _ in range(10)]
        peak_chance, peak_size = self.rng.random(), self.rng.random()

        self.speed += (self.setpoint - self.speed) * (1 - math.exp(-dt / SPEED_TAU))
        if self.setpoint == 0 and self.speed < STOP_SPEED:
            self.speed = 0.0
        t = self._targets()
        self.winding += (t.winding - self.winding) * (1 - math.exp(-dt / WINDING_TAU))
        self.bearing += (t.bearing - self.bearing) * (1 - math.exp(-dt / BEARING_TAU))

        running = self.speed > 0
        ratio = self.speed / RATED_SPEED
        cavitation = self._effect('cavitation')
        bearing_wear = self._effect('bearing-wear')
        suction = SUCTION_PRESSURE - (0.8 * cavitation if running else 0.0) + 0.01 * n[7]
        if running:
            flow = t.flow * (1 + 0.005 * n[1] + 0.06 * cavitation * n[2])
            rise = RHO_G * t.head / BAR * (1 + 0.005 * n[3] + 0.05 * cavitation * n[4])
            vibration = 1.0 + 0.8 * ratio ** 2 + 6.0 * bearing_wear + 1.0 * cavitation
            if peak_chance < 0.3 * cavitation:
                vibration += (2 + 3 * peak_size) * cavitation
            vibration *= 1 + 0.03 * n[5]
            speed = self.speed * (1 + 0.002 * n[0])
            current = t.current * (1 + 0.005 * n[6])
            power = t.power * (1 + 0.005 * n[6])
        else:
            flow = rise = vibration = speed = current = power = 0.0
        return {
            'Running': running,
            'Speed': max(speed, 0.0),
            'Flow': max(flow, 0.0),
            'SuctionPressure': suction,
            'DischargePressure': suction + max(rise, 0.0),
            'MotorCurrent': max(current, 0.0),
            'MotorPower': max(power, 0.0),
            'WindingTemperature': self.winding + 0.1 * n[8],
            'BearingVibration': max(vibration, 0.0),
            'BearingTemperature': self.bearing + 0.1 * n[9],
        }
