# Architecture

The demo runs one persistent Fledge instance on a single-node AMD64 EVE-k cluster.
The optional pump demo adds a simulator that serves three simulated pumps over
secure OPC UA, and InfluxDB and Grafana that store and show their readings. Its
optional inference stage scores each pump with an anomaly model that ZEDEDA Edge
Inference Services (EIS) serves on the CPU. For the procedures, see the
[setup guide](../README.md), the [pump demo](pump-demo.md) and
[pump inference](pump-inference.md).

## Components

| Part | Source | Managed by |
| --- | --- | --- |
| Fledge core and optional GUI | Helm chart in `deploy/fledge/chart` | App Flows GitOps, from a pinned commit |
| Namespace, PVC `fledge-data`, Secret `fledge-admin` | `deploy/prerequisites.yaml` | The operator, with `kubectl` |
| Admin password, sources, north services and retention | `scripts/setup-fledge.py`, `demos/sinusoid.json`, `demos/pump.json`, `demos/influxdb.json`, `demos/inference.json` | The operator, through the Fledge API |
| Pump simulator, InfluxDB and Grafana | Helm chart in `deploy/pump-demo/chart` | A second App Flows registration of the same repository |
| OPC UA certificates, Secrets `pump-simulator-server` and `pump-simulator-operator` | `scripts/make-opcua-certs.sh` | The operator, outside the repository |
| PVC `influxdb-data`, Secrets `influxdb-admin`, `grafana-admin`, `grafana-influxdb` and `fledge-influxdb` | `deploy/pump-demo-prerequisites.yaml`, the [pump demo](pump-demo.md) | The operator, with `kubectl` |
| Core, GUI and simulator images | `image/fledge`, `image/fledge-gui`, `image/pump-simulator` | Built and published by a maintainer |
| InfluxDB and Grafana images | Upstream `influxdb` 2.7 and `grafana/grafana`, pinned by digest | The chart values |
| Pump anomaly model | `model/pump-anomaly/generate_model.py` | Created by the operator, uploaded to EIS |
| Inference engine (OpenVINO Model Server) | Chart that EIS generates | EIS, after a second user approves |

## How it runs

The core image is the pinned upstream Fledge 3.1.0 image with plugins added at
build time (see [Build the images](building-image.md)). Its PID 1 is a small
adapter, `runtime.py`, that starts Fledge, answers the Kubernetes probes and
shuts Fledge down cleanly on SIGTERM. Fledge stores its SQLite data on the PVC,
and the adapter locks the data directory so that only one pod can use it.

Readiness requires working core, storage and admin credentials, but not a
working source. A fresh pod stays unready, without restarting, until
`setup-fledge.py initialize` replaces the default password. `configure` then
applies the sources declared in `demos/sinusoid.json`: it creates missing ones and
updates existing ones with the same name and plugin. Pod replacement doesn't
rerun setup.

The pump simulator is a separate Deployment with a ClusterIP Service on port
4840. Its model computes each pump's speed, flow, pressures, motor values,
temperatures and vibration from one shared state once per second. Operators
change a pump's setpoint, fault scenario and severity with `pump-control`, run in
the simulator pod; the server publishes those controls as ground truth. The
server offers one endpoint, `SignAndEncrypt` with `Basic256Sha256` and anonymous
users. Clients whose certificates the demo CA signed, such as Fledge and
`pump-control`, get full access. Other clients with a valid client certificate
can browse, read and subscribe, but can't write. It loads
its certificates at startup and keeps no state, so a restart resets the pumps.

Fledge reads the pumps with the `s2opcua` plugin, configured by
`demos/pump.json`. `configure` first uploads the CA, its revocation list, the
server certificate and the Fledge client pair through the Fledge certificate API;
the plugin reads them when its service starts. Each Variable update becomes one
reading with one datapoint, named after the Variable, in an asset named after
the pump Object, with the OPC UA source timestamp as its user timestamp. The
simulator updates every Variable once per second, including the controls, so one
step arrives as 13 readings that share a timestamp; consumers merge them. After a disconnection the
plugin keeps retrying and reconnects by itself. If the source starts with
certificates that don't match, it doesn't retry until the source is restarted.

The pump demo workload also runs InfluxDB and Grafana. InfluxDB 2.7 stores points on
the `influxdb-data` PVC and creates its organization, bucket and admin user from
the `influxdb-admin` Secret on its first start. Grafana starts with a provisioned
InfluxDB data source (Flux) and the **Pumps** dashboard from ConfigMaps; it keeps
no data of its own. Fledge's `influxdb-sink` north service runs the `influxdb`
plugin from this repository. For each batch of readings, the plugin merges
readings with the same asset and timestamp into one line-protocol point and
writes the batch to InfluxDB's v2 write API with a token that can only write to
the bucket. It reports the batch as sent only after InfluxDB accepts it;
otherwise the north service keeps the batch and retries it with a backoff. If
InfluxDB rejects part of a batch as invalid, the plugin writes the valid points
one at a time, logs each rejected one and reports the batch as sent, so one bad
point can't stop the stream. Grafana reads with a token that can only read the
bucket.

