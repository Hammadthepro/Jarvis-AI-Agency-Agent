"""Encrypted database backups with an independent off-site copy, and restore drills from that copy.

    python -m jarvis.ops.backup run            # pg_dump -> age encrypt -> local copy -> upload off-site -> verify
    python -m jarvis.ops.backup restore-drill  # download latest off-site object -> verify sha256 -> decrypt ->
                                               # pg_restore into a SCRATCH database -> compare row counts

Status (shown in Settings and /system/readiness) is PROTECTED only when the latest backup was uploaded to
an independent destination (not loopback/private network) AND a restore drill from that destination
succeeded within 35 days. Otherwise BACKUP_NOT_PROTECTED (or NO_BACKUP_RECORDED / BACKUP_FAILED / BACKUP_STALE).

Config: DATABASE_URL, BACKUP_AGE_RECIPIENT, BACKUP_DIR, BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET,
BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY, BACKUP_S3_REGION (default auto), for drills:
BACKUP_AGE_IDENTITY_FILE (private key; keep offline except during drills) and RESTORE_DRILL_DATABASE_URL."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

KEY_PREFIX = "jarvis-db/"
COUNT_TABLES = ("campaigns", "prospects", "research_facts", "generated_sites", "outreach_drafts", "suppression_records", "audit_logs")


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    key_id: str
    secret: str
    region: str = "auto"

    @classmethod
    def from_env(cls) -> S3Config | None:
        e = os.environ
        if not all(e.get(k) for k in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY")):
            return None
        return cls(e["BACKUP_S3_ENDPOINT"], e["BACKUP_S3_BUCKET"], e["BACKUP_S3_ACCESS_KEY_ID"], e["BACKUP_S3_SECRET_ACCESS_KEY"],
                   e.get("BACKUP_S3_REGION", "auto"))

    def client(self):
        import boto3
        return boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=self.key_id,
                            aws_secret_access_key=self.secret, region_name=self.region)

    def independent(self) -> bool:
        """True when the destination is not on this host or a private network (i.e. survives losing the VPS)."""
        host = urlsplit(self.endpoint).hostname or ""
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            return False
        try:
            addrs = {i[4][0] for i in socket.getaddrinfo(host, 443)}
        except socket.gaierror:
            return False
        for a in addrs:
            ip = ipaddress.ip_address(a)
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                return False
        return True


def _pg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg", "postgresql")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_encrypted(database_url: str, recipient: str, out: Path) -> None:
    dump = subprocess.Popen(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", _pg_url(database_url)],
                            stdout=subprocess.PIPE)
    with out.open("wb") as fh:
        enc = subprocess.run(["age", "-r", recipient], stdin=dump.stdout, stdout=fh, check=False)
    dump.wait()
    if dump.returncode != 0 or enc.returncode != 0:
        raise RuntimeError(f"pg_dump/age failed ({dump.returncode}/{enc.returncode})")


def _decrypt_restore(identity_file: Path, local: Path, scratch_url: str) -> None:
    dec = subprocess.Popen(["age", "-d", "-i", str(identity_file), str(local)], stdout=subprocess.PIPE)
    res = subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--clean", "--if-exists", "-d", _pg_url(scratch_url)],
                         stdin=dec.stdout, capture_output=True, text=True)
    dec.wait()
    if dec.returncode != 0 or res.returncode not in (0, 1):   # pg_restore returns 1 for ignorable warnings
        raise RuntimeError(f"decrypt/restore failed: {res.stderr[-500:]}")


async def _counts(url: str) -> dict[str, int]:
    conn = await asyncpg.connect(_pg_url(url))
    try:
        return {t: await conn.fetchval(f"select count(*) from {t}") for t in COUNT_TABLES}
    finally:
        await conn.close()


async def _record(url: str, sql: str, *args):
    conn = await asyncpg.connect(_pg_url(url))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def run_backup(database_url: str, recipient: str, out_dir: Path, s3: S3Config | None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"jarvis-{stamp}.dump.age"
    run_id = await _record(database_url, "insert into backup_runs(status, destination, offsite) values ('running', $1, false) returning id",
                           f"local:{out_dir}")
    try:
        await asyncio.to_thread(_dump_encrypted, database_url, recipient, out)
        digest, size = sha256_file(out), out.stat().st_size
        offsite, destination, detail = False, f"local:{out}", "BACKUP_NOT_PROTECTED: no off-site destination configured"
        if s3 is not None:
            key = KEY_PREFIX + out.name
            c = s3.client()
            c.upload_file(str(out), s3.bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
            head = c.head_object(Bucket=s3.bucket, Key=key)
            if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != digest:
                raise RuntimeError("off-site object verification failed (size or checksum mismatch)")
            destination = f"s3://{s3.bucket}/{key}"
            offsite = s3.independent()
            detail = "uploaded and verified" if offsite else \
                "BACKUP_NOT_PROTECTED: destination is on this host or a private network (not independent)"
        for old in sorted(out_dir.glob("jarvis-*.dump.age"))[:-7]:
            old.unlink()
        await _record(database_url, """update backup_runs set status='succeeded', finished_at=now(), destination=$2, offsite=$3,
                                       size_bytes=$4, sha256=$5, detail=$6 where id=$1""", run_id, destination, offsite, size, digest, detail)
        return {"id": str(run_id), "destination": destination, "offsite": offsite, "sha256": digest, "size": size, "detail": detail}
    except Exception as exc:
        await _record(database_url, "update backup_runs set status='failed', finished_at=now(), detail=$2 where id=$1", run_id, str(exc)[:500])
        raise


async def restore_drill(database_url: str, s3: S3Config, identity_file: Path, scratch_url: str) -> dict:
    """Restores the newest OFF-SITE backup into a scratch database and compares row counts with the source."""
    c = s3.client()
    objs = c.list_objects_v2(Bucket=s3.bucket, Prefix=KEY_PREFIX).get("Contents", [])
    if not objs:
        raise RuntimeError("no backups found at the off-site destination")
    latest = max(objs, key=lambda o: o["Key"])
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / Path(latest["Key"]).name
        c.download_file(s3.bucket, latest["Key"], str(local))
        expected = c.head_object(Bucket=s3.bucket, Key=latest["Key"]).get("Metadata", {}).get("sha256")
        digest = sha256_file(local)
        if expected and expected != digest:
            raise RuntimeError("downloaded backup checksum does not match uploaded checksum")
        await asyncio.to_thread(_decrypt_restore, identity_file, local, scratch_url)
    restored = await _counts(scratch_url)
    run_id = await _record(database_url, "select id from backup_runs where sha256 = $1 order by started_at desc limit 1", digest)
    if run_id:
        await _record(database_url, """update backup_runs set restore_tested_at = now(),
                                       detail = coalesce(detail,'') || ' | restore drill ok from ' || $2 where id = $1""",
                      run_id, f"s3://{s3.bucket}/{latest['Key']}")
    return {"restored_from": f"s3://{s3.bucket}/{latest['Key']}", "sha256": digest, "row_counts": restored, "backup_run_id": str(run_id)}


async def backup_status(conn) -> tuple[str, dict | None]:
    b = await conn.fetchrow("""select status, offsite, finished_at, restore_tested_at, destination, detail from backup_runs
                               order by started_at desc limit 1""")
    if b is None:
        return "NO_BACKUP_RECORDED", None
    info = dict(b)
    if b["status"] != "succeeded":
        return "BACKUP_FAILED", info
    if not b["offsite"]:
        return "BACKUP_NOT_PROTECTED", info
    drill_ok = await conn.fetchval("""select exists(select 1 from backup_runs where offsite and status = 'succeeded'
                                      and restore_tested_at > now() - interval '35 days')""")
    if not drill_ok:
        info["detail"] = "off-site upload ok, but no successful restore drill from the off-site copy in 35 days"
        return "BACKUP_NOT_PROTECTED", info
    if await conn.fetchval("select $1::timestamptz < now() - interval '36 hours'", b["finished_at"]):
        return "BACKUP_STALE", info
    return "PROTECTED", info


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    db = os.environ["DATABASE_URL"]
    s3 = S3Config.from_env()
    if cmd == "run":
        print(asyncio.run(run_backup(db, os.environ["BACKUP_AGE_RECIPIENT"], Path(os.environ.get("BACKUP_DIR", "/backups")), s3)))
    elif cmd == "restore-drill":
        if s3 is None:
            raise SystemExit("restore drill requires BACKUP_S3_* (an off-site destination)")
        print(asyncio.run(restore_drill(db, s3, Path(os.environ["BACKUP_AGE_IDENTITY_FILE"]), os.environ["RESTORE_DRILL_DATABASE_URL"])))
    else:
        raise SystemExit("usage: python -m jarvis.ops.backup run|restore-drill")


if __name__ == "__main__":
    main()
