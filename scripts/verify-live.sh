#!/usr/bin/env bash
# LIVE verification against the real providers, using the credentials saved in the Integrations Center.
# Run it in the environment where JARVIS is running (scripts/dev-up.sh). Secrets are never printed.
#
#   scripts/verify-live.sh                     providers + Claude conversation + voice status + live voice conversation
#                                              (real LiveKit room: long request, follow-up, barge-in, reconnect) +
#                                              browser voice test (Chromium mic = real speech, plays JARVIS's reply)
#   scripts/verify-live.sh --skip-voice        everything except the spoken tests
#   scripts/verify-live.sh --website "Business Name" "https://their-site.com" "City" "ST" [business type]
#                                              ALSO run the full workflow for that ONE business: verify, research,
#                                              qualify, build a preview, browser-QA it, deploy it to Cloudflare Pages,
#                                              read the deployment back, and print the real pages.dev URL.
#                                              Uses SerpApi, Firecrawl, Claude and Cloudflare within your budget caps.
#
# Exit code 0 only if every check that ran passed.
set -Euo pipefail
source "$(dirname "$0")/lib/dev-common.sh"
cd "$ROOT"
API="http://127.0.0.1:$API_PORT"
SKIP_VOICE=0; SITE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-voice) SKIP_VOICE=1; shift ;;
    --website) shift; SITE=("$@"); [ ${#SITE[@]} -ge 4 ] || die "--website needs: name url city state [type]"; break ;;
    *) die "unknown option $1" ;;
  esac
done
passed=(); failed=(); skipped=()
pass() { ok "$1"; passed+=("$1"); }
flunk() { fail "$1"; failed+=("$1"); }
skip() { warn "$1"; skipped+=("$1"); }
jq_() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

curl -sf "$API/ready" >/dev/null || die "JARVIS is not running or not ready: run scripts/dev-up.sh first"

info "1. Provider connections (real authenticated requests, free endpoints)"
for p in anthropic serp_api firecrawl cloudflare livekit resend supabase; do
  r="$(curl -s --max-time 90 -X POST "$API/integrations/$p/test")"
  st="$(printf '%s' "$r" | jq_ "d.get('state')" 2>/dev/null || echo ERROR)"
  detail="$(printf '%s' "$r" | jq_ "(d.get('detail') or json.dumps(d.get('result') or d.get('quota') or {}))[:160]" 2>/dev/null || true)"
  case "$st" in
    CONNECTED) pass "$p CONNECTED ${detail}" ;;
    NOT_CONFIGURED) skip "$p NOT_CONFIGURED: ${detail}" ;;
    *) flunk "$p $st: ${detail}" ;;
  esac
done

info "2. Claude conversation (one real turn through the same agent voice uses)"
r="$(curl -s --max-time 120 -X POST "$API/conversation/turn" -H 'Content-Type: application/json' -d '{"text":"What have you completed today?"}')"
kind="$(printf '%s' "$r" | jq_ "d.get('kind')" 2>/dev/null || echo none)"
agent="$(printf '%s' "$r" | jq_ "d.get('agent') or 'claude'" 2>/dev/null || echo none)"
say="$(printf '%s' "$r" | jq_ "d.get('say','')[:300]" 2>/dev/null || echo "$r")"
if [ "$kind" = "provider_error" ]; then flunk "Claude FAILED: $say"
elif [ "$agent" = "claude" ] && [ -n "$say" ]; then pass "Claude answered (real message, tools available): \"$say\""
else flunk "conversation did not use Claude (agent=$agent): $say"; fi

info "3. Voice"
vs="$(curl -s "$API/voice/status")"
vstate="$(printf '%s' "$vs" | jq_ "d['state']")"
if [ "$vstate" = "OPERATIONAL" ]; then pass "voice OPERATIONAL (LiveKit validated, agent heartbeating)"
else flunk "voice $vstate: $(printf '%s' "$vs" | jq_ "' '.join(d['missing'])")"; fi
tok="$(curl -s -X POST "$API/voice/token")"
if printf '%s' "$tok" | jq_ "'token' in d" | grep -q True; then
  scope="$(printf '%s' "$tok" | "$PY" -c "
