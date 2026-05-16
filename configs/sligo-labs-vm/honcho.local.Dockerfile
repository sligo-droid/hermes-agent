# Local-only Honcho image for sligo-labs-vm.
#
# This mirrors /home/droid/honcho/Dockerfile but avoids the recursive
# chown of the in-image virtualenv, which is prohibitively slow on this VM.
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-group dev

COPY uv.lock pyproject.toml /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-group dev

ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/app
ENV UV_CACHE_DIR=/tmp/uv-cache

RUN mkdir -p /tmp/uv-cache

COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY scripts/ /app/scripts/
COPY docker/ /app/docker/
COPY alembic.ini /app/alembic.ini
COPY config.toml* /app/

EXPOSE 8000

CMD ["fastapi", "run", "--host", "0.0.0.0", "src/main.py"]
