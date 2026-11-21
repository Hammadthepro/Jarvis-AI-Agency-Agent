-- Why a preview website failed (build, QA or deployment), shown to the owner with its retry information. Additive.
alter table generated_sites add column last_error text check (length(last_error) <= 1000);
alter table generated_sites add column last_error_at timestamptz;
