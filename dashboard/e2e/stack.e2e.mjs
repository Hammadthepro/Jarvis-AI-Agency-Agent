// Real-stack E2E: real API + worker + Postgres + built dashboard in Chromium. No provider credentials are
// configured, so this verifies the HONEST failure states end to end (nothing faked).
// Usage: node e2e/stack.e2e.mjs <dashboard_url> <screenshot_dir>
import { chromium } from "playwright";
const [, , base = "http://localhost:4173", out = "/tmp"] = process.argv;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
const assert = (c, m) => { if (!c) { throw new Error("ASSERT: " + m); } };

// 0) No sign-in: the dashboard opens directly to the Command Center (local development access mode).
await page.goto(base);
await page.getByLabel("Message JARVIS").waitFor();                       // voice-first JARVIS screen
// A real text turn through the SAME conversation the voice agent uses. This environment has no Anthropic key, so the
// honest result is an explicit provider failure: no silent basic-parser answer, nothing started.
const unavailable = page.getByText(/Claude unavailable: Anthropic is not configured/);
const before = await unavailable.count();
await page.getByLabel("Message JARVIS").fill("Find 10 roofers in Denver, CO");
await page.getByRole("button", { name: "Send" }).click();
for (let i = 0; i < 60 && (await unavailable.count()) <= before; i++) await page.waitForTimeout(500);
assert((await unavailable.count()) === before + 1, "the new reply must be an explicit 'Claude unavailable' failure");
await page.screenshot({ path: `${out}/e2e-jarvis.png` });
await page.goto(`${base}/commands`);
await page.getByText("What should JARVIS work on?").waitFor();
assert(await page.locator('input[type="email"], input[type="password"]').count() === 0, "no login form");
assert(!(await page.getByRole("button", { name: /sign in/i }).count()), "no sign-in button");
await page.getByText("Local development mode").waitFor();

// 1) Places-based command: parsed, plan shown, compliance gate blocks authorization
await page.getByLabel("Command").fill("Find 20 plumbers in Houston with at least a 3.5 Google rating and no website.");
await page.getByRole("button", { name: "Send" }).click();
await page.getByText(/Exclude them, or keep them/).waitFor();
await page.getByRole("button", { name: "exclude" }).click();
await page.getByText(/blocked pending approval/).waitFor();
await page.screenshot({ path: `${out}/e2e-console.png` });
await page.getByRole("button", { name: "Authorize campaign" }).click();
await page.getByText("COMPLIANCE_BLOCKED").first().waitFor();

// 2) Manual import campaign: runs on the real worker, blocks honestly because SerpApi is NOT_CONFIGURED
await page.getByRole("link", { name: "Campaigns" }).first().click();
await page.getByRole("button", { name: "Import business list" }).click();
await page.locator("#imp-niche").fill("plumbing");
await page.locator("#imp-city").fill("Houston");
await page.locator("#imp-state").fill("TX");
await page.getByLabel("CSV rows").fill("business_name,phone,city,state\nBayou City Plumbing Co,(713) 555-0142,Houston,TX\n");
await page.getByText("I confirm I have the right").click();
await page.getByRole("button", { name: "Create campaign" }).click();
await page.getByRole("link", { name: /Plumbing · Houston, TX/ }).first().click();
await page.getByText(/NOT_CONFIGURED/).first().waitFor({ timeout: 30000 });
const body = await page.textContent("body");
assert(/blocked/.test(body), "campaign should be blocked");
assert(/SERPAPI_API_KEY/.test(body), "blocked reason should name the missing credential");
await page.screenshot({ path: `${out}/e2e-campaign-blocked.png`, fullPage: true });

