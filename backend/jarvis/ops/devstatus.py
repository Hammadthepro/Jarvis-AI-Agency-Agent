"""Development status report used by scripts/dev-status.sh. Prints JSON; never prints secret values."""

from __future__ import annotations

import asyncio
import json
import sys

import asyncpg

from jarvis.config import get_settings
from jarvis.db.migrate import _describe_connect_error, status_conn


async def collect() -> dict:
    s = get_settings()
    out: dict = {"environment": s.jarvis_env, "local_dev_access": s.local_dev_access_active, "database": {}, "migrations": {},
                 "worker": {}, "integrations": {}, "missing_config": []}
    if not s.jarvis_encryption_key:
        out["missing_config"].append("JARVIS_ENCRYPTION_KEY (needed to save provider credentials in the dashboard)")
    if s.jarvis_env == "local" and not s.jarvis_local_dev_access:
        out["missing_config"].append("JARVIS_LOCAL_DEV_ACCESS=true (otherwise the dashboard gets 401 without sign-in)")
    try:
        conn = await asyncpg.connect(s.database_url, timeout=5)
    except Exception as exc:  # report, do not raise
        out["database"] = {"ok": False, "detail": _describe_connect_error(exc, s.database_url)}
        return out
    try:
        out["database"] = {"ok": True, "detail": await conn.fetchval("select current_database() || ' as ' || current_user")}
        st = await status_conn(conn)
        out["migrations"] = {"ok": st.ok, "applied": len(st.applied), "pending": st.pending, "modified": st.modified}
        if st.ok:
            w = await conn.fetchrow("""select worker_id, extract(epoch from now() - last_seen_at)::int age_s from worker_heartbeats
                                       order by last_seen_at desc limit 1""")
            out["worker"] = {"ok": bool(w and w["age_s"] < 90), "detail": f"{w['worker_id']} seen {w['age_s']}s ago" if w else "no heartbeat"}
            rows = await conn.fetch("select provider, state::text, last_success_at is not null validated from integration_status")
            stored = {r["provider"] for r in await conn.fetch("select provider from integration_credentials")}
            out["integrations"] = {"stored_in_dashboard": sorted(stored),
                                   "validated": {r["provider"]: r["state"] for r in rows}}
    finally:
        await conn.close()
    return out


def main() -> None:
    print(json.dumps(asyncio.run(collect())))
    sys.exit(0)


if __name__ == "__main__":
    main()
