"""QA runner self-check: runs the REAL browser QA (the configured mode: isolated Docker sandbox or local subprocess) against
a small static page and requires a real report with screenshots. Used by scripts/dev-up.sh and dev-status.sh so
"installed" means "QA actually executes", not "npm install exited 0".

    python -m jarvis.agents.qa_selfcheck        prints one JSON line; exit 0 only if QA executed
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from jarvis.agents.qa import QaRunnerError, run_qa
from jarvis.config import get_settings

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS QA self-check</title><meta name="description" content="A small page used to confirm that browser QA runs.">
<style>body{font-family:system-ui,sans-serif;margin:0;padding:2rem;color:#111;background:#fff}h1{font-size:2rem}</style></head>
<body><header><nav><a href="#main">Skip to content</a></nav></header><main id="main"><h1>JARVIS QA self-check</h1>
<p>This page exists only to confirm that the isolated browser QA runner works.</p></main><footer><p>Self-check</p></footer></body></html>"""


async def selfcheck() -> dict:
    s = get_settings()
    base = Path(s.sites_artifact_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qa-selfcheck-", dir=base))
    try:
        site = work / "dist"
        site.mkdir()
        (site / "index.html").write_text(PAGE)
        work.chmod(0o755)
        site.chmod(0o755)
        (site / "index.html").chmod(0o644)
        try:
            report, runner = await run_qa(s, site, {"business_name": "JARVIS QA self-check", "phones": []}, work)
        except QaRunnerError as exc:
            return {"ok": False, "mode": s.qa_runner_mode, "error": str(exc)[-600:]}
        shots = [n for n in report.get("screenshots", []) if (work / "qa" / n).exists()]
        if not shots:
            return {"ok": False, "mode": s.qa_runner_mode, "runner": runner, "error": "QA ran but produced no screenshots"}
        return {"ok": True, "mode": s.qa_runner_mode, "runner": runner, "screenshots": len(shots),
                "findings": len(report.get("findings", []))}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    out = asyncio.run(selfcheck())
    print(json.dumps(out))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
