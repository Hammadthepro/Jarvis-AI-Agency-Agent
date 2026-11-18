# JARVIS API + worker image (same image, different command).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg postgresql-client age awscli \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*
# Pre-fetch the pinned wrangler CLI used for Cloudflare Pages Direct Upload.
RUN npm install -g wrangler@4.160.1 --no-audit --no-fund
WORKDIR /app
COPY backend/pyproject.toml backend/
COPY backend/jarvis backend/jarvis
COPY database database
RUN pip install ./backend
RUN useradd -r -u 10002 -m jarvis && mkdir -p /var/lib/jarvis/sites && chown -R jarvis /var/lib/jarvis
USER jarvis
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "jarvis.api.app:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
