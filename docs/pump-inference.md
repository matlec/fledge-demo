# Score the pumps with an anomaly model

This stage of the [pump demo](pump-demo.md) scores each pump's condition with an
anomaly model that ZEDEDA Edge Inference Services (EIS) serves on the node's CPU.
Fledge sends each pump's readings to the model and stores every result as a
reading of its own. The **Condition monitoring** row of the Grafana dashboard
shows the score next to the fault that you injected.

Complete the [pump demo](pump-demo.md) through step 8 first. The commands assume
the same terminal settings: `KUBECONFIG`, `FLEDGE_ADMIN_USERNAME`,
`FLEDGE_ADMIN_PASSWORD`, `FLEDGE_INFLUXDB_TOKEN` and the API port-forward on
`127.0.0.1:8081`.

## How it works

- **The model** compares a pump's flow, pressures, motor current, power and
  vibration with the values a healthy pump has at the same speed. It looks at
  windows of 10 one-second steps and returns one anomaly score per window. A
  score of 1.0 or more is anomalous. It doesn't use the temperatures, the
  setpoint or the injected fault. A script in this repository trains it on the
  simulator's own pump model, so the same script always produces the same model
  file.
- **EIS serves the model** with OpenVINO Model Server on the CPU, through a
  KServe v2 inference endpoint inside the cluster. EIS owns this deployment: you
  create and delete it in EIS, not through App Flows. EIS also deploys its
  standard business app with the model. That app is built for image models, so
  for this model it runs unused; Fledge and Grafana are the demo's application.
- **Fledge scores the readings** with the `pump-inference` north service and the
  `pump_inference` plugin from this repository, which is part of the core image.
  For each pump, the plugin merges the readings of one step, keeps a window of
  the latest 10 steps, and sends the windows to the endpoint in batches.
- **The scores come back into Fledge.** The plugin posts each score to the
  `pump-scores` south service, which runs the upstream `http_south` plugin. That
  service listens only inside the Fledge pod. Each score is a reading of its own
  asset, such as `Pump1Score`, with the step's timestamp and the datapoints
  `AnomalyScore` and `Model`.
- **The scores reach InfluxDB like any other reading.** The existing
  `influxdb-sink` service forwards them, and Grafana reads them from there.
- **Scoring never blocks acquisition.** If the endpoint is unavailable, Fledge
  keeps acquiring and forwarding readings. The inference service keeps the
  unscored readings and scores them when the endpoint is back.

[demos/inference.json](../demos/inference.json) declares both services.

## Before you start

You need:

- The pump demo, with the pump and InfluxDB checks from its step 6 passing.
- The core image with the `pump_inference` and `http_south` plugins. The pinned
  image in the [Fledge chart values](../deploy/fledge/chart/values.yaml)
  includes them.
- Python 3.10 to 3.12 on your workstation, to create the model. Its pinned NumPy
  version has no builds for newer Python versions.
- An EIS organization with an existing upload provider that you can write to, and
  permission to write models. A second authorized EIS user must approve the
  deployment, and later its deletion: EIS doesn't let you approve your own.
- Capacity for the deployment that EIS generates. Its engine requests about 1 CPU
  and 2 GiB of memory, and its business app about 500m CPU and 1 GiB. Check the
  generated configuration before it is approved.
- NodePort 30080 free on the node. The EIS business app uses it. The Fledge GUI
  uses 30082 for this reason.

EIS uses its own credentials, separate from your ZEDEDA infrastructure
credentials. Keep its token out of files, chat and Git.

## 1. Create the model

Create a Python environment with the generator's pinned dependencies, then create
the model in a directory outside the repository:

```sh
python3 -m venv ~/.cache/fledge-demo/model-venv
~/.cache/fledge-demo/model-venv/bin/pip install -r model/pump-anomaly/requirements.txt
~/.cache/fledge-demo/model-venv/bin/python model/pump-anomaly/generate_model.py \
  ~/.cache/fledge-demo/pump-anomaly
```

The generator writes the directory that you upload in step 2, and one file next
to it:

- `pump-anomaly/model.onnx`, the model.
- `pump-anomaly/README.md`, the model card that EIS shows with the model. It
  describes the score, the input and output, the training and the limits, with
  the evaluation results and the model's SHA256.
