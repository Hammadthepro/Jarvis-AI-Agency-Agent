"""Append-only audit log and task-event helpers (task events drive the SSE progress stream)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


async def audit(conn: asyncpg.Connection, *, workspace_id: UUID | None, actor_type: str, actor_id: str | None,
                action: str, entity_type: str | None = None, entity_id: Any = None,
                from_state: str | None = None, to_state: str | None = None, data: dict | None = None) -> None:
    await conn.execute(
        """insert into audit_logs(workspace_id, actor_type, actor_id, action, entity_type, entity_id,
                                  from_state, to_state, data) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        workspace_id, actor_type, actor_id, action, entity_type,
        str(entity_id) if entity_id is not None else None, from_state, to_state, data or {})


async def emit(conn: asyncpg.Connection, *, workspace_id: UUID, message: str, event_type: str = "progress",
               task_id: UUID | None = None, campaign_id: UUID | None = None, data: dict | None = None) -> None:
    await conn.execute(
        """insert into task_events(workspace_id, task_id, campaign_id, event_type, message, data)
           values ($1,$2,$3,$4,$5,$6)""",
        workspace_id, task_id, campaign_id, event_type, message, data or {})
    await conn.execute("select pg_notify('jarvis_events', $1)", str(workspace_id))
