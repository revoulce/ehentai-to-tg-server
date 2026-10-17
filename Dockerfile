FROM python:3.12.14-slim AS build
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.1
COPY pyproject.toml uv.lock README.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN uv sync --locked --extra source --no-dev --no-editable

FROM python:3.12.14-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 ehtg && useradd --uid 10001 --gid 10001 --no-create-home ehtg \
    && mkdir -p /app/data/db /app/data/files /app/secrets /app/imports \
    && chown -R ehtg:ehtg /app
COPY --from=build /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "ehtg.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
