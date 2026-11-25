"""Small pure helpers shared across Inky."""

from __future__ import annotations

import re
import shlex
import time


def parse_color(value: str) -> tuple[int, int, int]:
    """Accept ``#rgb``, ``#rrggbb`` and ``rgb(...)`` and return an RGB triple."""
    value = (value or "").strip()
    if value.startswith("#"):
        body = value[1:]
        if len(body) == 3:
            body = "".join(ch * 2 for ch in body)
        if len(body) != 6:
            raise ValueError(f"bad color: {value!r}")
        return int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16)
    match = re.match(r"rgba?\(([^)]+)\)", value)
    if match:
        parts = [p.strip() for p in match.group(1).split(",")]
        return int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
    raise ValueError(f"bad color: {value!r}")


def hexc(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(a: str, b: str, t: float) -> str:
    """Blend two colors; ``t=0`` is *a*, ``t=1`` is *b*."""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = parse_color(a)
    rb, gb, bb = parse_color(b)
    return hexc((ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t))


def alpha(color: str, a: float) -> str:
    r, g, b = parse_color(color)
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, a)):.3f})"


def luminance(color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = parse_color(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def is_dark(color: str) -> bool:
    return luminance(color) < 0.42


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1.0 … 21.0)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def readable(color: str, bg: str, target: float = 3.0) -> str:
    """Nudge a colour away from the background until it is legible on it.

    Coding agents paint a lot of their output in the dim end of the ANSI
    palette (comments, metadata, diff context). On a dark theme those slots can
    sit one shade above the background and read as smudges, so each one is
    lifted just far enough to hit ``target`` while keeping its hue.
    """
    if target <= 1.0 or contrast(color, bg) >= target:
        return color
    toward = "#ffffff" if is_dark(bg) else "#000000"
    candidate = color
    for step in range(1, 25):
        candidate = mix(color, toward, step * 0.03)
        if contrast(candidate, bg) >= target:
            break
    return candidate


def readable_on(bg: str) -> str:
    return "#0a0b0f" if luminance(bg) > 0.45 else "#f5f7ff"


def fuzzy(query: str, text: str) -> int | None:
    """Subsequence match score. ``None`` means no match."""
    q = query.lower().strip()
    t = text.lower()
    if not q:
        return 0
    score = 0
    index = 0
    streak = 0
    for ch in q:
        found = t.find(ch, index)
        if found < 0:
            return None
        gap = found - index
        if gap == 0:
            streak += 1
            score += 10 + streak * 2
        else:
            streak = 0
            score += max(1, 8 - min(gap, 7))
        index = found + 1
    if t.startswith(q):
        score += 60
    elif any(t.startswith(p) for p in q.split()):
        score += 12
    return score


def human_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def shell_quote(path: str) -> str:
    return shlex.quote(path)


def slugify(text: str, limit: int = 42) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip()).strip("-")
    return (slug or "transcript")[:limit]


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def ellipsize_middle(text: str, limit: int = 60) -> str:
    if len(text) <= limit:
        return text
    keep = max(4, (limit - 1) // 2)
    return f"{text[:keep]}…{text[-keep:]}"
