# syntax=docker/dockerfile:1
ARG BASE_IMAGE=fledgeiot/fledge@sha256:322dca6485e830d3cef23a9067ffa1b2df7a0ae52d615cacd7d4887fadd01e63
ARG PLUGIN_PACKAGES=""
FROM ${BASE_IMAGE} AS builder
ARG PLUGIN_PACKAGES
USER root
WORKDIR /src
COPY build-packages.txt build-packages.sha256 /build-lock/
COPY sources.sha256 ./
COPY sinusoid /src/sinusoid
COPY packages/ /extra-packages/
COPY package.sh /src/package.sh
# Reject malformed selections before acquiring any optional build inputs.
RUN python3 - <<'PYTHON'
import os,re
seen=set()
for pin in os.environ['PLUGIN_PACKAGES'].split():
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*=[0-9][A-Za-z0-9.+:~_-]*', pin):
        raise SystemExit('plugin packages require explicit name=version pins')
    name=pin.split('=', 1)[0]
    if name in seen:
        raise SystemExit('conflicting or duplicate package identity')
    seen.add(name)
PYTHON
# Source fallback recipes are explicit Dockerfile steps. Omitted plugins have no
# downloads, compiler installation, source builds or package output.
RUN <<'SH'
set -eu
mkdir -p /packages
: > /packages/sources.sha256
: > /packages/build-packages.txt
: > /packages/build-packages.sha256
# Normalize the same whitespace-separated package list accepted by apt.
set -- $PLUGIN_PACKAGES
selection=" $* "
case "$selection" in
    *" fledge-south-s2opcua=3.1.0 "*)
        # The Ubuntu archive drops superseded versions; its snapshot service keeps
        # the pinned ones. The checksums below still verify every package.
        snapshot=20260923T000000Z
        sed -e "s|http://archive.ubuntu.com/ubuntu/|http://snapshot.ubuntu.com/ubuntu/$snapshot/|" \
            -e "s|http://security.ubuntu.com/ubuntu/|http://snapshot.ubuntu.com/ubuntu/$snapshot/|" \
            /etc/apt/sources.list > /tmp/build-snapshot.list
        apt-get -o Dir::Etc::sourcelist=/tmp/build-snapshot.list -o Dir::Etc::sourceparts=- update -qq
        mkdir /build-debs
        cd /build-debs
        xargs apt-get -o Dir::Etc::sourcelist=/tmp/build-snapshot.list -o Dir::Etc::sourceparts=- download < /build-lock/build-packages.txt
        sha256sum --strict -c /build-lock/build-packages.sha256
        apt-get -o Dir::Cache::archives=/build-debs --no-download -y --no-install-recommends install ./*.deb
        cd /src
        rm -rf /build-debs
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o s2opcua.tar.gz https://api.github.com/repos/fledge-iot/fledge-south-s2opcua/tarball/9c1773f73075b36ab97d91c88b9aa6408f9bd41a
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o core.tar.gz https://api.github.com/repos/fledge-iot/fledge/tarball/4e34f009d742d11cb4e8e15ea026a0dcb23ee4a5
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o mbedtls.tar.gz https://api.github.com/repos/Mbed-TLS/mbedtls/tarball/v2.28.7
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o expat.tar.gz https://api.github.com/repos/libexpat/libexpat/tarball/R_2_6_0
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o toolkit.tar.gz https://gitlab.com/systerel/S2OPC/-/archive/6dfd44155df6ad47e73086cb5756411dce2c9409/S2OPC-6dfd44155df6ad47e73086cb5756411dce2c9409.tar.gz
        sha256sum --strict -c sources.sha256
        for name in s2opcua core mbedtls expat toolkit; do
            mkdir "$name"
            tar -xzf "$name.tar.gz" --strip-components=1 -C "$name"
        done
        cmake -S mbedtls -B mbedtls/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DUSE_SHARED_MBEDTLS_LIBRARY=OFF -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF
        cmake --build mbedtls/build -j4
        cmake --install mbedtls/build
        cmake -S expat/expat -B expat/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DEXPAT_SHARED_LIBS=OFF -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=OFF -DEXPAT_BUILD_TOOLS=OFF
        cmake --build expat/build -j4
        cmake --install expat/build
        cmake -S toolkit -B toolkit/build -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTING=OFF -DENABLE_SAMPLES=OFF -DS2OPC_CLIENTSERVER_ONLY=ON
        cmake --build toolkit/build -j4
        cmake --install toolkit/build
        cp toolkit/src/ClientServer/frontend/client_wrapper/libs2opc_client_config_custom.h /usr/local/include/s2opc/clientserver/
        mkdir -p core/cmake_build/C
        ln -s /usr/local/fledge/lib core/cmake_build/C/lib
        cmake -S s2opcua -B s2opcua/build -DFLEDGE_SRC=/src/core
        cmake --build s2opcua/build -j4

        # Ordinary Debian packages without maintainer scripts or instance setup.
        mkdir -p /pkg/toolkit/DEBIAN /pkg/s2opcua/DEBIAN
        cat > /pkg/toolkit/DEBIAN/control <<'CONTROL'
Package: fledge-s2opcua
Version: 1.5.0
Architecture: amd64
Maintainer: Fledge demo maintainers
Depends: libc6 (>= 2.35)
Description: S2OPC toolkit with pinned static mbedTLS and Expat dependencies
CONTROL
        cat > /pkg/s2opcua/DEBIAN/control <<'CONTROL'
Package: fledge-south-s2opcua
Version: 3.1.0
Architecture: amd64
Maintainer: Fledge demo maintainers
Depends: fledge (= 3.1.0), fledge-s2opcua (= 1.5.0)
Description: Fledge S2OPC OPC UA south plugin
CONTROL
        mkdir -p /pkg/toolkit/usr/local/lib /pkg/s2opcua/usr/local/fledge/plugins/south/s2opcua
        cp -a /usr/local/lib/libs2opc*.so* /pkg/toolkit/usr/local/lib/
        cp -a /src/s2opcua/build/libs2opcua.so* /pkg/s2opcua/usr/local/fledge/plugins/south/s2opcua/
        for name in toolkit s2opcua; do
            mkdir -p "/pkg/$name/usr/share/fledge-bundle/licenses/$name"
        done
        cp /src/s2opcua/LICENSE /pkg/s2opcua/usr/share/fledge-bundle/licenses/s2opcua/LICENSE
        cp /src/toolkit/LICENSE /src/toolkit/NOTICE /pkg/toolkit/usr/share/fledge-bundle/licenses/toolkit/
        cp /src/mbedtls/LICENSE /pkg/toolkit/usr/share/fledge-bundle/licenses/toolkit/mbedtls-LICENSE
        cp /src/expat/expat/COPYING /pkg/toolkit/usr/share/fledge-bundle/licenses/toolkit/expat-COPYING
        for name in toolkit s2opcua; do dpkg-deb --root-owner-group --build "/pkg/$name" /packages; done
        cp /src/sources.sha256 /packages/
        cp /build-lock/build-packages.txt /build-lock/build-packages.sha256 /packages/
        ;;
esac
case "$selection" in
    *" fledge-south-sinusoid=3.1.0 "*)
        mkdir -p /pkg/sinusoid/DEBIAN
        cat > /pkg/sinusoid/DEBIAN/control <<'CONTROL'
Package: fledge-south-sinusoid
Version: 3.1.0
Architecture: all
Maintainer: Fledge demo maintainers
Depends: fledge (= 3.1.0)
Description: Fledge sinusoid south plugin
CONTROL
        mkdir -p /pkg/sinusoid/usr/local/fledge/python/fledge/plugins/south/sinusoid /pkg/sinusoid/usr/share/fledge-bundle/licenses/sinusoid
        cd /src/sinusoid
        printf '%s\n' '3b5b7188683b43aac78ada586f7161c6ec64f2a5eee6ecf79e7c3a2ce50deee2  sinusoid.py' | sha256sum -c -
        cp __init__.py sinusoid.py LICENSE /pkg/sinusoid/usr/local/fledge/python/fledge/plugins/south/sinusoid/
        cp LICENSE /pkg/sinusoid/usr/share/fledge-bundle/licenses/sinusoid/LICENSE
        dpkg-deb --root-owner-group --build /pkg/sinusoid /packages
        sha256sum sinusoid.py >> /packages/sources.sha256
        ;;
esac
SH
# Plugins maintained in this repository. A separate step keeps the layers above
# cached when only this source changes.
COPY influxdb /src/influxdb
COPY pump-inference /src/pump-inference
RUN <<'SH'
set -eu
set -- $PLUGIN_PACKAGES
selection=" $* "
case "$selection" in
    *" fledge-north-influxdb=1.0.0 "*)
        # Maintained in this repository; SHA256SUMS catches a source that
        # changed without its checksums being updated.
        mkdir -p /pkg/influxdb/DEBIAN
        cat > /pkg/influxdb/DEBIAN/control <<'CONTROL'
Package: fledge-north-influxdb
Version: 1.0.0
Architecture: all
Maintainer: Fledge demo maintainers
Depends: fledge (= 3.1.0)
Description: Fledge north plugin that writes readings to InfluxDB 2
CONTROL
        mkdir -p /pkg/influxdb/usr/local/fledge/python/fledge/plugins/north/influxdb /pkg/influxdb/usr/share/fledge-bundle/licenses/influxdb
        cd /src/influxdb
        sha256sum --strict -c SHA256SUMS
        cp __init__.py influxdb.py /pkg/influxdb/usr/local/fledge/python/fledge/plugins/north/influxdb/
        cp LICENSE /pkg/influxdb/usr/share/fledge-bundle/licenses/influxdb/LICENSE
        dpkg-deb --root-owner-group --build /pkg/influxdb /packages
        sed 's|  |  influxdb/|' SHA256SUMS >> /packages/sources.sha256
        ;;
esac
case "$selection" in
    *" fledge-north-pump-inference=1.0.2 "*)
        # Maintained in this repository, like the InfluxDB plugin.
        mkdir -p /pkg/pump-inference/DEBIAN
        cat > /pkg/pump-inference/DEBIAN/control <<'CONTROL'
Package: fledge-north-pump-inference
Version: 1.0.2
Architecture: all
Maintainer: Fledge demo maintainers
Depends: fledge (= 3.1.0)
Description: Fledge north plugin that scores pump readings with an anomaly model
CONTROL
        mkdir -p /pkg/pump-inference/usr/local/fledge/python/fledge/plugins/north/pump_inference /pkg/pump-inference/usr/share/fledge-bundle/licenses/pump-inference
        cd /src/pump-inference
        sha256sum --strict -c SHA256SUMS
        cp __init__.py pump_inference.py /pkg/pump-inference/usr/local/fledge/python/fledge/plugins/north/pump_inference/
        cp LICENSE /pkg/pump-inference/usr/share/fledge-bundle/licenses/pump-inference/LICENSE
        dpkg-deb --root-owner-group --build /pkg/pump-inference /packages
        sed 's|  |  pump-inference/|' SHA256SUMS >> /packages/sources.sha256
        ;;
esac
SH
# Upstream's HTTP south plugin, which ingests the pump scores. It is pure Python;
# its imports (aiohttp, aiohttp_cors, numpy) are already in the base image.
RUN <<'SH'
set -eu
set -- $PLUGIN_PACKAGES
selection=" $* "
case "$selection" in
    *" fledge-south-http-south=3.1.0 "*)
        cd /src
        curl -fL --retry 3 --connect-timeout 30 --max-time 180 -o http_south.tar.gz https://api.github.com/repos/fledge-iot/fledge-south-http/tarball/e4f028dc515230e4ff2bb36b16e241c113af7cd4
        printf '%s\n' '86d7eab0b612039912df1bf48116f34b1cc67141a37e4f6a8ae04f9c31704e3c  http_south.tar.gz' | sha256sum --strict -c -
        mkdir http_south
        tar -xzf http_south.tar.gz --strip-components=1 -C http_south
        mkdir -p /pkg/http-south/DEBIAN
        cat > /pkg/http-south/DEBIAN/control <<'CONTROL'
Package: fledge-south-http-south
Version: 3.1.0
Architecture: all
Maintainer: Fledge demo maintainers
Depends: fledge (= 3.1.0)
Description: Fledge HTTP south plugin (upstream fledge-south-http 3.1.0)
CONTROL
        mkdir -p /pkg/http-south/usr/local/fledge/python/fledge/plugins/south/http_south /pkg/http-south/usr/share/fledge-bundle/licenses/http-south
        cp http_south/python/fledge/plugins/south/http_south/__init__.py http_south/python/fledge/plugins/south/http_south/http_south.py \
            /pkg/http-south/usr/local/fledge/python/fledge/plugins/south/http_south/
        cp http_south/LICENSE /pkg/http-south/usr/share/fledge-bundle/licenses/http-south/LICENSE
        dpkg-deb --root-owner-group --build /pkg/http-south /packages
        sha256sum http_south.tar.gz >> /packages/sources.sha256
        ;;
esac
SH
RUN sh /src/package.sh

FROM ${BASE_IMAGE} AS packaged
ARG BASE_IMAGE
ARG PLUGIN_PACKAGES
USER root
COPY install.sh /tmp/install-fledge-plugins.sh
RUN --mount=from=builder,source=/packages,target=/opt/fledge-packages sh /tmp/install-fledge-plugins.sh && rm /tmp/install-fledge-plugins.sh
# Check the source-built OPC UA plugin when selected. Keep plugin-specific
# identity and native loading checks outside the generic package installer.
RUN python3 - <<'PY'
import json,os,pathlib,subprocess
plugin=pathlib.Path('/usr/local/fledge/plugins/south/s2opcua/libs2opcua.so')
if plugin.exists():
    env=dict(os.environ, LD_LIBRARY_PATH='/usr/local/fledge/lib:/usr/local/lib')
    output=subprocess.check_output(['/usr/local/fledge/extras/C/get_plugin_info',str(plugin)],text=True,env=env)
    info=json.loads(output)
    assert info['name']=='s2opcua' and info['version']=='3.1.0'
    deps=subprocess.check_output(['ldd',str(plugin)],text=True,env=env)
    assert 'not found' not in deps, 'native runtime dependency missing'
PY
COPY runtime.py /opt/fledge/runtime.py
# The published core ships generated certificate/key files in its data directory.
# Copy the cleaned filesystem into a fresh final layer so those bytes are absent
# from the distributable image's layers, not merely hidden by deletion markers.
RUN rm -rf /usr/local/fledge/data/* && rm -f /var/log/apt/* /var/log/dpkg.log
FROM scratch
COPY --from=packaged / /
ENV PATH=/usr/local/fledge/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive FLEDGE_ROOT=/usr/local/fledge \
    FLEDGE_DATA=/usr/local/fledge/data PYTHONPATH=/usr/local/fledge/python \
    LC_ALL=C.UTF-8 LANG=C.UTF-8
VOLUME ["/usr/local/fledge/data", "/var/log"]
HEALTHCHECK NONE
ENTRYPOINT ["python3", "/opt/fledge/runtime.py", "run"]
