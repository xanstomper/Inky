"""The VTE terminal widget used by every Inky tab."""

from __future__ import annotations

import os
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GLib, GObject, Gtk, Pango, Vte  # noqa: E402

from . import config  # noqa: E402
from .theme import Theme  # noqa: E402
from .util import shell_quote  # noqa: E402

CURSOR_SHAPES = {
    "block": getattr(Vte.CursorShape, "BLOCK", None),
    "bar": getattr(Vte.CursorShape, "BAR", None),
    "underline": getattr(Vte.CursorShape, "UNDERLINE", None),
}


def rgba(color: str, alpha: float = 1.0) -> Gdk.RGBA:
    value = Gdk.RGBA()
    value.parse(color)
    value.alpha = max(0.0, min(1.0, alpha))
    return value


class InkyTerminal(Gtk.Box):
    """VTE widget attached to one persistent tmux session."""

    __gsignals__ = {
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "hover": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, session: str, cfg: dict, theme: Theme, tmux, cwd: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session = session
        self.cfg = cfg
        self.theme = theme
        self.tmux = tmux
        self.cwd = cwd or os.path.expanduser("~")

        self.term = Vte.Terminal()
        self.term.set_scrollback_lines(int(cfg["behavior"]["scrollback"]))
        self.term.set_scroll_on_keystroke(True)
        self.term.set_scroll_on_output(False)
        self.term.set_mouse_autohide(True)
        self.term.set_allow_hyperlink(True)
        try:
            # Agent CLIs ring the bell on completion; VTE would otherwise ask
            # the session bus to beep, which is both noise and a round trip.
            self.term.set_audible_bell(False)
        except Exception:
            pass
        try:
            self.term.set_allow_bold(True)
        except Exception:
            pass
        try:
            self.term.set_word_char_exceptions("@-./_~+:")
        except Exception:
            pass
        try:
            self.term.set_cell_height_scale(float(cfg["font"].get("line_height", 1.0)))
        except Exception:
            pass
        self.set_cursor_mode()

        self.pack_start(self.term, True, True, 0)
        self.apply_theme(theme)

        self.term.connect("child-exited", self._on_child_exited)
        self.term.connect("hyperlink-hover-uri-changed", self._on_hover)
        if cfg["behavior"].get("copy_on_select"):
            self.term.connect("selection-changed", self._on_selection)

        self._setup_drop()
        self.term.connect("key-press-event", self._on_key)
        self.spawn()

    # -- setup ---------------------------------------------------------
    def _on_key(self, _widget, event) -> bool:
        window = self.get_toplevel()
        if isinstance(window, Gtk.Window) and hasattr(window, "handle_shortcut"):
            return bool(window.handle_shortcut(event))
        return False

    def _setup_drop(self) -> None:
        targets = [
            Gtk.TargetEntry.new("text/uri-list", 0, 0),
            Gtk.TargetEntry.new("text/plain;charset=utf-8", 0, 1),
            Gtk.TargetEntry.new("UTF8_STRING", 0, 1),
        ]
        try:
            self.term.drag_dest_set(
                Gtk.DestDefaults.ALL, targets, Gdk.DragAction.COPY
            )
            self.term.connect("drag-data-received", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, _widget, _context, _x, _y, data, _info, _time) -> None:
        paths: list[str] = []
        try:
            for uri in data.get_uris() or []:
                path = GLib.filename_from_uri(uri)[0]
                if path:
                    paths.append(path)
        except Exception:
            paths = []
        if paths:
            self.paste_text(" ".join(shell_quote(p) for p in paths))
            return
        try:
            text = data.get_text()
        except Exception:
            text = None
        if text:
            self.paste_text(text)

    # -- appearance ----------------------------------------------------
    def set_cursor_mode(self) -> None:
        behavior = self.cfg["behavior"]
        shape = CURSOR_SHAPES.get(str(behavior.get("cursor_shape", "bar")).lower())
        if shape is not None:
            try:
                self.term.set_cursor_shape(shape)
            except Exception:
                pass
        try:
            mode = Vte.CursorBlinkMode.ON if behavior.get("cursor_blink", True) else Vte.CursorBlinkMode.OFF
            self.term.set_cursor_blink_mode(mode)
        except Exception:
            pass

    def apply_font(self) -> None:
        font = self.cfg["font"]
        desc = Pango.FontDescription()
        desc.set_family(font.get("family", "JetBrains Mono"))
        desc.set_size(int(float(font.get("size", 11.5)) * Pango.SCALE))
        weight = int(font.get("weight", 400) or 400)
        try:
            desc.set_weight(weight)
        except Exception:
            pass
        self.term.set_font(desc)

    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.apply_font()
        try:
            floor = float(self.cfg["behavior"].get("min_contrast", 3.0))
        except (TypeError, ValueError):
            floor = 3.0
        palette = [rgba(color) for color in theme.palette_for(floor)]
        try:
            self.term.set_colors(rgba(theme.fg), rgba(theme.bg), palette)
            self.term.set_color_cursor(rgba(theme.cursor))
            self.term.set_color_cursor_foreground(rgba(theme.bg))
            self.term.set_color_highlight(rgba(theme.selection))
            self.term.set_color_highlight_foreground(rgba(theme.fg))
            # A dedicated bold colour keeps agent headings crisp even when the
            # theme's bright-white slot is nearly the same as its foreground.
            self.term.set_color_bold(rgba(theme.ui.get("bold") or "#ffffff"))
            self.term.set_bold_is_bright(True)
        except Exception:
            pass

    # -- process -------------------------------------------------------
    def spawn(self) -> None:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("TMUX", "TMUX_PANE", "VTE_VERSION")
        }
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "INKY": "1",
            "INKY_SESSION": self.session,
            "INKY_SOCKET": self.tmux.socket,
        })
        envv = [f"{k}={v}" for k, v in env.items()]
        argv = self.tmux.attach_argv(self.session)
        try:
            self.term.spawn_async(
                Vte.PtyFlags.DEFAULT,
                self.cwd if os.path.isdir(self.cwd) else None,
                argv,
                envv,
                GLib.SpawnFlags.SEARCH_PATH,
                None,
                None,
                -1,
                None,
                self._on_spawn,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.feed_notice(f"Could not attach session: {exc}")

    def _on_spawn(self, _term, _pid, error) -> None:
        if error:
            self.feed_notice(f"Could not attach session: {error.message}")

    def _on_child_exited(self, _term, status: int) -> None:
        self.emit("exited", status)

    def feed_notice(self, message: str) -> None:
        try:
            self.term.feed(f"\r\n\x1b[2m{message}\x1b[0m\r\n".encode())
        except Exception:
            pass

    def _on_hover(self, _term, uri: str, _bbox) -> None:
        self.emit("hover", uri or "")

    def _on_selection(self, _term) -> None:
        try:
            self.term.copy_clipboard_format(Vte.Format.TEXT)
        except Exception:
            pass

    # -- actions -------------------------------------------------------
    def paste_text(self, text: str) -> None:
        self.tmux.send_text(self.session, text, submit=False)

    def send_prompt(self, text: str) -> None:
        self.tmux.send_text(self.session, text, submit=True)

    def copy_selection(self) -> None:
        try:
            self.term.copy_clipboard_format(Vte.Format.TEXT)
        except Exception:
            try:
                self.term.copy_clipboard()
            except Exception:
                pass

    def paste_clipboard(self) -> None:
        """Paste text, or save a clipboard image and paste its path.

        Terminals cannot carry images, but every AI agent can read a file —
        so screenshots become ``/home/you/.cache/inky/paste/….png``.
        """
        try:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            if clipboard.wait_is_image_available():
                pixbuf = clipboard.wait_for_image()
                if pixbuf is not None:
                    folder = config.CACHE_DIR / "paste"
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"inky-{time.strftime('%Y%m%d-%H%M%S')}.png"
                    pixbuf.savev(str(path), "png", [], [])
                    self.paste_text(shell_quote(str(path)))
                    return
            self.term.paste_clipboard()
        except Exception:
            try:
                self.term.paste_clipboard()
            except Exception:
                pass

    def feed(self, text: str) -> None:
        try:
            self.term.feed(text.encode())
        except Exception:
            pass

    def search(self, pattern: str, forward: bool = True, case_sensitive: bool = False) -> bool:
        if not pattern:
            try:
                self.term.search_set_regex(None, 0)
            except Exception:
                pass
            return False
        try:
            flags = 0 if case_sensitive else GLib.RegexCompileFlags.CASELESS
            try:
                regex = GLib.Regex(pattern, flags, 0)
            except TypeError:
                regex = GLib.Regex.new(pattern, flags, 0)
            self.term.search_set_regex(regex, 0)
            self.term.search_set_wrap_around(True)
            finder = self.term.search_find_next if forward else self.term.search_find_previous
            return bool(finder())
        except Exception:
            return False

    def clear_search(self) -> None:
        try:
            self.term.search_set_regex(None, 0)
        except Exception:
            pass

    def grab(self) -> None:
        self.term.grab_focus()
