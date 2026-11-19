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

await page.goto(base);
await page.getByLabel("Email").fill(`e2e-${Date.now()}@stack.dev`);
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByText("What should JARVIS work on?").waitFor();

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

// 3) Settings shows every provider NOT_CONFIGURED and the Places gate blocked; overview counts real rows
await page.getByRole("link", { name: "Settings" }).first().click();
await page.getByText("Google Places compliance gate").waitFor();
await page.getByText("Set LIVEKIT_URL").waitFor();
await page.getByText("google_places_prospect_selection", { exact: true }).waitFor();
const settingsText = await page.textContent("body");
assert((settingsText.match(/NOT_CONFIGURED/g) || []).length >= 7, "all providers NOT_CONFIGURED");
await page.screenshot({ path: `${out}/e2e-settings.png`, fullPage: true });
await page.getByRole("link", { name: "Overview" }).first().click();
await page.getByText("System health").waitFor();
await page.screenshot({ path: `${out}/e2e-overview.png` });
assert(errors.length === 0, "page errors: " + errors.join("; "));
await browser.close();
console.log("E2E OK");
