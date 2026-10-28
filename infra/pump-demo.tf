# Part 2 (docs/2-pump-pipeline.md): volume, Secrets and App Flows registration
# of the pump demo. Create the certificates first: scripts/make-opcua-certs.sh.
# The InfluxDB tokens need the running InfluxDB: infra/fledge.sh influx-tokens.

variable "pump_demo" {
  description = "Deploy Part 2, the pump data pipeline."
  type        = bool
  default     = false
}

variable "cert_dir" {
  description = "Directory of scripts/make-opcua-certs.sh (FLEDGE_CERT_DIR)."
  type        = string
  default     = "~/.config/fledge-demo/opcua"
}

locals {
  pump        = var.pump_demo ? { this = true } : {}
  cert        = pathexpand(var.cert_dir)
  pump_prereq = provider::kubernetes::manifest_decode_multi(file("${path.module}/../deploy/pump-demo-prerequisites.yaml"))
}

resource "kubernetes_manifest" "pump_prerequisites" {
  for_each   = var.pump_demo ? { for m in local.pump_prereq : "${m.kind}/${m.metadata.name}" => m } : {}
  manifest   = each.value
  depends_on = [kubernetes_manifest.namespace]
}

resource "kubernetes_secret_v1" "pump_simulator_server" {
  for_each = local.pump
  metadata {
    name      = "pump-simulator-server"
    namespace = "fledge-demo"
  }
  binary_data = {
    for f in ["pump-simulator.der", "pump-simulator.pem", "demo-ca.der", "demo-ca-crl.der"] :
    f => filebase64("${local.cert}/${f}")
  }
  depends_on = [kubernetes_manifest.namespace]
}

resource "kubernetes_secret_v1" "pump_simulator_operator" {
  for_each = local.pump
  metadata {
    name      = "pump-simulator-operator"
    namespace = "fledge-demo"
  }
  binary_data = {
    for f in ["pump-operator.der", "pump-operator.pem"] : f => filebase64("${local.cert}/${f}")
  }
  depends_on = [kubernetes_manifest.namespace]
}

resource "random_password" "influxdb_admin" {
  for_each = local.pump
  length   = 24
  special  = false
}

resource "random_id" "influxdb_token" {
  for_each    = local.pump
  byte_length = 32
}

resource "random_password" "grafana_admin" {
  for_each = local.pump
  length   = 24
  special  = false
}

resource "kubernetes_secret_v1" "influxdb_admin" {
  for_each = local.pump
  metadata {
    name      = "influxdb-admin"
    namespace = "fledge-demo"
  }
  data = {
    username = "admin"
    password = random_password.influxdb_admin[each.key].result
    token    = random_id.influxdb_token[each.key].hex
  }
  depends_on = [kubernetes_manifest.namespace]
}

resource "kubernetes_secret_v1" "grafana_admin" {
  for_each = local.pump
  metadata {
    name      = "grafana-admin"
    namespace = "fledge-demo"
  }
  data = {
    password = random_password.grafana_admin[each.key].result
  }
  depends_on = [kubernetes_manifest.namespace]
}

resource "zedcloud_kubernetes_gitops" "pump_demo" {
  for_each = local.pump
  metadata {
    name = "pump-demo"
  }
  data {
    project_id = data.zedcloud_project.this.id
    auth {
      insecure_skip_tls_verify = false
    }
    repo_details {
      url       = var.repository_url
      commit    = var.revision
      path_list = ["deploy/pump-demo"]
    }
    target {
      type  = "GIT_REPO_TARGET_TYPE_CLUSTER"
      value = var.cluster_name
    }
  }
  depends_on = [
    kubernetes_manifest.pump_prerequisites,
    kubernetes_secret_v1.pump_simulator_server,
    kubernetes_secret_v1.pump_simulator_operator,
    kubernetes_secret_v1.influxdb_admin,
    kubernetes_secret_v1.grafana_admin,
  ]
}
