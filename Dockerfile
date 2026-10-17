# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock .

FROM python:3.12-slim
ARG VERSION=0.3.1
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Financial API" \
      org.opencontainers.image.description="User-imported financial and market data; no dataset included" \
      org.opencontainers.image.source="https://github.com/liweihan389/financial_api" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels market-fundamentals \
    && rm -rf /wheels \
    && groupadd --gid 10001 financial \
    && useradd --uid 10001 --gid financial --create-home financial \
    && mkdir -p /data /app && chown financial:financial /data /app
ENV FINANCIAL_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FINANCIAL_ALLOW_FULL_IMPORT=1
WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).read()"
CMD ["financial-data", "serve", "--host", "0.0.0.0", "--port", "8765"]
