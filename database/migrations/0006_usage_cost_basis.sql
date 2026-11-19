-- Usage accounting: state explicitly what a reported cost is based on, so a locally priced estimate can never
-- be read as a provider-confirmed figure or invoice. Additive; existing rows keep their values and get a basis.
--   provider_reported - cost returned by the provider's usage API for the account
--   provider_invoice  - imported from the provider's billing export / invoice
--   local_estimate    - priced by JARVIS from its own price table (not confirmed by the provider)
--   manual_entry      - typed in by an operator (unverified)
alter table provider_usage_reports add column cost_basis text
  check (cost_basis in ('provider_reported', 'provider_invoice', 'local_estimate', 'manual_entry'));

update provider_usage_reports set cost_basis = case source
    when 'response_usage' then 'local_estimate'
    when 'manual_entry' then 'manual_entry'
    when 'billing_export' then 'provider_invoice'
    else 'provider_reported' end
  where reported_cost_micros is not null;

alter table provider_usage_reports add constraint usage_cost_has_basis
  check (reported_cost_micros is null or cost_basis is not null);
alter table provider_usage_reports add constraint usage_response_cost_is_local_estimate
  check (source <> 'response_usage' or cost_basis is null or cost_basis = 'local_estimate');
alter table provider_usage_reports add constraint usage_invoice_needs_billing_source
  check (cost_basis is distinct from 'provider_invoice' or source = 'billing_export');