import json,sys,base64; d=json.load(sys.stdin); p=d['token'].split('.')[1]; c=json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4)))
v=c['video']; print(v.get('room')==d['room'] and v.get('canPublishSources')==['microphone'] and not v.get('roomAdmin') and c['exp']-c['nbf']<=610)")"
  [ "$scope" = "True" ] && pass "LiveKit token: one room, microphone only, expires in 10 minutes" || flunk "LiveKit token scope unexpected"
  curl -s -X POST "$API/voice/end" -H 'Content-Type: application/json' -d "{\"room\":\"$(printf '%s' "$tok" | jq_ "d['room']")\"}" >/dev/null
else flunk "could not mint a LiveKit token: $(printf '%s' "$tok" | head -c 200)"; fi

if [ "$SKIP_VOICE" = 1 ]; then skip "spoken round trip skipped (--skip-voice)"
elif [ "$vstate" != "OPERATIONAL" ]; then skip "spoken round trip skipped: voice is not operational"
elif [ ! -x apps/voice/.venv/bin/python ]; then skip "spoken round trip skipped: voice agent not installed (scripts/dev-up.sh installs it when LiveKit is configured)"
else
  apps/voice/.venv/bin/python -m pip install -q pytest pytest-asyncio >/dev/null 2>&1 || true
  secret="$(setting voice_agent_shared_secret)"
  cfg="$(curl -s "$API/internal/voice/config" -H "X-Voice-Agent-Key: $secret")"
  me="$(curl -s "$API/me")"
  if (export LIVEKIT_URL="$(printf '%s' "$cfg" | jq_ "d['url']")" LIVEKIT_API_KEY="$(printf '%s' "$cfg" | jq_ "d['api_key']")" \
             LIVEKIT_API_SECRET="$(printf '%s' "$cfg" | jq_ "d['api_secret']")" VOICE_AGENT_SHARED_SECRET="$secret" JARVIS_API_URL="$API" \
             JARVIS_LIVE_USER_ID="$(printf '%s' "$me" | jq_ "d['user_id']")" JARVIS_LIVE_WORKSPACE_ID="$(printf '%s' "$me" | jq_ "d['workspace_id']")" \
             JARVIS_LIVE_USE_RUNNING_AGENT=1 VOICE_AGENT_NAME="$(setting voice_agent_name)";
      cd apps/voice && timeout 600 .venv/bin/python -m pytest -q -rs -s tests/test_voice_live.py) >"$LOG_DIR/voice-live-test.log" 2>&1; then
    last="$(curl -s "$API/conversation/messages?limit=1")"
    if [ "$(printf '%s' "$last" | jq_ "d[-1]['channel'] + ':' + str(d[-1]['meta'].get('agent')) + ':' + str(d[-1]['meta'].get('kind'))")" = "voice:claude:reply" ] ||
       printf '%s' "$last" | jq_ "d[-1]['channel']=='voice' and d[-1]['meta'].get('agent')=='claude' and d[-1]['meta'].get('kind')!='provider_error'" | grep -q True; then
      pass "live voice conversation in a real room: one turn per utterance (pause not cut off), Claude replies spoken, follow-up, barge-in, reconnect ($(grep -o 'agent joined after [0-9.]* s' "$LOG_DIR/voice-live-test.log" | head -1))"
    else
      flunk "voice round trip ran but the reply was not a Claude answer: $(printf '%s' "$last" | jq_ "d[-1]['content'][:200]")"
    fi
  else flunk "live voice conversation failed (see .jarvis/logs/voice-live-test.log): $(tail -n 3 "$LOG_DIR/voice-live-test.log" | tr '\n' ' ')"; fi

  # Browser: the real dashboard in Chromium, whose microphone is a WAV of real synthesized speech.
  wav="$LOG_DIR/voice-e2e-speech.wav"
  if ! (cd dashboard && node -e "require.resolve('playwright')" >/dev/null 2>&1); then
    skip "browser voice test skipped: Playwright is not installed in dashboard/ (npm ci)"
  elif ! (export LIVEKIT_URL="$(printf '%s' "$cfg" | jq_ "d['url']")" LIVEKIT_API_KEY="$(printf '%s' "$cfg" | jq_ "d['api_key']")" \
                 LIVEKIT_API_SECRET="$(printf '%s' "$cfg" | jq_ "d['api_secret']")";
          apps/voice/.venv/bin/python apps/voice/tests/make_speech_wav.py "$wav" @15 "Find potential customers for" @0.7 \
            "web development in Denver." @35 "Focus on roofing companies, and prioritize businesses with outdated websites." @20) \
          >"$LOG_DIR/voice-e2e.log" 2>&1; then
    flunk "browser voice test: could not synthesize the test speech (see .jarvis/logs/voice-e2e.log)"
  elif (cd dashboard && (npx playwright install chromium >/dev/null 2>&1 || true) &&
        timeout 300 node e2e/voice.e2e.mjs "http://127.0.0.1:$WEB_PORT" "$LOG_DIR" "$wav") >>"$LOG_DIR/voice-e2e.log" 2>&1; then
    pass "browser voice test: live caption, ONE message per spoken turn, Claude replies, follow-up, audio played in Chromium ($LOG_DIR/e2e-voice.png)"
  else flunk "browser voice test failed (see .jarvis/logs/voice-e2e.log): $(tail -n 3 "$LOG_DIR/voice-e2e.log" | tr '\n' ' ')"; fi
  unset secret cfg
