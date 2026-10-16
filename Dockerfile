FROM python:3.11.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY app ./app
COPY tools ./tools
COPY tests ./tests
COPY batch_inputs ./batch_inputs
COPY catalogue ./catalogue
COPY pytest.ini openapi.yaml ./

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=10 \
  CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["python", "-m", "app.serve"]
