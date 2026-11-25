"""Multi-line prompt composer for AI agents."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


class Composer(Gtk.Revealer):
    """A floating, multi-line prompt box.

    Typing a long prompt straight into a TUI agent is painful: every newline
    submits. The composer collects the text first, then hands it over as one
    bracketed paste so the agent receives the prompt exactly once.
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(130)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(38)
        self.set_no_show_all(True)
        self._on_send = None
        self.on_close = None

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("composer")
        card.set_size_request(760, -1)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title = Gtk.Label(label="Prompt")
        self.title.get_style_context().add_class("composer-hint-accent")
        header.pack_start(self.title, False, False, 0)
        self.target = Gtk.Label(label="")
        self.target.get_style_context().add_class("composer-hint")
        header.pack_start(self.target, False, False, 0)
        card.pack_start(header, False, False, 0)

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_monospace(True)
        self.view.set_accepts_tab(False)
        self.view.set_left_margin(4)
        self.view.set_right_margin(4)
        self.view.set_top_margin(2)
        self.view.connect("key-press-event", self._on_key)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(96)
        scroll.set_max_content_height(240)
        scroll.add(self.view)
        card.pack_start(scroll, True, True, 0)

        hint = Gtk.Label(
            label="⏎ send    ⇧⏎ newline    esc close    —  pasted as one bracketed block"
        )
        hint.get_style_context().add_class("composer-hint")
        hint.set_halign(Gtk.Align.START)
        card.pack_start(hint, False, False, 0)

        self.add(card)

    def open(self, label: str, on_send) -> None:
        self._on_send = on_send
        self.target.set_text(label)
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.set_reveal_child(True)
        self.show()
        GLib.idle_add(self._focus_view)

    def _focus_view(self) -> bool:
        self.view.grab_focus()
        return False

    def close(self) -> None:
        self.set_reveal_child(False)
        callback = getattr(self, "on_close", None)
        if callable(callback):
            callback()

    def is_open(self) -> bool:
        return bool(self.get_reveal_child())

    def _on_key(self, _widget, event) -> bool:
        key = Gdk.keyval_name(event.keyval)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if key == "Escape":
            self.close()
            return True
        if key in ("Return", "KP_Enter"):
            if shift:
                return False
            self._send()
            return True
        if ctrl and key == "Return":
            self._send()
            return True
        return False

    def _send(self) -> None:
        buffer = self.view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        text = text.rstrip()
        if not text:
            return
        callback = self._on_send
        buffer.set_text("")
        self.close()
        if callable(callback):
            callback(text)
