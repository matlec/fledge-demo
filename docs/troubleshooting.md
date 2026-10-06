# Troubleshooting

Start with the logs of the part that fails. Run the commands with the settings
from [Set up a terminal](terminal-setup.md).

```sh
python3 scripts/app-flows.py status                       # Fledge registration
kubectl -n fledge-demo get events --sort-by=.metadata.creationTimestamp
kubectl -n fledge-demo logs deployment/fledge             # core and every Fledge service
kubectl -n fledge-demo logs deployment/pump-simulator
kubectl -n fledge-demo logs deployment/influxdb
kubectl -n fledge-demo logs deployment/grafana
```

To see the messages of one Fledge service, filter the core log by its name, for
example `grep -E 'pump-opcua|influxdb-sink|pump-inference|pump-scores'`.

## Cluster and App Flows

| Symptom | What to check |
| --- | --- |
| zcli prints `Please run zcli configure before using any commands.` | Run `zcli configure` and `zcli login` with the same alias. zcli can exit 0 after an error. |
| `app-flows.py` reports HTTP 401 or 403 | The token has expired or lacks GitOps permissions. Create a new token, then run `zcli configure` and `zcli login` again. |
| `app-flows.py` reports several registrations with one name | Choose a unique `FLEDGE_GITOPS_NAME`, or remove the unintended registration in the console. |
| PVC stays `Pending` | With `WaitForFirstConsumer`, binding waits for pod scheduling. Check pod and storage events. |
| `ImagePullBackOff` | Check the registry, repository, digest and image-pull Secret. Keep the pinned image digest. |
| A pod stays in `ContainerCreating` with a `FailedMount` event, or in `CreateContainerConfigError` | A Secret or PVC that the pod needs is missing. Create it as in its part; the pod starts by itself afterwards. Fledge needs `fledge-admin`; the simulator `pump-simulator-server` and `pump-simulator-operator`; InfluxDB `influxdb-data` and `influxdb-admin`; Grafana `grafana-admin` and `grafana-influxdb`. |

## Fledge