fi

if [ ${#SITE[@]} -ge 4 ]; then
  info "4. Website workflow for ${SITE[0]} (real research, build, QA and Cloudflare deployment)"
  body="$("$PY" -c "
import json,sys
n,u,c,s=sys.argv[1:5]; t=(sys.argv[5] if len(sys.argv)>5 else 'local business').lower()
print(json.dumps({'niche': t, 'location': {'city': c, 'state': s.upper()}, 'rights_attestation': True, 'sites_to_build': 1,
                  'rows': [{'business_name': n, 'website': u, 'city': c, 'state': s.upper(), 'source': 'scripts/verify-live.sh'}]}))" "${SITE[@]}")"
  cid="$(curl -s -X POST "$API/campaigns/import" -H 'Content-Type: application/json' -d "$body" | jq_ "d.get('campaign_id','')")"
  [ -n "$cid" ] || die "could not start the campaign"
  ok "campaign $cid started; waiting up to 30 minutes"
  for _ in $(seq 1 180); do
    c="$(curl -s "$API/campaigns/$cid")"
    st="$(printf '%s' "$c" | jq_ "(d.get('campaign') or d).get('state')")"
    stage="$(printf '%s' "$c" | jq_ "(d.get('campaign') or d).get('current_stage')")"
    printf '\r  state=%s stage=%s   ' "$st" "$stage"
    case "$st" in completed|blocked|failed|cancelled|requires_human_review) break ;; esac
    sleep 10
  done; echo
  reason="$(printf '%s' "$c" | jq_ "(d.get('campaign') or d).get('blocked_reason')")"
  url="$(curl -s "$API/deployments" | jq_ "next((x['url'] for x in d if x.get('status')=='verified' and x.get('campaign_id')=='$cid'), '')" 2>/dev/null || true)"
  if [ -n "$url" ] && [ "$url" != "None" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url")"
    [ "$code" = 200 ] && pass "preview deployed, verified by Cloudflare read-back and reachable: $url" || flunk "preview URL $url returned HTTP $code"
  else
    flunk "no verified preview for ${SITE[0]} (campaign state: $st; reason: ${reason}). Open Campaigns > $cid for the stage details."
  fi
fi

info "Summary"
printf '  passed: %d   failed: %d   not run / not configured: %d\n' "${#passed[@]}" "${#failed[@]}" "${#skipped[@]}"
for f in "${failed[@]}"; do printf '  ✗ %s\n' "$f"; done
[ ${#failed[@]} -eq 0 ]
