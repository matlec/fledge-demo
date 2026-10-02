#!/usr/bin/env bash
# Create the demo OPC UA certificate authority and the certificates it signs.
#
# Usage: scripts/make-opcua-certs.sh [--force] [--namespace NAMESPACE] [DIRECTORY]
#
# DIRECTORY defaults to $FLEDGE_CERT_DIR, or ~/.config/fledge-demo/opcua. It must
# be outside the repository working directory. Existing files are kept unless
# --force is given; --force replaces the authority and every certificate.
#
# Each <name>.der file is a DER certificate and each <name>.pem file is the
# matching PEM private key:
#   demo-ca              certificate authority; keep demo-ca.pem on the workstation
#   demo-ca-crl.der      empty revocation list of the authority
#   pump-simulator       OPC UA server, for the simulator's Secret
#   fledge-opcua-client  OPC UA client, uploaded to Fledge
#   pump-operator        OPC UA client, for the control tool's Secret
set -euo pipefail

usage() { sed -n '4p' "$0" | sed 's/^# //' >&2; exit 2; }

force=false
namespace=fledge-demo
directory=${FLEDGE_CERT_DIR:-$HOME/.config/fledge-demo/opcua}
while (($#)); do
  case $1 in
    --force) force=true ;;
    --namespace) (($# > 1)) || usage; namespace=$2; shift ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) directory=$1 ;;
  esac
  shift
done
[[ $namespace =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || { echo "invalid namespace: $namespace" >&2; exit 2; }

repository=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
target=$(realpath -m -- "$directory")
if [[ $target == "$repository" || $target == "$repository"/* ]]; then
  echo "refusing to write key material inside the repository: $target" >&2
  exit 1
fi

names=(demo-ca pump-simulator fledge-opcua-client pump-operator)
files=(demo-ca-crl.der)
for name in "${names[@]}"; do files+=("$name.der" "$name.pem"); done
if ! $force; then
  for file in "${files[@]}"; do
    if [[ -e $target/$file ]]; then
      echo "$target/$file exists; pass --force to replace all certificates" >&2
      exit 1
    fi
  done
fi

umask 077
mkdir -p -- "$target"
work=$(mktemp -d "$target/.make-opcua-certs.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

ca_days=3650
leaf_days=825
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days "$ca_days" \
  -keyout "$work/demo-ca.pem" -out "$work/demo-ca.crt" \
  -subj '/O=Fledge demo/CN=Fledge demo OPC UA CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' 2>/dev/null
openssl x509 -in "$work/demo-ca.crt" -outform DER -out "$work/demo-ca.der"

# OPC UA application instance certificates (OPC 10000-6, 6.2.2): the URI in
# subjectAltName must equal the application URI. S2OPC in the Fledge plugin
# always uses the application URI fledge.south.s2opcua.
issue() { # name, application URI, extended key usage, extra subjectAltName entries
  local name=$1 uri=$2 usage=$3 extra=${4:-}
  cat > "$work/$name.ext" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
extendedKeyUsage = $usage
subjectAltName = URI:$uri$extra
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
  openssl req -new -newkey rsa:2048 -sha256 -nodes -keyout "$work/$name.pem" \
    -subj "/O=Fledge demo/CN=$name" -out "$work/$name.csr" 2>/dev/null
  openssl x509 -req -in "$work/$name.csr" -CA "$work/demo-ca.crt" -CAkey "$work/demo-ca.pem" \
    -set_serial "0x$(openssl rand -hex 16)" -days "$leaf_days" -sha256 \
    -extfile "$work/$name.ext" -outform DER -out "$work/$name.der" 2>/dev/null
}
issue pump-simulator urn:fledge-demo:pump-simulator serverAuth,clientAuth \
  ",DNS:pump-simulator,DNS:pump-simulator.$namespace,DNS:pump-simulator.$namespace.svc,DNS:pump-simulator.$namespace.svc.cluster.local,DNS:localhost"
issue fledge-opcua-client fledge.south.s2opcua clientAuth
issue pump-operator urn:fledge-demo:pump-operator clientAuth

# S2OPC refuses certificates from an authority without a revocation list.
touch "$work/index.txt"
echo 01 > "$work/crlnumber"
cat > "$work/ca.cnf" <<EOF
[ca]
default_ca = demo
[demo]
database = $work/index.txt
crlnumber = $work/crlnumber
default_md = sha256
default_crl_days = $ca_days
crl_extensions = crl_ext
[crl_ext]
authorityKeyIdentifier = keyid:always
EOF
openssl ca -batch -config "$work/ca.cnf" -gencrl -cert "$work/demo-ca.crt" -keyfile "$work/demo-ca.pem" \
  -out "$work/demo-ca-crl.crl" 2>/dev/null
openssl crl -in "$work/demo-ca-crl.crl" -outform DER -out "$work/demo-ca-crl.der"

for file in "${files[@]}"; do mv -f -- "$work/$file" "$target/$file"; done
echo "created demo OPC UA certificates in $target"
