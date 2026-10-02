#!/bin/sh
set -eu
mkdir -p /packages
# Optional additional pinned Debian artifacts use the standard SHA256SUMS format.
for artifact in /extra-packages/*.deb; do
    [ -e "$artifact" ] || continue
    (cd /extra-packages && sha256sum "$(basename "$artifact")" | grep -Fx -f SHA256SUMS)
    cp "$artifact" /packages/
done
cd /packages
# Write a standard Debian package index using the tools already in the base.
# Indexing a Python-only or empty selection does not need a compiler toolchain.
python3 - <<'PYTHON'
import hashlib,pathlib,subprocess
records=[]
checksums=[]
seen=set()
for artifact in sorted(pathlib.Path('.').glob('*.deb')):
    name=subprocess.check_output(['dpkg-deb', '-f', str(artifact), 'Package'], text=True).strip()
    if name in seen:
        raise SystemExit('conflicting plugin/dependency package identities')
    seen.add(name)
    control=subprocess.check_output(['dpkg-deb', '-f', str(artifact)], text=True).rstrip()
    checksum=hashlib.sha256(artifact.read_bytes()).hexdigest()
    filename='./' + artifact.name
    records.append(f'{control}\nFilename: {filename}\nSize: {artifact.stat().st_size}\nSHA256: {checksum}\n\n')
    checksums.append(f'{checksum}  {filename}\n')
pathlib.Path('Packages').write_text(''.join(records))
pathlib.Path('SHA256SUMS').write_text(''.join(checksums))
PYTHON
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > builder-packages.tsv
