# --- CSS: Tailwind standalone CLI (no Node runtime anywhere) -------------------------------
FROM python:3.12-slim AS css
ARG TAILWIND_VERSION=3.4.17
ARG TARGETARCH
RUN arch=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64) \
 && python -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], '/usr/local/bin/tailwindcss')" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-${arch}" \
 && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY tailwind.config.js ./
COPY app ./app
RUN tailwindcss -c tailwind.config.js -i app/web/assets/input.css -o /out/app.css --minify

# --- Runtime -------------------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY backtest ./backtest
COPY scripts ./scripts
RUN pip install ".[live]"
COPY --from=css /out/app.css app/web/static/css/app.css
USER app
EXPOSE 8000
CMD ["python", "-m", "app.worker"]
