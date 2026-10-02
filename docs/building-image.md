# Build Fledge images

Build an AMD64 image with the plugins your instance needs, publish it to your
registry, and supply its digest to the chart. You need Docker with BuildKit and
registry access to pull the build images. Selecting s2opcua also requires access
to Ubuntu's snapshot archive (`snapshot.ubuntu.com`), GitHub and GitLab for its
compiler and source inputs. The snapshot archive keeps the exact package versions
that the build pins, after the regular Ubuntu archive has replaced them. You need registry
push permission if you choose to publish.

## Select plugins and build

Run from the repository root. `PLUGIN_PACKAGES` is empty by default, so a plain
build adds no optional plugins to the pinned Fledge core:

```sh
docker build --platform linux/amd64 -t fledge-bundle:core image/fledge
```

Set `PLUGIN_PACKAGES` to a quoted, space-separated list of exact `name=version`
pins. To build the demo image, explicitly select sinusoid 3.1.0, s2opcua 3.1.0,
the InfluxDB north plugin 1.0.0, the pump inference north plugin 1.0.2 and the
HTTP south plugin 3.1.0:

```sh
docker build --platform linux/amd64 -t fledge-bundle:local \
  --build-arg 'PLUGIN_PACKAGES=fledge-south-sinusoid=3.1.0 fledge-south-s2opcua=3.1.0 fledge-north-influxdb=1.0.0 fledge-north-pump-inference=1.0.2 fledge-south-http-south=3.1.0' \
  image/fledge
```

The package manager resolves dependencies from the bundled, pinned artifacts and
the base image. Missing versions or dependencies fail the build. An image without
sinusoid can run, but it cannot configure the supplied sinusoid demo. Omitted
fallback plugins are not downloaded, compiled or packaged. A sinusoid-only build
skips the OPC UA sources and compiler toolchain. Omitting `PLUGIN_PACKAGES` or
passing `--build-arg PLUGIN_PACKAGES=` builds none of the supplied plugins.

The InfluxDB north plugin is maintained in this repository, in
`image/fledge/influxdb/`. Its `SHA256SUMS` file pins the source that the build
packages; a source that doesn't match it fails the build. After you change the
plugin, update the file and run its unit tests:

```sh
(cd image/fledge/influxdb && sha256sum __init__.py influxdb.py > SHA256SUMS)
python3 -m unittest tests.test_influxdb_plugin
```

The pump inference plugin, `pump_inference` in `image/fledge/pump-inference/`,
works the same way:

```sh
(cd image/fledge/pump-inference && sha256sum __init__.py pump_inference.py > SHA256SUMS)
python3 -m unittest tests.test_pump_inference_plugin
```

The HTTP south plugin comes from a pinned commit of upstream
`fledge-south-http`. The build downloads it and checks it against a SHA-256
checksum in the Dockerfile. Its imports are already in the base image.

To add another compatible Debian plugin, place its exact `.deb` artifact and any
additional dependencies in `image/fledge/packages/`. Record their verified SHA-256
checksums in that directory's standard `SHA256SUMS` file, then include the plugin's
`name=version` in `PLUGIN_PACKAGES`. Keep those artifacts available to every builder.
Duplicate package identities are rejected. No chart or Dockerfile edit is needed
for this package installation path.

Plugins requiring source compilation or special installation need corresponding
Dockerfile instructions or builder stages. Package hooks must not create instance
state. Before deployment, check that the plugin's native libraries load, that it
appears in the inventory, and that the [integration tests](testing.md) pass. Disable or remove persisted sources before omitting their plugin
from a replacement image.

The remaining commands use `fledge-bundle:local`. If you built a different tag,
substitute it when inspecting and publishing the image. Inspect the selected
inputs and installed package inventory:

```sh
docker run --rm --entrypoint cat fledge-bundle:local \
  /usr/share/fledge-bundle/inventory.json
```

