"""Theme maker and settings dialogs.

Both dialogs are non-modal on purpose: a colour or setting is applied to the
live window while the dialog is still open, so the preview is the real thing.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:  # pragma: no cover - only missing on very small GTK installs
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import PangoCairo  # noqa: E402
except Exception:  # pragma: no cover
    PangoCairo = None  # type: ignore[assignment]

from . import config  # noqa: E402
from .theme import Theme, _from_dict, delete_theme, is_user_theme, save_theme  # noqa: E402
from .util import hexc, is_dark, mix, parse_color, slugify  # noqa: E402


def _rgb(color: str) -> tuple[float, float, float]:
    r, g, b = parse_color(color)
    return r / 255.0, g / 255.0, b / 255.0


def _hex(button) -> str:
    c = button.get_rgba()
    return hexc((c.red * 255, c.green * 255, c.blue * 255))


def _slug_for(name: str) -> str:
    slug = slugify((name or "").lower()).strip("-._")
    return slug or "custom"


class ThemeMaker(Gtk.Dialog):
    """Colour editor with a live preview, import/export and user themes."""

    FIELDS = (
        ("bg", "Background"),
        ("fg", "Foreground"),
        ("cursor", "Cursor"),
        ("selection", "Selection"),
        ("accent", "Accent"),
    )

    ANSI = (
        "Black", "Red", "Green", "Yellow", "Blue", "Magenta", "Cyan", "White",
        "Bright black", "Bright red", "Bright green", "Bright yellow",
        "Bright blue", "Bright magenta", "Bright cyan", "Bright white",
    )

    def __init__(self, parent, themes: dict, current: str, on_save=None,
                 on_delete=None) -> None:
        super().__init__(title="Theme maker", transient_for=parent, modal=False)
        self.themes = themes
        self.on_save = on_save
        self.on_delete = on_delete
        self._buttons: dict[str, Gtk.ColorButton] = {}
        self._loading = True
        self._base_slug = current if current in themes else None
        self._slug = self._base_slug if is_user_theme(self._base_slug or "") else None

        self.set_default_size(760, 800)
        self.set_resizable(True)
        area = self.get_content_area()
        area.set_spacing(9)
        area.set_border_width(14)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("Theme name")
        self.name_entry.set_width_chars(22)
        self.name_entry.connect("changed", lambda *_: self._refresh_save())
        head.pack_start(self.name_entry, True, True, 0)
        base_label = Gtk.Label(label="Start from")
        base_label.get_style_context().add_class("maker-label")
        head.pack_start(base_label, False, False, 0)

        self.base_combo = Gtk.ComboBoxText()
        for slug, theme in themes.items():
            self.base_combo.append(slug, theme.name)
        self.base_combo.set_active_id(self._base_slug or next(iter(themes), ""))
        self.base_combo.set_size_request(170, -1)
        self.base_combo.connect("changed", self._on_base_changed)
        head.pack_start(self.base_combo, False, False, 0)
        area.pack_start(head, False, False, 0)

        area.pack_start(self._section("Accent colours"), False, False, 0)
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)
        grid.set_column_homogeneous(True)
        for index, (key, label) in enumerate(self.FIELDS):
            grid.attach(self._color_cell(key, label), index, 0, 1, 1)
        area.pack_start(grid, False, False, 0)

        area.pack_start(self._section("ANSI palette  ·  what agents paint their output with"),
                        False, False, 0)
        ansigrid = Gtk.Grid()
        ansigrid.set_column_spacing(4)
        ansigrid.set_row_spacing(4)
        ansigrid.set_column_homogeneous(True)
        for index, label in enumerate(self.ANSI):
            cell = self._color_cell(f"ansi{index}", f"{index} {label}")
            ansigrid.attach(cell, index % 8, index // 8, 1, 1)
        area.pack_start(ansigrid, False, False, 0)

        area.pack_start(self._section("Live preview"), False, False, 0)
        self.preview = Gtk.DrawingArea()
        self.preview.set_size_request(700, 180)
        self.preview.get_style_context().add_class("maker-preview")
        self.preview.connect("draw", self._draw_preview)
        area.pack_start(self.preview, True, True, 0)

        self.status = Gtk.Label(label="", xalign=0.0)
        self.status.get_style_context().add_class("maker-status")
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        area.pack_start(self.status, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.import_button = Gtk.Button(label="Import…")
        self.import_button.connect("clicked", lambda *_: self._import())
        footer.pack_start(self.import_button, False, False, 0)
        self.export_button = Gtk.Button(label="Export…")
        self.export_button.connect("clicked", lambda *_: self._export())
        footer.pack_start(self.export_button, False, False, 0)
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.get_style_context().add_class("maker-danger")
        self.delete_button.connect("clicked", lambda *_: self._delete())
        footer.pack_start(self.delete_button, False, False, 0)

        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        footer.pack_end(close, False, False, 0)
        self.save_button = Gtk.Button(label="Save & apply")
        self.save_button.get_style_context().add_class("maker-save")
        self.save_button.connect("clicked", lambda *_: self._save())
        footer.pack_end(self.save_button, False, False, 0)
        area.pack_start(footer, False, False, 0)

        self.show_all()
        base = self._base_slug or next(iter(themes), "")
        theme = themes.get(base)
        if theme is not None:
            self.name_entry.set_text(self._editable_name(theme))
            self._load(theme)
        self._loading = False
        self._refresh_save()
        self._refresh_delete()

    # -- construction helpers ------------------------------------------
    def _section(self, title: str) -> Gtk.Label:
        label = Gtk.Label(label=title, xalign=0.0)
        label.get_style_context().add_class("maker-section")
        return label

    def _color_cell(self, key: str, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        name = Gtk.Label(label=label, xalign=0.0)
        name.get_style_context().add_class("maker-label")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(name, False, False, 0)
        button = Gtk.ColorButton()
        button.set_use_alpha(False)
        button.set_size_request(-1, 26)
        button.set_title(label)
        button.get_style_context().add_class("maker-swatch")
        button.connect("color-set", self._on_color_set)
        box.pack_start(button, False, False, 0)
        self._buttons[key] = button
        return box

    def _editable_name(self, theme: Theme) -> str:
        if is_user_theme(theme.slug):
            return theme.name
        return f"{theme.name} custom"

    # -- data ----------------------------------------------------------
    def _load(self, theme: Theme) -> None:
        values = {
            "bg": theme.bg,
            "fg": theme.fg,
            "cursor": theme.cursor,
            "selection": theme.selection,
            "accent": theme.accent,
        }
        palette = theme.palette_for(1.0)
        for index in range(16):
            values[f"ansi{index}"] = palette[index]
        was_loading = self._loading
        self._loading = True
        for key, value in values.items():
            button = self._buttons.get(key)
            if button is None or not isinstance(value, str):
                continue
            rgba = Gdk.RGBA()
            if rgba.parse(value):
                button.set_rgba(rgba)
        self._loading = was_loading
        self.preview.queue_draw()

    def _collect(self) -> dict:
        colors = {key: _hex(button) for key, button in self._buttons.items()}
        palette = [colors.pop(f"ansi{index}") for index in range(16)]
        return {
            "bg": colors["bg"],
            "fg": colors["fg"],
            "cursor": colors["cursor"],
            "selection": colors["selection"],
            "accent": colors["accent"],
            "palette": palette,
        }

    def theme_data(self) -> dict:
        data = self._collect()
        name = self.name_entry.get_text().strip() or "Custom"
        return {
            "name": name,
            "bg": data["bg"],
            "fg": data["fg"],
            "cursor": data["cursor"],
            "selection": data["selection"],
            "accent": data["accent"],
            "palette": data["palette"],
            "dark": is_dark(data["bg"]),
        }

    # -- events --------------------------------------------------------
    def _on_base_changed(self, combo) -> None:
        if self._loading:
            return
        theme = self.themes.get(combo.get_active_id() or "")
        if theme is None:
            return
        self.name_entry.set_text(self._editable_name(theme))
        self._load(theme)
        # Saving now edits the loaded theme when it is yours, and forks it when
        # it is built in.
        self._slug = theme.slug if is_user_theme(theme.slug) else None
        self._refresh_delete()
        self._refresh_save()

    def _on_color_set(self, _button) -> None:
        if not self._loading:
            self.preview.queue_draw()

    def _refresh_save(self) -> None:
        name = self.name_entry.get_text().strip()
        self.save_button.set_sensitive(bool(name))

    def _refresh_delete(self) -> None:
        self.delete_button.set_sensitive(bool(self._slug and is_user_theme(self._slug)))

    def _say(self, message: str) -> None:
        self.status.set_text(message)
        self.status.set_tooltip_text(message)

    # -- save / delete / import / export -------------------------------
    def _save(self) -> str | None:
        name = self.name_entry.get_text().strip()
        if not name:
            self._say("Give the theme a name first")
            return None
        slug = self._slug or _slug_for(name)
        if slug in self.themes and not is_user_theme(slug):
            # Never silently shadow a built-in theme.
            slug = f"{slug}-custom"
        try:
            path = save_theme(slug, self.theme_data())
        except OSError as exc:
            self._say(f"Could not write theme: {exc}")
            return None
        self._slug = slug
        self._refresh_delete()
        self._say(f"Saved {Path(path).name}  ·  {path}")
        if callable(self.on_save):
            self.on_save(slug)
        return slug

    def _delete(self) -> None:
        if not self._slug:
            return
        name = self.name_entry.get_text().strip() or self._slug
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
            text=f"Delete “{name}”?",
        )
        dialog.format_secondary_text(
            "The theme file is removed from your config folder. "
            "Built-in themes are never touched."
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        slug = self._slug
        try:
            delete_theme(slug)
        except OSError as exc:
            self._say(f"Could not delete theme: {exc}")
            return
        self._slug = None
        self._refresh_delete()
        self._say(f"Deleted {slug}.json")
        if callable(self.on_delete):
            self.on_delete(slug)

    def _import(self) -> None:
        chooser = Gtk.FileChooserDialog(
            "Import theme", self, Gtk.FileChooserAction.OPEN,
            ("Cancel", Gtk.ResponseType.CANCEL, "Import", Gtk.ResponseType.OK),
        )
        _add_json_filter(chooser)
        if chooser.run() == Gtk.ResponseType.OK:
            path = chooser.get_filename()
            chooser.destroy()
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                theme = _from_dict(Path(path).stem, data)
            except Exception as exc:
                self._say(f"Not an Inky theme: {exc}")
                return
            self._loading = True
            self.name_entry.set_text(theme.name)
            self._load(theme)
            self._loading = False
            self._slug = None
            self._refresh_delete()
            self._refresh_save()
            self._say(f"Imported {Path(path).name} — Save & apply to keep it")
            return
        chooser.destroy()

    def _export(self) -> None:
        chooser = Gtk.FileChooserDialog(
            "Export theme", self, Gtk.FileChooserAction.SAVE,
            ("Cancel", Gtk.ResponseType.CANCEL, "Export", Gtk.ResponseType.OK),
        )
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name(f"{self._slug or _slug_for(self.name_entry.get_text())}.json")
        _add_json_filter(chooser)
        if chooser.run() == Gtk.ResponseType.OK:
            path = chooser.get_filename()
            chooser.destroy()
            try:
                Path(path).write_text(
                    json.dumps(self.theme_data(), indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                self._say(f"Could not export: {exc}")
                return
            self._say(f"Exported to {path}")
            return
        chooser.destroy()

    # -- preview -------------------------------------------------------
    def _font(self) -> Pango.FontDescription:
        return Pango.FontDescription("JetBrains Mono 9.5")

    def _draw_spans(self, cr, spans, x: float, y: float) -> float:
        for text, fg, bg in spans:
            if not text:
                continue
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(self._font())
            layout.set_text(text, -1)
            width, height = layout.get_pixel_size()
            if bg:
                cr.set_source_rgba(*_rgb(bg))
                cr.rectangle(x - 1, y - 1, width + 2, height + 2)
                cr.fill()
            cr.set_source_rgba(*_rgb(fg))
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, layout)
            x += width
        return x

    def _draw_preview(self, area, cr) -> bool:
        colors = self._collect()
        palette = colors["palette"]
        bg, fg = colors["bg"], colors["fg"]
        accent = colors["accent"]
        muted = mix(fg, bg, 0.5)
        width = area.get_allocated_width()
        height = area.get_allocated_height()

        cr.set_source_rgba(*_rgb(bg))
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Titlebar strip with the three window dots, so the preview also shows
        # how the chrome will read against the background.
        bar = mix(bg, fg, 0.085)
        cr.set_source_rgba(*_rgb(bar))
        cr.rectangle(0, 0, width, 26)
        cr.fill()
        for index, color in enumerate((palette[1], palette[3], palette[2])):
            cr.set_source_rgba(*_rgb(color))
            cr.arc(15 + index * 14, 13, 4, 0, 6.2832)
            cr.fill()
        cr.set_source_rgba(*_rgb(muted))
        if PangoCairo is not None:
            layout = PangoCairo.create_layout(cr)
            desc = self._font()
            desc.set_size(int(8.5 * Pango.SCALE))
            layout.set_font_description(desc)
            layout.set_text("inky — live theme preview", -1)
            cr.move_to(70, 6)
            PangoCairo.show_layout(cr, layout)

        cr.set_source_rgba(*_rgb(mix(bg, fg, 0.14)))
        cr.rectangle(0.5, 0.5, width - 1, height - 1)
        cr.set_line_width(1)
        cr.stroke()

        rows = (
            [("~/inky ", palette[4], None), ("❯ ", accent, None),
             ("inky themes", fg, None)],
            [("✓ ", palette[2], None), ("50 themes", fg, None),
             ("  loaded in ", muted, None), ("3 ms", palette[6], None)],
            [("✗ ", palette[1], None), ("clippy.toml", fg, None),
             (" missing", muted, None)],
            [("⚠ ", palette[3], None), ("line_height", fg, None),
             (" below 1.0", muted, None)],
            [("  ", fg, None), ("selection text", fg,
                                mix(bg, colors["selection"], 0.75))],
            [("● ", palette[5], None), ("agent output stays readable", fg, None),
             ("  +42 ", palette[2], None), (" −7", palette[1], None)],
        )
        y = 40.0
        last_x = 0.0
        for spans in rows:
            last_x = self._draw_spans(cr, spans, 16, y)
            y += 19
        # Block cursor sitting after the last line.
        cr.set_source_rgba(*_rgb(colors["cursor"]))
        cr.rectangle(last_x + 3, y - 19, 6, 15)
        cr.fill()
        return False


def _add_json_filter(chooser) -> None:
    filter = Gtk.FileFilter()
    filter.set_name("Inky themes (*.json)")
    filter.add_pattern("*.json")
    chooser.add_filter(filter)


_FAMILY_CACHE: dict[bool, list[str]] = {}


def _fc_families(mono_only: bool) -> list[str]:
    query = ":spacing=mono family" if mono_only else ": family"
    try:
        proc = subprocess.run(
            ["fc-list", query], capture_output=True, text=True, timeout=2.5
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        first = line.split(":")[0]
        for part in first.split(","):
            name = part.strip()
            if name:
                names.add(name)
    return sorted(names, key=str.lower)


def font_families(mono_only: bool) -> list[str]:
    """Installed font families, base names only, cached per scope."""
    if mono_only in _FAMILY_CACHE:
        return _FAMILY_CACHE[mono_only]
    names: list[str] = []
    if PangoCairo is not None:
        try:
            for family in PangoCairo.FontMap.get_default().list_families():
                is_mono = getattr(family, "is_monospace", lambda: False)()
                if mono_only and not is_mono:
                    continue
                names.append(family.get_name())
        except Exception:
            names = []
    if not names:
        names = _fc_families(mono_only)
    names = sorted(set(names), key=str.lower)
    _FAMILY_CACHE[mono_only] = names
    return names


def _is_nerd_font(family: str) -> bool:
    lowered = family.lower()
    return "nerd font" in lowered or " nf" in lowered or lowered.endswith("nf")


class FontGallery(Gtk.Box):
    """A searchable wall of installed fonts, each row drawn in that font.

    The GTK font chooser is a modal list of names; this is the opposite — you
    see the specimen, you click it, the terminal recolours and re-types itself.
    """

    SAMPLE = "The quick brown fox 0123456789"
    GLYPHS = "❯ ✓ ⚠ ⎇ 0O1l"

    def __init__(self, on_pick) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_pick = on_pick
        self._mono = True
        self.current = ""
        self._marks: dict[str, Gtk.Label] = {}
        self._ready = False

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Search installed fonts…")
        self.entry.connect("changed", self._on_search)
        head.pack_start(self.entry, True, True, 0)
        self.count = Gtk.Label(label="")
        self.count.get_style_context().add_class("maker-status")
        head.pack_end(self.count, False, False, 0)
        self.pack_start(head, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.set_filter_func(self._filter)
        self.listbox.get_style_context().add_class("theme-list")
        self.listbox.connect("row-activated", self._on_row)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(196)
        scroll.set_max_content_height(250)
        scroll.set_propagate_natural_height(True)
        scroll.get_style_context().add_class("theme-list")
        scroll.add(self.listbox)
        self.pack_start(scroll, True, True, 0)

    # -- public --------------------------------------------------------
    def set_scope(self, mono_only: bool, current: str) -> None:
        self._mono = bool(mono_only)
        self.current = current or ""
        self._rebuild()

    # -- rows ----------------------------------------------------------
    def _rebuild(self) -> None:
        self._ready = False
        for child in list(self.listbox.get_children()):
            self.listbox.remove(child)
        self._marks.clear()
        families = font_families(self._mono)
        for family in families:
            self.listbox.add(self._row(family, family == self.current))
        self.listbox.invalidate_filter()
        self.listbox.show_all()
        self._update_count()
        self._ready = True
        if self.current:
            GLib.idle_add(self._scroll_current)

    def _row(self, family: str, active: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.inky_family = family  # type: ignore[attr-defined]
        row.set_tooltip_text(family)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        box.set_margin_start(4)
        box.set_margin_end(8)

        mark = Gtk.Label(label="●" if active else "")
        mark.get_style_context().add_class("theme-mark")
        mark.set_size_request(14, -1)
        box.pack_start(mark, False, False, 0)
        self._marks[family] = mark

        name = Gtk.Label(label=family, xalign=0.0)
        name.get_style_context().add_class("theme-name")
        name.override_font(self._desc(family, 10.5))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(name, False, False, 0)

        sample = Gtk.Label(label=f"{self.SAMPLE}   {self.GLYPHS}", xalign=0.0)
        sample.get_style_context().add_class("font-sample")
        sample.override_font(self._desc(family, 8.5))
        sample.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_end(sample, True, True, 0)

        if _is_nerd_font(family):
            badge = Gtk.Label(label="nerd")
            badge.get_style_context().add_class("font-badge")
            box.pack_end(badge, False, False, 6)

        row.add(box)
        return row

    @staticmethod
    def _desc(family: str, size: float) -> Pango.FontDescription:
        desc = Pango.FontDescription()
        desc.set_family(family)
        desc.set_size(int(size * Pango.SCALE))
        return desc

    # -- events --------------------------------------------------------
    def _on_row(self, _listbox, row) -> None:
        if not self._ready or row is None:
            return
        family = getattr(row, "inky_family", None)
        if not family:
            return
        self.current = family
        for key, mark in self._marks.items():
            mark.set_text("●" if key == family else "")
        if callable(self.on_pick):
            self.on_pick(family)

    def _on_search(self, _entry) -> None:
        self.listbox.invalidate_filter()
        self._update_count()

    def _filter(self, row) -> bool:
        query = self.entry.get_text().strip().lower()
        if not query:
            return True
        family = getattr(row, "inky_family", "").lower()
        return all(part in family for part in query.split())

    def _visible(self) -> int:
        return sum(1 for child in self.listbox.get_children() if self._filter(child))

    def _update_count(self) -> None:
        total = len(self.listbox.get_children())
        visible = self._visible()
        scope = "monospace" if self._mono else "interface"
        self.count.set_text(f"{visible}/{total} {scope}")

    def _scroll_current(self) -> bool:
        for child in self.listbox.get_children():
            if getattr(child, "inky_family", "") == self.current:
                self.listbox.select_row(child)
                self._scroll_to(child)
                break
        return False

    def _scroll_to(self, row) -> None:
        parent = self.listbox.get_parent()
        if not isinstance(parent, Gtk.ScrolledWindow):
            return
        adjustment = parent.get_vadjustment()
        if adjustment is None:
            return
        allocation = row.get_allocation()
        top = allocation.y
        bottom = top + allocation.height
        view_top = adjustment.get_value()
        view_bottom = view_top + adjustment.get_page_size()
        if top < view_top or bottom > view_bottom:
            adjustment.set_value(max(0.0, top - adjustment.get_page_size() / 2))


class SettingsDialog(Gtk.Dialog):
    """Live settings: appearance, terminal behaviour and agent extras."""

    def __init__(self, parent, cfg: dict, theme: Theme, themes: dict,
                 on_apply=None, on_pick_theme=None, on_make_theme=None,
                 on_action=None) -> None:
        super().__init__(title="Settings", transient_for=parent, modal=False)
        self.cfg = cfg
        self.theme = theme
        self.themes = themes
        self.on_apply = on_apply
        self.on_pick_theme = on_pick_theme
        self.on_make_theme = on_make_theme
        self.on_action = on_action
        self._loading = True
        self._timer = 0

        self.set_default_size(640, 620)
        self.set_resizable(True)
        area = self.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)

        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        self.notebook.append_page(self._appearance_page(), Gtk.Label(label="Appearance"))
        self.notebook.append_page(self._terminal_page(), Gtk.Label(label="Terminal"))
        self.notebook.append_page(self._agents_page(), Gtk.Label(label="Agents & extras"))
        area.pack_start(self.notebook, True, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        reset = Gtk.Button(label="Reset to defaults")
        reset.get_style_context().add_class("maker-danger")
        reset.connect("clicked", lambda *_: self._reset())
        footer.pack_start(reset, False, False, 0)
        hint = Gtk.Label(label="Changes apply live", xalign=0.0)
        hint.get_style_context().add_class("maker-status")
        footer.pack_start(hint, True, True, 8)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        footer.pack_end(close, False, False, 0)
        area.pack_start(footer, False, False, 0)

        self.show_all()
        self._loading = False

    # -- helpers -------------------------------------------------------
    def _page(self, name: str) -> tuple[Gtk.Box, Gtk.Box]:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        body.set_border_width(14)
        scroll.add(body)
        scroll.get_style_context().add_class(f"settings-{name}")
        return scroll, body

    def _section(self, body, title: str, note: str = "") -> None:
        label = Gtk.Label(label=title.upper(), xalign=0.0)
        label.get_style_context().add_class("settings-section")
        body.pack_start(label, False, False, 8 if note else 2)
        if note:
            sub = Gtk.Label(label=note, xalign=0.0)
            sub.get_style_context().add_class("settings-help")
            sub.set_line_wrap(True)
            body.pack_start(sub, False, False, 0)

    def _row(self, body, title: str, note: str, widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        label = Gtk.Label(label=title, xalign=0.0)
        label.get_style_context().add_class("settings-label")
        text.pack_start(label, False, False, 0)
        if note:
            sub = Gtk.Label(label=note, xalign=0.0)
            sub.get_style_context().add_class("settings-help")
            sub.set_line_wrap(True)
            sub.set_max_width_chars(42)
            text.pack_start(sub, False, False, 0)
        row.pack_start(text, True, True, 0)
        widget.set_valign(Gtk.Align.CENTER)
        row.pack_end(widget, False, False, 0)
        body.pack_start(row, False, False, 0)
        return row

    def _switch(self, value: bool, callback) -> Gtk.Switch:
        switch = Gtk.Switch()
        switch.set_active(bool(value))
        switch.connect("notify::active", lambda w, _p: callback(bool(w.get_active())))
        return switch

    def _scale(self, low: float, high: float, step: float, value: float,
               digits: int, callback) -> Gtk.Scale:
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, step)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_size_request(180, -1)
        scale.set_value(float(value))
        scale.connect("value-changed", lambda s: callback(s.get_value()))
        return scale

    def _spin(self, low: float, high: float, step: float, value: float,
              callback) -> Gtk.SpinButton:
        spin = Gtk.SpinButton.new_with_range(low, high, step)
        spin.set_digits(0)
        spin.set_value(float(value))
        spin.set_size_request(96, -1)
        spin.connect("value-changed", lambda s: callback(int(s.get_value())))
        return spin

    def _changed(self) -> None:
        """Debounce live application; GTK fires scales once per pixel."""
        if self._loading:
            return
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(150, self._flush)

    def _flush(self) -> bool:
        self._timer = 0
        if callable(self.on_apply):
            self.on_apply()
        return False

    # -- pages ---------------------------------------------------------
    def _appearance_page(self) -> Gtk.ScrolledWindow:
        scroll, body = self._page("appearance")
        self._section(body, "Theme", "Every theme is a 16-colour ANSI palette plus accents.")

        theme_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.theme_label = Gtk.Label(label=self.theme.name, xalign=0.0)
        self.theme_label.get_style_context().add_class("settings-theme")
        theme_row.pack_start(self.theme_label, True, True, 0)
        pick = Gtk.Button(label="Choose…")
        pick.connect("clicked", lambda *_: self._pick_theme())
        theme_row.pack_start(pick, False, False, 0)
        make = Gtk.Button(label="Make one…")
        make.connect("clicked", lambda *_: self._make_theme())
        theme_row.pack_start(make, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.pack_start(theme_row, True, True, 0)
        body.pack_start(row, False, False, 0)

        self._scope_mono = True
        fonts_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="FONTS", xalign=0.0)
        title.get_style_context().add_class("settings-section")
        fonts_head.pack_start(title, False, False, 0)
        self.scope_terminal = Gtk.RadioButton.new_with_label_from_widget(None, "Terminal")
        self.scope_terminal.set_active(True)
        self.scope_terminal.get_style_context().add_class("settings-scope")
        self.scope_terminal.connect("toggled", self._on_scope_toggle)
        self.scope_interface = Gtk.RadioButton.new_with_label_from_widget(
            self.scope_terminal, "Interface"
        )
        self.scope_interface.get_style_context().add_class("settings-scope")
        self.scope_interface.connect("toggled", self._on_scope_toggle)
        fonts_head.pack_end(self.scope_interface, False, False, 0)
        fonts_head.pack_end(self.scope_terminal, False, False, 6)
        body.pack_start(fonts_head, False, False, 8)

        note = Gtk.Label(
            label="Every installed font is listed and drawn in itself — click one to "
                  "apply it live. Terminal fonts are monospace-only, because agent "
                  "tables and diffs depend on it. Agent output uses the terminal font.",
            xalign=0.0,
        )
        note.get_style_context().add_class("settings-help")
        note.set_line_wrap(True)
        body.pack_start(note, False, False, 0)

        self.gallery = FontGallery(self._on_font_pick)
        body.pack_start(self.gallery, False, False, 0)
        self.gallery.set_scope(True, self.cfg["font"].get("family", ""))

        font_cfg = self.cfg["font"]
        self.size_scale = self._scale(
            7.0, 22.0, 0.5, font_cfg.get("size", 11.5), 1, self._on_size
        )
        self._row(
            body, "Font size", "Ctrl+Shift+= and Ctrl+Shift+- still zoom on the fly.",
            self.size_scale,
        )
        weight = Gtk.ComboBoxText()
        for key, label in (("300", "Light"), ("400", "Regular"), ("500", "Medium"),
                           ("600", "SemiBold"), ("700", "Bold")):
            weight.append(key, label)
        weight.set_active_id(str(int(font_cfg.get("weight", 400) or 400)))
        weight.connect("changed", lambda c: self._set(
            ("font", "weight"), int(c.get_active_id() or 400)))
        weight.set_size_request(120, -1)
        self.weight_row = self._row(
            body, "Font weight", "Heavier stems read better on high-DPI panels.", weight
        )

        self._row(
            body, "Line height", "Slightly airier rows; 1.06 is the tuned default.",
            self._scale(0.9, 1.5, 0.01, font_cfg.get("line_height", 1.06), 2,
                        lambda v: self._set(("font", "line_height"), round(v, 2))),
        )

        self._section(body, "Cursor & contrast")
        shape = Gtk.ComboBoxText()
        for key in ("bar", "block", "underline"):
            shape.append(key, key.title())
        shape.set_active_id(str(self.cfg["behavior"].get("cursor_shape", "bar")))
        shape.connect("changed", lambda c: self._set(
            ("behavior", "cursor_shape"), c.get_active_id() or "bar"))
        self._row(body, "Cursor shape", "", shape)

        self._row(
            body, "Blinking cursor", "Agents can sit quiet for a while; a still cursor is calmer.",
            self._switch(self.cfg["behavior"].get("cursor_blink", True),
                         lambda v: self._set(("behavior", "cursor_blink"), v)),
        )
        self._row(
            body, "Legibility floor",
            "Lifts dim ANSI slots until they reach this WCAG contrast against the "
            "background. 3.0 keeps moods, 4.5 is AA for body text.",
            self._scale(1.0, 7.0, 0.1, self.cfg["behavior"].get("min_contrast", 3.0), 1,
                        lambda v: self._set(("behavior", "min_contrast"), round(v, 1))),
        )

        self._section(body, "Window")
        self._row(
            body, "Padding", "Breathing room between the window edge and the grid.",
            self._spin(0, 40, 1, self.cfg["window"].get("padding", 0),
                       lambda v: self._set(("window", "padding"), v)),
        )
        self._row(
            body, "Opacity",
            "Needs a compositor. 1.0 is fully opaque.",
            self._scale(0.4, 1.0, 0.01, self.cfg["window"].get("opacity", 1.0), 2,
                        lambda v: self._set(("window", "opacity"), round(v, 2))),
        )
        return scroll

    def _terminal_page(self) -> Gtk.ScrolledWindow:
        scroll, body = self._page("terminal")
        self._section(body, "Scrollback",
                      "tmux owns the deep history; this is VTE's own ring buffer.")
        self._row(
            body, "Lines kept in VTE", "2,000 keeps memory small; 100k is possible.",
            self._spin(500, 100000, 500, self.cfg["behavior"].get("scrollback", 2000),
                       lambda v: self._set(("behavior", "scrollback"), v)),
        )
        self._section(body, "Interaction")
        self._row(
            body, "Copy on select", "No Ctrl+Shift+C needed.",
            self._switch(self.cfg["behavior"].get("copy_on_select", False),
                         lambda v: self._set(("behavior", "copy_on_select"), v)),
        )
        self._row(
            body, "Scroll on output", "Off by default: agent logs would drag the view down.",
            self._switch(self.cfg["behavior"].get("scroll_on_output", False),
                         lambda v: self._set(("behavior", "scroll_on_output"), v)),
        )
        self._row(
            body, "Scroll on keystroke", "Typing jumps back to the prompt.",
            self._switch(self.cfg["behavior"].get("scroll_on_keystroke", True),
                         lambda v: self._set(("behavior", "scroll_on_keystroke"), v)),
        )
        self._row(
            body, "Hide mouse while typing", "",
            self._switch(self.cfg["behavior"].get("mouse_autohide", True),
                         lambda v: self._set(("behavior", "mouse_autohide"), v)),
        )
        self._row(
            body, "Audible bell", "Off keeps agent completion chimes out of your speakers.",
            self._switch(self.cfg["behavior"].get("audible_bell", False),
                         lambda v: self._set(("behavior", "audible_bell"), v)),
        )
        self._section(body, "Chrome")
        self._row(
            body, "Status bar", "Shows cwd, git branch and the live agent runtime.",
            self._switch(self.cfg["window"].get("status_bar", False),
                         lambda v: self._set(("window", "status_bar"), v)),
        )
        return scroll

    def _agents_page(self) -> Gtk.ScrolledWindow:
        scroll, body = self._page("agents")
        self._section(body, "Agent awareness",
                      "Inky watches the PTY and the process tree, never the network.")
        self._row(
            body, "Notify when an agent finishes",
            "Desktop notification when a long run ends.",
            self._switch(self.cfg["behavior"].get("notify_agent_done", True),
                         lambda v: self._set(("behavior", "notify_agent_done"), v)),
        )
        self._row(
            body, "Notify when an agent is waiting",
            "A quiet tab after 12s of no output — usually a permission prompt.",
            self._switch(self.cfg["behavior"].get("notify_agent_waiting", True),
                         lambda v: self._set(("behavior", "notify_agent_waiting"), v)),
        )
        self._row(
            body, "Confirm before closing an agent tab",
            "Closing a tab ends its tmux session and the agent with it.",
            self._switch(self.cfg["behavior"].get("confirm_agent_close", True),
                         lambda v: self._set(("behavior", "confirm_agent_close"), v)),
        )
        self._row(
            body, "Agent poll interval",
            "Seconds between process-tree scans. 1.4s is imperceptible and cheap.",
            self._scale(0.6, 4.0, 0.1, self.cfg["behavior"].get("poll_interval", 1.4), 1,
                        lambda v: self._set(("behavior", "poll_interval"), round(v, 1))),
        )

        self._section(body, "Extras")
        extras = (
            ("Zen mode", "Hide the tab strip and chrome; Ctrl+Shift+Z returns.", "zen"),
            ("Broadcast prompt…", "Send one prompt to every open tab at once.", "broadcast"),
            ("Shuffle theme", "Jump to a random palette.", "shuffle"),
            ("Themes folder", "Open ~/.config/inky/themes in your file manager.", "folder"),
        )
        for title, note, kind in extras:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            label = Gtk.Label(label=title, xalign=0.0)
            label.get_style_context().add_class("settings-label")
            sub = Gtk.Label(label=note, xalign=0.0)
            sub.get_style_context().add_class("settings-help")
            text.pack_start(label, False, False, 0)
            text.pack_start(sub, False, False, 0)
            box.pack_start(text, True, True, 0)
            button = Gtk.Button(label="Open" if kind == "folder" else "Run")
            button.connect("clicked", lambda _b, k=kind: self._action(k))
            box.pack_end(button, False, False, 0)
            body.pack_start(box, False, False, 0)

        self._section(body, "Keyboard")
        for keys, what in (
            ("Ctrl+,", "settings"),
            ("Ctrl+Shift+P", "theme picker"),
            ("Ctrl+Shift+M", "theme maker"),
            ("Ctrl+Shift+Z", "zen mode"),
            ("Ctrl+Shift+B", "broadcast prompt"),
            ("Ctrl+Shift+K", "command palette"),
        ):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            key = Gtk.Label(label=keys, xalign=0.0)
            key.get_style_context().add_class("settings-key")
            key.set_size_request(150, -1)
            val = Gtk.Label(label=what, xalign=0.0)
            val.get_style_context().add_class("settings-help")
            line.pack_start(key, False, False, 0)
            line.pack_start(val, False, False, 0)
            body.pack_start(line, False, False, 0)
        return scroll

    # -- actions -------------------------------------------------------
    def _set(self, path: tuple[str, str], value) -> None:
        self.cfg[path[0]][path[1]] = value
        self._changed()

    def _on_scope_toggle(self, button) -> None:
        if not button.get_active():
            return
        self._scope_mono = button is self.scope_terminal
        self._loading = True
        section = "font" if self._scope_mono else "ui_font"
        self.size_scale.set_value(float(self.cfg[section].get("size", 11.5)))
        self._loading = False
        self.weight_row.set_visible(self._scope_mono)
        self.gallery.set_scope(
            self._scope_mono, self.cfg[section].get("family", "")
        )

    def _on_font_pick(self, family: str) -> None:
        section = "font" if self._scope_mono else "ui_font"
        self.cfg[section]["family"] = family
        self._changed()

    def _on_size(self, value: float) -> None:
        section = "font" if self._scope_mono else "ui_font"
        self._set((section, "size"), round(value, 1))

    def _pick_theme(self) -> None:
        if callable(self.on_pick_theme):
            self.on_pick_theme()

    def _make_theme(self) -> None:
        if callable(self.on_make_theme):
            self.on_make_theme()

    def _action(self, kind: str) -> None:
        if callable(self.on_action):
            self.on_action(kind)

    def _reset(self) -> None:
        self.cfg.clear()
        self.cfg.update(copy.deepcopy(config.DEFAULTS))
        if callable(self.on_apply):
            self.on_apply()
        self.response(Gtk.ResponseType.CLOSE)

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        try:
            self.theme_label.set_text(theme.name)
        except Exception:
            pass
