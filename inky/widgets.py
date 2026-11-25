"""Small reusable overlays: toast, search bar, theme settings dialog."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402  

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402  


class Toast(Gtk.Revealer):
    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(160)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(38)
        self.set_no_show_all(True)
        self.label = Gtk.Label(label="")
        self.label.get_style_context().add_class("toast")
        self.add(self.label)
        self._timer = 0

    def show_message(self, message: str, duration_ms: int = 2600) -> None:
        self.label.set_text(message)
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.set_reveal_child(True)
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(duration_ms, self._hide)

    def _hide(self) -> bool:
        self._timer = 0
        self.set_reveal_child(False)
        return False


class ThemePicker(Gtk.Dialog):
    """Theme chooser: search, click once, the window recolours immediately."""

    def __init__(self, parent, themes: dict, current: str, on_pick) -> None:
        super().__init__(title="Themes", transient_for=parent, modal=False)
        self.themes = themes
        self.on_pick = on_pick
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._marks: dict[str, Gtk.Label] = {}
        self._ready = False
        self.set_default_size(600, 640)
        self.set_resizable(True)
        self.get_content_area().set_spacing(8)
        self.get_content_area().set_border_width(12)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text(
            f"Search {len(themes)} themes — click one to apply it live"
        )
        self.entry.connect("changed", self._on_search)
        self.entry.connect("activate", self._activate_first)
        head.pack_start(self.entry, True, True, 0)
        self.count = Gtk.Label(label=f"{len(themes)}")
        self.count.get_style_context().add_class("theme-slug")
        head.pack_end(self.count, False, False, 0)
        self.get_content_area().pack_start(head, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.set_filter_func(self._filter)
        self.listbox.get_style_context().add_class("theme-list")
        self.listbox.connect("row-selected", self._on_selected)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("theme-list")
        scroll.add(self.listbox)
        self.get_content_area().pack_start(scroll, True, True, 0)

        for slug, theme in themes.items():
            row = self._row(slug, theme, slug == current)
            self.listbox.add(row)
            self._rows[slug] = row

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = Gtk.Label(
            label="Themes apply live · your choice is remembered", xalign=0.0
        )
        hint.get_style_context().add_class("theme-slug")
        foot.pack_start(hint, True, True, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        foot.pack_end(close, False, False, 0)
        self.get_content_area().pack_start(foot, False, False, 0)

        self.show_all()
        active = self._rows.get(current)
        if active is not None:
            # Show the current theme, not the top of a fifty-row list.
            GLib.idle_add(self._focus_row, active)
        GLib.idle_add(self.entry.grab_focus)

    def _focus_row(self, row) -> bool:
        self._ready = True
        self.listbox.select_row(row)
        row.grab_focus()
        self._scroll_to(row)
        self._sync_marks(getattr(row, "inky_slug", ""))
        return False

    def _scroll_to(self, row) -> None:
        parent = self.listbox.get_parent()
        if not isinstance(parent, Gtk.ScrolledWindow):
            return
        alloc = row.get_allocation()
        adjustment = parent.get_vadjustment()
        if adjustment is None:
            return
        top = alloc.y
        bottom = top + alloc.height
        view_top = adjustment.get_value()
        view_bottom = view_top + adjustment.get_page_size()
        if top < view_top or bottom > view_bottom:
            adjustment.set_value(max(0.0, top - adjustment.get_page_size() / 2))

    def _row(self, slug: str, theme, active: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.inky_slug = slug  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(8)

        mark = Gtk.Label(label="●" if active else "")
        mark.get_style_context().add_class("theme-mark")
        mark.set_size_request(14, -1)
        box.pack_start(mark, False, False, 0)
        self._marks[slug] = mark

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name = Gtk.Label(label=theme.name, xalign=0.0)
        name.get_style_context().add_class("theme-name")
        sub = Gtk.Label(label=slug, xalign=0.0)
        sub.get_style_context().add_class("theme-slug")
        text.pack_start(name, False, False, 0)
        text.pack_start(sub, False, False, 0)
        box.pack_start(text, True, True, 0)

        swatch = Gtk.DrawingArea()
        swatch.set_size_request(156, 15)
        swatch.connect("draw", self._draw_swatch, theme)
        box.pack_end(swatch, False, False, 0)

        row.add(box)
        return row

    @staticmethod
    def _draw_swatch(area, cr, theme) -> bool:
        colors = (theme.palette_css or [theme.bg])[:8]
        height = area.get_allocated_height()
        width = area.get_allocated_width() / max(1, len(colors))
        for index, color in enumerate(colors):
            rgba = Gdk.RGBA()
            if not rgba.parse(color):
                continue
            cr.set_source_rgb(rgba.red, rgba.green, rgba.blue)
            cr.rectangle(index * width, 0, width + 0.5, height)
            cr.fill()
        return False

    def _on_search(self, entry) -> None:
        self.listbox.invalidate_filter()
        visible = sum(
            1 for slug, row in self._rows.items()
            if self._filter(row)
        )
        self.count.set_text(f"{visible}")
        if not entry.get_text():
            self.count.set_text(str(len(self.themes)))

    def _filter(self, row) -> bool:
        query = self.entry.get_text().strip().lower()
        if not query:
            return True
        slug = getattr(row, "inky_slug", "")
        theme = self.themes.get(slug)
        haystack = f"{slug} {theme.name if theme else ''}".lower()
        return all(part in haystack for part in query.split())

    def _visible_rows(self) -> list[Gtk.ListBoxRow]:
        return [row for slug, row in self._rows.items() if self._filter(row)]

    def _activate_first(self, *_args) -> None:
        rows = self._visible_rows()
        if rows:
            self.listbox.select_row(rows[0])

    def _on_selected(self, _listbox, row) -> None:
        if not self._ready or row is None:
            return
        slug = getattr(row, "inky_slug", None)
        if not slug:
            return
        if callable(self.on_pick):
            self.on_pick(slug)
        self._sync_marks(slug)

    def _sync_marks(self, active: str) -> None:
        for slug, mark in self._marks.items():
            mark.set_text("●" if slug == active else "")


class SearchBar(Gtk.Revealer):
    def __init__(self, on_search, on_close) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(120)
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.START)
        self.set_margin_top(8)
        self.set_margin_end(14)
        self.set_no_show_all(True)
        self._on_search = on_search
        self._on_close = on_close
        self._case = False

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("searchbar")

        icon = Gtk.Label(label="⌕")
        icon.get_style_context().add_class("search-count")
        bar.pack_start(icon, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("search scrollback…")
        self.entry.set_width_chars(26)
        self.entry.connect("changed", lambda *_: self.run(True))
        self.entry.connect("activate", lambda *_: self.run(True))
        self.entry.connect("key-press-event", self._on_key)
        bar.pack_start(self.entry, True, True, 0)

        self.count = Gtk.Label(label="")
        self.count.get_style_context().add_class("search-count")
        bar.pack_start(self.count, False, False, 0)

        self.case_btn = Gtk.ToggleButton(label="Aa")
        self.case_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.case_btn.set_tooltip_text("Case sensitive")
        self.case_btn.connect("toggled", self._toggle_case)
        bar.pack_start(self.case_btn, False, False, 0)

        for glyph, direction, tip in (("↑", False, "Previous"), ("↓", True, "Next")):
            button = Gtk.Button(label=glyph)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda _b, d=direction: self.run(d))
            bar.pack_start(button, False, False, 0)

        close = Gtk.Button(label="✕")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.close())
        bar.pack_start(close, False, False, 0)

        self.add(bar)

    def open(self) -> None:
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.set_reveal_child(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def close(self) -> None:
        self.set_reveal_child(False)
        if callable(self._on_close):
            self._on_close()

    def is_open(self) -> bool:
        return bool(self.get_reveal_child())

    def _toggle_case(self, button) -> None:
        self._case = button.get_active()
        if self.entry.get_text():
            self.run(True)

    def _on_key(self, _widget, event) -> bool:
        from gi.repository import Gdk

        key = Gdk.keyval_name(event.keyval)
        if key == "Escape":
            self.close()
            return True
        if key in ("Return", "KP_Enter"):
            shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            self.run(not shift)
            return True
        return False

    def run(self, forward: bool) -> None:
        text = self.entry.get_text()
        if not text:
            self.count.set_text("")
            if callable(self._on_search):
                self._on_search("", True, self._case)
            return
        found = self._on_search(text, forward, self._case) if callable(self._on_search) else False
        self.count.set_text("✓" if found else "✗")
