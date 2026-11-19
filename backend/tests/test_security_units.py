import pytest

from jarvis.agents.outreach import parse_unsubscribe_token, unsubscribe_token
from jarvis.core.errors import ValidationFailed
from jarvis.integrations.resend import verify_svix
from jarvis.security.ssrf import UnsafeDestination, is_public_ip, safe_fetch, validate_url_shape


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.1", "169.254.169.254", "100.64.0.1", "::1",
                                "fc00::1", "fe80::1", "0.0.0.0", "::ffff:127.0.0.1", "224.0.0.1"])
def test_private_ips_blocked(ip):
    assert not is_public_ip(ip)


def test_public_ip_allowed():
    assert is_public_ip("93.184.216.34") and is_public_ip("2606:4700::6810:85e5")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/",
                                 "http://example.com:8080/", "http://user:pw@example.com/", "http://metadata.google.internal/",
                                 "http://foo.internal/", "http://[::1]/"])
def test_url_shape_rejects(url):
    with pytest.raises(UnsafeDestination):
        validate_url_shape(url)


async def test_dns_to_private_blocked():
    async def resolver(host, port):
        raise UnsafeDestination(f"{host} resolves to non-public address(es): ['10.0.0.5']")
    r = await safe_fetch("https://rebind.example/", resolver=resolver)
    assert r.error and r.error.startswith("unsafe")


async def test_redirect_to_private_blocked(monkeypatch):
    import httpx

    from jarvis.security import ssrf
    calls = []

    async def resolver(host, port):
        calls.append(host)
        if host == "evil.example":
            return "93.184.216.34"
        raise UnsafeDestination("non-public")

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})

    monkeypatch.setattr(ssrf, "_PinnedTransport", lambda ip, **kw: T())
    r = await safe_fetch("http://evil.example/", resolver=resolver)
    assert r.error and r.error.startswith("unsafe") and calls == ["evil.example", "internal.example"]


def test_unsubscribe_token_roundtrip_and_tamper():
    import uuid
    ws = uuid.uuid4()
    t = unsubscribe_token("k" * 32, ws, "Owner@Biz.com")
    assert parse_unsubscribe_token("k" * 32, t) == (ws, "owner@biz.com")
    with pytest.raises(ValidationFailed):
        parse_unsubscribe_token("other-key-xxxxxxxxxxxxxxxxxxxxxxxx", t)


def test_svix_signature():
    import base64
    import hashlib
    import hmac
    import time
    secret = "whsec_" + base64.b64encode(b"0123456789abcdef").decode()
    ts, body = str(int(time.time())), b'{"type":"email.bounced"}'
    sig = base64.b64encode(hmac.new(b"0123456789abcdef", f"m1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    assert verify_svix(secret, "m1", ts, body, f"v1,{sig}")
    assert not verify_svix(secret, "m1", ts, body + b"x", f"v1,{sig}")
    assert not verify_svix(secret, "m1", str(int(time.time()) - 3600), body, f"v1,{sig}")
