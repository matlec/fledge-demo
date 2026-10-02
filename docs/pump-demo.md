# Run the pump demo

The pump demo adds a simulated fleet of three centrifugal pumps that Fledge reads
over a secure OPC UA connection. You can inject faults while it runs, such as
bearing wear, and see them in the readings. Each pump also publishes its current
fault as ground truth. Fledge forwards the readings to InfluxDB, a time-series
database, and a Grafana dashboard shows each pump's signals, its fault and the
age of its newest data. If InfluxDB is unavailable, Fledge keeps the readings and
sends them when InfluxDB is back. To score the pumps with an anomaly model, continue
with [Score the pumps with an anomaly model](pump-inference.md) after step 8.

This guide extends the [setup guide](../README.md). Complete its steps 1 to 5
first. The commands assume the `.env` settings, `KUBECONFIG`,
`FLEDGE_ADMIN_USERNAME` and `FLEDGE_ADMIN_PASSWORD` from those steps.

## How it works

- **One workload, three parts.** App Flows deploys the pump demo from
  `deploy/pump-demo` into the `fledge-demo` namespace, as a second registration of
  your repository. It contains the pump simulator, InfluxDB and Grafana. The
  simulator and InfluxDB are reachable only inside the cluster: the simulator on
  the Service `pump-simulator`, port 4840, and InfluxDB on `influxdb`, port 8086.
  Grafana serves its dashboard on your LAN, on NodePort 30083.
- **The simulator** updates every value of every pump once per second. It keeps no
  state: when its pod restarts, every pump returns to normal operation at its
  default speed.
- **The OPC UA connection is encrypted.** The simulator offers only the
  `SignAndEncrypt` security mode with the `Basic256Sha256` policy. Clients whose
  certificate a demo certificate authority (CA) that you create signed, such as
  Fledge and `pump-control`, get full access. Any other client with a valid
  client certificate can browse and read the pumps, but can't change them. Fledge
  trusts exactly the simulator certificate that you upload to it.
- **Fledge reads the pumps** with its `s2opcua` plugin, configured by
  [demos/pump.json](../demos/pump.json). The source is named `pump-opcua` and
  stores one asset per pump: `Pump1`, `Pump2` and `Pump3`.
- **Fledge sends the readings to InfluxDB** with its `influxdb-sink` north
  service, configured by [demos/influxdb.json](../demos/influxdb.json). It uses
  the `influxdb` north plugin from this repository, which is part of the core
  image. Fledge counts a reading as sent only after InfluxDB accepts it. While
  InfluxDB is unavailable, or rejects the token, Fledge keeps the readings and
  retries. Readings sent twice overwrite themselves, so they don't appear twice.
- **Each service has its own token.** Fledge writes with a token that can only
  write to the `pumps` bucket, and Grafana reads with one that can only read it.
- **Grafana is ready to use.** Its InfluxDB data source and the **Pumps** dashboard
  come from the chart. Changes that you make to the dashboard in Grafana are lost
  when its pod restarts; change `deploy/pump-demo/chart/files/pumps.json` instead.
- **You control the pumps** with `pump-control`, which runs inside the simulator
  pod through `kubectl exec`.

InfluxDB stores its data on its own PVC, `influxdb-data`. Like `fledge-data`, it
stays when you remove the workload.

## Before you start

You need:

- The demo from the setup guide, with Fledge ready and the API port-forward
  running on `127.0.0.1:8081`.
- The core image with the `influxdb` north plugin. The pinned image in the
  [Fledge chart values](../deploy/fledge/chart/values.yaml) includes it.
- A default storage class for a second 2 GiB volume.
- `bash`, `openssl` and `jq` on your workstation.
- Capacity for the workload:

  | Part | Requests | Limits |
  | --- | --- | --- |
  | Pump simulator | 50m CPU, 96 MiB memory | 500m CPU, 256 MiB |
  | InfluxDB | 100m CPU, 256 MiB | 1 CPU, 1 GiB |
  | Grafana | 50m CPU, 192 MiB | 500m CPU, 512 MiB |

The workload uses the pinned published simulator image and the digest-pinned
`influxdb` 2.7 and `grafana/grafana` images set in the
[chart values](../deploy/pump-demo/chart/values.yaml). To build your own
simulator or core image, see [Build the images](building-image.md).

