# Single-stage: the dependency set is small and the image is for local demonstration,
# not a size-sensitive deployment.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install dependencies before copying the source, so a code change does not
# invalidate the dependency layer. At this point `app/` does not exist yet, so
# setuptools finds zero packages and this builds a dependency-only install --
# which is precisely what this layer is for. The source is imported from
# WORKDIR via PYTHONPATH above, not from site-packages, so there is never a
# second stale copy of the code to shadow the real one.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Run as a non-root user: a container that does not need root should not have it.
RUN useradd --create-home --uid 10001 smarthealth && chown -R smarthealth /app
USER smarthealth

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