- `pump-anomaly.reference.json`, three test windows and the scores the model
  must return for them. You use it to check the deployed model in step 3.

It prints the model's SHA256, its input and output, and its scores on simulated
operation that it didn't train on. It fails, and writes nothing, if the model
file doesn't compute the same scores as the generator, or if the model doesn't
meet these thresholds:

- At least 99.9% of normal windows score below 1.0, including setpoint changes,
  stops and starts.
- For each fault at severity 0.5 or more, at least 95% of windows score 1.0 or
  more, at speeds from 300 to 1800 rpm.

The model's input is `window`, FP32, shape `[N, 10, 9]`: N windows of 10 steps of
the nine measured values, in the order `Speed`, `Flow`, `SuctionPressure`,
`DischargePressure`, `MotorCurrent`, `MotorPower`, `WindingTemperature`,
`BearingVibration` and `BearingTemperature`. Its output is `score`, FP32, shape
`[N]`.

## 2. Upload and deploy the model

1. In the terminal you use for the upload, set the EIS settings. Read the token
   without showing it:

   ```sh
   export EDGEAI_SERVICE_URL=https://YOUR_EIS_HOST
   read -rs EDGEAI_ACCESS_TOKEN && export EDGEAI_ACCESS_TOKEN
   export EDGEAI_CURRENT_ORG=YOUR_ORGANIZATION
   ```

2. Install the EIS SDK and upload the model directory as `pump-anomaly`.
   Replace the provider ID. The SDK's own output is suppressed, so that it can't print
   server details; the command prints only the import job:

   ```sh
   ~/.cache/fledge-demo/model-venv/bin/pip install zededa-eip-client==3.2.0
   ~/.cache/fledge-demo/model-venv/bin/python - <<'PY'
   import contextlib, io, json, os
   from zededa_eip_client.client import ZededaEIPClient
   try:
       with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
           job = ZededaEIPClient().upload_model_from_local_files(
               model_name="pump-anomaly",
               file_paths=os.path.expanduser("~/.cache/fledge-demo/pump-anomaly"),
               provider_id="YOUR_PROVIDER_ID", organization_id=os.environ["EDGEAI_CURRENT_ORG"], wait=False,
               import_config={"description": "Anomaly score for the Fledge pump demo"})
   except Exception:
       raise SystemExit("Upload failed; check the import status in EIS before you retry.") from None
   print(json.dumps({key: job[key] for key in ("job_id", "id", "status") if key in job}))
   PY
   ```

   EIS shows the description with the model and its new version; without one, it
   shows `Imported from local`. Select the provider by its ID. If the upload reports
   `models:write permission required`, your EIS user needs that permission;
   signing in again isn't enough. If it returns no job, check the import status
   in EIS before you retry.

3. Optional: give the model its picture in the catalog. In EIS, under
   **Models**, open `pump-anomaly`, hold the pointer over its image, select
   **Upload** and choose [model/pump-anomaly/thumbnail.png](../model/pump-anomaly/thumbnail.png).
   EIS shows **Thumbnail uploaded**. The thumbnail belongs to the model, not to
   a version, so later uploads keep it. EIS accepts JPEG, PNG, WebP or GIF up
   to 1 MB.
4. In EIS, under **Models**, wait until `pump-anomaly` has a new version, and
   note its number: 1 for the first upload. Open the model and select
   **Deploy**:
   - **Basic Info**: enter a **Name** for the deployment.
   - **Model Configuration**: select that **Version**. Keep the default
     **Business App Image**; the demo doesn't use it.
   - **Target Configuration**: select **Single Cluster** as **Target Type**,
     your cluster as **Target Cluster** and AMD64 as **Architecture**. EIS
     fills in **Project Name**.
   - **Review**: check the settings and select **Deploy**.
5. Ask the second user to open **Deployments → Pending Reviews**, review the
   generated configuration and approve it. Before approving, check the ports,
   the images, the resource requests and limits, and that the model loads from
   your provider.

The first deployment can take several minutes while the node downloads the
images and the model.

[demos/inference.json](../demos/inference.json) expects `pump-anomaly` version 1.
EIS never reuses a version number, so each upload of the same name gets the next
one. If your version or name differ, use them in the check in section 3, and
set `version` and `model` in the declaration to match before section 4.

## 3. Check the deployed model

