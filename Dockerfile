# Control plane image (API, or worker with `python -m vmlm.worker_main`).
#
# - multi-stage: build a wheel, install it plus the exact locked dependencies into a venv,
#   copy only the venv into the runtime image (no compilers, no pip cache, no source tree)
# - runs as an unprivileged user with a read-only root filesystem (see docker-compose.yml);
#   the only writable path is /data (SQLite database)
# - no secrets are baked in; API keys arrive through the environment at run time
# Base images are multi-arch, so this builds natively for arm64 on Apple Silicon.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY requirements.lock ./
RUN /opt/venv/bin/pip install --require-virtualenv -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --require-virtualenv --no-deps .

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="vm-lifecycle-manager" \
      org.opencontainers.image.description="Mini VM control plane (simulated provider by default)"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VMLM_DATABASE_URL=sqlite:////data/vmlm.db
RUN groupadd --system --gid 10001 vmlm \
 && useradd --system --uid 10001 --gid vmlm --home-dir /nonexistent --shell /usr/sbin/nologin vmlm \
 && mkdir /data && chown vmlm:vmlm /data && chmod 0700 /data
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
WORKDIR /data
EXPOSE 8000
# No curl in the slim image; use the interpreter that is already there.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else 1)"]
# Exec form, so no shell sits between the init process and uvicorn: SIGTERM reaches uvicorn
# directly and the lifespan shutdown (stop worker loops, dispose engine) runs.
# Compose and Kubernetes add an init (init: true / the pod's pause process) to reap zombies.
CMD ["uvicorn", "vmlm.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
