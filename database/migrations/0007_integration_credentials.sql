-- Integrations Center: provider credentials entered from the dashboard, stored ENCRYPTED (AES-256-GCM,
-- jarvis/security/vault.py). Provider accounts belong to the deployment, not a workspace, so these tables are
-- deployment-level and only deployment administrators (JARVIS_ADMIN_USER_IDS / JARVIS_ADMIN_EMAILS) can change
-- them. No plaintext secret is ever stored: `secrets` holds only encryption envelopes {v, kid, n, c}.
-- Additive; nothing existing is changed.

create table integration_credentials (
  provider text primary key,
  secrets jsonb not null default '{}'::jsonb,          -- field -> encrypted envelope
  config jsonb not null default '{}'::jsonb,           -- non-secret settings (account id, URL, models, project)
  enabled boolean not null default true,               -- false => DISABLED: no calls are made
  key_id text,                                         -- encryption key id of the envelopes
  updated_by uuid references app_users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (jsonb_typeof(secrets) = 'object' and jsonb_typeof(config) = 'object'),
  -- defence in depth: every secret value must be an envelope, never a bare string
  check (not jsonb_path_exists(secrets, '$.* ? (@.type() != "object")'))
);

-- Latest validation result per provider (from "Test connection" and scheduled checks).
create table integration_status (
  provider text primary key,
  state integration_state not null,
  detail text,
  capabilities jsonb not null default '[]'::jsonb,
  quota jsonb,
  last_tested_at timestamptz,
  last_success_at timestamptz,
  last_error_at timestamptz,
  last_error text,
  tested_by uuid references app_users(id)
);

alter table integration_credentials enable row level security;
alter table integration_status enable row level security;
