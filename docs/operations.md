# Operate the Fledge demo

These procedures extend the [setup guide](../README.md). They assume the
`KUBECONFIG`, `FLEDGE_ADMIN_USERNAME` and `FLEDGE_ADMIN_PASSWORD` variables from
its steps 1 and 5, and a running API port-forward on `127.0.0.1:8081`.

## Change the source configuration

Source and retention settings live in [demos/sinusoid.json](../demos/sinusoid.json).
Changes take effect when you run `setup-fledge.py configure` again:

- Settings omitted from the file and unrelated sources stay unchanged.
- Edits made through the API or GUI survive a restart. A later `configure` run
  reapplies only the settings declared in the file.
- Pod replacement does not rerun setup.
- Run one setup command at a time.

The verifier supports the sinusoid configuration, the
[pump demo](pump-demo.md)'s `demos/pump.json` and `demos/influxdb.json`, and the
[inference stage](pump-inference.md)'s `demos/inference.json`; snapshots and
comparisons are for sinusoid only. If you use a different configuration file, pass it with `--config`
to `configure` and to every verifier run, including snapshot comparisons.
Readings are kept for 24 hours by default. Retention applies to all of Fledge:
the pump declaration sets 6 hours, and the file applied last wins. The PVC
capacity is not a strict database quota.

## Confirm readings survive a restart

This check shows that data persists. Complete it within the 24-hour retention
period of the snapshot from setup step 5.

```sh
kubectl -n fledge-demo get pvc fledge-data -o wide
kubectl -n fledge-demo rollout restart deployment/fledge
kubectl -n fledge-demo rollout status deployment/fledge --timeout=240s
kubectl -n fledge-demo get pvc fledge-data -o wide
```

Confirm that the PVC still uses the same volume. Restart the port-forward in
your second terminal, then compare the saved readings:

```sh
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --compare /tmp/fledge-readings.json
```

Success ends with `PASS: exact stored readings and source settings retained; new
acquisition observed`.

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

Keep the PVC, the admin Secret and the source names. Before you deploy a new
revision:

1. Save a reading snapshot with `verify-fledge.py --snapshot`.
2. Note the current commit, so that you can return to it.
3. If the new image omits a plugin, disable or remove the sources that use it
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

Push the change and run `app-flows.py apply` with the new commit. Reconnect the
port-forward to the replacement pod. Existing credentials keep working; don't run
`initialize` again unless the pod stays unready. Rerun `configure` if the
configuration file changed, then compare the snapshot:

```sh
python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 --config demos/sinusoid.json
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --compare /tmp/fledge-readings.json
```

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

## Delete the saved data

**This deletes the demo's stored configuration and readings.** First
[remove the demo](../README.md#remove-the-demo) and confirm that no pod uses the
claim. Inspect the claim's namespace, labels and bound volume:

```sh
kubectl -n fledge-demo get pods
kubectl -n fledge-demo get pvc fledge-data -o yaml
export FLEDGE_VOLUME=REPLACE_WITH_THE_CLAIMS_SPEC_VOLUME_NAME
kubectl get pv "$FLEDGE_VOLUME" -o yaml
```

Confirm that the volume's `claimRef` matches this namespace and claim. Check its
reclaim policy: `Delete` asks the provisioner to remove the backing volume and
data; `Retain` leaves data behind for the storage owner to clean up.

```sh
kubectl -n fledge-demo delete pvc fledge-data
# For a volume with the Delete reclaim policy:
kubectl wait --for=delete "pv/$FLEDGE_VOLUME" --timeout=180s
```

If the volume is already absent, deletion has completed. If it remains, inspect
the provisioner events rather than removing finalizers. When you are finished
with the demo, delete the admin Secret:

```sh
kubectl -n fledge-demo delete secret fledge-admin
```

Leave the namespace unless it is empty and you intend to remove it.

The ZEDEDA API can create the SSH secret but can't update or delete it, so it
stays in ZEDEDA. Remove the deploy key from your GitHub repository to revoke its
access. If you deploy again later without the original key pair, choose a new
`FLEDGE_GITOPS_SECRET` name for the new key.

## Troubleshooting

For deployment problems, start with `python3 scripts/app-flows.py status` or the
registration's **Status** tab in the ZEDEDA console. For pod problems, inspect
events and logs:

```sh
kubectl -n fledge-demo get events --sort-by=.metadata.creationTimestamp
kubectl -n fledge-demo logs deployment/fledge
```

| Symptom | What to check |
| --- | --- |
| zcli prints `Please run zcli configure before using any commands.` | Run `zcli configure` and `zcli login` with the same alias. zcli can exit 0 after an error. |
| `app-flows.py` reports HTTP 401 or 403 | The token has expired or lacks GitOps permissions. Create a new token, then run `zcli configure` and `zcli login` again. |
| `app-flows.py` reports several registrations with one name | Choose a unique `FLEDGE_GITOPS_NAME`, or remove the unintended registration in the console. |
| PVC stays `Pending` | With `WaitForFirstConsumer`, binding waits for pod scheduling. Check pod and storage events. |
| `ImagePullBackOff` | Check the registry, repository, digest and image-pull Secret. Keep the pinned image digest. |
| Missing Secret or `FailedMount` | Confirm that the admin Secret exists in the same namespace. |
| Fresh pod stays unready | Forward to the running pod and run `initialize`; readiness follows setup. For retained data, restore the matching Secret without resetting storage. |
| Port-forward fails or disconnects | Check `KUBECONFIG` and that the pod is running. Restart forwarding after pod replacement. If port 8081 is in use, use another local port in all commands. |
| API responds but readings do not appear | Run the verifier and inspect source settings. An API response alone does not confirm acquisition. |
| Snapshot comparison reports missing or expired readings | Readings may have passed the retention period. Capture a new snapshot before repeating the check. |
| GUI, login or LAN problems | See [Browser and LAN access](browser-and-lan.md#troubleshooting). |