If you renamed the namespace, pass the new name to the certificate script in
step 1. Also update `defaultNamespace` in
[deploy/pump-demo/fleet.yaml](../deploy/pump-demo/fleet.yaml), the namespace in
[deploy/pump-demo-prerequisites.yaml](../deploy/pump-demo-prerequisites.yaml),
and the `url` in `demos/pump.json` and `demos/influxdb.json`. The simulator's
certificate must contain the name that Fledge connects to.

## 1. Create the certificates

Choose a directory outside the repository for the certificates and keys. The
certificate script and every later command read it from `FLEDGE_CERT_DIR`:

```sh
export FLEDGE_CERT_DIR=~/.config/fledge-demo/opcua
```

Create the demo CA and the certificates it signs:

```sh
scripts/make-opcua-certs.sh
```

The script writes to `FLEDGE_CERT_DIR`, or to `~/.config/fledge-demo/opcua` if it
is not set. It refuses a directory inside the repository, so that no key can be
committed. It doesn't overwrite existing files
unless you pass `--force`.

Each `.der` file is a certificate and each `.pem` file is its private key:

| Name | Used by |
| --- | --- |
| `demo-ca` | Signs the other certificates. Keep `demo-ca.pem` on your workstation. |
| `demo-ca-crl.der` | The CA's revocation list. It is empty, but Fledge's OPC UA client requires one. |
| `pump-simulator` | The simulator's server certificate. |
| `fledge-opcua-client` | Fledge, uploaded in step 5. |
| `pump-operator` | `pump-control`. |

## 2. Create the volume and Secrets

Store the server and operator certificates in the namespace. The simulator
expects these exact file names:

```sh
kubectl -n fledge-demo create secret generic pump-simulator-server \
  --from-file="$FLEDGE_CERT_DIR/pump-simulator.der" \
  --from-file="$FLEDGE_CERT_DIR/pump-simulator.pem" \
  --from-file="$FLEDGE_CERT_DIR/demo-ca.der" \
  --from-file="$FLEDGE_CERT_DIR/demo-ca-crl.der"
kubectl -n fledge-demo create secret generic pump-simulator-operator \
  --from-file="$FLEDGE_CERT_DIR/pump-operator.der" \
  --from-file="$FLEDGE_CERT_DIR/pump-operator.pem"
```

[deploy/pump-demo-prerequisites.yaml](../deploy/pump-demo-prerequisites.yaml)
defines the `influxdb-data` PVC. Check for an existing one, then apply it:

```sh
kubectl get -f deploy/pump-demo-prerequisites.yaml --ignore-not-found
kubectl apply -f deploy/pump-demo-prerequisites.yaml
```

Create the InfluxDB and Grafana Secrets with generated passwords and an InfluxDB
admin token:

```sh
kubectl -n fledge-demo create secret generic influxdb-admin \
  --from-literal=username=admin \
  --from-literal=password="$(openssl rand -base64 18)" \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl -n fledge-demo create secret generic grafana-admin \
  --from-literal=password="$(openssl rand -base64 18)"
```

InfluxDB uses `influxdb-admin` only on its first start, to create the admin user,
the `fledge-demo` organization and the `pumps` bucket. If you reinstall over a
retained `influxdb-data` volume, keep the existing Secret. Like the admin Secret,
these Secrets and the PVC stay outside App Flows, so removing the workload keeps
them.

## 3. Deploy the pump demo

Register `deploy/pump-demo` as a second App Flows registration of your
repository. It uses the same SSH secret as the Fledge registration. Override the
registration name and path on each command. The commit must already be on GitHub:

```sh
FLEDGE_GITOPS_NAME=pump-demo FLEDGE_GITOPS_PATH=deploy/pump-demo \
  python3 scripts/app-flows.py apply --revision "$(git rev-parse HEAD)"
FLEDGE_GITOPS_NAME=pump-demo FLEDGE_GITOPS_PATH=deploy/pump-demo \
  python3 scripts/app-flows.py status
kubectl -n fledge-demo rollout status deployment/pump-simulator --timeout=180s
kubectl -n fledge-demo rollout status deployment/influxdb --timeout=300s
kubectl -n fledge-demo logs deployment/pump-simulator
```

