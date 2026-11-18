# Isolated browser-QA sandbox for generated preview sites.
# Runs with: --network none --read-only --user 10001 --cap-drop ALL --security-opt no-new-privileges
# Contains NO application secrets and never mounts the Docker socket.
FROM mcr.microsoft.com/playwright:v1.63.0-noble
WORKDIR /runner
COPY qa-runner/package.json qa-runner/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY qa-runner/run.mjs ./
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright NODE_ENV=production
USER 10001:10001
ENTRYPOINT []
