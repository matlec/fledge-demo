# Operate the demo

These procedures assume the settings and the API port-forward from
[Set up a terminal](terminal-setup.md).

## Change the source configuration

Fledge's sources, north services and retention are declared in
[demos/](../demos): `sinusoid.json` (part 1), `pump.json` and `influxdb.json`
(part 2) and `inference.json` (part 3). Changes take effect when you run
`setup-fledge.py configure` again with the file:

- Settings omitted from the file and undeclared sources stay unchanged.
- Edits made through the API or GUI survive a restart. A later `configure` run
  reapplies only the settings declared in the file.
- Pod replacement does not rerun setup.
- Run one setup command at a time.
- Retention applies to all of Fledge, and the file applied last sets it.
  `pump.json` also disables the sinusoid, and `sinusoid.json` enables it again.
  The PVC capacity is not a strict database quota.

The verifier supports all four files. Pass the file with `--config` to
`configure` and to every verifier run; without it, the verifier checks the
sinusoid. Snapshots and comparisons are for the sinusoid only.

## Confirm readings survive a restart

This check shows that data persists. It uses the sinusoid snapshot from
[part 1, step 5](1-install-fledge.md#5-initialize-and-configure-fledge), so run
it before part 2, within the snapshot's 24-hour retention.

```sh
kubectl -n fledge-demo get pvc fledge-data -o wide
kubectl -n fledge-demo rollout restart deployment/fledge
kubectl -n fledge-demo rollout status deployment/fledge --timeout=240s
kubectl -n fledge-demo get pvc fledge-data -o wide
```

Confirm that the PVC still uses the same volume. Restart the port-forward in
your second terminal, then compare the saved readings:

```sh
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/sinusoid.json \
  --compare /tmp/fledge-readings.json
```

Success ends with `PASS: exact stored readings and source settings retained; new
acquisition observed`. After part 2, restart the pod the same way and run the
pump and InfluxDB checks of [part 2, step 6](2-pump-pipeline.md#6-check-acquisition-and-storage)
instead: pump readings resume without setup.

## Check controlled shutdown

On a normal stop, Fledge shuts down its services and the container exits with
code `0`. To watch this, capture the old pod's logs and status during a restart.

Find the current pod name:

```sh
kubectl -n fledge-demo get pods -l app.kubernetes.io/name=fledge,app.kubernetes.io/instance=fledge
export FLEDGE_OLD_POD=REPLACE_WITH_THE_CURRENT_POD_NAME
```

In two more terminals with the same `KUBECONFIG` and `FLEDGE_OLD_POD`, start the
captures:

```sh
kubectl -n fledge-demo logs -f "$FLEDGE_OLD_POD" > /tmp/fledge-replacement.log
```

```sh
kubectl -n fledge-demo get pod "$FLEDGE_OLD_POD" --watch -o json > /tmp/fledge-replacement-status.json
```

Then run the restart from the previous section. In the log, look for `Executing
controlled shutdown`, storage shutdown scheduling, core event-loop exit and
`controlled shutdown completed`. In the captured status, look for container exit
code `0`. Use a normal restart: a forced pod deletion skips controlled shutdown.

## Upgrade the image or chart

Keep the PVCs, the Secrets and the source names. Before you deploy a new
revision:

1. Note the current commit, so that you can return to it.
2. If the new image omits a plugin, disable or remove the sources that use it
   first.

If the new revision pins a different core image, pull it onto the node first.
The Deployment stops the old pod before it starts the new one, so the pull would
otherwise add to the time without acquisition. A pull takes about three
minutes; Fledge is then down for about one. Run this from the new commit:

```sh
FLEDGE_IMAGE="$(awk '/^  repository:/ {r = $2} /^  digest:/ {print r "@" $2; exit}' deploy/fledge/chart/values.yaml)"
FLEDGE_NODE="$(kubectl -n fledge-demo get pods -l app.kubernetes.io/name=fledge,app.kubernetes.io/instance=fledge -o jsonpath='{.items[0].spec.nodeName}')"
kubectl -n fledge-demo run fledge-pull --image="$FLEDGE_IMAGE" --restart=Never \
  --overrides="{\"apiVersion\": \"v1\", \"spec\": {\"nodeName\": \"$FLEDGE_NODE\"}}" \
  --command -- true
kubectl -n fledge-demo wait pod/fledge-pull --for=jsonpath='{.status.phase}'=Succeeded --timeout=600s
kubectl -n fledge-demo delete pod fledge-pull
```

Push the change and run `app-flows.py apply` with the new commit; for the pump
demo, set `FLEDGE_GITOPS_NAME=pump-demo FLEDGE_GITOPS_PATH=deploy/pump-demo` as
in part 2. Reconnect the port-forward to the replacement pod. Existing
credentials keep working; don't run `initialize` again unless the pod stays
unready. If a declaration changed, run `configure` again with each changed file,
then run the verifier with each file you use. Before part 2, you can also save a
sinusoid snapshot before the upgrade and compare it afterwards, as in the
persistence check.

`configure` manages an existing source by its declared name. It refuses a name
that belongs to another plugin; don't clear storage to get past this. To roll
back, apply the earlier commit. Don't roll back to an image that lacks a plugin
that configured sources still use.

## Use custom names or several deployments

The namespace `fledge-demo`, release `fledge`, PVC `fledge-data` and Secret
`fledge-admin` appear in [deploy/prerequisites.yaml](../deploy/prerequisites.yaml),
the [Fleet configuration](../deploy/fledge/fleet.yaml), the
[chart values](../deploy/fledge/chart/values.yaml) and the setup commands. Change
them in all places together before you push. If you move the application
path, keep the packaged chart layout intact.

If you rename the namespace, also update `defaultNamespace` in
[deploy/pump-demo/fleet.yaml](../deploy/pump-demo/fleet.yaml), the namespace in
[deploy/pump-demo-prerequisites.yaml](../deploy/pump-demo-prerequisites.yaml),
and the `url` in `demos/pump.json` and `demos/influxdb.json`, and pass the new
name to `scripts/make-opcua-certs.sh`. The simulator's certificate must contain
the name that Fledge connects to.

The App Flows registration name, SSH secret name and application path are set in
`.env` (`FLEDGE_GITOPS_NAME`, `FLEDGE_GITOPS_SECRET`, `FLEDGE_GITOPS_PATH`).
Names must be unique within the project. `app-flows.py` finds resources by name
and refuses to act when a name matches more than one registration. Before it
updates or deletes a registration, it shows the name, ID, repository URL and path.
Check that output: a registration with the same name from another deployment
is not refused automatically.

For a second deployment, use its own settings file, deploy key, kubeconfig and
Kubernetes names:

```sh
python3 scripts/app-flows.py --env-file .env.other status
```

## Replace the OPC UA certificates

The simulator loads its certificates when it starts, and Fledge's source loads
them when the source starts. To replace them all, for example before they
expire:

1. Create a new set with `scripts/make-opcua-certs.sh --force`. This also
   replaces the CA, so every certificate changes.
2. Replace both simulator Secrets: delete them with
   `kubectl -n fledge-demo delete secret pump-simulator-server pump-simulator-operator`,
   then create them again as in [part 2, step 2](2-pump-pipeline.md#2-create-the-volume-and-secrets).
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

## Replace an InfluxDB token

Define `influxdb_token_secret` as in
[part 2, step 4](2-pump-pipeline.md#4-create-the-influxdb-tokens), then replace
the token's Secret.

For Fledge's write token, for example after `HTTP 401` write failures:

```sh
kubectl -n fledge-demo delete secret fledge-influxdb
influxdb_token_secret write fledge-write fledge-influxdb
export FLEDGE_INFLUXDB_TOKEN="$(kubectl -n fledge-demo get secret fledge-influxdb -o jsonpath='{.data.token}' | base64 -d)"
python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 --config demos/influxdb.json
```

Fledge reloads the plugin with the new token and sends the readings it held; the
storage check shows when InfluxDB has caught up.

For Grafana's read token, delete `grafana-influxdb`, create it with
`influxdb_token_secret read grafana-read grafana-influxdb`, and restart Grafana
with `kubectl -n fledge-demo rollout restart deployment/grafana`. Grafana reads
its token only when it starts.

To revoke a token that is no longer used, list the tokens and delete it by ID:

```sh
kubectl -n fledge-demo exec deployment/influxdb -- sh -c 'INFLUX_TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" influx auth list'
kubectl -n fledge-demo exec deployment/influxdb -- sh -c 'INFLUX_TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" influx auth delete --id <ID>'
```

## Change InfluxDB's retention

The chart value `influxdb.retention` applies only on InfluxDB's first start.
To change the retention of an existing volume, change the bucket's retention in
InfluxDB, for example with `influx bucket update` in the pod.

## Change the dashboard

Grafana loads the **Pumps** dashboard from the chart. Changes that you make in
Grafana are lost when its pod restarts. Change
`deploy/pump-demo/chart/files/pumps.json` instead, push it, and run `apply` for
the pump demo with the new commit.

## Deploy the model again

The engine's Service name contains part of the EIS deployment's ID, so every new
deployment gets a new name. After deploying the model again, repeat steps 3 and
4 of [part 3](3-anomaly-scoring.md) with the new name. Until then, Fledge keeps
the unscored readings and scores them once the address is set.

## Troubleshooting

See [Troubleshooting](troubleshooting.md).
