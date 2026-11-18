// JARVIS browser QA runner. Usage: node run.mjs <site_dir> <out_dir> [facts.json]
// Serves the static site on 127.0.0.1 (inside the sandbox) and runs real-browser checks with
// Playwright + axe-core on desktop and mobile. Writes <out_dir>/report.json and screenshots.
// No network access is required (or available in the sandbox): any request to a non-local host fails the run.
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import AxeBuilder from "@axe-core/playwright";

const [, , siteDir, outDir, factsPath] = process.argv;
if (!siteDir || !outDir) { console.error("usage: run.mjs <site_dir> <out_dir> [facts.json]"); process.exit(2); }
const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".txt": "text/plain", ".json": "application/json" };

async function parseHeaders() {
  try {
    const raw = await fs.readFile(path.join(siteDir, "_headers"), "utf8");
    const h = {};
    for (const line of raw.split("\n").slice(1)) { const m = line.match(/^\s+([^:]+):\s*(.+)$/); if (m) h[m[1].toLowerCase()] = m[2]; }
    return h;
  } catch { return {}; }
}

const siteHeaders = await parseHeaders();
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  let p = path.normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, "");
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(siteDir, p);
  if (!file.startsWith(path.resolve(siteDir))) { res.writeHead(403).end(); return; }
  try {
    const body = await fs.readFile(file);
    res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream", ...siteHeaders });
    res.end(body);
  } catch { res.writeHead(404, { "content-type": "text/html" }).end("not found"); }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;
await fs.mkdir(outDir, { recursive: true });

const findings = [];
const add = (check, severity, viewport, message, details = {}) => findings.push({ check, severity, viewport, message, details });
const facts = factsPath ? JSON.parse(await fs.readFile(factsPath, "utf8")) : null;
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const metrics = {};
const screenshots = [];
const viewports = [{ name: "desktop", width: 1366, height: 900 }, { name: "mobile", width: 390, height: 844, isMobile: true, hasTouch: true }];

