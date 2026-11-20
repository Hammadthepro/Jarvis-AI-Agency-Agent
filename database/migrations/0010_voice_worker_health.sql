-- Voice health must reflect the real LiveKit worker, not just a live parent process: whether the worker is
-- registered with LiveKit, whether a job process has actually started (prewarm ran), and the last SDK error.
alter table voice_agent_heartbeats add column registered boolean not null default false;
alter table voice_agent_heartbeats add column worker_id text;
alter table voice_agent_heartbeats add column process_ready_at timestamptz;
alter table voice_agent_heartbeats add column last_error text;