// 3) Integrations Center renders real backend states; Places is restricted; voice reports what is missing
await page.getByRole("link", { name: "Integrations" }).first().click();
await page.getByRole("heading", { name: "Integrations" }).waitFor();
await page.getByText("Missing: Server URL, API key, API secret").waitFor();
const integText = await page.textContent("body");
assert((integText.match(/NOT_CONFIGURED/g) || []).length >= 6, "unconfigured providers show NOT_CONFIGURED");
assert(/Restricted/.test(integText), "Google Places is shown as restricted");
assert(/Administrator: can edit/.test(integText), "the local operator administers the local deployment");
await page.screenshot({ path: `${out}/e2e-integrations.png`, fullPage: true });
await page.getByRole("link", { name: "Voice Setup" }).first().click();
await page.getByText(/LiveKit server URL, API key and API secret/).waitFor();
await page.getByRole("link", { name: /Open Integrations/ }).waitFor();
assert(await page.getByRole("button", { name: "Start voice session" }).isDisabled(), "voice start disabled when not ready");
await page.screenshot({ path: `${out}/e2e-voice.png`, fullPage: true });
await page.getByRole("link", { name: "Settings" }).first().click();
await page.getByText("google_places_prospect_selection", { exact: true }).waitFor();
await page.screenshot({ path: `${out}/e2e-settings.png`, fullPage: true });
await page.getByRole("link", { name: "Overview" }).first().click();
await page.getByText("System health").waitFor();
await page.screenshot({ path: `${out}/e2e-overview.png` });
// 4) Deployment admin: save deliberately INVALID (format-valid) keys, run Test connection against the REAL providers,
//    expect INVALID_CREDENTIALS from each, then remove them. Requires network access to the providers.
if (process.env.E2E_LIVE_PROVIDER_CHECKS) {
  await page.getByRole("link", { name: "Integrations" }).first().click();
  await page.getByText("Administrator: can edit").waitFor();
  const bogus = {
    "SerpApi": { "API key": "0".repeat(64) },
    "Anthropic (Claude)": { "API key": "sk-ant-api03-" + "x".repeat(40) },
    "Firecrawl": { "API key": "fc-" + "0".repeat(32) },
    "Cloudflare Pages": { "API token": "e2einvalidtoken".repeat(3), "Account ID": "0123456789abcdef0123456789abcdef" },
    "Resend": { "API key": "re_" + "e2einvalid".repeat(3) },
  };
  const live = {};
  for (const [name, fields] of Object.entries(bogus)) {
    const card = page.getByRole("region", { name });
    await card.getByRole("button", { name: /Configure|Update/ }).click();
    const dlg = page.getByRole("dialog");
    for (const [label, value] of Object.entries(fields)) await dlg.getByLabel(new RegExp(`^${label}`)).fill(value);
    await dlg.getByRole("button", { name: "Save" }).click();
    await dlg.waitFor({ state: "detached" });
    await card.getByRole("button", { name: "Test connection" }).click();
    const outcome = card.getByRole("status");
    await outcome.waitFor({ timeout: 45000 });
    live[name] = (await outcome.textContent()).match(/(INVALID_CREDENTIALS|CONNECTED|NOT_CONFIGURED|INVALID_CREDENTIALS|PERMISSION_DENIED|CONNECTION_FAILED|RATE_LIMITED|QUOTA_EXCEEDED|DISABLED|BLOCKED_PENDING_APPROVAL)/)?.[0];
    assert(!(await card.textContent()).includes(Object.values(fields)[0]), `${name}: secret must never be rendered`);
  }
  console.log("LIVE provider responses to invalid keys:", JSON.stringify(live));
  for (const [name, st] of Object.entries(live)) assert(st === "INVALID_CREDENTIALS", `${name}: expected INVALID_CREDENTIALS, got ${st}`);
  await page.screenshot({ path: `${out}/e2e-integrations-admin.png`, fullPage: true });
  page.on("dialog", (d) => void d.accept());
  for (const name of Object.keys(bogus)) {
    const card = page.getByRole("region", { name });
    await card.getByRole("button", { name: "Remove" }).click();
    await card.getByText("NOT_CONFIGURED").waitFor();
  }
}
assert(errors.length === 0, "page errors: " + errors.join("; "));
await browser.close();
console.log("E2E OK");
