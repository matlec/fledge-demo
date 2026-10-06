# Remove the demo

Remove the parts in reverse order: anomaly scoring, then the pump pipeline, then
Fledge. Each removal keeps the data, so you can deploy again and continue. Delete
the data separately, as described after the removals. Run the commands with the
settings from [Set up a terminal](terminal-setup.md).

## Remove anomaly scoring

This removes the inference deployment and both scoring services. Acquisition,
the sink and their data stay.

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
its own retention removes them. To delete the score points now, run this once; it
leaves the pump readings in place:

```sh
for pump in Pump1 Pump2 Pump3; do
  kubectl -n fledge-demo exec deployment/influxdb -- sh -c \
    "INFLUX_TOKEN=\"\$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN\" influx delete --bucket pumps \
     --start 1970-01-01T00:00:00Z --stop \$(date -u +%Y-%m-%dT%H:%M:%SZ) \
     --predicate '_measurement=\"fledge\" AND asset=\"${pump}Score\"'"
done
```

To score again later, repeat steps 2 to 4 of [part 3](3-anomaly-scoring.md).

## Remove the pump pipeline

This removes the simulator, InfluxDB, Grafana, the pump source and the north
service. The InfluxDB volume and the Secrets stay.

1. In the Fledge GUI, open **South**, select `pump-opcua`, and disable or delete
   it. Then open **North**, select `influxdb-sink`, and disable or delete it.
   Without the workload, they only log connection and write failures.
2. Delete the pump demo's registration. The command shows it and asks you to
   confirm. App Flows then removes the workload:

   ```sh
   FLEDGE_GITOPS_NAME=pump-demo FLEDGE_GITOPS_PATH=deploy/pump-demo \
     python3 scripts/app-flows.py delete
   ```

3. Optional: turn the sinusoid from part 1 back on, with its 24-hour retention:

   ```sh
   python3 scripts/setup-fledge.py configure --api http://127.0.0.1:8081 --config demos/sinusoid.json
   ```

To deploy the pump pipeline again with the same data, repeat steps 3 and 5 of
[part 2](2-pump-pipeline.md). The InfluxDB tokens are stored in InfluxDB's volume
and stay valid.

## Remove Fledge

Delete the Fledge App Flows registration. The command shows the registration and
asks you to confirm. App Flows then removes the workload; the PVC and admin
Secret stay:

```sh
python3 scripts/app-flows.py delete
python3 scripts/app-flows.py status
kubectl -n fledge-demo get deployment,pod,service -l app.kubernetes.io/instance=fledge
kubectl -n fledge-demo get pvc fledge-data
kubectl -n fledge-demo get secret fledge-admin
```

When `status` reports `not found`, check again after two minutes (two default
reconciliation intervals). The workload should stay absent. To resume, run
`apply` with a compatible commit that uses the same PVC and Secret, wait for
readiness and restart the port-forward.

## Delete the pump pipeline's data

> [!WARNING]
> This deletes every stored point, the InfluxDB tokens and the simulator's
> certificates in the cluster. You can't undo it.

After you remove the pump pipeline, delete its volume and Secrets:

```sh
kubectl -n fledge-demo delete pvc influxdb-data
kubectl -n fledge-demo delete secret pump-simulator-server pump-simulator-operator \
  influxdb-admin grafana-admin grafana-influxdb fledge-influxdb
```

With the default `local-path` storage class, deleting the PVC also deletes the
data on the node. Other storage classes follow their own reclaim policy. Fledge's
volume, readings and sources are not affected. The certificate files in
`FLEDGE_CERT_DIR` stay on your workstation.

To deploy the pump pipeline again after this, repeat steps 2 to 5 of part 2. The
existing `influxdb-sink` service continues from the last reading it sent, so the
new InfluxDB receives only readings from then on.

## Delete Fledge's data

> [!WARNING]
> This deletes Fledge's stored configuration and readings. You can't undo it.

First remove Fledge and confirm that no pod uses the claim. Inspect the claim's
namespace, labels and bound volume:

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
the provisioner events rather than removing finalizers. Then delete the admin
Secret:

```sh
kubectl -n fledge-demo delete secret fledge-admin
```

## Delete the namespace

After you delete the data of every part, delete the namespace if nothing else
uses it. Deleting a namespace also deletes everything that is still in it:

```sh
kubectl -n fledge-demo get all,pvc,secret
kubectl delete namespace fledge-demo
```

## Revoke repository access

The ZEDEDA API can create the SSH secret but can't update or delete it, so it
stays in ZEDEDA. To revoke the repository access, remove the deploy key from your
GitHub repository. To deploy again with a new key pair, set a new
`FLEDGE_GITOPS_SECRET` name in `.env`.
