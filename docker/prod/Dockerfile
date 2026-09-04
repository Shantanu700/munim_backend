# Production image: multi-stage, no dev dependencies, no build tools in the
# runtime layer, non-root, static files baked in at build time.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

# collectstatic needs importable settings but must not need a live DB or a real
# secret, so feed it throwaway values. DEBUG=0 means Django stops serving
# static files itself; whitenoise serves what lands in staticfiles/.
RUN PUBLIC_HOST=build.invalid RZP_WEBHOOK_SECRET=build \
    DJANGO_SECRET_KEY=build DJANGO_DEBUG=0 \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 munim && chown -R munim:munim /app
USER munim

EXPOSE 8000
# stateless_http=True, so multiple workers are safe -- no session affinity.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