The inference stage adds two Fledge services. The `pump-inference` north service
runs the `pump_inference` plugin from this repository. Like a sink, it receives
every reading from its own stream position. For each pump, it merges the readings
of one step and keeps the latest 10 consecutive running steps. It sends each full
window to the engine's KServe v2 endpoint inside the cluster, in batches. The
model returns one anomaly score per window. The plugin posts the scores to the
`pump-scores` south service, which runs upstream's `http_south` plugin bound to
the pod's loopback interface. Each score becomes a reading of its own asset, such
as `Pump1Score`, with the timestamp of the window's last step. The plugin reads
only the pump assets, so it never scores its own results. It reports a block as
sent only after the engine and the ingest accept it, so Fledge retries the block
while the engine is unavailable. `influxdb-sink` forwards the scores to InfluxDB
with the other readings.

The model compares each step's flow, pressures, motor values and vibration with
the values expected at that step's speed, and summarizes the differences per
window. It uses only standard ONNX operators. The generator trains it on the
simulator's model with a fixed seed and checks the exported file against its
own computation.

The GUI is a separate Deployment. The browser calls the Fledge API directly, at
the address chosen in the GUI's Connection Setup. Both Services default to
ClusterIP; the demo's [fleet.yaml](../deploy/fledge/fleet.yaml) switches them to
NodePorts 30082 (GUI) and 30081 (API), and the pump demo's
[fleet.yaml](../deploy/pump-demo/fleet.yaml) switches Grafana to NodePort 30083.

ZEDEDA operations use zcli directly. `scripts/app-flows.py` covers only the App
Flows GitOps calls that zcli lacks, one REST call per command. Credentials stay in
the tools' usual places (`~/.config/zededa`, `~/.kube`, `~/.ssh`), and the
Kubernetes Secret is the only copy of the Fledge admin password.

## Design choices

- **Plugins are baked into the image**, not installed at startup, so a pod starts
  without access to package repositories.
- **A small adapter instead of patching Fledge**: the upstream entrypoint reports
  a failed stop as success, and a fork would be costly to maintain.
- **Setup runs from the workstation**, not in the container or a Helm hook, so
  credentials and settings stay out of the deployment.
- **Data resources stay outside GitOps**, so removing the workload can't delete
  the data.
- **The GUI is a separate workload**, so GUI updates never restart acquisition.
- **The pump simulator is maintained here**, built on `asyncua`, because generic
  OPC UA simulators produce independent values; a demo of condition monitoring
  needs coherent physics, injected faults and ground truth.
- **A demo CA signs the OPC UA certificates**, so the server trusts any client
  the operator issues without a list of client certificates.
- **Unknown OPC UA clients are read-only**, so you can inspect the pumps with any
  client, while only CA-signed clients can change a pump.
- **Every step reaches Fledge**: the source requests notifications on timestamp
  changes, so unchanged values, such as a stopped pump's zeros, still arrive.
- **The InfluxDB north plugin is maintained here**, because Fledge has no
  upstream north plugin for a self-hosted time-series database. Relaying through
  the generic HTTP plugin and Telegraf would confirm a write before InfluxDB
  stores it, so an InfluxDB outage could lose data held only in the relay.
- **Fledge's retention is the buffer for InfluxDB.** Readings are purged by age and
  count even when unsent, so an outage longer than the retention loses the
  oldest readings but never fills Fledge's volume.
- **Each step carries its ground truth**: the simulator republishes its controls
  every second, so every stored point has its fault label, and no label falls
  out of InfluxDB's retention while the fault persists.
- **Scores come back into Fledge** through a loopback HTTP south service, so the
  GUI shows them, they share the InfluxDB buffer of the other readings, and
  InfluxDB has a single writer. A north service has its own stream position, so
  inference can't hold back acquisition or the sink.
- **The demo calls the inference engine directly**, inside the cluster, rather
  than through EIS's business app. EIS deploys a model together with a business
  app that receives the engine's address, and its standard app is built for
  images. Here the consumer is Fledge, an existing data pipeline, which already
  provides the stream position, retries and storage that scoring needs.
- **The model learns normal behavior** rather than the faults, so it needs no
  labelled data and stays small enough for standard ONNX operators.
- **Platform tools are used directly**, so readers learn ZEDEDA rather than a
  wrapper. The ZEDEDA Terraform provider would also work, but it doesn't report
  reconciliation status and keeps the deploy key in its state.

## Limits

- Single node: local storage gives no recovery from node or disk loss.
- AMD64 only.
- Plain HTTP; LAN access relies on a restricted network.
- The core image reuses the published upstream binaries, whose native build
  revision can't be verified.
- Pump readings carry one datapoint each: about 39 readings per second for three
  pumps, next to one for sinusoid. InfluxDB stores one point per pump and step.
- Retention is a Fledge-wide setting: the declaration applied last sets it.
- The simulated values are illustrative, not a validated pump model; restarting
  the simulator clears injected faults.
- The simulator is reachable only inside the cluster; OPC UA sources on another
  network are untested.
- Fledge, InfluxDB and Grafana talk plain HTTP inside the cluster.
- Delivery to InfluxDB is at least once; a resent point overwrites itself.
- Grafana keeps no dashboard edits across restarts; the chart holds the dashboard.
- The anomaly score is trained on the simulator's own physics; it doesn't name
  the fault and isn't validated for real pumps. Weak faults, and impeller wear at
  low speed, can stay below the alarm threshold.
- Readings that Fledge purges before the inference service scores them never get
  a score. Window state lives in memory, so each pump's scores resume 10 seconds
  after a Fledge restart.
- EIS generates the inference deployment with its own image tags; its engine pod
  fetches the model from EIS's model store whenever it starts.
- EIS's standard business app runs unused next to the engine. The engine's
  Service name comes from the deployment's ID, so a new deployment needs a new
  `FLEDGE_INFERENCE_URL`.
