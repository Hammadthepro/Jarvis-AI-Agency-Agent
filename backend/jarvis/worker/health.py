"""Container healthcheck for the worker: healthy only if this host's worker wrote a heartbeat in the last
2 minutes with a healthy database state. Exit 0 = healthy, 1 = unhealthy.  python -m jarvis.worker.health"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import asyncpg


async def check(dsn: str, host: str) -> bool:
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg", "postgresql"), timeout=5)
    try:
        return bool(await conn.fetchval(
            """select exists(select 1 from worker_heartbeats where worker_id like $1 || '%'
                             and last_seen_at > now() - interval '2 minutes' and db_state = 'healthy')""", host))
    finally:
        await conn.close()


def main() -> None:
    host = os.environ.get("JARVIS_WORKER_ID") or socket.gethostname()
    try:
        ok = asyncio.run(check(os.environ["DATABASE_URL"], host))
    except Exception as exc:  # unreachable DB => unhealthy
        print(f"unhealthy: {exc}")
        sys.exit(1)
    print("healthy" if ok else "unhealthy: no recent heartbeat")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
