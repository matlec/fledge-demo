# Automates the cluster steps of docs/1-install-fledge.md for an existing
# ZKS cluster: the prerequisites, the admin Secret and the App Flows
# registration. The Fledge setup through its REST API stays a script step.
#
# Settings come from ../.envrc (cluster URL and token, KUBE_CONFIG_PATH).

terraform {
  required_version = ">= 1.10"

  required_providers {
    zedcloud = {
      source  = "zededa/zedcloud"
      version = "2.8.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}

provider "zedcloud" {}
provider "kubernetes" {}

variable "cluster_name" {
  description = "ZKS cluster (Edge Kubernetes > Clusters)."
  type        = string
  default     = "sa-k"
}

variable "project_name" {
  description = "ZEDEDA project of the cluster."
  type        = string
  default     = "sa-k"
}

variable "repository_url" {
  description = "Git repository that App Flows deploys from. Public, so no Git secret."
  type        = string
  default     = "https://github.com/matlec/fledge-demo.git"
}

variable "revision" {
  description = "Full commit SHA that App Flows deploys (git rev-parse HEAD, pushed)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.revision))
    error_message = "Use a full 40-character commit SHA."
  }
}

# The provider finds the project by name. It requires the other arguments,
# but does not use them for the lookup.
data "zedcloud_project" "this" {
  name  = var.project_name
  title = var.project_name
  type  = "TAG_TYPE_PROJECT"
}

# Namespace and PVC, from the same file that the manual steps use. The
# namespace is a separate resource, so that it exists before the PVC.
locals {
  prerequisites = provider::kubernetes::manifest_decode_multi(file("${path.module}/../deploy/prerequisites.yaml"))
}

resource "kubernetes_manifest" "namespace" {
  manifest = one([for m in local.prerequisites : m if m.kind == "Namespace"])
}

resource "kubernetes_manifest" "prerequisites" {
  for_each   = { for m in local.prerequisites : "${m.kind}/${m.metadata.name}" => m if m.kind != "Namespace" }
  manifest   = each.value
  depends_on = [kubernetes_manifest.namespace]
}

resource "random_password" "fledge_admin" {
  length  = 24
  special = false
}

resource "kubernetes_secret_v1" "fledge_admin" {
  metadata {
    name      = "fledge-admin"
    namespace = "fledge-demo"
  }
  data = {
    username = "admin"
    # Fledge requires upper and lower case, a digit and a special character.
    password = "${random_password.fledge_admin.result}aA1!"
  }
  depends_on = [kubernetes_manifest.prerequisites]
}

resource "zedcloud_kubernetes_gitops" "fledge" {
  metadata {
    name = "fledge-demo"
  }
  data {
    project_id = data.zedcloud_project.this.id
    # The server returns this default. Setting it avoids a plan diff.
    auth {
      insecure_skip_tls_verify = false
    }
    repo_details {
      url       = var.repository_url
      commit    = var.revision
      path_list = ["deploy/fledge"]
    }
    target {
      type  = "GIT_REPO_TARGET_TYPE_CLUSTER"
      value = var.cluster_name
    }
  }
  depends_on = [kubernetes_secret_v1.fledge_admin]
}
