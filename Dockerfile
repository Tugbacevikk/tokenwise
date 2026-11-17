# The hosted Tokenwise: one image for the web app and the worker.
#   docker compose up -d        (see docs/hosting.md)
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The worker starts check containers through the host's Docker; it only needs the client.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tokenwise ./tokenwise
RUN pip install --no-cache-dir ".[server]"
RUN useradd --uid 10001 --create-home tokenwise
USER 10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["tokenwise", "server", "web"]
