# Browser and LAN access

The demo serves the upstream Fledge GUI next to the core. The supplied
[Fleet configuration](../deploy/fledge/fleet.yaml) enables the GUI and exposes
both it and the API on the node's network. This page covers the options and the
GUI tasks beyond the [setup guide](../README.md#6-open-the-gui).

## Configuration

The Fleet configuration selects the published `matthiasl381/fledge-gui` image
by digest. Its overrides have this structure:

```yaml
helm:
  chart: ./chart
  releaseName: fledge
  values:
    service:
      type: NodePort
      nodePort: 30081
    ui:
      enabled: true
      image:
        repository: registry.example.com/your-project/fledge-gui
        digest: sha256:REPLACE_WITH_THE_64_CHARACTER_PUBLISHED_DIGEST
      service:
        type: NodePort
        nodePort: 30082
```

To use your own image, [build and publish the GUI](building-image.md#build-and-publish-the-optional-gui)
and replace the image selection. Push the change and run `app-flows.py apply`
with the new commit. The chart itself defaults to no GUI and private `ClusterIP`
Services.

- API-only NodePort access works without the GUI.
- Custom NodePorts must be distinct numbers from 30000 to 32767 and free in your
  cluster. A port allocation conflict fails the deployment.
- The GUI requests 10m CPU and 16 MiB memory, with limits of 100m CPU and 64 MiB.

The demo uses UI port `30082` rather than the chart default `30080`, which the
standard ZEDEDA Edge Inference Services business app uses. Changing only a
Service port replaces neither pod and keeps stored data, but a new GUI address
needs Connection Setup and login again.

## LAN access

Your browser must reach the Kubernetes node on both ports. Provide a restricted
network before enabling them: NodePort can expose several node interfaces and
does not enforce LAN-only reachability. This HTTP demo sends credentials and
tokens without encryption. It does not configure firewalls or internet access.

For a fresh instance, complete private initialization first. The API Service
has no ready endpoint until managed credentials work, even with NodePort enabled.
The GUI can load before the API is ready. After setup, check both workloads:

```sh
kubectl -n fledge-demo rollout status deployment/fledge-ui --timeout=120s
kubectl -n fledge-demo get service fledge fledge-ui
```

Open `http://<node-address>:30082` with the node's LAN address, not a cluster
Service DNS name. On EVE-k, confirm the address of the edge node's network
adapter in ZEDEDA; the Kubernetes internal address can differ. See ZEDEDA's
[edge-node inspection instructions](https://help.zededa.com/hc/en-us/articles/36350180833307-ZCLI-Create-and-Manage-Edge-Nodes).
A fresh browser may briefly show the loading screen while the GUI checks its
default API port before it opens Connection Setup. After setup, neither the GUI
nor the API needs port-forwarding.

## Private access only

Set both Service types to `ClusterIP` in the Fleet configuration, push, and run
`app-flows.py apply` with the new commit. Set `ui.enabled: false` to remove the
GUI. The NodePorts disappear; the core pod, PVC and credentials remain. To roll
back a GUI image change on its own, select the earlier GUI digest.

With the GUI enabled privately, keep the API port-forward from setup step 5
running and start this in another terminal with the same `KUBECONFIG`:

```sh
kubectl -n fledge-demo port-forward --address 127.0.0.1 service/fledge-ui 8080:8080
```

Open `http://127.0.0.1:8080`. In **Connection Setup**, use host `127.0.0.1` and
port `8081`. Restart a port-forward if its pod is replaced.

## Select the instance and log in

1. On the login screen, choose **Setup Instance**. If the default API is
   unavailable, the GUI opens **Connection Setup** and shows **Failed to connect**.
2. Select `http`, then enter **Host** and **Port**: the node address and `30081`
   for LAN access, or `127.0.0.1` and `8081` for private forwarding. Use your
   selected API port if you changed it. Click **Set the URL & Restart**.
3. Enter the admin username and the password from the Secret, and click
   **Log In**. The dashboard's **Received** count increases as readings arrive.
   Open **Readings** and use **Most recent reading** beside `gitops-sinusoid` to
   inspect its value. Use the verifier from setup step 5 to check acquisition.

The API selection belongs to this browser profile and GUI address. Reloads and UI
pod replacement keep it; a new browser profile or GUI address needs setup again.
Use **Settings → Connection Setup** to change the selected instance.

## Edit the demo source

Clear **Flow Editor** under **Settings → GUI Settings** to use the list view.
Open **South**, then **gitops-sinusoid**. Its **Basic** and **Advanced** tabs
show the plugin and acquisition settings. Change a field and click **Save**.

Source edits made in the GUI persist across core replacement. Running
`setup-fledge.py configure` later reapplies only the settings declared in its
file, including any of those fields you changed in the GUI. Opening or restarting
the GUI does not run setup or configure sources.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Browser cannot load the GUI | Check `service/fledge-ui`, its NodePort or port-forward, and `deployment/fledge-ui` events and logs. A GUI failure does not mean the core failed. |
| GUI loads but shows **Failed to connect** | Check the Connection Setup host, protocol and API port, core readiness and direct API reachability. A cluster-only DNS name is not reachable from a LAN browser. |
| **Log In** fails | Use the managed credentials for the selected instance. The replaced default admin password is rejected. Do not reset retained data to repair a credential mismatch. |
| NodePort allocation fails | Inspect Service reconciliation events and other Services for the selected ports. Select unused, distinct values, push, and apply the new commit. |
| LAN ports time out | Verify the node address, routing and network restrictions with the network owner. Working port-forward access does not show LAN reachability. |
