"""Static site renderer: SiteSpec (copy) + Design (tokens) -> static files.

The model never writes executable code: it supplies text; this renderer owns all HTML/CSS/JS, which
is escaped by Jinja autoescaping. Output is static only, with a restrictive CSP (no external
scripts, styles, fonts, frames or connections) and noindex headers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jarvis.integrations.cloudflare import headers_file
from jarvis.sites.design import Design

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
ALLOWED_EXT = {".html", ".css", ".js", ".txt", ".xml", ".svg", ".json"}
NAV = {"services": "Services", "about": "About", "area": "Service area", "faq": "FAQ", "contact": "Contact"}

SITE_JS = """(function(){var b=document.querySelector('.nav-toggle'),n=document.getElementById('site-nav');
if(!b||!n)return;b.addEventListener('click',function(){var o=b.getAttribute('aria-expanded')==='true';
b.setAttribute('aria-expanded',String(!o));n.classList.toggle('open',!o);});
n.addEventListener('click',function(e){if(e.target.tagName==='A'){b.setAttribute('aria-expanded','false');n.classList.remove('open');}});})();
"""


def art_svg(d: Design, t: dict, seed: str) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    p, a, m = t["primary"], t["accent"], t["muted"]
    shapes = []
    if d.motif == "dots":
        for i in range(36):
            x, y = (i % 6) * 60 + 30, (i // 6) * 60 + 30
            r = 6 + (h >> i) % 14
            shapes.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{a if (h >> (i + 3)) % 3 == 0 else p}" opacity="0.85"/>')
    elif d.motif == "waves":
        for i in range(7):
            yy = 40 + i * 48
            amp = 14 + (h >> i) % 18
            shapes.append(f'<path d="M0 {yy} Q 90 {yy - amp} 180 {yy} T 360 {yy}" stroke="{a if i % 2 else p}" stroke-width="10" fill="none" stroke-linecap="round"/>')
    elif d.motif == "blocks":
        for i in range(9):
            x, y = (i % 3) * 120, (i // 3) * 120
            fill = [p, a, t["ink"]][(h >> i) % 3]
            shapes.append(f'<rect x="{x + 8}" y="{y + 8}" width="104" height="104" rx="{4 + (h >> i) % 30}" fill="{fill}"/>')
    elif d.motif == "rings":
        for i in range(6):
            shapes.append(f'<circle cx="180" cy="180" r="{170 - i * 26}" fill="none" stroke="{a if i % 2 else p}" stroke-width="{8 + i % 3 * 4}"/>')
    elif d.motif == "diagonal":
        for i in range(10):
            shapes.append(f'<rect x="{-200 + i * 70}" y="-40" width="34" height="520" transform="rotate(30 180 180)" fill="{a if i % 3 == 0 else p}" opacity="0.9"/>')
    else:
        for i in range(8):
            shapes.append(f'<line x1="{i * 48}" y1="0" x2="{i * 48}" y2="360" stroke="{p}" stroke-width="2"/>')
            shapes.append(f'<line x1="0" y1="{i * 48}" x2="360" y2="{i * 48}" stroke="{p}" stroke-width="2"/>')
        shapes.append(f'<rect x="{96 + h % 100}" y="{96 + (h >> 8) % 100}" width="96" height="96" fill="{a}"/>')
    return (f'<svg viewBox="0 0 360 360" role="presentation" focusable="false" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="360" height="360" fill="{m}"/>{"".join(shapes)}</svg>')


def css(t: dict) -> str:
    s = t["space"]
    sp = lambda n: f"{round(n * s, 2)}rem"  # noqa: E731
    hero = {
        "split": f".hero-grid{{display:grid;grid-template-columns:1.1fr .9fr;gap:{sp(3)};align-items:center}}",
        "editorial": f".hero-grid{{display:grid;grid-template-columns:1fr;gap:{sp(2)}}}.hero h1{{font-size:clamp(2.4rem,6vw,4.6rem);max-width:16ch}}.hero-art{{max-height:180px;overflow:hidden;border-radius:var(--r)}}.hero-art svg{{height:180px;width:100%}}",
        "band": f".hero{{background:var(--primary);color:var(--on-primary)}}.hero .lede,.hero .eyebrow{{color:var(--on-primary)}}.hero .btn-ghost{{color:var(--on-primary);border-color:var(--on-primary)}}.hero-grid{{display:grid;grid-template-columns:1.4fr .6fr;gap:{sp(2)};align-items:center}}",
        "centered": f".hero{{text-align:center}}.hero-grid{{display:grid;justify-items:center;gap:{sp(2)}}}.hero-copy{{max-width:44rem}}.hero-actions{{justify-content:center}}.hero-art{{width:min(320px,70%)}}",
        "asymmetric": f".hero-grid{{display:grid;grid-template-columns:.8fr 1.2fr;gap:{sp(3)};align-items:end}}.hero-art{{order:-1}}",
        "panel": f".hero-grid{{display:grid;grid-template-columns:1fr 1fr;background:var(--muted);border-radius:var(--r);padding:{sp(2.5)};gap:{sp(2)};align-items:center}}",
    }[t["hero"]]
    services = {
        "cards": f".service-list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:{sp(1.25)}}}.service{{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:{sp(1.5)}}}.service-index{{display:none}}",
        "numbered": f".service{{display:grid;grid-template-columns:4rem 1fr;column-gap:{sp(1)};border-top:2px solid var(--ink);padding:{sp(1.25)} 0}}.service p{{grid-column:2}}.service-index{{font-family:var(--fh);font-size:2rem;color:var(--primary);grid-row:span 2}}",
        "list": f".service{{border-left:6px solid var(--accent);padding:{sp(.75)} {sp(1.25)};margin-bottom:{sp(1)};background:var(--muted)}}.service-index{{display:none}}",
        "grid-icons": f".service-list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:{sp(2)};text-align:center}}.service-index{{display:inline-grid;place-items:center;width:3rem;height:3rem;border-radius:50%;background:var(--accent);color:var(--on-accent);font-weight:700}}",
        "two-column": f".service-list{{columns:2 18rem;column-gap:{sp(3)}}}.service{{break-inside:avoid;padding-bottom:{sp(1.25)}}}.service-index{{color:var(--accent-text);font-weight:700}}",
        "panels": f".service-list{{display:grid;gap:{sp(1)}}}.service{{display:grid;grid-template-columns:1fr 2fr;gap:{sp(1.5)};background:var(--primary);color:var(--on-primary);padding:{sp(1.5)};border-radius:var(--r)}}.service p{{color:var(--on-primary)}}.service-index{{display:none}}",
    }[t["services"]]
    nav = {
        "bar": "",
        "centered": ".header-inner{flex-wrap:wrap;justify-content:center}.brand{width:100%;text-align:center}",
        "minimal": ".site-nav ul{gap:1.5rem}.site-nav a{text-transform:uppercase;letter-spacing:.08em;font-size:.8rem}",
        "pill": ".site-nav ul{background:var(--muted);border-radius:999px;padding:.35rem .9rem}",
    }[t["nav"]]
    return f"""*,*::before,*::after{{box-sizing:border-box}}
