# syntax=docker/dockerfile:1.7

ARG GATEWAY_BASE_IMAGE=tyrag/ragflow:v0.26.4
FROM ${GATEWAY_BASE_IMAGE}

USER root
WORKDIR /ragflow

RUN groupadd --system --gid 10001 tyrag \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
       --no-create-home --shell /usr/sbin/nologin tyrag \
    && mkdir -p /ragflow/enterprise /ragflow/contracts /var/lib/tyrag/state \
    && chown -R tyrag:tyrag /ragflow/enterprise /ragflow/contracts /var/lib/tyrag

# The contracts named context keeps the build context small. Build with:
# docker build --build-context contracts=./contracts -f enterprise/gateway/Dockerfile ... enterprise
COPY --chown=tyrag:tyrag gateway /ragflow/enterprise/gateway
COPY --chown=tyrag:tyrag scripts/wp03 /ragflow/enterprise/scripts/wp03
COPY --from=contracts --chown=tyrag:tyrag metadata-schema.json /ragflow/contracts/metadata-schema.json
COPY requirements-runtime.txt /tmp/enterprise-gateway-requirements.txt

RUN /ragflow/.venv/bin/python -m pip install --no-cache-dir \
    --disable-pip-version-check \
    -r /tmp/enterprise-gateway-requirements.txt

# The base image installs its standalone Python under /root/.local. Make the
# interpreter tree executable/readable without granting the runtime user write
# access to it.
RUN chmod o+x /root /root/.local /root/.local/share /root/.local/share/uv \
    /root/.local/share/uv/python \
    && chmod -R a+rX /root/.local/share/uv/python

ENV PYTHONPATH=/ragflow \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

RUN /ragflow/.venv/bin/python -c "import aiosqlite, fastapi, httpx, jsonschema, jwt, pydantic, uvicorn"

USER tyrag
ENTRYPOINT []
CMD ["/ragflow/.venv/bin/python", "-m", "uvicorn", "enterprise.gateway.app:app", "--host", "0.0.0.0", "--port", "5188", "--workers", "1"]