| Symptom | What to check |
| --- | --- |
| Fresh pod stays unready | Forward to the running pod and run `initialize`; readiness follows setup. For retained data, restore the matching Secret without resetting storage. |
| Port-forward fails or disconnects | Check `KUBECONFIG` and that the pod is running. Restart forwarding after pod replacement. If port 8081 is in use, use another local port in all commands. |
| `configure` reports `set <VARIABLE> for setting ...` | Export the variable named in the message: `FLEDGE_INFLUXDB_TOKEN` (part 2, step 5) or `FLEDGE_INFERENCE_URL` (part 3, step 4). Nothing was changed. |
| `configure` reports `missing or unreadable certificate file` | Pass the directory from part 2, step 1 with `--cert-dir` or `FLEDGE_CERT_DIR`. Nothing was changed. |
| `configure` reports `required plugin/version is unavailable` | The core image lacks a plugin that the declaration uses. Deploy an image that includes it; see [Build the images](building-image.md). |
| The verifier reports `expected exactly one enabled source` | You ran it without `--config` after part 2, which disables the sinusoid. Pass `--config demos/pump.json`. |
| API responds but readings do not appear | Run the verifier and inspect source settings. An API response alone does not confirm acquisition. |
| Snapshot comparison reports missing or expired readings | Readings may have passed the retention period. Capture a new snapshot before repeating the check. |
| GUI, login or LAN problems | See [Browser and LAN access](browser-and-lan.md#troubleshooting). |

## Pump source and simulator

| Symptom | What to check |
| --- | --- |
| The simulator pod restarts and its log names a missing or unusable file | Check that the `pump-simulator-server` Secret contains the four files from part 2, step 2 with these exact names, and that the key belongs to the certificate. |
| No pump readings, and the Fledge log shows `Error 8 connecting` | The certificates don't match: Fledge doesn't trust the simulator's certificate, or the simulator doesn't trust Fledge's. Upload matching files, then run `configure` with `--restart-sources`. A source that started with non-matching certificates doesn't retry by itself. |
| The Fledge log shows `Disconnected` and `Attempting reconnection` | The simulator is restarting or unreachable. The source reconnects by itself when the simulator is ready again. |
| The verifier reports `newest complete step is ... s old` | Readings have stopped. Check that the simulator pod is ready and look for connection errors in the Fledge log. |
| `pump-control set` reports `not changed: the server allows writes only from a certificate that the demo CA signed` | The operator certificate isn't signed by the CA in the `pump-simulator-server` Secret. Recreate the operator Secret from the same set of certificates. |
| `pump-control` reports `cannot connect to opc.tcp://localhost:4840` | The simulator isn't running, or its certificate differs from the one in the `pump-simulator-server` Secret that the tool reads. Check the simulator pod, then its log. |
| Your OPC UA client reports `BadCertificateTimeInvalid` or `BadCertificateUriInvalid` | Its own certificate has expired, or doesn't contain its application URI. Create a new application certificate in the client. |
| A write from your OPC UA client fails with `BadUserAccessDenied` | Expected: clients that the demo CA didn't sign are read-only. Change pumps with `pump-control`. |
| Injected faults are gone | The simulator pod restarted. Inject them again. |

## InfluxDB and Grafana

| Symptom | What to check |
| --- | --- |
| The Fledge log shows `InfluxDB write failed ... (HTTP 401: unauthorized access)` | InfluxDB rejects the token. Fledge keeps the readings. [Replace the write token](operations.md#replace-an-influxdb-token). |
| The Fledge log shows `InfluxDB write failed ... (no response: ...)` | InfluxDB is unreachable or restarting. Fledge retries by itself and logs a repeated failure once a minute. Check the InfluxDB pod. |
| The Fledge log shows `InfluxDB writes resumed after ... failed attempt(s)` | Writes work again after a failure. Fledge now sends the readings it held. |
| The Fledge log shows `Dropped point of asset ... at ...` | InfluxDB rejected that point as invalid, for example because its timestamp is older than the bucket's retention. Fledge sent the other points of the batch and continues. |
| **Data age** is red and the storage check reports `no points in the last 2 minutes` | New points don't reach InfluxDB. Check that the north service is running (the check reports it) and look for write failures in the Fledge log. |
| The dashboard shows `No data` | Check that the time range covers recent data and that **Pump** has a pump selected. |

## Scoring

| Symptom | What to check |
| --- | --- |
| The Fledge log shows `Scoring failed ... inference unavailable` | Fledge can't reach the engine, or the request timed out. Check that the engine pod is ready and that `FLEDGE_INFERENCE_URL` names its Service, namespace and port 8001. Fledge retries by itself and logs a repeated failure once a minute. |
| The Fledge log shows `Scoring failed ... inference rejected the request (HTTP 404)` | The engine doesn't serve the model name or version in `demos/inference.json`. Correct them and run `configure` again. |
| The Fledge log shows `Scoring failed ... answered for model ...` or `... unexpected output ...` | The endpoint serves a different model, or a model with another contract. Check it as in part 3, step 3. |
| The Fledge log shows `Scoring failed ... score ingest ...` | The `pump-scores` service isn't running. Enable it in the GUI under **South**, or run `configure` again. |
| The Fledge log shows `Scoring resumed after ... failed attempt(s)` | Scoring works again after a failure. Fledge now scores the readings it held. |
| The check reports `newest score is ... s old` and **Score age** is red | Scores have stopped. Look for `Scoring failed` in the Fledge log. |
| The check reports `no recent readings with Running` or `no scores` for a running pump | Fledge has no recent, complete readings of that pump, or scoring hasn't started yet. Run the pump check from part 2, step 6. |
| `check_engine.py` reports `endpoint unavailable or request timed out` | The port-forward isn't running, or the engine pod isn't ready. |

When the `pump-scores` service starts, its log can show `http_south: 'app'` once.
It has no effect.
