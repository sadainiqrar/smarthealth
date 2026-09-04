#!/usr/bin/env bash
set -euo pipefail

# Migrating here is right for one replica and wrong for several, which would race.
# Alembic takes a lock so the race is safe rather than corrupting, but replicas would
# serialise on startup. In production this becomes a separate job.
echo "applying migrations..."
python -m alembic upgrade head

echo "starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
