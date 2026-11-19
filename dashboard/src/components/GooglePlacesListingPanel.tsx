import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Button, ErrorState } from "./ui";

type Listing = { display_name: string | null; formatted_address: string | null; business_status: string | null; rating: number | null;
  user_rating_count: number | null; google_maps_uri: string | null; attributions: Array<{ provider?: string; providerUri?: string }> };

/** The ONLY view that displays Google Places content (incl. rating). Fetched live on demand (billable,
 *  gated by compliance scope google_places_listing_display), never cached or stored, visually separated,
 *  with required "Google Maps" attribution (text form: no translation, no wrapping, Roboto/sans-serif 400, #5E5E5E/#1F1F1F/white, 12-16px). */
export default function GooglePlacesListingPanel({ prospectId, enabled }: { prospectId: string; enabled: boolean }) {
  const m = useMutation({ mutationFn: () => api<Listing>(`/prospects/${prospectId}/google-listing`) });
  if (!enabled) return null;
  return (
    <section aria-label="Google Places listing" className="rounded-xl border-2 border-ink-600 bg-ink-850 p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-medium text-ink-200">Google Places listing</h2>
        <GoogleMapsAttribution />
      </div>
      {!m.data && <Button variant="ghost" onClick={() => m.mutate()} disabled={m.isPending}>{m.isPending ? "Loading…" : "Show listing (live)"}</Button>}
      {m.error && <ErrorState error={m.error} />}
      {m.data && (
        <div className="space-y-1 text-sm">
          <div className="text-ink-100">{m.data.display_name}</div>
          <div className="text-ink-400">{m.data.formatted_address ?? "Service-area business (no public address)"}</div>
          <div className="text-ink-300">{m.data.rating != null ? `Google rating ${m.data.rating} (${m.data.user_rating_count ?? 0})` : "No Google rating"}</div>
          {m.data.google_maps_uri && <a className="text-signal-300 hover:underline" href={m.data.google_maps_uri} target="_blank" rel="noreferrer">View on Google Maps</a>}
          {m.data.attributions.length > 0 && <div className="pt-1 text-xs text-ink-400">Data also from: {m.data.attributions.map((a, i) => <a key={i} href={a.providerUri} className="underline" target="_blank" rel="noreferrer">{a.provider}</a>)}</div>}
          <p className="pt-1 text-[11px] text-ink-500">Fetched live from Google; not stored.</p>
        </div>
      )}
    </section>
  );
}

export function GoogleMapsAttribution() {
  return (
    <span translate="no" aria-label="Google Maps" className="whitespace-nowrap"
      style={{ fontFamily: "Roboto, ui-sans-serif, system-ui, sans-serif", fontWeight: 400, fontSize: 12, color: "#FFFFFF" }}>Google Maps</span>
  );
}
