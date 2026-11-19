"""Encryption for provider credentials stored in the database.

AES-256-GCM (cryptography's AESGCM, an AEAD) with a fresh 96-bit random nonce per value. The associated data
binds each ciphertext to its provider and field, so a value copied to another row or field fails to decrypt.
Each envelope records a key id (first 16 hex chars of SHA-256 of the key), so a wrong or missing key is
reported explicitly instead of producing garbage.

The key is JARVIS_ENCRYPTION_KEY: 32 random bytes, URL-safe base64 encoded (44 characters). Generate with
    python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
Rotation: set the new key as JARVIS_ENCRYPTION_KEY and the old one as JARVIS_ENCRYPTION_KEY_PREVIOUS, then run
    python -m jarvis.security.vault rotate
which re-encrypts every stored value under the new key. Without a valid key, storing credentials fails closed:
nothing is ever written in plaintext."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis.core.errors import JarvisError

ENVELOPE_VERSION = 1


class EncryptionNotConfigured(JarvisError):
    code = "ENCRYPTION_NOT_CONFIGURED"
    http_status = 503


class DecryptionFailed(JarvisError):
    code = "CREDENTIAL_DECRYPTION_FAILED"
    http_status = 500


GENERATE_HINT = ('Generate one with: python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" '
                 "and set it as JARVIS_ENCRYPTION_KEY in the API and worker environment.")


def parse_key(raw: str | None, name: str = "JARVIS_ENCRYPTION_KEY") -> bytes:
    if not raw:
        raise EncryptionNotConfigured(f"{name} is not set, so credentials cannot be stored securely. {GENERATE_HINT}")
    try:
        key = base64.urlsafe_b64decode(raw.strip().encode())
    except (binascii.Error, ValueError) as exc:
        raise EncryptionNotConfigured(f"{name} is not valid URL-safe base64. {GENERATE_HINT}") from exc
    if len(key) != 32:
        raise EncryptionNotConfigured(f"{name} must decode to exactly 32 bytes (got {len(key)}). {GENERATE_HINT}")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _aad(provider: str, field: str) -> bytes:
    return f"jarvis-credential:v{ENVELOPE_VERSION}:{provider}:{field}".encode()


class Vault:
    def __init__(self, key: bytes, previous: bytes | None = None) -> None:
        self.key, self.kid = key, key_id(key)
        self.previous = previous
        self.previous_kid = key_id(previous) if previous else None

    @classmethod
    def from_settings(cls, settings) -> Vault:
        cur = settings.jarvis_encryption_key.get_secret_value() if settings.jarvis_encryption_key else None
        prev = settings.jarvis_encryption_key_previous.get_secret_value() if settings.jarvis_encryption_key_previous else None
        return cls(parse_key(cur), parse_key(prev, "JARVIS_ENCRYPTION_KEY_PREVIOUS") if prev else None)

    def encrypt(self, provider: str, field: str, plaintext: str) -> dict:
        nonce = os.urandom(12)
        ct = AESGCM(self.key).encrypt(nonce, plaintext.encode(), _aad(provider, field))
        return {"v": ENVELOPE_VERSION, "kid": self.kid, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(ct).decode()}

    def decrypt(self, provider: str, field: str, env: dict) -> str:
        kid = env.get("kid")
        if kid == self.kid:
            key = self.key
        elif self.previous is not None and kid == self.previous_kid:
            key = self.previous
        else:
            raise DecryptionFailed(f"{provider}.{field} was encrypted with a different key (key id {kid}); set the original key "
                                   "as JARVIS_ENCRYPTION_KEY or JARVIS_ENCRYPTION_KEY_PREVIOUS, or re-enter the credential")
        try:
            return AESGCM(key).decrypt(base64.b64decode(env["n"]), base64.b64decode(env["c"]), _aad(provider, field)).decode()
        except (InvalidTag, KeyError, binascii.Error, ValueError) as exc:
            raise DecryptionFailed(f"{provider}.{field} failed authentication (tampered or corrupted)") from exc

    def needs_rotation(self, env: dict) -> bool:
        return env.get("kid") != self.kid


async def rotate(db, vault: Vault) -> int:
    """Re-encrypts every stored credential under the current key. Returns the number of values rewritten."""
    n = 0
    async with db.transaction() as conn:
        for row in await conn.fetch("select provider, secrets from integration_credentials for update"):
            secrets = dict(row["secrets"])
            changed = False
            for field, env in secrets.items():
                if vault.needs_rotation(env):
                    secrets[field] = vault.encrypt(row["provider"], field, vault.decrypt(row["provider"], field, env))
                    changed, n = True, n + 1
            if changed:
                await conn.execute("update integration_credentials set secrets=$2, key_id=$3, updated_at=now() where provider=$1",
                                   row["provider"], secrets, vault.kid)
        await conn.execute("""insert into audit_logs(actor_type, actor_id, action, data) values ('system','vault','credentials.rotated',$1)""",
                           {"values": n, "key_id": vault.kid})
    return n


def main() -> None:
    import asyncio
    import sys

    from jarvis.config import get_settings
    from jarvis.db.pool import Database
    if sys.argv[1:] != ["rotate"]:
        raise SystemExit("usage: python -m jarvis.security.vault rotate")
    s = get_settings()

    async def run() -> int:
        db = Database(s.database_url)
        await db.connect()
        try:
            return await rotate(db, Vault.from_settings(s))
        finally:
            await db.close()
    print(f"re-encrypted {asyncio.run(run())} credential values")


if __name__ == "__main__":
    main()
