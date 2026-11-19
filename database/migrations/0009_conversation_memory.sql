-- One conversation per operator, shared by voice and text: the same history, focus and pending action are used
-- whichever channel a turn arrives on. Additive.

create table conversation_messages (
  id bigint generated always as identity primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references app_users(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  channel text not null check (channel in ('text', 'voice')),
  content text not null check (length(content) <= 8000),
  meta jsonb not null default '{}'::jsonb,          -- kind, command/campaign ids, tools used (never secrets)
  created_at timestamptz not null default now()
);
create index conversation_messages_user_idx on conversation_messages(workspace_id, user_id, id desc);

-- What the conversation is about right now ("this company", "this prospect") and an action awaiting the
-- operator's explicit yes. Anything that starts work or spends money waits here until the operator confirms.
create table conversation_state (
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references app_users(id) on delete cascade,
  focus jsonb not null default '{}'::jsonb,
  pending_action jsonb,
  updated_at timestamptz not null default now(),
  primary key (workspace_id, user_id)
);

alter table conversation_messages enable row level security;
alter table conversation_state enable row level security;
