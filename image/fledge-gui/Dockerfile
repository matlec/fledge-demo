ARG BUILDER_IMAGE=node:18.20.8-bookworm-slim@sha256:f9ab18e354e6855ae56ef2b290dd225c1e51a564f87584b9bd21dd651838830e
ARG RUNTIME_IMAGE=nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
FROM ${BUILDER_IMAGE} AS build
ARG BUILDER_IMAGE
ARG RUNTIME_IMAGE
ARG GUI_REVISION=80c4c32289a41c95f758a5bb06fe748665c892d2
ARG GUI_SHA256=24354fb9af263f6c7f57d949600ee9a6499a7cb8e7d48ea21a618f6343b12ab4
WORKDIR /build
ADD https://codeload.github.com/fledge-iot/fledge-gui/tar.gz/${GUI_REVISION} /tmp/gui.tar.gz
RUN echo "${GUI_SHA256}  /tmp/gui.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/gui.tar.gz --strip-components=1 \
    && echo '0df84dba8512c8fbf037f74d376e63ef2d5d1d01461a851cfc456d9049e77cd8  yarn.lock' | sha256sum -c - \
    && test "$(node --version)" = v18.20.8 \
    && test "$(yarn --version)" = 1.22.22
# Cypress is an upstream development dependency, not part of the shipped UI.
ENV CYPRESS_INSTALL_BINARY=0 NG_CLI_ANALYTICS=false
RUN yarn install --frozen-lockfile --non-interactive \
    && echo '0df84dba8512c8fbf037f74d376e63ef2d5d1d01461a851cfc456d9049e77cd8  yarn.lock' | sha256sum -c -
# The upstream prebuild script needs .git. Supply archive provenance explicitly
# and call the identical Angular build without that metadata-only prebuild hook.
RUN test "$GUI_REVISION" = 80c4c32289a41c95f758a5bb06fe748665c892d2 \
    && printf '{"hash":"%s","tag":"v3.1.0","distance":0,"semverString":"3.1.0+0"}\n' "$GUI_REVISION" > src/git-version.json \
    && yarn ng build --configuration production --build-optimizer \
    && mkdir -p dist/provenance \
    && cp LICENSE yarn.lock dist/provenance/ \
    && printf 'GUI 3.1.0\nrevision=%s\nsource_sha256=%s\nbuilder=%s\nruntime=%s\nnode=18.20.8\nyarn=1.22.22\n' \
       "$GUI_REVISION" "$GUI_SHA256" "$BUILDER_IMAGE" "$RUNTIME_IMAGE" > dist/provenance/build.txt

FROM ${RUNTIME_IMAGE}
ARG GUI_REVISION=80c4c32289a41c95f758a5bb06fe748665c892d2
LABEL org.opencontainers.image.title="Fledge GUI" \
      org.opencontainers.image.version="3.1.0" \
      org.opencontainers.image.source="https://github.com/fledge-iot/fledge-gui" \
      org.opencontainers.image.revision="${GUI_REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN rm -rf /usr/share/nginx/html/*
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /build/dist/ /usr/share/nginx/html/
USER nginx
EXPOSE 8080
# Bypass base-image configuration scripts: serve only the baked assets.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
