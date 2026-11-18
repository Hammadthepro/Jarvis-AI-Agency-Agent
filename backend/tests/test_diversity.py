"""Cross-niche, multi-campaign diversity test (4 campaigns x 3 profiles, 9 niches, 4 metros).
Every site is rendered and QA'd in a real browser; pairs must differ structurally, visually and in copy."""
import itertools
import json
import re
from pathlib import Path

import pytest

from jarvis.agents.qa import run_qa
from jarvis.config import Settings
from jarvis.sites.design import choose_design, similarity
from jarvis.sites.render import render_site

DATA = json.loads((Path(__file__).parent / "datasets" / "diversity.json").read_text())


def words(html: str) -> set[str]:
    """3-word shingles of the page's business content (<main>, excluding the fixed preview form)."""
    main = re.search(r"<main[^>]*>(.*)</main>", html, re.S).group(1)
    main = re.sub(r"<form.*?</form>", " ", main, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", main, flags=re.S))
    toks = re.findall(r"[a-z]+", text.lower())
    return {" ".join(toks[i:i + 3]) for i in range(len(toks) - 2)}


@pytest.mark.timeout(600) if hasattr(pytest.mark, "timeout") else (lambda f: f)
async def test_cross_niche_diversity(tmp_path):
    assert len({d["campaign"] for d in DATA}) == 4 and len({d["niche"] for d in DATA}) >= 6 and len({d["city"] for d in DATA}) == 4
    settings = Settings(jarvis_env="test", qa_runner_mode="subprocess")
    designs, shingles, sections = [], [], []
    for d in DATA:
        design = choose_design(d["seed"], d["niche"], designs)
        designs.append(design)
        c = dict(d["content"], business_name=d["business_name"], phone_display=f"({d['phone'][:3]}) {d['phone'][3:6]}-{d['phone'][6:]}",
                 phone_tel=f"+1{d['phone']}")
        out = tmp_path / d["seed"]
        render_site(out / "dist", content=c, design=design, agency="Test Studio", seed=d["seed"])
        html = (out / "dist" / "index.html").read_text()
        shingles.append(words(html))
        sections.append(re.findall(r'<section (?:id="(\w+)" )?class="([\w -]+)"', html))
        report, runner = await run_qa(settings, out / "dist", {"business_name": d["business_name"], "phones": [d["phone"]]}, out)
        assert report["passed"], (d["seed"], [f for f in report["findings"] if f["severity"] in ("critical", "serious")])
    for (i, a), (j, b) in itertools.combinations(enumerate(designs), 2):
        assert not (a.archetype == b.archetype and a.palette == b.palette), (i, j)
        assert similarity(a, b) <= 4, (i, j, a, b)
        jac = len(shingles[i] & shingles[j]) / max(1, len(shingles[i] | shingles[j]))
        assert jac < 0.25, (i, j, jac)
    # same-niche pairs (Houston plumbing pair; Houston vs Denver plumbing) differ in >= 3 design dimensions
    plumb = [i for i, d in enumerate(DATA) if d["niche"] == "plumbing"]
    for i, j in itertools.combinations(plumb, 2):
        assert 6 - similarity(designs[i], designs[j]) >= 3, (i, j)
    assert len({d.archetype for d in designs}) >= 5 and len({d.palette for d in designs}) >= 8
    assert len({tuple(s) for s in sections}) >= 5   # distinct section structures
