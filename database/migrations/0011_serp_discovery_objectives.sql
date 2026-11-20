-- Provider-specific discovery and durable conversation objectives. Additive; no data is changed or removed.
--
-- 1. SerpApi (Google Maps engine via SerpApi) becomes a discovery provider in its own right. The Google Places API (New)
--    restriction is unchanged: google_places campaigns still require the recorded compliance approval (enforced in code).
--    A minimum rating filter is allowed for serp_api as well, because SerpApi returns the public rating it lists.
-- 2. campaigns.objective keeps the owner's whole objective (original words, scope, preferences, what to build and show),
--    so it survives clarification and is visible after research finishes.
-- 3. conversation_state.objective is the structured objective being agreed in the conversation (voice and text).

alter table campaigns drop constraint campaigns_discovery_provider_check;
alter table campaigns add constraint campaigns_discovery_provider_check
  check (discovery_provider in ('google_places', 'serp_api', 'manual_import', 'licensed_business_data'));

alter table campaigns drop constraint campaigns_check1;
alter table campaigns add constraint campaigns_rating_provider_check
  check (minimum_google_rating is null or discovery_provider in ('google_places', 'serp_api'));

alter table campaigns add column objective jsonb not null default '{}'::jsonb;
alter table conversation_state add column objective jsonb not null default '{}'::jsonb;
