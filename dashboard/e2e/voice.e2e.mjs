// LIVE browser voice test: the real dashboard in Chromium, a real LiveKit room, the running JARVIS voice agent, real
// STT/TTS and Claude. Chromium's microphone is a WAV of real synthesized speech (apps/voice/tests/make_speech_wav.py):
//   15 s silence (agent joins and greets) · "Find potential customers for" · 0.7 s pause · "web development in Denver"
//   · 30 s silence (JARVIS replies) · "Focus on roofing companies, and prioritize businesses with outdated websites."
// Verifies: one live caption while speaking, exactly ONE stored user message per spoken turn (no fragments), a Claude
// reply to each turn (not provider_error), the follow-up in the same conversation, and that the browser actually PLAYS
// JARVIS's audio. Nothing here is simulated: the transcripts come from LiveKit STT of the audio above.
// Usage: node e2e/voice.e2e.mjs <dashboard_url> <screenshot_dir> <speech.wav>
import { chromium } from "playwright";

const [, , base = "http://localhost:5173", out = "/tmp", wav] = process.argv;
if (!wav) throw new Error("usage: node e2e/voice.e2e.mjs <dashboard_url> <screenshot_dir> <speech.wav>");
const assert = (c, m) => { if (!c) throw new Error("ASSERT: " + m); };
const log = (...a) => console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...a);

const browser = await chromium.launch({
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}%noloop`],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, permissions: ["microphone"] });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
const api = async (p) => (await page.request.get(`${base}/api${p}`)).json();

try {
  await page.goto(base);
  await page.getByLabel("Message JARVIS").waitFor();
  const status = await api("/voice/status");
  assert(status.state === "OPERATIONAL", `voice is not OPERATIONAL: ${JSON.stringify(status.missing)}`);
  const before = ((await api("/conversation/messages?limit=1")).at(-1) ?? { id: 0 }).id;

  await page.getByRole("button", { name: "Talk to JARVIS" }).click();          // the user gesture also unlocks audio
  const seen = { caption: false, playing: false, captions: new Set() };
  const turns = async () => (await api(`/conversation/messages?after=${before}`)).filter((m) => m.channel === "voice");
  const until = async (cond, ms, what) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const cap = page.getByTestId("live-caption");
      if (await cap.count()) { seen.caption = true; seen.captions.add(await cap.first().textContent()); }
      seen.playing ||= await page.evaluate(() => [...document.querySelectorAll("audio[data-jarvis-audio]")]
        .some((a) => !a.paused && a.currentTime > 0));
      const r = await cond();
      if (r) return r;
      await page.waitForTimeout(250);
    }
    throw new Error(`timed out waiting for ${what}; messages: ${JSON.stringify(await turns())}`);
  };

  // Turn 1: long request with a mid-sentence pause -> ONE user message, then a Claude reply
  const t1 = await until(async () => { const m = await turns(); return m.some((x) => x.role === "assistant") ? m : null; }, 90_000, "reply 1");
  const users1 = t1.filter((m) => m.role === "user");
  log("turn 1:", users1.map((m) => m.content), "->", t1.find((m) => m.role === "assistant").content);
  assert(users1.length === 1, `one spoken request must be ONE message, got ${users1.length}: ${JSON.stringify(users1.map((m) => m.content))}`);
  assert(/customers/i.test(users1[0].content) && /denver/i.test(users1[0].content), `turn 1 was cut off: ${users1[0].content}`);
  const r1 = t1.find((m) => m.role === "assistant");
  assert(r1.meta.kind !== "provider_error", `Claude failed: ${r1.content}`);
  assert(r1.meta.turn_id && r1.meta.turn_id === users1[0].meta.turn_id, "reply and request share the turn's correlation id");
  await until(async () => seen.playing, 20_000, "the browser to play JARVIS's reply");

  // Turn 2: follow-up in the same conversation
  const t2 = await until(async () => { const m = await turns(); return m.filter((x) => x.role === "assistant").length >= 2 ? m : null; }, 90_000, "reply 2");
  const users2 = t2.filter((m) => m.role === "user");
  const r2 = t2.filter((m) => m.role === "assistant")[1];
  log("turn 2:", users2.at(-1).content, "->", r2.content);
  assert(users2.length === 2 && /roof/i.test(users2[1].content), `follow-up not received as one turn: ${JSON.stringify(users2.map((m) => m.content))}`);
  assert(r2.meta.kind !== "provider_error", `Claude failed: ${r2.content}`);

  assert(seen.caption, "no live caption was shown while speaking");
  assert(seen.playing, "JARVIS's audio never played in the browser");
  await page.screenshot({ path: `${out}/e2e-voice.png` });
  // The feed shows each turn once (no partial-transcript bubbles)
  const feed = await page.getByLabel("Conversation").textContent();
  assert(feed.split(users1[0].content).length === 2, "turn 1 appears more than once in the feed");
  assert(!errors.length, "page errors: " + errors.join("; "));
  log("captions seen:", [...seen.captions].slice(0, 6));
  log("VOICE BROWSER E2E OK");
} finally {
  await page.getByRole("button", { name: "End voice session" }).click().catch(() => undefined);
  await browser.close();
}
