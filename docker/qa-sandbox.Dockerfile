# Isolated browser-QA sandbox for generated preview sites.
# Runs with: --network none --read-only --user 10001 --cap-drop ALL --security-opt no-new-privileges
# Contains NO application secrets and never mounts the Docker socket.
# Base image pinned by content digest (v1.63.0-noble). Built ONLY during controlled provisioning/CI;
# the runtime never pulls (docker run --pull=never) and runs with --network none.
FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
WORKDIR /runner
COPY qa-runner/package.json qa-runner/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY qa-runner/run.mjs ./
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright NODE_ENV=production
USER 10001:10001
ENTRYPOINT []