Find the engine. EIS chooses its namespace and names. Its pod carries the label
`app.kubernetes.io/component=openvino-server`:

```sh
kubectl get pods -A -l app.kubernetes.io/component=openvino-server
kubectl -n <namespace> get services
```

Wait until the pod is ready, then forward the engine Service's REST port, 8001,
to your workstation in another terminal:

```sh
kubectl -n <namespace> port-forward --address 127.0.0.1 service/<engine-service> 18001:8001
```

Run the check with the reference file from step 1:

```sh
python3 model/pump-anomaly/check_engine.py --endpoint http://127.0.0.1:18001 \
  --model pump-anomaly --version 1 \
  --reference ~/.cache/fledge-demo/pump-anomaly.reference.json
```

The check sends the three test windows in one request. It passes, and prints the
three scores, when the endpoint answers for the same model and version with one
FP32 `score` output of shape `[3]`, and every score matches the reference. It
fails with the reason otherwise, for example `engine rejected the request: HTTP
404` for a model or version that the engine doesn't serve. Stop the port-forward
when it passes.

Do not print the generated Helm values or the full pod arguments: they can
contain the credentials of your model store.

## 4. Configure scoring

Set the engine's address inside the cluster, with the Service and namespace from
step 3, and apply the inference declaration:

```sh
export FLEDGE_INFERENCE_URL=http://<engine-service>.<namespace>.svc:8001
python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 --config demos/inference.json
```

The Service name contains part of the deployment's ID, so every new deployment
gets a new name. After deploying the model again, repeat steps 3 and 4 with the
new name. Until then, Fledge
keeps the unscored readings and scores them once the address is set.

`configure` creates the `pump-scores` south service first, then the
`pump-inference` north service. It also enables both services if they were
disabled. It refuses to change anything if
`FLEDGE_INFERENCE_URL` is unset or empty. Running it again updates both services
and leaves them running. A new north service starts with the oldest reading that
Fledge still retains, so Fledge also scores the readings it acquired before this
step.

## 5. Check the scores

```sh
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/inference.json
```

The check passes when both services are enabled and running, and every running
pump has a score that is less than 30 seconds old. It waits up to 120 seconds for
one. A stopped pump gets no scores, so the check reports it as stopped instead of
failing. For each running pump, it prints the score's age and its lag behind the
pump's readings, and it ends with
`PASS: services pump-inference, pump-scores running; every running pump has a score within 30 s`.

In the Fledge GUI, **Readings** now also lists `Pump1Score`, `Pump2Score` and
`Pump3Score`.

## 6. See a fault in the score

On the Grafana dashboard, the **Condition monitoring** row shows:

- **Score age**: the time since each pump's newest score. It turns orange after 10
  seconds and red after 30. A stopped pump gets no scores, so its age keeps
  growing.
- **Anomaly score and severity**: each pump's score as a solid line on the left
  axis, with the anomalous range from 1.0 shaded red, and the injected severity
  as a dashed line on the right axis. The line shows the highest score in each
  display interval.
- **Scenario (ground truth)**: the fault that you set, on the same time axis.

Inject a fault as in step 8 of the pump demo:

```sh
kubectl -n fledge-demo exec deployment/pump-simulator -- \
  pump-control set Pump2 --scenario bearing-wear --severity 0.8
```

About 10 seconds later, `Pump2`'s score rises above 1.0, while the other pumps
stay below it. After a minute, check it:

```sh
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/inference.json \
  --expect-anomalous Pump2
```

With `--expect-anomalous`, the check also requires `Pump2`'s median score over the
last 60 seconds to be at least 1.0, and every other running pump's median to be
below 1.0. A single high score on a healthy pump doesn't fail it. Clear the fault
with `pump-control set Pump2 --scenario normal --severity 0`; the score falls
below 1.0 within about 10 seconds.

## What the score means

- **It shows that a pump departs from normal behavior**, not which fault it has.
  Compare it with the ground truth to see what you injected.
- **It is as illustrative as the simulator.** The model was trained on the
  simulator's own physics, so the demo shows the path from readings to
  inference, not a model that would detect faults in a real pump.
- **Weak faults and slow pumps are harder to detect.** At severity 0.25, a fault
  can score below 1.0. Impeller wear lowers flow in proportion to speed, so below
  300 rpm even severe impeller wear may stay below 1.0.
