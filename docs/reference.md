# Reference

The data that the demo produces, its retention, and what happens during an
outage. For the procedures, start at the [README](../README.md).

## Pump data

The simulator serves each pump as an OPC UA Object in the namespace
`urn:fledge-demo:pumps` (index 2). Its Variables are direct children of the Object,
with node IDs such as `ns=2;s=Pump1.Speed`:

| Variable | Unit or type | Access |
| --- | --- | --- |
| `SpeedSetpoint` | rpm, Double | read/write |
| `Scenario` | String | read/write |
| `Severity` | 0.0 to 1.0, Double | read/write |
| `Running` | Boolean | read |
| `Speed` | rpm, Double | read |
| `Flow` | m³/h, Double | read |
| `SuctionPressure` | bar, Double | read |
| `DischargePressure` | bar, Double | read |
| `MotorCurrent` | A, Double | read |
| `MotorPower` | kW, Double | read |
| `WindingTemperature` | °C, Double | read |
| `BearingVibration` | mm/s RMS, Double | read |
| `BearingTemperature` | °C, Double | read |

Every Variable updates once per second, including the controls and `Running`
when they are unchanged, so each step carries its own ground truth. All values of
one step carry the same source timestamp, from the simulator's UTC clock. The
values come from one model of each pump: flow rises with speed, pressure rise and
power rise faster than speed, and temperatures respond with a delay. They are
illustrative, not a validated pump model.

### In Fledge

- Each reading has one datapoint, named after the Variable, in the pump's asset.
  One step therefore arrives as 13 readings with the same timestamp. To get one
  record per pump and step, merge readings by asset and timestamp. The
  `influxdb` north plugin does this when it writes to InfluxDB.
- The reading's timestamp is the OPC UA source timestamp.
- `Running` is stored as `1` or `0`.
- When the source starts or reconnects, it first receives the current value of
  every Variable, with the timestamp of that value's last update.
- A stopped pump still reports all its values every second.

The three pumps produce about 39 readings per second.

### In InfluxDB

The `pumps` bucket holds one point per pump and step: measurement `fledge`, tag
`asset` with the pump name, one field per Variable, and the source timestamp.
Numbers are stored as floating-point values, so `Running` is `1.0` or `0.0`.
Score readings from part 3 arrive in the same measurement under their own
assets, such as `Pump1Score`.

A new north service starts with the oldest reading that Fledge still retains. So
InfluxDB also receives the readings acquired before part 2's `configure`,
including sinusoid readings from part 1 that Fledge still holds.

### Inspect the pumps with your own OPC UA client

You can browse, read and subscribe to the pumps with any OPC UA client, such as
UaExpert. Writes need a certificate that the demo CA signed, so use `pump-control`
to change a pump.

1. Forward the simulator's port to your workstation:

   ```sh
   kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/pump-simulator 4840:4840
   ```

2. In your client, add the server `opc.tcp://localhost:4840` and choose the
   endpoint with security policy `Basic256Sha256`, mode **Sign & Encrypt** and
   anonymous authentication.
3. When the client asks, trust the server certificate. It is `pump-simulator`,
   signed by your demo CA.
4. Connect. The pumps are under **Objects**, with the node IDs listed above.

The client uses its own certificate. It must be valid and contain the client's
application URI, which clients normally ensure when they create it. The simulator
log shows `read-only session for untrusted client <URI>` for each such client.
A write from it fails with `BadUserAccessDenied` and changes nothing.

## Score readings

Each score is a reading of its own asset, `Pump1Score`, `Pump2Score` or
`Pump3Score`, with the timestamp of the window's last step and two datapoints:
`AnomalyScore` and `Model`. The three pumps produce about 3 score readings per
second.

## Anomaly model

The model's input is `window`, FP32, shape `[N, 10, 9]`: N windows of 10 steps of
the nine measured values, in the order `Speed`, `Flow`, `SuctionPressure`,
`DischargePressure`, `MotorCurrent`, `MotorPower`, `WindingTemperature`,
`BearingVibration` and `BearingTemperature`. Its output is `score`, FP32, shape
`[N]`. The model ignores the two temperatures in its input, because they lag
load changes by minutes. It doesn't use the setpoint or the injected fault.

The generator writes the model only if it meets these thresholds on simulated
operation that it didn't train on:

- At least 99.9% of normal windows score below 1.0, including setpoint changes,
  stops and starts.
- For each fault at severity 0.5 or more, at least 95% of windows score 1.0 or
  more, at speeds from 300 to 1800 rpm.

The same script always produces the same model file. The model card, which the
generator writes next to the model, lists the evaluation results.

## Retention

| Store | Keeps | Set by |
| --- | --- | --- |
| Fledge | 6 hours or 1,000,000 readings, whichever limit is reached first | `retention` in [demos/pump.json](../demos/pump.json) |
| InfluxDB | 7 days of points in the `pumps` bucket | `influxdb.retention` in the [chart values](../deploy/pump-demo/chart/values.yaml), used on the first start |

With part 3, Fledge stores about 42 readings per second: 39 from the pumps and 3
scores. In 6 hours, that comes to roughly 910,000 readings, which take about
160 MiB of the 2 GiB Fledge volume, so the age limit is reached first. Fledge's
purge task runs every 10 minutes, so readings can stay a little longer.

Retention applies to all of Fledge, so the declaration applied last sets it.
Part 1's `demos/sinusoid.json` sets 24 hours; applying it again after part 2
restores that and enables the sinusoid again. To change InfluxDB's retention
after the first start, see
[Change InfluxDB's retention](operations.md#change-influxdbs-retention).

## Outages

Fledge's retention is also its buffer. Fledge purges readings by age and count
whether or not it has sent them, so its volume never fills up.

**InfluxDB unavailable.** Acquisition continues. The `influxdb-sink` service
keeps the readings and retries. When InfluxDB is back, Fledge sends the held
readings in order, oldest first; the storage check and **Data age** show
InfluxDB catching up. An outage shorter than about 6 hours loses nothing. After a
longer one, the oldest readings are missing from InfluxDB, because Fledge purged
them before it could send them.

**Inference engine unavailable or rejecting requests.** Acquisition and the
`influxdb-sink` service continue. The `pump-inference` service keeps the
unscored readings and retries. When the engine is back, Fledge scores the held
readings in order, oldest first, so each step still gets its score, as long as
Fledge still retains it. An engine pod that EIS replaces loads the model again
from your model store, so the model store and the runtime images must be
reachable from the cluster when it starts.

**Simulator restart.** Every pump returns to normal operation at its default
speed; injected faults are gone. Fledge's source reconnects by itself.

## Ports

| Port | Service | Reachable from |
| --- | --- | --- |
| 30081 | Fledge API | LAN (NodePort) |
| 30082 | Fledge GUI | LAN (NodePort) |
| 30083 | Grafana | LAN (NodePort) |
| 30080 | EIS business app | LAN (NodePort, set by EIS) |
| 4840 | Pump simulator, OPC UA | Cluster only |
| 8086 | InfluxDB | Cluster only |
| 8001 | Inference engine, KServe v2 REST | Cluster only |
| 6683 | `pump-scores` ingest | Fledge pod only |

To change the NodePorts or keep services off the LAN, see
[Browser and LAN access](browser-and-lan.md).
