"""Deterministic design system for generated previews + diversity guard.

Variation is structural (archetype => section order, hero composition, navigation, service layout),
visual (palette, type pairing, radius, density, decorative motif) and textual (copy from the SiteSpec).
The guard rejects designs too close to recently generated sites in the same workspace."""

from __future__ import annotations

import colorsys
import hashlib
from dataclasses import asdict, dataclass

ARCHETYPES = {
    "split-hero":      {"order": ["hero", "services", "about", "area", "faq", "cta", "contact"], "hero": "split", "nav": "bar", "services": "cards"},
    "editorial":       {"order": ["hero", "about", "services", "faq", "area", "contact"], "hero": "editorial", "nav": "centered", "services": "numbered"},
    "band":            {"order": ["hero", "cta", "services", "area", "about", "contact"], "hero": "band", "nav": "bar", "services": "list"},
    "centered":        {"order": ["hero", "services", "faq", "about", "cta", "contact"], "hero": "centered", "nav": "minimal", "services": "grid-icons"},
    "asymmetric":      {"order": ["hero", "about", "area", "services", "cta", "faq", "contact"], "hero": "asymmetric", "nav": "bar", "services": "two-column"},
    "stacked-panels":  {"order": ["hero", "services", "cta", "about", "faq", "area", "contact"], "hero": "panel", "nav": "pill", "services": "panels"},
}

PALETTES = {  # name: (primary, accent, ink, paper, muted-surface)
    "harbor":    ("#0F4C81", "#F2A541", "#14202E", "#FBFAF7", "#E8EEF4"),
    "forest":    ("#1F5135", "#D98E04", "#13201A", "#F7F8F3", "#E4ECE3"),
    "terracotta": ("#A23E2A", "#2F6F73", "#2A1A16", "#FFF9F4", "#F4E5DC"),
    "graphite":  ("#22272E", "#E4572E", "#111418", "#F6F7F8", "#E6E8EB"),
    "plum":      ("#5B2A6E", "#E0A458", "#21142A", "#FCF9FD", "#EDE3F1"),
    "teal":      ("#0B6E6E", "#F25C54", "#0E1F21", "#F5FBFA", "#DDEFED"),
    "navy-gold": ("#1B2A4A", "#C9A227", "#0F1626", "#FAF8F2", "#E7E4D8"),
    "brick":     ("#8C2F39", "#3B7080", "#22161A", "#FDF8F6", "#F1E2E2"),
    "olive":     ("#4B5D23", "#B5562B", "#1C2112", "#FAFAF3", "#E8EBD9"),
    "cobalt":    ("#1D4ED8", "#F59E0B", "#0F172A", "#F8FAFC", "#E2E8F0"),
    "charcoal-mint": ("#2B2D33", "#2BB673", "#121317", "#F7F8F7", "#E3E7E4"),
    "sand-ink":  ("#3D3A35", "#C2410C", "#1A1917", "#FBF7EF", "#EEE6D6"),
}

TYPE_PAIRS = {  # system font stacks only (no external font requests; CSP forbids them)
    "classic-serif": ("Georgia, 'Times New Roman', serif", "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"),
    "modern-sans":   ("'Helvetica Neue', Arial, sans-serif", "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"),
    "humanist":      ("'Gill Sans', 'Gill Sans MT', Calibri, sans-serif", "'Trebuchet MS', 'Lucida Sans', sans-serif"),
    "book":          ("'Palatino Linotype', Palatino, 'Book Antiqua', serif", "Verdana, Geneva, sans-serif"),
    "geometric":     ("Futura, 'Century Gothic', 'Avenir Next', sans-serif", "Avenir, 'Segoe UI', system-ui, sans-serif"),
    "slab":          ("Rockwell, 'Roboto Slab', 'Courier New', serif", "system-ui, -apple-system, 'Segoe UI', sans-serif"),
    "condensed":     ("'Arial Narrow', 'Roboto Condensed', Impact, sans-serif", "Arial, Helvetica, sans-serif"),
    "transitional":  ("Baskerville, 'Baskerville Old Face', Garamond, serif", "'Segoe UI', Tahoma, sans-serif"),
}
RADII = {"sharp": "2px", "soft": "10px", "round": "22px"}
DENSITY = {"airy": 1.25, "standard": 1.0, "compact": 0.85}
MOTIFS = ["dots", "waves", "blocks", "rings", "diagonal", "grid"]


