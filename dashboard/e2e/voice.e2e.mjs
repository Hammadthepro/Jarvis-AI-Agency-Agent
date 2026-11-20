// LIVE browser voice test: the real dashboard in Chromium, a real LiveKit room, the running JARVIS voice agent, real
// STT/TTS and Claude. Chromium's microphone is a WAV of real synthesized speech (apps/voice/tests/make_speech_wav.py):
//   15 s silence (agent joins and greets) · "Find potential customers for website development." · 0.7 s pause ·
//   "Build their websites and show me." · 30 s · "Veterinary businesses." · 30 s · "Anywhere in the US. Only businesses
//   rated at least three point five, preferably without websites."   (no "yes": nothing is started, nothing is spent
//   beyond STT/TTS/Claude)
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

  // The owner's conversation, spoken: each turn must be ONE stored message, answered by Claude, without re-asking for
  // anything already said.
  const expected = [
    { said: /customers/i, alsoSaid: /website/i },
    { said: /veterinar/i, mustNotAsk: /what (kind|type) of business|which industry/i },
    { said: /(US|United States|anywhere)/i, mustNotAsk: /which (area|city|region)|where should I look|what (kind|type) of business|which industry/i },
  ];
  let users = [], replies = [];
  for (let i = 0; i < expected.length; i++) {
    const msgs = await until(async () => { const m = await turns(); return m.filter((x) => x.role === "assistant").length > i ? m : null; },
      120_000, `reply ${i + 1}`);
    users = msgs.filter((m) => m.role === "user"); replies = msgs.filter((m) => m.role === "assistant");
    const e = expected[i], u = users[i], r = replies[i];
    log(`turn ${i + 1}:`, u?.content, "->", r.content);
    assert(users.length === i + 1, `turn ${i + 1} must be ONE message, got: ${JSON.stringify(users.map((m) => m.content))}`);
    assert(e.said.test(u.content) && (!e.alsoSaid || e.alsoSaid.test(u.content)), `turn ${i + 1} was misheard or cut off: ${u.content}`);
    assert(r.meta.kind !== "provider_error" && r.meta.agent === "claude", `turn ${i + 1}: Claude failed: ${r.content}`);
    assert(r.meta.turn_id && r.meta.turn_id === u.meta.turn_id, "reply and request share the turn's correlation id");
    if (e.mustNotAsk) assert(!e.mustNotAsk.test(r.content), `turn ${i + 1}: JARVIS re-asked for something already given: ${r.content}`);
    if (i === 0) await until(async () => seen.playing, 20_000, "the browser to play JARVIS's reply");
  }
  const state = await api("/conversation/messages?limit=1");
  log("objective-driven conversation OK; last reply:", state.at(-1)?.content);
  assert(seen.caption, "no live caption was shown while speaking");
  assert(seen.playing, "JARVIS's audio never played in the browser");
  await page.screenshot({ path: `${out}/e2e-voice.png` });
  // The feed shows each turn once (no partial-transcript bubbles)
  const feed = await page.getByLabel("Conversation").textContent();
  assert(feed.split(users[0].content).length === 2, "turn 1 appears more than once in the feed");
  assert(!errors.length, "page errors: " + errors.join("; "));
  log("captions seen:", [...seen.captions].slice(0, 6));
  log("VOICE BROWSER E2E OK");
} finally {
  await page.getByRole("button", { name: "End voice session" }).click().catch(() => undefined);
  await browser.close();
}
