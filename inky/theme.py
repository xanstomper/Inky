"""Theme loading and color derivation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import BUILTIN_THEMES, USER_THEMES
from .util import alpha, is_dark, mix, parse_color, readable, readable_on

# ANSI slot 0 is the terminal background itself; everything else is text and
# gets a legibility floor. 3.0 is the WCAG threshold for large text and keeps
# the palette's intended mood instead of flattening it to white.
MIN_CONTRAST = 3.0


@dataclass
class Theme:
    slug: str
    name: str
    bg: str = "#0b0c12"
    fg: str = "#d9deee"
    cursor: str = ""
    selection: str = ""
    accent: str = ""
    accent_alt: str = ""
    palette: list[str] = field(default_factory=list)
    dark: bool | None = None
    ui_overrides: dict = field(default_factory=dict)
    ui: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.palette = [p for p in (self.palette or []) if isinstance(p, str)]
        if len(self.palette) < 16:
            fillers = [self.bg, "#ff6b81", "#7ee2a1", "#f2cf7a", "#8fb0ff", "#c9a0ff",
                       "#89e3e3", self.fg, "#4a5068", "#ff8fa3", "#a5f0b6", "#ffe08a",
                       "#a3c0ff", "#d9b6ff", "#9ef0ef", "#f2f4ff"]
            while len(self.palette) < 16:
                self.palette.append(fillers[len(self.palette)])
        self.accent = self.accent or self.palette[4]
        self.accent_alt = self.accent_alt or self.palette[5]
        self.cursor = self.cursor or self.accent
        self.selection = self.selection or mix(self.bg, self.accent, 0.28)
        if self.dark is None:
            self.dark = is_dark(self.bg)
        self._derive()

    def _legible_palette(self, target: float = MIN_CONTRAST) -> list[str]:
        """Palette with the text slots lifted off the background."""
        palette = list(self.palette[:16])
        if target <= 1.0:
            return palette
        for index in range(1, len(palette)):
            palette[index] = readable(palette[index], self.bg, target)
        return palette

    def _derive(self) -> None:
        bg, fg = self.bg, self.fg
        tint = lambda k: mix(bg, fg, k)
        derived = {
            "bg_alt": tint(0.035),
            "surface": tint(0.07),
            "surface_hi": tint(0.11),
            "surface_hi2": tint(0.16),
            "border": tint(0.135),
            "border_hi": tint(0.22),
            "fg_dim": mix(fg, bg, 0.26),
            "muted": mix(fg, bg, 0.47),
            "scrim": alpha("#04050a" if self.dark else "#f4f6ff", 0.5),
            "danger": self.palette[1],
            "success": self.palette[2],
            "warning": self.palette[3],
            "info": self.palette[4],
            "on_accent": readable_on(self.accent),
            "shadow": alpha("#000000" if self.dark else "#1a2233", 0.35),
            "bold": readable(mix(fg, "#ffffff" if self.dark else "#000000", 0.12),
                             bg, max(7.0, MIN_CONTRAST)),
        }
        derived.update(self.ui_overrides or {})
        self.ui = derived

    # convenience -------------------------------------------------------
    def get(self, key: str) -> str:
        return self.ui.get(key) or self.palette[7]

    @property
    def palette_css(self) -> list[str]:
        return self._legible_palette()

    def palette_for(self, min_contrast: float = MIN_CONTRAST) -> list[str]:
        """The 16 ANSI colours as the terminal should show them."""
        return self._legible_palette(min_contrast)


def _from_dict(slug: str, data: dict) -> Theme:
    known = {
        "slug", "name", "bg", "fg", "cursor", "selection", "accent",
        "accent_alt", "palette", "dark", "ui",
    }
    kwargs = {k: v for k, v in data.items() if k in known and k != "slug"}
    kwargs["name"] = kwargs.get("name") or slug.replace("-", " ").title()
    kwargs["ui_overrides"] = data.get("ui") or {}
    # Validate every color early so one broken theme cannot take down the app.
    for key in ("bg", "fg", "cursor", "selection", "accent", "accent_alt"):
        if kwargs.get(key):
            parse_color(kwargs[key])
    for color in kwargs.get("palette", []):
        parse_color(color)
    return Theme(slug=slug, **kwargs)


def load_themes() -> dict[str, Theme]:
    themes: dict[str, Theme] = {}
    for directory in (BUILTIN_THEMES, USER_THEMES):
        if not directory or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                theme = _from_dict(path.stem, data)
            except Exception:
                continue
            themes[theme.slug] = theme
    if not themes:
        themes["inky"] = Theme(slug="inky", name="Inky")
    return dict(sorted(themes.items(), key=lambda item: item[1].name.lower()))


def pick(themes: dict[str, Theme], slug: str | None) -> Theme:
    if slug and slug in themes:
        return themes[slug]
    for fallback in ("inky", "nord", "dracula"):
        if fallback in themes:
            return themes[fallback]
    return next(iter(themes.values()))