The simulator becomes ready when the server listens, which happens only after it
has loaded its certificates. The log then shows a line that starts with
`serving opc.tcp://0.0.0.0:4840 (SignAndEncrypt, Basic256Sha256, anonymous)`.
InfluxDB becomes ready when it answers its health check. The Grafana pod stays in
`CreateContainerConfigError` until you create its token Secret in the next step.
This is expected.

## 4. Create the InfluxDB tokens

Create the two scoped tokens inside the InfluxDB pod, and store each one in a
Secret. The commands in the pod read the admin token from the pod's own
environment, so it doesn't reach your workstation:

```sh
influxdb_token_secret() {  # $1: write or read, $2: token description, $3: Secret name
  token=$(kubectl -n fledge-demo exec deployment/influxdb -- sh -c '
    export INFLUX_ORG="$DOCKER_INFLUXDB_INIT_ORG" INFLUX_TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"
    bucket=$(influx bucket list --name "$DOCKER_INFLUXDB_INIT_BUCKET" --hide-headers | cut -f1)
    influx auth create --'"$1"'-bucket "$bucket" --description "'"$2"'" --json' | jq -er .token) \
    || { echo "could not create the $2 token" >&2; return 1; }
  kubectl -n fledge-demo create secret generic "$3" --from-literal=token="$token"
}
influxdb_token_secret write fledge-write fledge-influxdb
influxdb_token_secret read grafana-read grafana-influxdb
kubectl -n fledge-demo rollout status deployment/grafana --timeout=300s
```

If a token can't be created, the function prints an error and creates no Secret.
`fledge-influxdb` holds the write token for the setup script; no pod uses it.
`grafana-influxdb` holds the read token for Grafana and for the storage check.
Grafana reads its token when it starts: if you replace `grafana-influxdb` later,
restart it with `kubectl -n fledge-demo rollout restart deployment/grafana`.

## 5. Configure Fledge

With the API port-forward running, apply the pump declaration:

```sh
python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 \
  --config demos/pump.json --cert-dir "$FLEDGE_CERT_DIR"
```

`configure` checks that every certificate file exists before it changes anything.
It then uploads the CA, its revocation list, the simulator certificate and the
Fledge certificate and key to the Fledge certificate store. Next, it creates or
updates the `pump-opcua` source. The output includes
`PASS: 4 declared certificates uploaded`. It never prints key material. Running it
again uploads the same files and leaves the source running.

Then read the write token from its Secret and apply the InfluxDB declaration:

```sh
export FLEDGE_INFLUXDB_TOKEN="$(kubectl -n fledge-demo get secret fledge-influxdb -o jsonpath='{.data.token}' | base64 -d)"
python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 --config demos/influxdb.json
```

The declaration contains `{"env": "FLEDGE_INFLUXDB_TOKEN"}` instead of the
token. `configure` refuses to change anything if the variable is unset or empty,
and it never prints the value. The Fledge API shows the token as `****`. A new
north service starts with the oldest reading that Fledge still retains, so
InfluxDB also receives the readings acquired before this step.

## 6. Check acquisition and storage

Check that readings arrive from all three pumps:

```sh
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/pump.json
```

For each pump, the verifier looks for a recent step with all 13 values, and waits
up to 120 seconds for one. It ends with
`PASS: source pump-opcua running; Pump1, Pump2, Pump3 each have all 13 datapoints in one step within 10 s`.
It checks only the latest readings, so it stays fast on a long-running instance.
In the GUI, **Readings** lists the assets `Pump1`, `Pump2` and `Pump3`.

To check InfluxDB, forward it to your workstation in another terminal:

```sh
kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/influxdb 8086:8086
```

Then run the storage check with the read token:

```sh
export FLEDGE_INFLUXDB_READ_TOKEN="$(kubectl -n fledge-demo get secret grafana-influxdb -o jsonpath='{.data.token}' | base64 -d)"
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/influxdb.json
```

For each pump, the check looks for a point with all 13 fields that is less than
10 seconds old, and waits up to 120 seconds for one. It also checks that the
north service is enabled and running. It prints the lag for each pump, the time
between the newest reading in Fledge and the newest point in InfluxDB, and ends
with
`PASS: north service influxdb-sink running; Pump1, Pump2, Pump3 each have a point with all 13 fields in InfluxDB within 10 s`.
Use `--influxdb` if you forward to another address.

## 7. Open the dashboard

