FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the workspace and build the WhatsApp bridge from the lockfile.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages/ packages/

WORKDIR /app/packages/bridge
RUN npm ci && npm run build && npm prune --omit=dev
WORKDIR /app

# Install the gateway package after the bridge dist is available for packaging.
RUN uv pip install --system --no-cache "./packages/gateway[overseer]"

# Run the gateway as a non-root user; runtime state should be mounted here.
RUN useradd --create-home --uid 10001 yeoman && \
    mkdir -p /home/yeoman/.yeoman && \
    chown -R yeoman:yeoman /home/yeoman/.yeoman /app
USER yeoman
ENV HOME=/home/yeoman

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["yeoman"]
CMD ["status"]
