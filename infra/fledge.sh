#!/usr/bin/env bash
# Run the Fledge REST steps of the docs through a temporary port-forward.
# Usage (from the repository root, with ../.envrc loaded):
#   infra/fledge.sh initialize
#   infra/fledge.sh configure <demos/x.json> [setup-fledge.py options]
#   infra/fledge.sh verify <demos/x.json> [verify-fledge.py options]
set -euo pipefail
cd "$(dirname "$0")/.."

port=18081
api=http://127.0.0.1:$port
kubectl -n fledge-demo port-forward --address 127.0.0.1 deployment/fledge $port:8081 >/dev/null &
pf=$!
trap 'kill $pf 2>/dev/null' EXIT
for _ in $(seq 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $api/fledge/ping)" != 000 ] && break
  sleep 1
done

export FLEDGE_ADMIN_USERNAME=admin
FLEDGE_ADMIN_PASSWORD=$(kubectl -n fledge-demo get secret fledge-admin -o jsonpath='{.data.password}' | base64 -d)
export FLEDGE_ADMIN_PASSWORD

cmd=$1
shift
case $cmd in
initialize) python3 scripts/setup-fledge.py initialize --api $api ;;
configure) python3 scripts/setup-fledge.py configure --api $api --config "$@" ;;
verify) python3 scripts/verify-fledge.py --api $api --config "$@" ;;
*) sed -n '3,6p' "$0" >&2; exit 2 ;;
esac
