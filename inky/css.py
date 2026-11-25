"""GTK3 stylesheet generation from a Theme."""

from __future__ import annotations

from .theme import Theme
from .util import mix


def build_css(theme: Theme, cfg: dict) -> str:
    u = theme.ui
    mono = cfg.get("font", {}).get("family", "JetBrains Mono")
    mono_size = float(cfg.get("font", {}).get("size", 11.5))
    ui_family = cfg.get("ui_font", {}).get("family", "Inter")
    ui_size = float(cfg.get("ui_font", {}).get("size", 10.0))
    tiny = max(7.0, ui_size - 1.7)
    row_sel = mix(u["surface"], theme.accent, 0.16)
    quiet = mix(theme.bg, theme.fg, 0.34)
    bar = mix(theme.bg, theme.fg, 0.085)
    tab_hover = mix(theme.bg, theme.fg, 0.14)

    return f"""
/* ---------------------------------------------------------------
   Inky — {theme.name}
   Alacritty geometry: square edges, no boxes, no chrome around the grid.
   --------------------------------------------------------------- */
* {{ outline: none; }}

window.inky-window,
.inky-window {{
    background-color: {theme.bg};
    color: {theme.fg};
    border: none;
}}

decoration {{
    background-color: {theme.bg};
    border-radius: 0;
    box-shadow: none;
    margin: 0;
    padding: 0;
}}

/* -- titlebar / tabs --------------------------------------------- */
headerbar.inky-header {{
    background-color: {bar};
    background-image: none;
    border: none;
    box-shadow: none;
    min-height: 38px;
    padding: 0 6px 0 4px;
}}
.window-title {{
    font-size: {ui_size + 0.7}pt;
    color: {u['fg_dim']};
}}
headerbar.inky-header button.titlebutton {{
    min-width: 22px;
    min-height: 22px;
    padding: 0;
    margin: 0 3px;
    border: none;
    border-radius: 0;
    background: transparent;
    background-image: none;
    box-shadow: none;
    color: {u['fg_dim']};
}}
headerbar.inky-header button.titlebutton:hover {{
    background-color: {u['bg_alt']};
    color: {theme.fg};
}}
headerbar.inky-header button.titlebutton.close:hover {{
    background-color: transparent;
    color: {u['danger']};
}}

.tabstrip {{
    background-color: {bar};
    border-bottom: 1px solid {u['border']};
}}
.tabstrip box {{
    background-color: transparent;
}}
.tabstrip scrollbar,
.tabstrip scrollbar slider {{
    min-width: 0;
    min-height: 0;
    background: transparent;
    border: none;
}}

/* Terminal-style tab bars: full-height rectangles that share the row, the
   active one drops to the terminal colour and carries an accent underline. */
.tab {{
    background-color: {bar};
    background-image: none;
    border: none;
    border-right: 1px solid {u['border']};
    border-radius: 0;
    padding: 0;
    margin: 0;
    min-height: 36px;
}}
.tab:hover {{
    background-color: {tab_hover};
}}
.tab.active {{
    background-color: {theme.bg};
    box-shadow: inset 0 -2px {theme.accent};
}}
.tab-title {{
    font-family: "{ui_family}", "Cantarell", sans-serif;
    font-size: {ui_size + 0.7}pt;
    color: {u['fg_dim']};
}}
.tab:hover .tab-title {{
    color: {theme.fg};
}}
.tab.active .tab-title {{
    color: {theme.fg};
}}
.tab.done .tab-title {{
    color: {u['success']};
}}
.tab-glyph {{
    font-size: {ui_size - 0.3}pt;
    color: {u['muted']};
}}
.tab.active .tab-glyph {{
    color: {theme.accent};
}}
.tab-close {{
    min-width: 16px;
    min-height: 16px;
    padding: 0;
    border: none;
    border-radius: 0;
    background-color: transparent;
    color: {quiet};
    font-size: {tiny + 1.0}pt;
}}
.tab-close:hover {{
    background-color: transparent;
    color: {theme.fg};
}}
.toolbutton {{
    min-width: 26px;
    min-height: 26px;
    padding: 0;
    margin: 0 2px;
    border: none;
    border-radius: 0;
    background: transparent;
    background-image: none;
    box-shadow: none;
    color: {quiet};
    font-size: {ui_size + 1.0}pt;
}}
.toolbutton:hover {{
    background-color: {u['bg_alt']};
    color: {theme.fg};
}}
/* -- theme settings dialog ---------------------------------------- */
.theme-list {{
    background-color: {u['surface']};
}}
.theme-list row {{
    background-color: transparent;
    border-radius: 0;
    padding: 2px 6px;
}}
.theme-list row:hover {{
    background-color: {u['surface_hi']};
}}
.theme-list row:selected {{
    background-color: {row_sel};
}}
.theme-name {{
    font-size: {ui_size + 0.6}pt;
    color: {theme.fg};
}}
.theme-slug {{
    font-size: {tiny}pt;
    color: {u['muted']};
}}
.theme-mark {{
    font-size: {ui_size}pt;
    color: {theme.accent};
}}
dialog {{
    background-color: {u['surface']};
}}
dialog button {{
    border-radius: 0;
    padding: 4px 12px;
}}

/* -- status line -------------------------------------------------- */
.statusbar {{
    background-color: transparent;
    border: none;
    padding: 1px 6px 3px 6px;
    min-height: 17px;
}}
.status-label {{
    font-family: "{ui_family}", "Cantarell", sans-serif;
    font-size: {tiny}pt;
    color: {u['muted']};
}}

.term-area {{
    background-color: transparent;
    border: none;
}}

/* -- command palette ---------------------------------------------- */
.palette-scrim {{
    background-color: {u['scrim']};
}}
.palette-card {{
    background-color: {u['surface']};
    border: 1px solid {u['border']};
    border-radius: 0;
    padding: 8px;
}}
.palette-entry {{
    background-color: {theme.bg};
    border: 1px solid {u['border']};
    border-radius: 0;
    padding: 5px 8px;
    color: {theme.fg};
    caret-color: {theme.accent};
    font-size: {ui_size}pt;
}}
.palette-entry:focus {{
    border-color: {theme.accent};
}}
.palette-list {{
    background-color: transparent;
}}
.palette-list row {{
    background-color: transparent;
    border-radius: 0;
    padding: 2px 8px;
    min-height: 25px;
}}
.palette-list row:hover {{
    background-color: {u['surface_hi']};
}}
.palette-list row:selected {{
    background-color: {row_sel};
}}
.palette-title {{
    font-size: {ui_size - 0.3}pt;
    color: {theme.fg};
}}
.palette-sub {{
    font-size: {tiny}pt;
    color: {u['muted']};
}}
.palette-glyph {{
    font-size: {ui_size + 0.5}pt;
    color: {theme.accent};
}}
.palette-section {{
    font-size: {tiny}pt;
    font-weight: 700;
    color: {u['muted']};
    padding: 7px 8px 2px 8px;
}}
.palette-empty {{
    color: {u['muted']};
    font-size: {ui_size - 0.5}pt;
    padding: 14px;
}}

/* -- prompt composer --------------------------------------------- */
.composer {{
    background-color: {u['surface']};
    border: 1px solid {theme.accent};
    border-radius: 0;
    padding: 8px;
}}
.composer textview,
.composer textview text {{
    background-color: transparent;
    color: {theme.fg};
    font-family: "{mono}";
    font-size: {mono_size - 0.5}pt;
}}
.composer-hint {{
    font-size: {tiny}pt;
    color: {u['muted']};
}}
.composer-hint-accent {{
    font-size: {tiny}pt;
    color: {theme.accent};
}}

/* -- search ------------------------------------------------------- */
.searchbar {{
    background-color: {u['surface']};
    border: 1px solid {u['border']};
    border-radius: 0;
    padding: 2px 4px;
}}
.searchbar entry {{
    background-color: transparent;
    border: none;
    box-shadow: none;
    color: {theme.fg};
    font-size: {ui_size}pt;
    min-height: 22px;
}}
.searchbar button {{
    background: transparent;
    border: none;
    box-shadow: none;
    color: {u['fg_dim']};
    min-width: 22px;
    min-height: 22px;
    border-radius: 0;
}}
.searchbar button:hover {{
    background-color: transparent;
    color: {theme.fg};
}}
.search-count {{
    color: {u['muted']};
    font-size: {tiny}pt;
    padding: 0 4px;
}}

.toast {{
    background-color: {u['surface']};
    border: 1px solid {u['border']};
    border-radius: 0;
    padding: 4px 12px;
    color: {theme.fg};
    font-size: {ui_size - 0.5}pt;
}}

menu {{
    background-color: {u['surface']};
    border: 1px solid {u['border']};
    border-radius: 0;
    padding: 3px;
}}
menu menuitem {{
    border-radius: 0;
    padding: 4px 12px;
    color: {u['fg_dim']};
    font-size: {ui_size - 0.4}pt;
}}
menu menuitem:hover {{
    background-color: {u['surface_hi']};
    color: {theme.fg};
}}
menu separator {{
    background-color: {u['border']};
    min-height: 1px;
    margin: 4px 2px;
}}

tooltip {{
    background-color: {u['surface']};
    border: 1px solid {u['border']};
    border-radius: 0;
    color: {theme.fg};
    font-size: {tiny}pt;
}}
tooltip label {{
    color: {theme.fg};
}}
"""
