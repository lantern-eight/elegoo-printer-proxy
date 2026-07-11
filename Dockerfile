# Base image: minimal Python runtime
FROM python:3.14-slim

# Copy uv (fast Python package manager) from official image into /bin
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached unless lockfile changes)
COPY pyproject.toml uv.lock ./
# --frozen: use lockfile exactly, no updates
# --no-dev: prod deps only
# --no-install-project: we copy src/ next
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY src/ src/

# Create non-root user. Requires CAP_NET_BIND_SERVICE at runtime to bind to port 80;
# see docker-compose.yml cap_add for the standard deployment.
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app

# HTTP proxy, MQTT TCP, WebSocket/SDCP, camera stream, MQTT WebSocket
EXPOSE 80 1883 3030 8080 9001

USER appuser

ENTRYPOINT ["uv", "run", "python", "-m", "src.main"]
