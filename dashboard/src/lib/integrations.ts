import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export type IntegrationField = {
  name: string; label: string; secret: boolean; required: boolean; hint: string; choices: string[]; default: string | null;
  set: boolean; source: "dashboard" | "environment" | "dashboard_unreadable" | null; display: string | null;
};
export type Integration = {
  provider: string; name: string; purpose: string; docs_url: string; notes: string[]; managed_in_dashboard: boolean;
  configured: boolean; enabled: boolean; state: string; provider_state: string | null; detail: string | null; tested: boolean;
  last_checked_at: string | null; last_success_at: string | null; last_error: string | null; last_error_at: string | null;
  capabilities: string[]; validated_capabilities: string[]; quota: Record<string, unknown> | null;
  usage: { unit: string; reported_units: number | null; status: string; detail: string | null; fetched_at: string } | null;
  fields: IntegrationField[]; has_dashboard_credentials: boolean;
  runtime: { state: string; detail: string | null; at: string | null } | null;
  compliance: { open: boolean; reason: string | null } | null;
};
export type IntegrationsResponse = {
  integrations: Integration[]; can_manage: boolean;
  vault: { ready: boolean; key_id: string | null; detail: string | null }; credentials_error: string | null;
};
export type TestResult = { provider: string; state: string; provider_state?: string; detail?: string; http_status?: number | null;
  result?: Record<string, unknown>; quota?: Record<string, unknown> | null; capabilities?: string[] };

export const useIntegrations = () =>
  useQuery({ queryKey: ["integrations"], queryFn: () => api<IntegrationsResponse>("/integrations"), refetchInterval: 30_000 });

/** Human explanation for each state, shown next to the pill. */
export const STATE_HELP: Record<string, string> = {
  CONNECTED: "Validated against the provider.",
  NOT_TESTED: "Credentials present but not yet validated. Run Test connection.",
  NOT_CONFIGURED: "Credentials are missing.",
  INVALID_CREDENTIALS: "The provider rejected the credentials.",
  PERMISSION_DENIED: "Credentials are valid but lack a required permission.",
  CONNECTION_FAILED: "The provider could not be reached or returned an error.",
  RATE_LIMITED: "The provider is throttling requests. Try again shortly.",
  QUOTA_EXCEEDED: "The plan's quota or credits are used up.",
  DISABLED: "Turned off by an administrator. No calls are made.",
  BLOCKED_PENDING_APPROVAL: "Restricted until written approval is recorded.",
};

export const humanize = (s: string) => s.replace(/_/g, " ");
