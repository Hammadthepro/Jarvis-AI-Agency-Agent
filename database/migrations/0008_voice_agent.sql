-- Voice agent liveness: the JARVIS LiveKit agent process heartbeats here (authenticated with the shared secret).
-- Voice is reported OPERATIONAL only when LiveKit is configured and validated AND an agent heartbeat is recent.
create table voice_agent_heartbeats (
  agent_id text primary key,
  version text,
  started_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  detail text
);
alter table voice_agent_heartbeats enable row level security;
