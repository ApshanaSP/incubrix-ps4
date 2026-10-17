FROM python:3.11.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
# Optional build secret "extra_ca": a CA certificate to trust while downloading packages, for machines whose
# antivirus or proxy re-signs HTTPS. Without it (the normal case) the build is unchanged.
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem && export PIP_CERT=/tmp/ca.pem; \
    fi; \
    pip install -r requirements-dev.txt && rm -f /tmp/ca.pem

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