try {
  for (const vp of viewports) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, isMobile: !!vp.isMobile, hasTouch: !!vp.hasTouch });
    const page = await ctx.newPage();
    const consoleErrors = [], failed = [], external = [];
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => consoleErrors.push(String(e)));
    page.on("requestfailed", (r) => failed.push(r.url()));
    page.on("response", (r) => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });
    await page.route("**/*", (route) => {
      const u = new URL(route.request().url());
      if (u.hostname !== "127.0.0.1") { external.push(u.href); return route.abort(); }
      return route.continue();
    });
    const t0 = Date.now();
    const resp = await page.goto(base + "/", { waitUntil: "load" });
    metrics[vp.name] = { load_ms: Date.now() - t0 };
    if (!resp || resp.status() !== 200) add("http_status", "critical", vp.name, `index returned ${resp && resp.status()}`);

    // Headers / metadata
    const hdr = resp ? resp.headers() : {};
    if (!(hdr["content-security-policy"] || "").includes("default-src 'none'")) add("csp", "critical", vp.name, "restrictive CSP header missing");
    if (!(hdr["x-robots-tag"] || "").includes("noindex")) add("noindex_header", "serious", vp.name, "X-Robots-Tag noindex missing");
    const meta = await page.evaluate(() => ({
      title: document.title, desc: document.querySelector('meta[name="description"]')?.content || "",
      robots: document.querySelector('meta[name="robots"]')?.content || "", h1: document.querySelectorAll("h1").length,
      lang: document.documentElement.lang, viewport: !!document.querySelector('meta[name="viewport"]'),
      jsonld: [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent),
      banner: document.querySelector(".preview-banner")?.innerText || "",
    }));
    if (!meta.title || meta.title.length > 70) add("meta_title", "moderate", vp.name, `title missing or too long (${meta.title.length})`);
    if (!meta.desc || meta.desc.length > 170) add("meta_description", "moderate", vp.name, `description missing or too long (${meta.desc.length})`);
    if (!meta.robots.includes("noindex")) add("meta_robots", "serious", vp.name, "meta robots noindex missing");
    if (meta.h1 !== 1) add("h1_count", "moderate", vp.name, `expected one h1, found ${meta.h1}`);
    if (!meta.lang) add("lang", "serious", vp.name, "html lang missing");
    if (!meta.banner.includes("Not affiliated")) add("preview_banner", "critical", vp.name, "non-affiliation preview banner missing");
    for (const j of meta.jsonld) { try { JSON.parse(j); } catch { add("jsonld", "serious", vp.name, "invalid JSON-LD"); } }

    // Links
    const links = await page.evaluate(() => [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href")));
    for (const href of links) {
      if (href.startsWith("#")) {
        if (href.length > 1 && !(await page.$(href))) add("anchor_link", "serious", vp.name, `anchor target missing: ${href}`);
      } else if (href.startsWith("tel:")) {
        if (!/^tel:\+1\d{10}$/.test(href)) add("tel_link", "serious", vp.name, `malformed tel link: ${href}`);
      } else if (href.startsWith("mailto:")) {
        if (!/^mailto:[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(href)) add("mailto_link", "serious", vp.name, `malformed mailto: ${href}`);
      } else if (href.startsWith("/")) {
        const r = await page.request.get(base + href); if (r.status() !== 200) add("internal_link", "serious", vp.name, `${href} -> ${r.status()}`);
      } else add("external_link", "moderate", vp.name, `external link present: ${href}`);
    }

    // Images / SVG render
    const broken = await page.evaluate(() => [...document.images].filter((i) => !i.complete || i.naturalWidth === 0).map((i) => i.src));
    if (broken.length) add("broken_images", "serious", vp.name, `${broken.length} broken images`, { broken });

    // Layout: horizontal overflow
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) add("horizontal_overflow", "serious", vp.name, `page overflows horizontally by ${overflow}px`);
    const tooWide = await page.evaluate(() => [...document.querySelectorAll("h1,h2,h3,p,a,li")].filter((e) => e.scrollWidth > e.clientWidth + 2 && getComputedStyle(e).overflow !== "visible").length);
    if (tooWide) add("text_overflow", "moderate", vp.name, `${tooWide} elements clip their text`);

    // Interactions
    if (vp.name === "mobile") {
      const toggle = await page.$(".nav-toggle");
      if (!toggle || !(await toggle.isVisible())) add("mobile_nav", "serious", vp.name, "mobile menu toggle not visible");
      else {
        await toggle.click();
        const open = await page.evaluate(() => document.getElementById("site-nav")?.classList.contains("open") && document.querySelector(".nav-toggle")?.getAttribute("aria-expanded") === "true");
        if (!open) add("mobile_nav", "serious", vp.name, "mobile menu does not open");
        await toggle.click();
      }
    }
    const form = await page.evaluate(() => {
      const f = document.querySelector("form.contact-form"); if (!f) return null;
      const controls = [...f.querySelectorAll("input,textarea,button")];
      const labelled = [...f.querySelectorAll("input,textarea")].every((el) => f.querySelector(`label[for="${el.id}"]`));
      return { allDisabled: controls.every((c) => c.disabled), labelled, note: !!f.querySelector(".form-note") };
    });
    if (form && (!form.allDisabled || !form.note)) add("preview_form", "serious", vp.name, "preview form must be disabled with a visible notice");
    if (form && !form.labelled) add("form_labels", "serious", vp.name, "form fields missing labels");

    // Accessibility
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
    for (const v of axe.violations) {
      if (v.impact === "serious" || v.impact === "critical") add(`axe:${v.id}`, v.impact, vp.name, v.help, { nodes: v.nodes.length, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) });
      else add(`axe:${v.id}`, "minor", vp.name, v.help, { nodes: v.nodes.length });
    }

    // Performance (lab metrics on the static build)
    const perf = await page.evaluate(() => {
      const n = performance.getEntriesByType("navigation")[0];
      const res = performance.getEntriesByType("resource");
      return { dcl_ms: n ? Math.round(n.domContentLoadedEventEnd) : null, transfer_bytes: res.reduce((s, r) => s + (r.transferSize || r.encodedBodySize || 0), 0) + (n ? n.encodedBodySize : 0), requests: res.length + 1 };
    });
    Object.assign(metrics[vp.name], perf);
    if (perf.transfer_bytes > 500000) add("page_weight", "moderate", vp.name, `page weight ${perf.transfer_bytes} bytes`);

    if (consoleErrors.length) add("console_errors", "serious", vp.name, `${consoleErrors.length} console errors`, { errors: consoleErrors.slice(0, 10) });
    if (failed.length) add("failed_requests", "serious", vp.name, `${failed.length} failed requests`, { failed: failed.slice(0, 10) });
    if (external.length) add("external_requests", "critical", vp.name, "page attempted external requests", { external: external.slice(0, 10) });

    // Content vs evidence: every phone number shown must be a verified fact
    if (facts) {
      const text = await page.evaluate(() => document.body.innerText);
      const phones = [...text.matchAll(/\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}/g)].map((m) => m[0].replace(/\D/g, ""));
      const allowed = new Set((facts.phones || []).map((p) => p.replace(/\D/g, "").slice(-10)));
      for (const p of phones) if (!allowed.has(p.slice(-10))) add("unsupported_phone", "critical", vp.name, `phone ${p} not in verified facts`);
      if (facts.business_name && !text.includes(facts.business_name)) add("business_name", "serious", vp.name, "verified business name not shown");
    }

    const shot = path.join(outDir, `${vp.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    screenshots.push(shot);
    await ctx.close();
  }
} catch (e) {
  add("runner_error", "critical", null, String(e && e.stack || e));
} finally {
  await browser.close();
  server.close();
}
const blocking = findings.filter((f) => f.severity === "critical" || f.severity === "serious");
const report = { passed: blocking.length === 0, runner_version: "qa-runner-1", playwright: "1.63.0", metrics, findings, screenshots: screenshots.map((s) => path.basename(s)) };
await fs.writeFile(path.join(outDir, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify({ passed: report.passed, findings: findings.length }));
process.exit(0);
