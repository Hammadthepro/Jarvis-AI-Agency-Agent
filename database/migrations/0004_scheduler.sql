-- Scheduled job bookkeeping shared by all workers (the scheduler itself runs under an advisory lock).
create table scheduled_jobs (
  name text primary key,
  last_started_at timestamptz,
  last_finished_at timestamptz,
  last_status text check (last_status in ('succeeded', 'failed')),
  last_detail text
);
alter table scheduled_jobs enable row level security;
