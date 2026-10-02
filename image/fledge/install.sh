#!/bin/sh
set -eu
case "$BASE_IMAGE" in *@sha256:*) ;; *) echo 'base image must be pinned by digest' >&2; exit 1;; esac
# Version pins and duplicate identities are checked before package installation.
python3 - <<'PY'
import json,os,pathlib,re
pins=os.environ['PLUGIN_PACKAGES'].split()
seen=set()
for pin in pins:
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*=[0-9][A-Za-z0-9.+:~_-]*',pin):
        raise SystemExit('plugin packages require explicit name=version pins')
    name=pin.split('=',1)[0]
    if name in seen: raise SystemExit('conflicting or duplicate package identity')
    seen.add(name)
# The pinned base has paired native/Python storage wrappers. Preserve those
# existing pairs while rejecting newly introduced conflicts in every category.
def identities(root):
    return {(kind.name, plugin.name) for kind in root.iterdir() if kind.is_dir()
            for plugin in kind.iterdir() if plugin.is_dir() and plugin.name != '__pycache__'}
native=pathlib.Path('/usr/local/fledge/plugins')
python=pathlib.Path('/usr/local/fledge/python/fledge/plugins')
baseline=identities(native) & identities(python)
pathlib.Path('/tmp/fledge-plugin-identities.json').write_text(json.dumps(sorted(baseline)))
PY
find /usr/local/fledge/data -type f -exec sha256sum {} + | sort > /tmp/data-before
cd /opt/fledge-packages
if [ -s SHA256SUMS ]; then sha256sum --strict -c SHA256SUMS; fi
# Resolve only this pinned local package set and the digest-pinned base. Missing
# versions or dependencies fail; no network repository can substitute artifacts.
if [ -n "$PLUGIN_PACKAGES" ]; then
    mkdir -p /tmp/fledge-apt/lists/partial
    printf '%s\n' 'deb [trusted=yes] file:/opt/fledge-packages ./' > /tmp/fledge-apt/sources.list
    apt-get -o Dir::Etc::sourcelist=/tmp/fledge-apt/sources.list -o Dir::Etc::sourceparts=- -o Dir::State::lists=/tmp/fledge-apt/lists update
    # Intentional splitting: only validated package=version words reach apt.
    apt-get -y --no-install-recommends -o Dir::Etc::sourcelist=/tmp/fledge-apt/sources.list -o Dir::Etc::sourceparts=- -o Dir::State::lists=/tmp/fledge-apt/lists install $PLUGIN_PACKAGES
fi
find /usr/local/fledge/data -type f -exec sha256sum {} + | sort > /tmp/data-after
cmp /tmp/data-before /tmp/data-after || { echo 'package hooks changed instance state' >&2; exit 1; }
rm /tmp/data-before /tmp/data-after
ldconfig
mkdir -p /usr/share/fledge-bundle
cp sources.sha256 SHA256SUMS builder-packages.tsv build-packages.txt build-packages.sha256 /usr/share/fledge-bundle/
python3 - <<'PY'
import json,os,pathlib,subprocess
root=pathlib.Path('/usr/share/fledge-bundle')
packages=[dict(zip(('name','version','architecture'),line.split('\t'))) for line in subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\t${Architecture}\n'],text=True).splitlines()]
(root/'inventory.json').write_text(json.dumps({'base':os.environ['BASE_IMAGE'],'selected':os.environ['PLUGIN_PACKAGES'].split(),'packages':packages},indent=2)+'\n')
def identities(root):
    return {(kind.name, plugin.name) for kind in root.iterdir() if kind.is_dir()
            for plugin in kind.iterdir() if plugin.is_dir() and plugin.name != '__pycache__'}
native=pathlib.Path('/usr/local/fledge/plugins')
python=pathlib.Path('/usr/local/fledge/python/fledge/plugins')
baseline={tuple(pair) for pair in json.loads(pathlib.Path('/tmp/fledge-plugin-identities.json').read_text())}
conflicts=(identities(native) & identities(python)) - baseline
assert not conflicts, 'conflicting native/Python plugin identities: ' + ', '.join('/'.join(pair) for pair in sorted(conflicts))
PY
rm -rf /tmp/fledge-apt /tmp/fledge-plugin-identities.json