- **A pump needs 10 running steps for a score.** After a pump starts, after a gap
  in its readings longer than 1.5 seconds, and after the Fledge pod restarts,
  its first score comes 10 seconds later. A change to the `pump-inference`
  settings, for example with `configure`, starts the windows again too, so up
  to about 20 seconds of steps around the change get no score. A stopped pump gets no score; while it
  slows down to a standstill, it still counts as running.

## Inference outages

When the engine is unavailable or rejects a request, acquisition and the
`influxdb-sink` service continue. The `pump-inference` service keeps the
unscored readings and retries. When the engine is back, Fledge scores the held
readings in order, oldest first, so each step still gets its score. This works
for as long as Fledge retains the readings: 6 hours with the pump declaration.
Readings that Fledge purged before it could score them never get a score.

An engine pod that EIS replaces loads the model again from your model store. It
needs the model store and the runtime images to be reachable from the cluster
when it starts.

## Troubleshooting

Fledge's log shows the messages of both services:

```sh
kubectl -n fledge-demo logs deployment/fledge | grep -E 'pump-inference|pump-scores'
```

| Symptom | What to check |
| --- | --- |
| `configure` reports `set FLEDGE_INFERENCE_URL for setting engineUrl of pump-inference` | Export the address as in step 4. Nothing was changed. |
| `configure` reports `required plugin/version is unavailable` for `demos/inference.json` | The core image lacks the `pump_inference` or `http_south` plugin. Deploy an image that includes both. |
| The Fledge log shows `Scoring failed ... inference unavailable` | Fledge can't reach the engine, or the request timed out. Check that the engine pod is ready and that `FLEDGE_INFERENCE_URL` names its Service, namespace and port 8001. Fledge retries by itself and logs a repeated failure once a minute. |
| The Fledge log shows `Scoring failed ... inference rejected the request (HTTP 404)` | The engine doesn't serve the model name or version in `demos/inference.json`. Correct them and run `configure` again. |
| The Fledge log shows `Scoring failed ... answered for model ...` or `... unexpected output ...` | The endpoint serves a different model, or a model with another contract. Check it as in step 3. |
| The Fledge log shows `Scoring failed ... score ingest ...` | The `pump-scores` service isn't running. Enable it in the GUI under **South**, or run `configure` again. |
| The Fledge log shows `Scoring resumed after ... failed attempt(s)` | Scoring works again after a failure. Fledge now scores the readings it held. |
| The check reports `newest score is ... s old` and **Score age** is red | Scores have stopped. Look for `Scoring failed` in the Fledge log. |
| The check reports `no recent readings with Running` | Fledge has no recent readings of that pump. Run the pump check from step 6 of the pump demo. |
| The check reports `no scores` for a running pump | Scoring hasn't started yet, or the pump's readings are incomplete. Run the pump check from step 6 of the pump demo. |
| `check_engine.py` reports `endpoint unavailable or request timed out` | The port-forward isn't running, or the engine pod isn't ready. |

When the `pump-scores` service starts, its log can show `http_south: 'app'` once.
It has no effect.

## Remove the inference stage

This removes the inference deployment and both services. Acquisition, the sink
and their data stay:

1. In the Fledge GUI, open **North**, select `pump-inference`, and disable or
   delete it. Then open **South**, select `pump-scores`, and disable or delete
   it.
2. In EIS, open the deployment's details and select the trash icon. In **Request
   Deployment Deletion**, enter a reason and select **Submit Request**. A second
   authorized EIS user must approve the request. EIS then removes the deployment
   and its workloads.
3. Stop any port-forward to the engine.

The uploaded model version and your upload provider stay in EIS. Fledge purges
the score readings with its retention, and InfluxDB keeps the score points until
its own retention removes them. To delete the score points now, run this once for
each pump; it leaves the pump readings in place:

```sh
for pump in Pump1 Pump2 Pump3; do
  kubectl -n fledge-demo exec deployment/influxdb -- sh -c \
    "INFLUX_TOKEN=\"\$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN\" influx delete --bucket pumps \
     --start 1970-01-01T00:00:00Z --stop \$(date -u +%Y-%m-%dT%H:%M:%SZ) \
     --predicate '_measurement=\"fledge\" AND asset=\"${pump}Score\"'"
done
```

To score again later, deploy the model again as in step 2 and repeat step 4 with
the new address.
