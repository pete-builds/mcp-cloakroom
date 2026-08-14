#!/bin/sh
# Load the published bulk data before serving, but only when the database is
# empty. On every subsequent start this is a no-op that exits immediately,
# because the store lives in a volume.
#
# Doing it here rather than inside the server keeps the ingest out of the
# request path and makes a cold start observable in `docker logs` as it runs.
set -e

if [ "${CLOAKROOM_AUTO_INGEST:-true}" = "true" ]; then
    echo "cloakroom: checking whether the roll call archive needs loading..."
    python ingest.py --if-needed
fi

exec "$@"
