"""LiveKit integration: access-token minting (HS256 JWT per LiveKit's token spec) and a server API
connectivity check (Twirp RoomService.ListRooms). The voice agent process lives in apps/voice."""

from __future__ import annotations

import time
import uuid

import httpx
import jwt

from jarvis.config import Settings
from jarvis.core.errors import not_configured
from jarvis.integrations.base import http_request

PROVIDER = "livekit"


class LiveKitClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=15)

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.livekit_url and s.livekit_api_key and s.livekit_api_secret)

    def _require(self) -> tuple[str, str, str]:
        if not self.configured:
            raise not_configured(PROVIDER, "LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET")
        s = self.settings
        assert s.livekit_url and s.livekit_api_key and s.livekit_api_secret
        return s.livekit_url, s.livekit_api_key.get_secret_value(), s.livekit_api_secret.get_secret_value()

    def token(self, *, identity: str, name: str, room: str, ttl_s: int = 900, metadata: str = "",
              agent: bool = False, room_admin: bool = False, dispatch_agent: str | None = None) -> str:
        _url, key, secret = self._require()
        now = int(time.time())
        # Browser tokens: join ONE room, publish only the microphone (no camera/screen), subscribe to the agent,
        # send data (interrupt/stop controls). No room creation, listing, admin or recording rights.
        video = {"room": room, "roomJoin": True, "canPublish": True, "canPublishSources": ["microphone"],
                 "canSubscribe": True, "canPublishData": True, "canUpdateOwnMetadata": False}
        if room_admin:
            video = {"roomList": True, "roomAdmin": True, "room": room}
        claims = {"iss": key, "sub": identity, "name": name, "nbf": now - 5, "exp": now + ttl_s, "jti": uuid.uuid4().hex,
                  "video": video, "metadata": metadata}
        if agent:
            claims["kind"] = "agent"
        if dispatch_agent:
            # LiveKit room configuration in the join token: the named agent is dispatched when this room is created.
            claims["roomConfig"] = {"agents": [{"agentName": dispatch_agent, "metadata": metadata}]}
        return jwt.encode(claims, secret, algorithm="HS256")

    def http_url(self) -> str:
        url, _, _ = self._require()
        return url.replace("wss://", "https://").replace("ws://", "http://")

    async def delete_room(self, room: str) -> None:
        """Ends a voice session for everyone in it (Twirp RoomService.DeleteRoom)."""
        tok = self.token(identity="jarvis-api", name="jarvis-api", room=room, ttl_s=60, room_admin=True)
        await http_request(PROVIDER, self.http, "POST", f"{self.http_url()}/twirp/livekit.RoomService/DeleteRoom",
                           json={"room": room}, headers={"Authorization": f"Bearer {tok}"})

    async def check(self) -> dict:
        tok = self.token(identity="jarvis-healthcheck", name="healthcheck", room="*", ttl_s=60, room_admin=True)
        r = await http_request(PROVIDER, self.http, "POST", f"{self.http_url()}/twirp/livekit.RoomService/ListRooms",
                               json={}, headers={"Authorization": f"Bearer {tok}"})
        return {"rooms": len(r.json().get("rooms", []))}