@dataclass(frozen=True)
class Design:
    archetype: str
    palette: str
    type_pair: str
    radius: str
    density: str
    motif: str
    contrast_boost: int = 0

    def tokens(self) -> dict:
        primary, accent, ink, paper, muted = PALETTES[self.palette]
        if self.contrast_boost:
            primary, accent = darken(primary, 0.12 * self.contrast_boost), darken(accent, 0.18 * self.contrast_boost)
        head, body = TYPE_PAIRS[self.type_pair]
        primary = ensure_contrast(primary, paper, 4.6)          # primary is used as text on paper/muted
        primary = ensure_contrast(primary, muted, 4.6)
        accent_text = ensure_contrast(accent, paper, 4.6)       # accent used as text
        return {"primary": primary, "accent": accent, "accent_text": accent_text, "ink": ink, "paper": paper, "muted": muted,
                "on_primary": best_on(primary), "on_accent": best_on(accent),
                "font_head": head, "font_body": body, "radius": RADII[self.radius], "space": DENSITY[self.density],
                **ARCHETYPES[self.archetype]}


def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(h: str) -> float:
    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def darken(h: str, amt: float) -> str:
    r, g, b = _rgb(h)
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(hh, max(0, ll - amt), ss)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


def best_on(bg: str) -> str:
    return "#FFFFFF" if contrast("#FFFFFF", bg) >= contrast("#111111", bg) else "#111111"


def ensure_contrast(fg: str, bg: str, ratio: float) -> str:
    """Darken (or lighten on dark backgrounds) fg until it meets the WCAG ratio against bg."""
    c, step = fg, 0
    while contrast(c, bg) < ratio and step < 40:
        c = darken(c, 0.03) if luminance(bg) > 0.4 else lighten(c, 0.03)
        step += 1
    return c


def lighten(h: str, amt: float) -> str:
    r, g, b = _rgb(h)
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(hh, min(1, ll + amt), ss)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


def _palette_distance(a: str, b: str) -> float:
    pa, pb = PALETTES[a][0], PALETTES[b][0]
    return sum(abs(x - y) for x, y in zip(_rgb(pa), _rgb(pb), strict=True))


def similarity(a: Design, b: Design) -> int:
    """Number of shared dimensions (0-6). Palette counts as shared when primaries are near-identical."""
    return sum([a.archetype == b.archetype, a.palette == b.palette or _palette_distance(a.palette, b.palette) < 0.25,
                a.type_pair == b.type_pair, a.radius == b.radius, a.density == b.density, a.motif == b.motif])


def choose_design(seed: str, niche: str, recent: list[Design], *, max_shared: int = 3) -> Design:
    """Deterministic for (seed, recent). Picks the candidate that is least similar to recent designs and
    reuses archetypes/palettes least; never repeats an archetype+palette pair."""
    from collections import Counter
    h = int(hashlib.sha256(f"{seed}|{niche}".encode()).hexdigest(), 16)
    arch, pal, typ = list(ARCHETYPES), list(PALETTES), list(TYPE_PAIRS)
    arch_n, pal_n, typ_n = Counter(r.archetype for r in recent), Counter(r.palette for r in recent), Counter(r.type_pair for r in recent)
    best, best_score = None, float("inf")
    for i in range(600):
        x = h + i * 7919
        d = Design(arch[x % len(arch)], pal[(x // 7) % len(pal)], typ[(x // 97) % len(typ)],
                   list(RADII)[(x // 991) % 3], list(DENSITY)[(x // 4001) % 3], MOTIFS[(x // 13001) % len(MOTIFS)])
        if any(d.archetype == r.archetype and d.palette == r.palette for r in recent):
            continue
        worst = max((similarity(d, r) for r in recent), default=0)
        score = (100 if worst > max_shared else 0) + worst * 10 + arch_n[d.archetype] * 6 + pal_n[d.palette] * 4 + typ_n[d.type_pair] * 2
        if score < best_score:
            best, best_score = d, score
    assert best is not None
    return best


def design_from_dict(d: dict) -> Design:
    return Design(**{k: d[k] for k in asdict(Design("split-hero", "harbor", "book", "soft", "standard", "dots")) if k in d})
