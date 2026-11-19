import type { SVGProps } from "react";

/** Minimal line icons (24px grid, currentColor). Provider marks are neutral monograms, not brand logos. */
const P = (d: string) => (props: SVGProps<SVGSVGElement>) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" {...props}><path d={d} /></svg>
);

export const IconCommand = P("M4 6h16M4 12h10M4 18h7M17 15l3 3-3 3");
export const IconMic = P("M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm6-3a6 6 0 0 1-12 0M12 18v3");
export const IconMicOff = P("M3 3l18 18M9 9v3a3 3 0 0 0 5 2.2M15 9.3V6a3 3 0 0 0-5.7-1.3M18 12a6 6 0 0 1-.7 2.8M6 12a6 6 0 0 0 9.5 4.9M12 18v3");
export const IconFlag = P("M5 21V4m0 0h11l-2 4 2 4H5");
export const IconUsers = P("M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM21 19v-1a4 4 0 0 0-3-3.9M15 4.1a3 3 0 0 1 0 5.8");
export const IconGlobe = P("M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-9-9h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18");
export const IconMail = P("M4 6h16v12H4zM4 7l8 6 8-6");
export const IconPlug = P("M9 2v5M15 2v5M7 7h10v4a5 5 0 0 1-10 0V7Zm5 9v6");
export const IconActivity = P("M3 12h4l3 8 4-16 3 8h4");
export const IconCoins = P("M12 7c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3Zm8-3v6c0 1.7-3.6 3-8 3s-8-1.3-8-3V4m16 6v6c0 1.7-3.6 3-8 3s-8-1.3-8-3v-6");
export const IconCog = P("M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.3l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2.2-1.3L14.4 3h-4l-.4 2.4a7.6 7.6 0 0 0-2.2 1.3l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2.2 1.3l.4 2.4h4l.4-2.4a7.6 7.6 0 0 0 2.2-1.3l2.4 1 2-3.4-2-1.6c.1-.4.1-.9.1-1.3Z");
export const IconGauge = P("M12 14l4-4M3.5 17a9 9 0 1 1 17 0");
export const IconStop = P("M7 7h10v10H7z");
export const IconPhoneOff = P("M3 3l18 18M16.7 13.3l1.8.8a2 2 0 0 1 1.2 2.2l-.3 1.6a2 2 0 0 1-2 1.6A15 15 0 0 1 9.6 15M6.4 11.5A15 15 0 0 1 4.5 6.3a2 2 0 0 1 1.6-2l1.6-.3a2 2 0 0 1 2.2 1.2l.8 1.8a2 2 0 0 1-.5 2.3l-1 .8");
export const IconCheck = P("M5 12l5 5 9-10");
export const IconAlert = P("M12 9v4m0 4h.01M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z");
export const IconExternal = P("M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5");

const MARKS: Record<string, { label: string; className: string }> = {
  anthropic: { label: "Cl", className: "from-[#d97757]/30 to-[#d97757]/5 text-[#f0a58a] ring-[#d97757]/40" },
  serp_api: { label: "Sr", className: "from-[#4f8cff]/30 to-[#4f8cff]/5 text-[#9cbcff] ring-[#4f8cff]/40" },
  firecrawl: { label: "Fc", className: "from-[#ff7a45]/30 to-[#ff7a45]/5 text-[#ffb08f] ring-[#ff7a45]/40" },
  cloudflare: { label: "Cf", className: "from-[#f6821f]/30 to-[#f6821f]/5 text-[#fbb36f] ring-[#f6821f]/40" },
  livekit: { label: "Lk", className: "from-[#1fd5f9]/30 to-[#1fd5f9]/5 text-[#8ae9fc] ring-[#1fd5f9]/40" },
  resend: { label: "Rs", className: "from-ink-300/25 to-ink-300/5 text-ink-100 ring-ink-300/40" },
  google_places: { label: "Gp", className: "from-[#34a853]/25 to-[#34a853]/5 text-[#8fd8a4] ring-[#34a853]/40" },
  supabase: { label: "Sb", className: "from-[#3ecf8e]/30 to-[#3ecf8e]/5 text-[#93e8c1] ring-[#3ecf8e]/40" },
};

export function ProviderMark({ provider, size = "md" }: { provider: string; size?: "sm" | "md" }) {
  const m = MARKS[provider] ?? { label: provider.slice(0, 2), className: "from-ink-600 to-ink-800 text-ink-200 ring-ink-500" };
  const dim = size === "sm" ? "h-7 w-7 text-[11px]" : "h-10 w-10 text-sm";
  return (
    <div className={`grid ${dim} shrink-0 place-items-center rounded-xl bg-gradient-to-b font-semibold ring-1 ring-inset ${m.className}`} aria-hidden="true">
      {m.label}
    </div>
  );
}
