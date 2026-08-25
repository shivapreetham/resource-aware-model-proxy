# RAMP in a container.
#
#   docker build -t ramp .
#   docker run --rm -p 8090:8090 \
#     -e RAMP_OLLAMA_URL=http://host.docker.internal:11434 \
#     --add-host=host.docker.internal:host-gateway \
#     ramp
#
# Note on what a container can and cannot see: RAMP reads the memory of the
# namespace it runs in. Inside Docker with a memory limit set, it sees that
# limit - which is usually what you want. Without one it sees the host's
# memory but only controls models in the Ollama it points at. GPU metrics
# need `--gpus all` and the NVIDIA container toolkit.

FROM python:3.12-slim

# curl is here for HEALTHCHECK; nothing else is needed at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Non-root: RAMP needs no privileges to read memory or proxy HTTP.
RUN useradd --create-home --uid 10001 ramp
USER ramp

ENV RAMP_OLLAMA_URL=http://host.docker.internal:11434
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

# --host 0.0.0.0 is required: bound to 127.0.0.1 the port is unreachable from
# outside the container even with -p.
ENTRYPOINT ["sh", "-c", "exec ramp run --host 0.0.0.0 --port 8090 --ollama-url \"$RAMP_OLLAMA_URL\""]
