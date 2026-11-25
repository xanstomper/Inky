"""A Raycast-style command palette for Inky."""

from __future__ import annotations

from collections import OrderedDict

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from .util import fuzzy  # noqa: E402


class Palette(Gtk.EventBox):
    """Floating fuzzy command palette."""

    def __init__(self) -> None:
        super().__init__()
        self.get_style_context().add_class("palette-scrim")
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.set_visible_window(True)
        self.set_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_scrim_click)

        self.items: list[dict] = []
        self._rows: list[Gtk.ListBoxRow] = []

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.card.get_style_context().add_class("palette-card")
        self.card.set_halign(Gtk.Align.CENTER)
        self.card.set_valign(Gtk.Align.START)
        self.card.set_margin_top(84)
        self.card.set_size_request(660, -1)
        self.add(self.card)

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("palette-entry")
        self.entry.set_placeholder_text("Type a command, theme, agent or session…")
        self.entry.connect("changed", lambda *_: self._rebuild())
        self.entry.connect("key-press-event", self._on_entry_key)
        self.card.pack_start(self.entry, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_max_content_height(430)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.scroll.get_style_context().add_class("palette-list")
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.get_style_context().add_class("palette-list")
        self.listbox.connect("row-activated", self._on_row_activated)
        self.scroll.add(self.listbox)
        self.card.pack_start(self.scroll, True, True, 0)

        self.hint = Gtk.Label(label="↑↓ move    ⏎ run    esc dismiss")
        self.hint.get_style_context().add_class("palette-sub")
        self.hint.set_halign(Gtk.Align.START)
        self.hint.set_margin_start(6)
        self.card.pack_start(self.hint, False, False, 0)

        self.set_no_show_all(True)

    # -- public --------------------------------------------------------
    def open(self, items: list[dict], placeholder: str = "Type a command…",
             query: str = "") -> None:
        self.items = items
        self.entry.set_placeholder_text(placeholder)
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.entry.set_text(query)
        self.entry.set_position(-1)
        self.show()
        self.entry.grab_focus()
        self._rebuild()

    def close(self) -> None:
        self.hide()
        self.items = []

    def is_open(self) -> bool:
        return bool(self.get_visible())

    # -- internals -----------------------------------------------------
    def _on_scrim_click(self, _widget, event) -> bool:
        try:
            coords = self.card.translate_coordinates(self, 0, 0)
            card_x, card_y = coords[-2], coords[-1]
            alloc = self.card.get_allocation()
            in_card = (card_x <= event.x <= card_x + alloc.width and
                       card_y <= event.y <= card_y + alloc.height)
        except Exception:
            in_card = False
        if not in_card:
            self.close()
            return True
        return False

    def _on_entry_key(self, _entry, event) -> bool:
        key = Gdk.keyval_name(event.keyval)
        if key == "Escape":
            self.close()
            return True
        if key in ("Down", "KP_Down", "Up", "KP_Up", "Page_Down", "Page_Up"):
            self._move(key)
            return True
        if key in ("Return", "KP_Enter"):
            self._activate_selected()
            return True
        return False

    def _move(self, key: str) -> None:
        rows = [row for row in self._rows if row.get_selectable()]
        if not rows:
            return
        current = self.listbox.get_selected_row()
        try:
            index = rows.index(current)
        except ValueError:
            index = -1
        if key in ("Down", "KP_Down"):
            index = min(len(rows) - 1, index + 1)
        elif key in ("Up", "KP_Up"):
            index = max(0, index - 1)
        elif key == "Page_Down":
            index = min(len(rows) - 1, index + 6)
        else:
            index = max(0, index - 6)
        self.listbox.select_row(rows[index])
        try:
            allocation = rows[index].get_allocation()
            self.scroll.get_vadjustment().clamp_page(
                allocation.y, allocation.y + allocation.height
            )
        except Exception:
            pass

    def _activate_selected(self) -> None:
        row = self.listbox.get_selected_row()
        if row is not None and row.get_selectable():
            self._on_row_activated(self.listbox, row)

    def _on_row_activated(self, _listbox, row) -> None:
        item = getattr(row, "inky_item", None)
        if not item:
            return
        self.close()
        run = item.get("run")
        if callable(run):
            run()

    def _rebuild(self) -> None:
        for child in list(self.listbox.get_children()):
            self.listbox.remove(child)
        self._rows = []
        query = self.entry.get_text().strip()

        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        for item in self.items:
            groups.setdefault(item.get("category") or "", []).append(item)

        first_selectable = None
        for category, items in groups.items():
            if category:
                header = Gtk.ListBoxRow()
                header.set_selectable(False)
                header.set_activatable(False)
                label = Gtk.Label(label=category.upper())
                label.set_halign(Gtk.Align.START)
                label.get_style_context().add_class("palette-section")
                header.add(label)
                self.listbox.add(header)
                self._rows.append(header)

            scored = []
            for index, item in enumerate(items):
                haystack = " ".join(filter(None, [
                    item.get("label", ""), item.get("sub", ""),
                    item.get("category", ""), item.get("keywords", ""),
                ]))
                score = fuzzy(query, haystack)
                if score is not None:
                    scored.append((score, index, item))
            if query:
                scored.sort(key=lambda entry: -entry[0])

            for _, _, item in scored:
                row = self._make_row(item)
                self.listbox.add(row)
                self._rows.append(row)
                if first_selectable is None:
                    first_selectable = row

        if first_selectable is None:
            empty = Gtk.ListBoxRow()
            empty.set_selectable(False)
            empty.set_activatable(False)
            label = Gtk.Label(label="Nothing matches.")
            label.get_style_context().add_class("palette-empty")
            empty.add(label)
            self.listbox.add(empty)
        self.listbox.show_all()
        if first_selectable is not None:
            self.listbox.select_row(first_selectable)

    def _make_row(self, item: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.inky_item = item  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        glyph = Gtk.Label(label=item.get("glyph") or "·")
        glyph.get_style_context().add_class("palette-glyph")
        glyph.set_size_request(20, -1)
        box.pack_start(glyph, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label=item.get("label", ""))
        title.set_halign(Gtk.Align.START)
        title.get_style_context().add_class("palette-title")
        text.pack_start(title, False, False, 0)
        if item.get("sub"):
            sub = Gtk.Label(label=item["sub"])
            sub.set_halign(Gtk.Align.START)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.get_style_context().add_class("palette-sub")
            text.pack_start(sub, False, False, 0)
        box.pack_start(text, True, True, 0)

        if item.get("hint"):
            hint = Gtk.Label(label=item["hint"])
            hint.get_style_context().add_class("palette-sub")
            box.pack_end(hint, False, False, 0)

        row.add(box)
        return row
