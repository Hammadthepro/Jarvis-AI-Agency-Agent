"""Docs and configuration must not contradict the actual Docker architecture.

Actual architecture: only the `sandbox-runner` service mounts the Docker socket; the worker reaches it over HTTP
(QA_RUNNER_MODE=runner). No socket proxy exists. Static checks only (no Docker needed), so they run everywhere."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")
PROXY = re.compile(r"socket[ -]?proxy|docker-proxy|docker-socket-proxy|tecnativa|DOCKER_HOST", re.I)
# A proxy may be mentioned only as history: the same sentence must say it was removed/replaced.
HISTORICAL = re.compile(r"removed|replace[sd]?|superseded|no socket proxy|not used|was used", re.I)


def _scanned_files():
    files = [ROOT / f for f in ("README.md", "ARCHITECTURE.md", "IMPLEMENTATION_PLAN.md", "ENVIRONMENT_VARIABLES.md",
                                "CHANGELOG.md", *COMPOSE_FILES)]
    files += sorted((ROOT / "docs").glob("*.md")) + sorted((ROOT / "scripts").glob("*")) + sorted((ROOT / "env").glob("*.example"))
    files += sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / "docker").glob("*"))
    files += sorted((ROOT / "backend" / "jarvis").rglob("*.py"))
    return [f for f in files if f.is_file()]


def _sentences(text):
    return re.split(r"(?<=[.!?])\s+|\n\s*\n|\n[-|#]", text)


def test_only_sandbox_runner_mounts_docker_socket():
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    holders = [n for n, s in svc.items() if any("docker.sock" in str(v) for v in s.get("volumes", []))]
    assert holders == ["sandbox-runner"]
    assert not svc["sandbox-runner"].get("ports"), "the runner must not publish ports"
    for name in ("api", "worker"):
        assert svc[name]["environment"]["QA_RUNNER_MODE"] == "runner"
    assert svc["worker"]["environment"]["SANDBOX_RUNNER_URL"].startswith("http://sandbox-runner:")
    for name, s in svc.items():
        env = s.get("environment") or {}
        assert "DOCKER_HOST" not in (env if isinstance(env, dict) else " ".join(env)), name
    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text()).get("services", {})
    assert not [n for n, s in dev.items() if any("docker.sock" in str(v) for v in s.get("volumes", []))]


def test_no_affirmative_socket_proxy_description():
    offenders = []
    for f in _scanned_files():
        for sentence in _sentences(f.read_text(errors="ignore")):
            if PROXY.search(sentence) and not HISTORICAL.search(sentence):
                offenders.append(f"{f.relative_to(ROOT)}: {sentence.strip()[:160]}")
    assert not offenders, "socket-proxy described as current:\n" + "\n".join(offenders)


def test_docs_name_the_sandbox_runner_as_sole_socket_holder():
    for doc in ("docs/OPERATIONS.md", "docs/IMPLEMENTATION_STATUS.md", "docs/SECURITY_AND_COMPLIANCE.md", "ARCHITECTURE.md"):
        text = (ROOT / doc).read_text()
        assert "sandbox-runner" in text and re.search(r"only[^.]{0,80}(Docker[- ]socket|docker\.sock)|"
                                                     r"(Docker socket|docker\.sock)[^.]{0,40}only", text, re.I), doc


def test_ci_and_deploy_pin_the_sandbox_image():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    assert "qa-sandbox.Dockerfile" in ci and "--pull=never" in ci and "--network none" in ci
    assert "SANDBOX_IMAGE=" in deploy and "image inspect" in deploy and "sandbox-runner" in deploy
    assert re.search(r"FROM\s+\S+@sha256:[0-9a-f]{64}", (ROOT / "docker/qa-sandbox.Dockerfile").read_text())
    runner = (ROOT / "backend/jarvis/sandbox_runner.py").read_text()
    for flag in ("--pull=never", "--network", "none", "--read-only", "--cap-drop", "no-new-privileges"):
        assert flag in runner
