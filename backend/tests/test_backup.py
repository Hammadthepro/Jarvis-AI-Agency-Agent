"""Off-site backup and restore drill through a real S3-protocol server (moto), real pg_dump/pg_restore and age.
The test server runs on loopback, so it is (correctly) NOT considered an independent destination."""
import os
import shutil
import subprocess
import uuid

import asyncpg
import pytest

from jarvis.ops import backup as B

PG_BIN = "/usr/lib/postgresql/16/bin"
pytestmark = pytest.mark.skipif(not shutil.which("age") or not (shutil.which("pg_dump") or os.path.exists(f"{PG_BIN}/pg_dump")),
                                reason="age or pg_dump not installed")


@pytest.fixture
def s3(monkeypatch):
    from moto.server import ThreadedMotoServer
    monkeypatch.setenv("PATH", f"{PG_BIN}:{os.environ['PATH']}")
    srv = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    srv.start()
    host, port = srv.get_host_and_port()
    cfg = B.S3Config(f"http://{host}:{port}", "jarvis-backups", "test", "test", "us-east-1")
    cfg.client().create_bucket(Bucket=cfg.bucket)
    yield cfg
    srv.stop()


@pytest.fixture
def agekey(tmp_path):
    key = tmp_path / "age.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    pub = next(line.split(": ")[1] for line in key.read_text().splitlines() if "public key" in line)
    return key, pub


async def scratch_db(dsn):
    name = f"restore_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    await admin.execute(f'create database "{name}"')
    await admin.close()
    return dsn.rsplit("/", 1)[0] + f"/{name}"


async def test_offsite_backup_and_restore_drill(db, dsn, s3, agekey, tmp_path):
    key, pub = agekey
    await db.execute("delete from backup_runs")
    async with db.acquire() as conn:
        assert (await B.backup_status(conn))[0] == "NO_BACKUP_RECORDED"
    out = await B.run_backup(dsn, pub, tmp_path / "local", s3)
    assert out["destination"].startswith("s3://jarvis-backups/jarvis-db/") and out["offsite"] is False   # loopback != independent
    head = s3.client().head_object(Bucket=s3.bucket, Key=out["destination"].split("/", 3)[3])
    assert head["Metadata"]["sha256"] == out["sha256"]
    async with db.acquire() as conn:
        assert (await B.backup_status(conn))[0] == "BACKUP_NOT_PROTECTED"
    source = await B._counts(dsn)
    scratch = await scratch_db(dsn)
    drill = await B.restore_drill(dsn, s3, key, scratch)
    assert drill["row_counts"] == source and drill["restored_from"] == out["destination"]
    assert await db.fetchval("select restore_tested_at is not null from backup_runs where sha256=$1", out["sha256"])
    async with db.acquire() as conn:
        assert (await B.backup_status(conn))[0] == "BACKUP_NOT_PROTECTED"   # still: destination not independent


async def test_protected_requires_independent_destination_and_drill(db, dsn, s3, agekey, tmp_path, monkeypatch):
    key, pub = agekey
    await db.execute("delete from backup_runs")
    monkeypatch.setattr(B.S3Config, "independent", lambda self: True)    # simulate an off-host bucket (e.g. Cloudflare R2)
    out = await B.run_backup(dsn, pub, tmp_path / "local", s3)
    assert out["offsite"] is True
    async with db.acquire() as conn:
        st, info = await B.backup_status(conn)
    assert st == "BACKUP_NOT_PROTECTED" and "restore drill" in info["detail"]
    await B.restore_drill(dsn, s3, key, await scratch_db(dsn))
    async with db.acquire() as conn:
        assert (await B.backup_status(conn))[0] == "PROTECTED"


async def test_tampered_offsite_copy_fails_drill(db, dsn, s3, agekey, tmp_path):
    key, pub = agekey
    out = await B.run_backup(dsn, pub, tmp_path / "local", s3)
    obj_key = out["destination"].split("/", 3)[3]
    s3.client().put_object(Bucket=s3.bucket, Key=obj_key, Body=b"tampered", Metadata={"sha256": out["sha256"]})
    with pytest.raises(RuntimeError, match="checksum"):
        await B.restore_drill(dsn, s3, key, await scratch_db(dsn))


def test_independence_check():
    assert not B.S3Config("http://127.0.0.1:9000", "b", "k", "s").independent()
    assert not B.S3Config("http://localhost:9000", "b", "k", "s").independent()
    assert not B.S3Config("http://10.0.0.5", "b", "k", "s").independent()
    assert B.S3Config("https://1.1.1.1", "b", "k", "s").independent()