:root{{--primary:{t['primary']};--accent:{t['accent']};--ink:{t['ink']};--paper:{t['paper']};--muted:{t['muted']};
--on-primary:{t['on_primary']};--on-accent:{t['on_accent']};--accent-text:{t['accent_text']};--line:rgba(0,0,0,.12);--r:{t['radius']};
--fh:{t['font_head']};--fb:{t['font_body']}}}
html{{scroll-behavior:smooth}}body{{margin:0;font-family:var(--fb);color:var(--ink);background:var(--paper);line-height:1.6;font-size:1.0625rem}}
img,svg{{max-width:100%;display:block}}h1,h2,h3{{font-family:var(--fh);line-height:1.15;margin:0 0 {sp(.75)}}}
h1{{font-size:clamp(2rem,5vw,3.4rem)}}h2{{font-size:clamp(1.5rem,3.2vw,2.3rem)}}h3{{font-size:1.2rem}}p{{margin:0 0 {sp(1)}}}
a{{color:var(--primary)}}.wrap{{width:min(72rem,100% - 2.5rem);margin-inline:auto}}.narrow{{width:min(46rem,100% - 2.5rem)}}
section{{padding:{sp(4)} 0}}.skip{{position:absolute;left:-999px}}.skip:focus{{left:1rem;top:1rem;background:#fff;padding:.5rem;z-index:10}}
.preview-banner{{background:#111;color:#fff;font-size:.85rem;text-align:center;padding:.45rem 1rem}}
.site-header{{border-bottom:1px solid var(--line);background:var(--paper);position:sticky;top:0;z-index:5}}
.header-inner{{display:flex;align-items:center;gap:1.25rem;padding:.9rem 0}}.brand{{font-family:var(--fh);font-weight:700;font-size:1.2rem;color:var(--ink);text-decoration:none;margin-right:auto}}
.site-nav ul{{display:flex;gap:1.1rem;list-style:none;margin:0;padding:0}}.site-nav a{{color:var(--ink);text-decoration:none}}.site-nav a:hover{{text-decoration:underline}}
.nav-toggle{{display:none;font:inherit;background:none;border:1px solid var(--ink);color:var(--ink);border-radius:var(--r);padding:.35rem .8rem}}
.btn{{display:inline-block;font-weight:700;text-decoration:none;border-radius:var(--r);padding:.8rem 1.3rem;border:2px solid transparent;font-size:1rem;cursor:pointer}}
.btn-small{{padding:.45rem .9rem;font-size:.9rem;background:var(--primary);color:var(--on-primary)}}.btn-primary{{background:var(--primary);color:var(--on-primary)}}
.btn-accent{{background:var(--accent);color:var(--on-accent)}}.btn-ghost{{border-color:var(--primary);color:var(--primary)}}.btn[disabled]{{opacity:.6;cursor:not-allowed}}
.hero-actions{{display:flex;flex-wrap:wrap;gap:.75rem;margin-top:{sp(1.5)}}}.eyebrow{{text-transform:uppercase;letter-spacing:.12em;font-size:.8rem;font-weight:700;color:var(--primary)}}
.lede{{font-size:1.2rem;max-width:38rem}}.hero-art svg{{border-radius:var(--r);width:100%;height:auto}}
.service-list{{list-style:none;margin:0;padding:0}}.about p{{font-size:1.1rem}}.area{{background:var(--muted)}}
.faq details{{border-bottom:1px solid var(--line);padding:{sp(.9)} 0}}.faq summary{{cursor:pointer;font-weight:700}}
.cta-band{{background:var(--ink);color:var(--paper);text-align:center}}.cta-band h2,.cta-band p{{color:var(--paper)}}.cta-inner{{max-width:46rem}}
.contact-grid{{display:grid;grid-template-columns:1fr 1fr;gap:{sp(3)}}}.contact-form{{display:grid;gap:.5rem;background:var(--muted);padding:{sp(1.5)};border-radius:var(--r)}}
.contact-form input,.contact-form textarea{{font:inherit;padding:.6rem;border:1px solid #767676;border-radius:calc(var(--r) / 2);background:#fff}}
.form-note{{font-size:.9rem}}.site-footer{{border-top:1px solid var(--line);padding:{sp(2)} 0;font-size:.95rem}}.fine{{font-size:.85rem;opacity:.85}}
{hero}
{services}
{nav}
@media (max-width:760px){{.hero-grid,.contact-grid,.service{{grid-template-columns:1fr!important}}.hero-art{{order:0!important}}
.nav-toggle{{display:inline-block}}.site-nav{{display:none;width:100%}}.site-nav.open{{display:block}}.site-nav ul{{flex-direction:column;gap:.6rem;padding:.5rem 0;background:none}}
.header-inner{{flex-wrap:wrap}}.header-call{{display:none}}section{{padding:{sp(2.5)} 0}}.service-list{{columns:1}}}}
"""


def build_jsonld(c: dict) -> str:
    data = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": c["business_name"]}
    if c.get("phone_tel"):
        data["telephone"] = c["phone_tel"]
    if c.get("email"):
        data["email"] = c["email"]
    if c.get("service_area"):
        data["areaServed"] = c["service_area_short"] or c["service_area"]
    return json.dumps(data).replace("<", "\\u003c")


def render_site(out_dir: Path, *, content: dict, design: Design, agency: str, seed: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    t = design.tokens() | {"archetype": design.archetype}
    nav = {k: v for k, v in NAV.items() if k in t["order"] and (k != "services" or content.get("services"))
           and (k != "about" or content.get("about")) and (k != "faq" or content.get("faq"))
           and (k != "area" or content.get("service_area"))}
    html = _env.get_template("page.html.j2").render(c=content, t=t, d=design, agency=agency, nav_labels=nav,
                                                    art=art_svg(design, t, seed), jsonld=build_jsonld(content))
    files = {"index.html": html, "styles.css": css(t), "site.js": SITE_JS,
             "robots.txt": "User-agent: *\nDisallow: /\n", "_headers": headers_file(CSP),
             "404.html": html.replace("<h1>", "<h1>Page not found · ", 1)}
    manifest = {}
    for name, body in files.items():
        p = out_dir / name
        p.write_text(body, encoding="utf-8")
        manifest[name] = hashlib.sha256(body.encode()).hexdigest()
    validate_output(out_dir)
    total = sum(f.stat().st_size for f in out_dir.iterdir())
    return {"files": manifest, "file_count": len(files), "total_bytes": total,
            "sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}


def validate_output(out_dir: Path, max_bytes: int = 20 * 1024 * 1024) -> None:
    total = 0
    for p in out_dir.rglob("*"):
        if p.is_dir():
            continue
        if p.name in ("_headers", "_redirects"):
            pass
        elif p.suffix not in ALLOWED_EXT:
            raise ValueError(f"disallowed output file type: {p.name}")
        if p.name == "_worker.js" or "functions" in p.parts:
            raise ValueError("server-side functions are not allowed in previews")
        total += p.stat().st_size
    if total > max_bytes:
        raise ValueError("output exceeds size limit")
    html = (out_dir / "index.html").read_text()
    if re.search(r"<script(?![^>]*src=\"/site\.js\")(?![^>]*application/ld\+json)", html):
        raise ValueError("unexpected inline or external script in output")
