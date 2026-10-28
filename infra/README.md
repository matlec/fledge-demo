# Automated deployment

This folder automates the cluster steps of [Part 1](../docs/1-install-fledge.md)
with OpenTofu, for an existing ZKS cluster. The manual steps in `docs/` stay
valid. Use them if you do not use OpenTofu.

| Manual step | Automation |
|---|---|
| Create storage and the admin Secret | `main.tf` (from `deploy/prerequisites.yaml`, generated password) |
| Give App Flows access, deploy Fledge | `main.tf` (`zedcloud_kubernetes_gitops`). The repository is public, so no Git secret is necessary. |
| Initialize and configure Fledge | `fledge.sh` (port-forward, then `scripts/setup-fledge.py`) |

The settings come from `../.envrc` (direnv): ZEDEDA URL and API token, and
`KUBE_CONFIG_PATH` for the cluster.

```sh
cd infra
direnv exec . tofu init
direnv exec . tofu apply -var "revision=$(git rev-parse origin/main)"
cd ..
direnv exec . infra/fledge.sh initialize
direnv exec . infra/fledge.sh configure demos/sinusoid.json
direnv exec . infra/fledge.sh verify demos/sinusoid.json
```

The tofu state contains the generated admin password. Git ignores it.
