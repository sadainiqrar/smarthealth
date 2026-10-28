#!/usr/bin/env bash
set -euo pipefail

# With arguments, run them instead of the API -- the worker service passes
# `python -m app.worker`. It skips migrations on purpose: the app container owns them,
# and the worker waits for the app to be healthy before it starts.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Migrating here is right for one replica and wrong for several, which would race.
# Alembic takes a lock so the race is safe rather than corrupting, but replicas would
# serialise on startup. In production this becomes a separate job.
echo "applying migrations..."
python -m alembic upgrade head

echo "starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
