#!/usr/bin/env bash
# Run the Fledge steps of the docs through temporary port-forwards.
# Usage (from the repository root, with ../.envrc loaded):
#   infra/fledge.sh initialize
#   infra/fledge.sh configure <demos/x.json> [setup-fledge.py options]
#   infra/fledge.sh verify <demos/x.json> [verify-fledge.py options]
#   infra/fledge.sh influx-tokens      Part 2: InfluxDB tokens for Fledge and Grafana
set -euo pipefail
cd "$(dirname "$0")/.."

ns=fledge-demo
# secret <name> <key>: the value, or nothing if the Secret does not exist yet.
secret() { kubectl -n $ns get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null | base64 -d || true; }

# forward <deployment> <local-port> <remote-port>: stays open until the script ends.
pids=()
trap 'kill "${pids[@]}" 2>/dev/null' EXIT
forward() {
  kubectl -n $ns port-forward --address 127.0.0.1 "deployment/$1" "$2:$3" >/dev/null &
  pids+=($!)
  for _ in $(seq 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$2/")" != 000 ] && return
    sleep 1
  done
}

# Step 4 of docs/2-pump-pipeline.md. $1: write or read, $2: description, $3: Secret.
influxdb_token_secret() {
  kubectl -n $ns get secret "$3" >/dev/null 2>&1 && { echo "$3 exists"; return; }
  token=$(kubectl -n $ns exec deployment/influxdb -- sh -c '
    export INFLUX_ORG="$DOCKER_INFLUXDB_INIT_ORG" INFLUX_TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"
    bucket=$(influx bucket list --name "$DOCKER_INFLUXDB_INIT_BUCKET" --hide-headers | cut -f1)
    influx auth create --'"$1"'-bucket "$bucket" --description "'"$2"'" --json' | jq -er .token)
  kubectl -n $ns create secret generic "$3" --from-literal=token="$token" >/dev/null
  echo "$3 created"
}

api=http://127.0.0.1:18081
export FLEDGE_ADMIN_USERNAME=admin
FLEDGE_ADMIN_PASSWORD=$(secret fledge-admin password)
FLEDGE_INFLUXDB_TOKEN=$(secret fledge-influxdb token)
FLEDGE_INFLUXDB_READ_TOKEN=$(secret grafana-influxdb token)
export FLEDGE_ADMIN_PASSWORD FLEDGE_INFLUXDB_TOKEN FLEDGE_INFLUXDB_READ_TOKEN

cmd=$1
shift
case $cmd in
initialize)
  forward fledge 18081 8081
  python3 scripts/setup-fledge.py initialize --api $api
  ;;
configure)
  forward fledge 18081 8081
  python3 scripts/setup-fledge.py configure --api $api --config "$@"
  ;;
verify)
  forward fledge 18081 8081
  if grep -q influxdb "$1"; then
    forward influxdb 18086 8086
    set -- "$@" --influxdb http://127.0.0.1:18086
  fi
  python3 scripts/verify-fledge.py --api $api --config "$@"
  ;;
influx-tokens)
  kubectl -n $ns rollout status deployment/influxdb --timeout=1800s
  influxdb_token_secret write fledge-write fledge-influxdb
  influxdb_token_secret read grafana-read grafana-influxdb
  kubectl -n $ns rollout status deployment/grafana --timeout=600s
  ;;
*) sed -n '3,7p' "$0" >&2; exit 2 ;;
esac
