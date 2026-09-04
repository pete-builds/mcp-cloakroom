# Pinned digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim && docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
# Dependabot keeps it current weekly via .github/dependabot.yml.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

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

# Drop pip now that the dependencies above are installed. Nothing at runtime
# uses it: the entrypoint and the healthcheck are plain `python` calls, and the packages
# themselves are already unpacked into site-packages.
#
# This is the only fix for two recurring Trivy HIGHs. pip ships a vendored
# dependency set (see pip/_vendor/vendor.txt) that Trivy scans as real packages:
# msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools 70.3.0 (CVE-2025-47273).
# Neither is an application dependency, so no lockfile change can move them, and
# no pip release ships fixed versions. Removing the unused component is the fix.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.*/site-packages/pip \
              /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

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
