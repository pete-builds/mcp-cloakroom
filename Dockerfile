# Pinned digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim && docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
# Dependabot keeps it current weekly via .github/dependabot.yml.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Hash-pinned lockfile; --require-hashes refuses anything that does not match.
# Regenerate with:
#   uv pip compile requirements.in -o requirements.lock --generate-hashes --universal --python-version 3.13
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY clients/ ./clients/
COPY tools/ ./tools/
COPY server.py ingest.py healthcheck.py docker-entrypoint.sh ./

# Non-root, pinned UID 1000. /app/data is where the SQLite store lands, and it
# is created (and owned) here so a named volume mounted over it inherits the
# right ownership instead of coming up root-owned and read-only to the app.
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/data \
    && useradd --create-home --uid 1000 --shell /bin/bash mcp \
    && chown -R mcp:mcp /app
USER mcp

ENV CLOAKROOM_DB_PATH=/app/data/cloakroom.db

EXPOSE 3728

# MCP_HEALTH_PATH is deliberately NOT set here. Leaving it unset lets
# healthcheck.py default to /healthz. Pointing it at /mcp leaks an unreaped
# transport session on every probe.
#
# start-period is long because a cold start runs the full bulk ingest before
# the server binds. Failing probes during that window do not count against the
# container, and after the first run the volume makes restarts immediate.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=900s \
    CMD python healthcheck.py || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "server.py"]
