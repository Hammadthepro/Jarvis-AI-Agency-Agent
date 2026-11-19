#!/usr/bin/env bash
# Daily encrypted backup with an independent off-site copy (see backend/jarvis/ops/backup.py).
set -euo pipefail
exec python -m jarvis.ops.backup run