> [!WARNING]
> The supplied [Fleet configuration](../deploy/pump-demo/fleet.yaml) exposes
> Grafana on NodePort `30083` over plain HTTP, like the Fledge GUI. NodePort does
> not restrict access to your LAN, and the login password is not encrypted. Use
> a restricted network, or switch to private access as described below.

Open `http://<node-address>:30083`, with the node address from step 6 of the
[setup guide](../README.md#6-open-the-gui), and
log in as `admin` with the password from the `grafana-admin` Secret:

```sh
kubectl -n fledge-demo get secret grafana-admin -o jsonpath='{.data.password}' | base64 -d; echo
```

Grafana opens the **Pumps** dashboard. Use **Pump** at the top to show some pumps
only. The dashboard shows:

- **Data age**: the time since each pump's newest point in InfluxDB. It turns
  orange after 10 seconds and red after 30.
- **Scenario (ground truth)**, **Severity (ground truth)** and **Scenario over
  time**: the fault that you set with `pump-control`.
- The measured signals, one panel per group, with one line per pump.
- **Condition monitoring**: each pump's anomaly score, once you add the
  [inference stage](pump-inference.md). Without it, the row shows no scores.

**Private access only.** To keep Grafana off the LAN, remove the `values`
section from [deploy/pump-demo/fleet.yaml](../deploy/pump-demo/fleet.yaml), push
the change and run `apply` with the new commit. Then forward Grafana and open
`http://127.0.0.1:3000`:

```sh
kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/grafana 3000:3000
```

A port-forward goes through ZEDEDA's cloud, so the dashboard refreshes more
slowly than on the LAN. To use another port, set `grafana.service.nodePort`, from
30000 to 32767, in the same file. For more on LAN access, see
[Browser and LAN access](browser-and-lan.md).

## 8. Inject and clear a fault

Show the pumps' controls and main values:

```sh
kubectl -n fledge-demo exec deployment/pump-simulator -- pump-control show
```

Set a fault scenario and its severity on one pump, from `0.0` (no effect) to
`1.0`:

```sh
kubectl -n fledge-demo exec deployment/pump-simulator -- \
  pump-control set Pump2 --scenario bearing-wear --severity 0.8
```

The tool prints the values that the pump reports after the change. The change
takes effect at the next one-second step; temperatures follow gradually. Clear
the fault by setting the scenario back:

```sh
kubectl -n fledge-demo exec deployment/pump-simulator -- \
  pump-control set Pump2 --scenario normal --severity 0
```

| Scenario | Effect with rising severity |
| --- | --- |
| `normal` | No fault. |
| `bearing-wear` | Higher bearing vibration and bearing temperature, at any load. |
| `cavitation` | Lower suction pressure, fluctuating flow and discharge pressure, and intermittent vibration peaks. |
| `impeller-wear` | Lower flow and discharge pressure at the same speed, and a lower motor current. |

`--setpoint` changes a pump's speed, from 0 to 1800 rpm; 0 stops the pump. The
default setpoints are 1450, 1200 and 1600 rpm for `Pump1`, `Pump2` and `Pump3`.
You can pass several options in one command. If a value is invalid, the tool
exits with an error and changes nothing.

On the dashboard, **Scenario (ground truth)** and **Severity (ground truth)**
change within a few seconds, and the affected signals follow.

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
one step carry the same source timestamp, from the simulator's UTC clock. The values come from one model of each pump: flow rises
with speed, pressure rise and power rise faster than speed, and temperatures
respond with a delay. They are illustrative, not a validated pump model.

In Fledge, the readings arrive like this:

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

In InfluxDB, the `pumps` bucket holds one point per pump and step: measurement
`fledge`, tag `asset` with the pump name, one field per Variable, and the source
timestamp. Numbers are stored as floating-point values, so `Running` is `1.0` or
`0.0`. Sinusoid readings arrive there too, under their own asset.

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

## Retention and outages

Fledge and InfluxDB keep data for different purposes:

| Store | Keeps | Set by |
| --- | --- | --- |
| Fledge | 6 hours or 1,000,000 readings, whichever limit is reached first | `retention` in [demos/pump.json](../demos/pump.json) |
| InfluxDB | 7 days of points in the `pumps` bucket | `influxdb.retention` in the [chart values](../deploy/pump-demo/chart/values.yaml), used on the first start |

The pumps and sinusoid produce about 40 readings per second, and the
[inference stage](pump-inference.md) adds about 3 score readings. At 43 readings
per second, 6 hours come to roughly 930,000 readings, which take about 160 MiB of
the 2 GiB Fledge volume, so the age limit is reached first. Fledge's purge
task runs every 10 minutes, so readings can stay a little longer. Retention
applies to all of Fledge, so the declaration applied last sets it. Applying
`demos/sinusoid.json` again restores its 24 hours.

Fledge's retention is also its buffer for InfluxDB. Fledge purges readings by age
and count whether or not it has sent them, so its volume never fills up. As a
result:

- **An InfluxDB outage shorter than about 6 hours loses nothing.** When InfluxDB
  is back, Fledge sends the held readings in order, oldest first. The storage
  check and **Data age** show InfluxDB catching up.
- **After a longer outage, the oldest readings are missing** from InfluxDB. Fledge
  purged them before it could send them. Newer readings still arrive.

To change InfluxDB's retention after the first start, change the bucket's
retention in InfluxDB, for example with `influx bucket update` in the pod.
Changing the chart value alone has no effect on an existing volume.

## Replace the certificates

The simulator loads its certificates when it starts, and Fledge's source loads
them when the source starts. To replace them all, for example before they
expire:

1. Create a new set with `scripts/make-opcua-certs.sh --force`. This also
   replaces the CA, so every certificate changes.
2. Replace both simulator Secrets: delete them with
   `kubectl -n fledge-demo delete secret pump-simulator-server pump-simulator-operator`,
   then create them again as in step 2.
3. Restart the simulator:
   `kubectl -n fledge-demo rollout restart deployment/pump-simulator`.
4. Upload the new files and restart the source:

   ```sh
   python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 \
     --config demos/pump.json --cert-dir "$FLEDGE_CERT_DIR" --restart-sources
   ```

   The output includes `PASS: restarted source pump-opcua to load its certificates`.

Fledge trusts the simulator certificate itself, not only the CA. Whenever the
simulator certificate changes, upload it to Fledge and restart the source.

## Troubleshooting

Fledge's log shows the messages of the pump source and the north service:

```sh
kubectl -n fledge-demo logs deployment/fledge | grep -E 'pump-opcua|influxdb-sink'
kubectl -n fledge-demo logs deployment/pump-simulator
kubectl -n fledge-demo logs deployment/influxdb
kubectl -n fledge-demo logs deployment/grafana
```

| Symptom | What to check |
| --- | --- |
| `configure` reports `missing or unreadable certificate file` | Pass the directory from step 1 with `--cert-dir` or `FLEDGE_CERT_DIR`. Nothing was changed. |
| The simulator pod stays in `ContainerCreating` with a `FailedMount` event | A simulator Secret from step 2 is missing. Create it; the pod starts once it can mount it. |
| The simulator pod restarts and its log names a missing or unusable file | Check that the `pump-simulator-server` Secret contains the four files from step 2 with these exact names, and that the key belongs to the certificate. |
| No pump readings, and the Fledge log shows `Error 8 connecting` | The certificates don't match: Fledge doesn't trust the simulator's certificate, or the simulator doesn't trust Fledge's. Upload matching files, then run `configure` with `--restart-sources`. A source that lost a working connection keeps retrying, but one that started with non-matching certificates doesn't retry by itself. |
| The Fledge log shows `Disconnected` and `Attempting reconnection` | The simulator is restarting or unreachable. The source reconnects by itself when the simulator is ready again. |
| The verifier reports `newest complete step is ... s old` | Readings have stopped. Check that the simulator pod is ready and look for connection errors in the Fledge log. |
| `pump-control set` reports `not changed: the server allows writes only from a certificate that the demo CA signed` | The operator certificate isn't signed by the CA in the `pump-simulator-server` Secret, so its session is read-only. Recreate the operator Secret from the same set of certificates. |
| Your OPC UA client reports `BadCertificateTimeInvalid` or `BadCertificateUriInvalid` | Its own certificate has expired, or doesn't contain its application URI. Create a new application certificate in the client. |
| A write from your OPC UA client fails with `BadUserAccessDenied` | Expected: clients that the demo CA didn't sign are read-only. Change pumps with `pump-control`. |
| `pump-control` reports `cannot connect to opc.tcp://localhost:4840` | The simulator isn't running, or its certificate differs from the one in the `pump-simulator-server` Secret that the tool reads. Check the simulator pod, then its log. |
| Injected faults are gone | The simulator pod restarted. Inject them again. |
| `configure` reports `set FLEDGE_INFLUXDB_TOKEN for setting token of influxdb-sink` | Export the token as in step 5. Nothing was changed. |
| `configure` reports `required plugin/version is unavailable` for `demos/influxdb.json` | The core image has no `influxdb` north plugin. Deploy an image that includes it. |
| The Fledge log shows `InfluxDB write failed ... (HTTP 401: unauthorized access)` | InfluxDB rejects the token. Fledge keeps the readings. Delete the Secret with `kubectl -n fledge-demo delete secret fledge-influxdb`, create a new token with `influxdb_token_secret write fledge-write fledge-influxdb` from step 4, then repeat the second `configure` of step 5. To revoke a token that is no longer used, list the tokens with `kubectl -n fledge-demo exec deployment/influxdb -- sh -c 'INFLUX_TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" influx auth list'` and delete it the same way with `influx auth delete --id <ID>`. Fledge reloads the plugin with the new token and sends the held readings; the storage check in step 6 shows when InfluxDB has caught up. |
| The Fledge log shows `InfluxDB write failed ... (no response: ...)` | InfluxDB is unreachable or restarting. Fledge retries by itself and logs a repeated failure once a minute. Check the InfluxDB pod. |
| The Fledge log shows `InfluxDB writes resumed after ... failed attempt(s)` | Writes work again after a failure, for example after an InfluxDB restart. Fledge now sends the readings it held. After a configuration change, such as a new token, Fledge reloads the plugin and this line doesn't appear. |
| The Fledge log shows `Dropped point of asset ... at ...` | InfluxDB rejected that point as invalid, for example because its timestamp is older than the bucket's retention. Fledge sent the other points of the same batch and continues. |
| The InfluxDB pod stays `Pending` or in `CreateContainerConfigError` | The `influxdb-data` PVC or the `influxdb-admin` Secret from step 2 is missing. |
| The Grafana pod stays in `CreateContainerConfigError` | The `grafana-influxdb` Secret from step 4 or the `grafana-admin` Secret from step 2 is missing. Create it; the pod starts after that. |
| **Data age** is red and the storage check reports `no points in the last 2 minutes` | New points don't reach InfluxDB. Check that the north service is running (the check reports it) and look for write failures in the Fledge log. |
| The dashboard shows `No data` | Check that the time range covers recent data and that **Pump** has a pump selected. |

## Remove the pump demo

This removes the simulator, InfluxDB, Grafana, the pump source and the north
service. The InfluxDB volume and the Secrets stay. If you added the inference
stage, [remove it](pump-inference.md#remove-the-inference-stage) first.

1. In the Fledge GUI, open **South**, select `pump-opcua`, and disable or delete
   it. Then open **North**, select `influxdb-sink`, and disable or delete it.
   Without the workload, they only log connection and write failures.
2. Delete the pump demo's registration. The command shows it and asks you to
   confirm. App Flows then removes the workload:

   ```sh
   FLEDGE_GITOPS_NAME=pump-demo FLEDGE_GITOPS_PATH=deploy/pump-demo \
     python3 scripts/app-flows.py delete
   ```

3. Optional: apply `demos/sinusoid.json` again to restore its 24-hour retention.

To deploy the pump demo again with the same data, repeat steps 3 and 5. The
InfluxDB tokens are stored in InfluxDB's volume and stay valid.

## Delete the pump demo's data

> [!WARNING]
> This deletes every stored point, the InfluxDB tokens and the simulator's
> certificates in the cluster. You can't undo it.

After you remove the pump demo, delete its volume and Secrets:

```sh
kubectl -n fledge-demo delete pvc influxdb-data
kubectl -n fledge-demo delete secret pump-simulator-server pump-simulator-operator \
  influxdb-admin grafana-admin grafana-influxdb fledge-influxdb
```

With the default `local-path` storage class, deleting the PVC also deletes the
data on the node. Other storage classes follow their own reclaim policy. Fledge's
volume, readings and sources are not affected. The certificate files in
`FLEDGE_CERT_DIR` stay on your workstation.

To deploy the pump demo again after this, repeat steps 2 to 5. The existing
`influxdb-sink` service continues from the last reading it sent, so the new
InfluxDB receives only readings from then on, not the ones it already sent
before the deletion.
