# Automated deployment

This folder automates the steps of [Part 1](../docs/1-install-fledge.md),
[Part 2](../docs/2-pump-pipeline.md) and [Part 3](../docs/3-anomaly-scoring.md) with OpenTofu and scripts, for an existing ZKS
cluster. The manual steps in `docs/` stay valid. Use them if you do not use
OpenTofu.

| Manual step | Automation |
|---|---|
| Part 1: create storage and the admin Secret | `main.tf` (from `deploy/prerequisites.yaml`, generated password) |
| Part 1: give App Flows access, deploy Fledge | `main.tf` (`zedcloud_kubernetes_gitops`). The repository is public, so no Git secret is necessary. |
| Part 1: initialize and configure Fledge | `fledge.sh initialize`, `fledge.sh configure` |
| Part 2: create the certificates | `scripts/make-opcua-certs.sh` (unchanged) |
| Part 2: volume, Secrets, deploy the pump demo | `pump-demo.tf` (`-var pump_demo=true`) |
| Part 2: create the InfluxDB tokens | `fledge.sh influx-tokens` |
| Part 2: configure and check Fledge | `fledge.sh configure`, `fledge.sh verify` |
| Part 3: upload and deploy the model | `eip.sh upload`, `submit`, `approve` (sa-demos platform) |
| Part 3: configure and check scoring | `fledge.sh configure`, `fledge.sh verify` |

The settings come from `../.envrc` (direnv): ZEDEDA URL and API token,
`KUBE_CONFIG_PATH` for the cluster, and `FLEDGE_CERT_DIR`.

## Part 1

```sh
cd infra
direnv exec . tofu init
direnv exec . tofu apply -var "revision=$(git rev-parse origin/main)"
cd ..
direnv exec . infra/fledge.sh initialize
direnv exec . infra/fledge.sh configure demos/sinusoid.json
direnv exec . infra/fledge.sh verify demos/sinusoid.json
```

## Part 2

```sh
direnv exec . scripts/make-opcua-certs.sh
cd infra
direnv exec . tofu apply -var "revision=$(git rev-parse origin/main)" -var pump_demo=true
cd ..
direnv exec . infra/fledge.sh influx-tokens
direnv exec . infra/fledge.sh configure demos/pump.json --cert-dir "$FLEDGE_CERT_DIR"
direnv exec . infra/fledge.sh configure demos/influxdb.json
direnv exec . infra/fledge.sh verify demos/pump.json
direnv exec . infra/fledge.sh verify demos/influxdb.json
```

## Part 3

`eip.sh` is in the sa-demos platform repository (`../../platform/scripts`).
It needs the two EIS API keys in `secrets/eip-prod.env`.

```sh
python3 -m venv ~/.cache/fledge-demo/model-venv
~/.cache/fledge-demo/model-venv/bin/pip install -r model/pump-anomaly/requirements.txt
~/.cache/fledge-demo/model-venv/bin/python model/pump-anomaly/generate_model.py ~/.cache/fledge-demo/pump-anomaly

eip=../../platform/scripts/eip.sh
direnv exec . $eip upload pump-anomaly ~/.cache/fledge-demo/pump-anomaly    # prints the version
direnv exec . $eip submit fledge-pump-anomaly pump-anomaly 1 sa-k       # prints the submission id
id=<submission id>
direnv exec . $eip approve $id

# The engine Service is dep-<first 6 characters of the id>-edgeai-inference-openvino-server.
export FLEDGE_INFERENCE_URL=http://dep-${id:0:6}-edgeai-inference-openvino-server.default.svc:8001
direnv exec . infra/fledge.sh configure demos/inference.json
direnv exec . infra/fledge.sh verify demos/inference.json
```

If the upload prints a version other than 1, set `version` in
`demos/inference.json` to match.

The tofu state contains the generated passwords and the InfluxDB admin token.
Git ignores it.
