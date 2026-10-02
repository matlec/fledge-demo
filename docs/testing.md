# Testing

Run the tests after you change the charts, the images, the simulator or the
scripts. They need no cluster. Run them from the repository root with Python 3.10
to 3.12 and the packages in `requirements-dev.txt` (PyYAML, asyncua, NumPy and
ONNX). The pinned NumPy version has no builds for newer Python versions.

```sh
pip install -r requirements-dev.txt
```

## Unit tests

The unit tests cover:

- `scripts/app-flows.py`, `setup-fledge.py` certificate handling, north services
  and secret settings, and the pump, InfluxDB and inference checks in
  `verify-fledge.py`, with fake HTTP layers in place of the App Flows, Fledge
  and InfluxDB APIs. The InfluxDB tests cover the query, the token and failure
  reports; the snapshot tests cover the single bounded request and exact matching.
- The InfluxDB north plugin against a fake InfluxDB write API: merging of one
  step's readings, line protocol, which failures keep a batch for retry, dropped
  points, log output without the token, and the source checksums.
- The pump inference north plugin against fake inference and ingest endpoints:
  steps split across blocks, retried blocks, stopped pumps, gaps, unexpected
  engine responses, outages, batching, ignored score assets and the source
  checksums.
- All three charts, rendered with Helm to check their variants, value validation
  and the provisioned Grafana dashboard.
- The pump model, with fixed random seeds.
- The pump anomaly model generator: the same hash from the same seed, the
  model's input and output contract, its operators, no ground-truth input, the
  model card, and nothing written when the exported model disagrees with the
  reference computation or misses the evaluation thresholds.
- The engine check in `model/pump-anomaly/check_engine.py`, against a fake
  KServe v2 endpoint: wrong values, shapes, types and model versions, rejected
  requests and an unavailable endpoint.
- The OPC UA server and `pump-control`, started locally with certificates created
  in a temporary directory: the address space, step timestamps, control writes,
  rejected insecure and untrusted clients, and startup diagnostics.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The chart tests need Helm; the server tests need `bash` and `openssl`. The suite
takes about a minute.

## Integration tests

The integration suite runs the real Fledge image in Docker and also needs Helm. It checks
initialization, configuration, reading retention, plugin checks, the data lock,
probes and controlled shutdown, and that no credentials appear in the output. It
takes several minutes, uses its own Docker resources and removes them when it
finishes.

Build the two images it uses from `image/fledge`: one with the demo plugins and
one without sinusoid.

```sh
docker build --platform linux/amd64 -t fledge-bundle:test \
  --build-arg 'PLUGIN_PACKAGES=fledge-south-sinusoid=3.1.0 fledge-south-s2opcua=3.1.0 fledge-north-influxdb=1.0.0 fledge-north-pump-inference=1.0.2 fledge-south-http-south=3.1.0' image/fledge
docker build --platform linux/amd64 -t fledge-bundle:no-sinusoid \
  --build-arg 'PLUGIN_PACKAGES=fledge-south-s2opcua=3.1.0' image/fledge
python3 tests/integration.py
```

To test other images, set `FLEDGE_TEST_IMAGE` and `FLEDGE_TEST_ALTERNATE`.

The suite checks that the OPC UA plugin loads and accepts a disabled source, but
it starts no OPC UA server, so it does not test OPC UA acquisition. Check Fledge
acquisition from the simulator with the [pump demo](pump-demo.md) steps and
`verify-fledge.py --config demos/pump.json` and `--config demos/influxdb.json`.
Tests on a real cluster (GitOps
reconciliation, image pulls, storage and LAN access) follow the
[setup guide](../README.md) and [operations](operations.md) procedures.
