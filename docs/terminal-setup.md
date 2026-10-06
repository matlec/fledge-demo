# Set up a terminal

Each part's commands assume settings and port-forwards from earlier parts. In a
new terminal, or after a break, restore them as below. Run the commands from the
repository root. None of them changes the cluster.

## Settings

Load `.env` and the cluster's kubeconfig, and read the Fledge admin credentials
from their Secret. These are needed in every part:

```sh
set -a; . ./.env; set +a
export KUBECONFIG=~/.kube/"$ZEDEDA_CLUSTER".yaml
export FLEDGE_ADMIN_USERNAME=admin
export FLEDGE_ADMIN_PASSWORD="$(kubectl -n fledge-demo get secret fledge-admin -o jsonpath='{.data.password}' | base64 -d)"
```

If `kubectl` can no longer reach the cluster, create the kubeconfig again as in
[part 1, step 1](1-install-fledge.md#1-connect-to-the-cluster-with-zcli).

After part 2, also set the certificate directory and the InfluxDB tokens:

```sh
export FLEDGE_CERT_DIR=~/.config/fledge-demo/opcua
export FLEDGE_INFLUXDB_TOKEN="$(kubectl -n fledge-demo get secret fledge-influxdb -o jsonpath='{.data.token}' | base64 -d)"
export FLEDGE_INFLUXDB_READ_TOKEN="$(kubectl -n fledge-demo get secret grafana-influxdb -o jsonpath='{.data.token}' | base64 -d)"
```

After part 3, set the engine's address only when you run `configure` with
`demos/inference.json` again. Find it as in
[part 3, step 3](3-anomaly-scoring.md#3-check-the-deployed-model).

## Port-forwards

Each port-forward runs in a terminal of its own with the settings above, until
you stop it. Restart it after its pod is replaced.

| Needed for | Command |
| --- | --- |
| The setup and verify scripts | `kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/fledge 8081:8081` |
| The InfluxDB check | `kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/influxdb 8086:8086` |

## Check the state

```sh
kubectl -n fledge-demo get pods
python3 scripts/verify-fledge.py --api http://127.0.0.1:8081 --config demos/pump.json
```

Use `demos/sinusoid.json` instead after part 1 only. The verifier needs
`--config` after part 2, because its default is the sinusoid declaration, which
part 2 disables.
