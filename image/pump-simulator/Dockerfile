# syntax=docker/dockerfile:1
# Build from the repository root with the simulator package as a named context:
#   docker build --platform linux/amd64 --build-context simulator=simulator \
#     -t pump-simulator:local image/pump-simulator
ARG BASE_IMAGE=python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
FROM ${BASE_IMAGE}
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps \
      -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY --from=simulator pump_simulator/*.py /opt/pump-simulator/pump_simulator/
RUN python -m compileall -q /opt/pump-simulator \
    && printf '#!/bin/sh\nexec python -m pump_simulator.server "$@"\n' > /usr/local/bin/pump-simulator \
    && printf '#!/bin/sh\nexec python -m pump_simulator.control "$@"\n' > /usr/local/bin/pump-control \
    && chmod 0755 /usr/local/bin/pump-simulator /usr/local/bin/pump-control
ENV PYTHONPATH=/opt/pump-simulator PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Certificates and keys are mounted from Kubernetes Secrets at runtime.
USER 10001:10001
EXPOSE 4840
ENTRYPOINT ["pump-simulator"]