The image contains the runtime and selected plugins. Credentials and source configuration
are supplied after deployment using the [operator setup steps](../README.md#5-initialize-and-configure-fledge).

## Publish and select the image

The chart needs an image built from this repository: the upstream Fledge core
image alone does not implement the chart's lifecycle contract. Choose a registry
and repository you control. Authenticate through your registry's normal Docker
login procedure, then replace the example repository:

The chart selects the image by digest, so the tag is only a label. Use the
Fledge version and move the tag when you publish a new build:

```sh
export FLEDGE_IMAGE_REPOSITORY=registry.example.com/your-project/fledge
export FLEDGE_IMAGE_TAG=3.1.0
docker tag fledge-bundle:local "$FLEDGE_IMAGE_REPOSITORY:$FLEDGE_IMAGE_TAG"
docker push "$FLEDGE_IMAGE_REPOSITORY:$FLEDGE_IMAGE_TAG"
docker image inspect "$FLEDGE_IMAGE_REPOSITORY:$FLEDGE_IMAGE_TAG" \
  --format '{{json .RepoDigests}}'
```

Use the digest associated with your published repository. Set these values in
[chart values](../deploy/fledge/chart/values.yaml) before publishing its Git revision:

```yaml
image:
  repository: registry.example.com/your-project/fledge
  digest: sha256:REPLACE_WITH_THE_64_CHARACTER_PUBLISHED_DIGEST
```

For a private registry, provision its Kubernetes pull Secret in the application
namespace and list it under `imagePullSecrets`. The chart requires a repository and
digest; it does not select plugins or install packages at startup.

## Build and publish the optional GUI

The GUI is a separate image. Build it from the repository root:

```sh
docker build --platform linux/amd64 -t fledge-gui:local image/fledge-gui
docker run --rm --entrypoint cat fledge-gui:local \
  /usr/share/nginx/html/provenance/build.txt
```

The Dockerfile pins GUI 3.1.0 source, its SHA-256 checksum, the upstream dependency
lock and both base images. It uses Node 18.20.8 and Yarn 1.22.22 to build the
unchanged application, then nginx 1.28.0 to serve it on port 8080 as a non-root user.
The build requires GitHub and the dependency registries referenced by `yarn.lock`.
Startup requires neither. This legacy toolchain is used only in the build stage.

`provenance/` in the static-content directory retains the input identities, upstream
license and dependency lock; `3rdpartylicenses.txt` contains generated dependency
notices. Release metadata identifies the real upstream commit even though the build
uses a source archive. Pinned inputs do not imply byte-identical builds.

Choose your GUI repository, publish, and inspect its repository digest:

```sh
export FLEDGE_UI_REPOSITORY=registry.example.com/your-project/fledge-gui
export FLEDGE_UI_TAG=3.1.0
docker tag fledge-gui:local "$FLEDGE_UI_REPOSITORY:$FLEDGE_UI_TAG"
docker push "$FLEDGE_UI_REPOSITORY:$FLEDGE_UI_TAG"
docker image inspect "$FLEDGE_UI_REPOSITORY:$FLEDGE_UI_TAG" \
  --format '{{json .RepoDigests}}'
```

Supply that repository and digest under `helm.values.ui.image` in the
[Fleet configuration](../deploy/fledge/fleet.yaml), and set `ui.enabled: true`.
Use the [optional browser access instructions](browser-and-lan.md)
to select private or LAN Services. The GUI image contains no API target or credentials;
each browser selects the instance after deployment. Updating it does not change the
core image or plugin selection. Both workloads reuse `imagePullSecrets` when configured.

## Build and publish the pump simulator

The [pump demo](pump-demo.md) uses a separate simulator image. Build it from the
repository root; the simulator package is passed as a named build context:

```sh
docker build --platform linux/amd64 --build-context simulator=simulator \
  -t pump-simulator:local image/pump-simulator
```

The Dockerfile pins the Python base image by digest and installs `asyncua` and
its dependencies from the hash-locked `image/pump-simulator/requirements.txt`; a
changed package hash fails the build. To change a dependency, edit
`requirements.in` and regenerate the lock file with the `pip-compile` command in
its header. The image runs as a non-root user and contains no certificates or
keys; they are mounted from Secrets at runtime. It includes `asyncua` (LGPL-3.0)
unmodified, with its license in the package metadata.

Publish it and inspect its repository digest:

The tag is the simulator's version, from the
[pump demo chart](../deploy/pump-demo/chart/Chart.yaml)'s `appVersion`:

```sh
export PUMP_SIMULATOR_REPOSITORY=registry.example.com/your-project/pump-simulator
export PUMP_SIMULATOR_TAG=0.3.0
docker tag pump-simulator:local "$PUMP_SIMULATOR_REPOSITORY:$PUMP_SIMULATOR_TAG"
docker push "$PUMP_SIMULATOR_REPOSITORY:$PUMP_SIMULATOR_TAG"
docker image inspect "$PUMP_SIMULATOR_REPOSITORY:$PUMP_SIMULATOR_TAG" \
  --format '{{json .RepoDigests}}'
```

Set the repository and digest under `simulator.image` in the
[pump demo chart values](../deploy/pump-demo/chart/values.yaml). The chart
refuses to render without a digest.
