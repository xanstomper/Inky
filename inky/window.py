"""Inky's main window."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import config  # noqa: E402
from .agents import AgentHit, available_agents, scan  # noqa: E402
from .composer import Composer  # noqa: E402
from .css import build_css  # noqa: E402
from .dialogs import SettingsDialog, ThemeMaker  # noqa: E402
from .palette import Palette  # noqa: E402
from .terminal import InkyTerminal  # noqa: E402
from .theme import Theme, load_themes, pick  # noqa: E402
from .tmux import Tmux, TmuxError  # noqa: E402
from .util import ellipsize_middle, human_seconds, slugify, timestamp  # noqa: E402
from .widgets import SearchBar, ThemePicker, Toast  # noqa: E402

HOME = str(Path.home())


def _pointer() -> Gdk.Cursor | None:
    try:
        return Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "pointer")
    except Exception:
        return None


def _set_pointer(widget: Gtk.Widget) -> None:
    """GTK3 cursors live on the GdkWindow, so wait for realize."""
    def on_realize(w) -> None:
        cursor = _pointer()
        window = w.get_window()
        if cursor and window is not None:
            window.set_cursor(cursor)

    widget.connect("realize", on_realize)


class Tab:
    """One terminal tab: its widgets, session and agent state."""

    def __init__(self, window: "InkyWindow", session: str, terminal: InkyTerminal,
                 title: str, auto_title: bool = True) -> None:
        self.window = window
        self.session = session
        self.terminal = terminal
        self.title = title
        self.auto_title = auto_title
        self.agent: AgentHit | None = None
        self.agent_seen_at = 0.0
        self.done = False
        self.closed = False
        self.missing = 0
        self.cwd = terminal.cwd
        self.command = ""
        self.last_screen: int | None = None
        self.last_change = time.time()
        self.waiting_since = 0.0
        self.last_wait_notice = 0.0
        self.respawns = 0
        self._visual_key: tuple | None = None

        self.widget = Gtk.EventBox()
        self.widget.get_style_context().add_class("tab")
        self.widget.set_visible_window(True)
        self.widget.set_events(Gdk.EventMask.BUTTON_PRESS_MASK
                               | Gdk.EventMask.SCROLL_MASK)
        self.widget.set_size_request(-1, 36)
        self.widget.set_hexpand(True)
        self.widget.set_valign(Gtk.Align.FILL)
        _set_pointer(self.widget)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_halign(Gtk.Align.FILL)
        inner.set_hexpand(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_valign(Gtk.Align.CENTER)
        row.set_halign(Gtk.Align.FILL)
        row.set_hexpand(True)
        row.set_margin_start(11)
        row.set_margin_end(7)

        self.glyph = Gtk.Label(label="◆")
        self.glyph.get_style_context().add_class("tab-glyph")
        self.glyph.set_size_request(12, -1)
        row.pack_start(self.glyph, False, False, 0)

        self.title_label = Gtk.Label(label=title)
        self.title_label.get_style_context().add_class("tab-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(40)
        self.title_label.set_xalign(0.0)
        row.pack_start(self.title_label, True, True, 0)

        self.close_widget = Gtk.EventBox()
        self.close_widget.get_style_context().add_class("tab-close")
        self.close_widget.set_visible_window(True)
        self.close_widget.set_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        _set_pointer(self.close_widget)
        close_label = Gtk.Label(label="✕")
        self.close_widget.add(close_label)
        self.close_widget.set_size_request(15, 15)
        self.close_widget.set_tooltip_text("Close tab")
        self.close_widget.connect("button-press-event", self._on_close_click)
        row.pack_end(self.close_widget, False, False, 0)

        inner.pack_start(row, True, True, 0)
        self.widget.add(inner)

        self.widget.connect("button-press-event", self._on_click)
        self.widget.connect("scroll-event",
                           lambda w, e: self.window._on_tab_strip_scroll(w, e))
        self.update_visual()

    # -- events --------------------------------------------------------
    def _on_close_click(self, _widget, _event) -> bool:
        self.window.close_tab(self)
        return True

    def _on_click(self, widget, event) -> bool:
        double_press = getattr(Gdk.EventType, "DOUBLE_BUTTON_PRESS", 5)
        if event.type == double_press and event.button == 1:
            self.window.rename_tab(self)
            return True
        if event.button == 1:
            self.window.activate(self.window.tabs.index(self))
            return True
        if event.button == 2:
            self.window.close_tab(self)
            return True
        if event.button == 3:
            self.window.tab_menu(self, event)
            return True
        return False

    # -- state ---------------------------------------------------------
    def set_title(self, title: str, auto: bool = False) -> None:
        if not title:
            return
        if auto and title == self.title:
            return
        if not auto:
            self.auto_title = False
        self.title = title
        self.title_label.set_text(title)
        self.title_label.set_tooltip_text(title)

    def set_agent(self, hit: AgentHit | None) -> None:
        self.agent = hit
        self.update_visual()

    def working(self, now: float | None = None) -> bool:
        now = now or time.time()
        return bool(self.agent) and (now - self.last_change) < 5.0

    def update_visual(self) -> bool:
        """Repaint the tab only when something it shows actually changed.

        Every set_markup/set_tooltip allocates a Pango layout and queues a
        relayout of the tab strip; at one poll per 1.4 s that is a permanent
        drip of needless work, so the state is fingerprinted first.
        """
        state = "working" if self.working() else "waiting"
        key = (self.done, self.agent.id if self.agent else "", state,
               self.command, self.cwd, self.title, self.session)
        if key == self._visual_key:
            return False
        self._visual_key = key
        ctx = self.widget.get_style_context()
        if self.done:
            ctx.add_class("done")
        else:
            ctx.remove_class("done")
        if self.agent:
            self.glyph.set_markup(
                f'<span foreground="{self.agent.color}">{self.agent.glyph}</span>'
            )
            self.glyph.set_tooltip_text(
                f"{self.agent.name} · {state} · {human_seconds(self.agent.runtime)}"
            )
        else:
            self.glyph.set_text("◆")
            self.glyph.set_tooltip_text(self.cwd or self.session)
        self.widget.set_tooltip_text(self._tooltip())
        return True

    def _tooltip(self) -> str:
        where = self.window._display_cwd(self.cwd) if self.cwd else self.session
        if self.agent:
            state = "working" if self.working() else "waiting"
            return f"{self.agent.name} · {state} · {where}"
        if self.command:
            return f"{self.command} · {where}"
        return f"shell · {where}"


class InkyWindow(Gtk.Window):
    def __init__(self, cfg: dict, state, attach_session: str | None = None,
                 attach_title: str | None = None, theme_slug: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.tmux = Tmux()
        self.themes = load_themes()
        self.theme: Theme = pick(self.themes, theme_slug or cfg.get("theme"))
        self.tabs: list[Tab] = []
        self._css_provider = None
        self._branch_cache: dict[str, tuple[float, str]] = {}
        self._status_cache: dict[str, str] = {}
        self._poll_id = 0
        self._status_tick = 0
        self._closing = False
        self.zen = False
        self.show_statusbar = bool(cfg["window"].get("status_bar", False))

        self.tmux.ensure_server()

        self.get_style_context().add_class("inky-window")
        self.set_title("Inky")
        try:
            self.set_icon_from_file(str(config.LOGO))
        except Exception:
            pass
        self.set_default_size(
            int(cfg["window"].get("width", 1240)),
            int(cfg["window"].get("height", 800)),
        )
        opacity = float(cfg["window"].get("opacity", 1.0))
        if 0.3 <= opacity < 1.0:
            self.set_opacity(opacity)
        self._restore_geometry()
        self._build()
        self.apply_theme(self.theme.slug, persist=False)
        self._restore_tabs(attach_session, attach_title)

        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", lambda _w, event: self.handle_shortcut(event))
        self.connect("destroy", lambda *_: Gtk.main_quit())
        self._poll_id = GLib.timeout_add(
            int(float(cfg["behavior"].get("poll_interval", 1.4)) * 1000), self._poll
        )

    # -- construction --------------------------------------------------
    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header = Gtk.HeaderBar()
        header.get_style_context().add_class("inky-header")
        header.set_show_close_button(True)

        self.header_title = Gtk.Label(label="Inky")
        self.header_title.get_style_context().add_class("window-title")
        self.header_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.header_title.set_max_width_chars(64)
        header.set_custom_title(self.header_title)

        self.new_button = self._icon_button("tab-new-symbolic", "New tab  (Ctrl+Shift+T)",
                                            self.add_tab)
        header.pack_start(self.new_button)
        self.search_button = self._icon_button("edit-find-symbolic",
                                              "Search  (Ctrl+Shift+F)", self.open_search)
        header.pack_end(self.search_button)
        self.menu_button = self._icon_button("open-menu-symbolic", "Menu",
                                             self.open_main_menu)
        header.pack_end(self.menu_button)
        self.set_titlebar(header)

        # Tab bar: one full-width row under the header, tabs sharing it equally
        # and switching to their own plate with an accent underline.
        self.tabbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tabbox.set_homogeneous(True)
        self.tabbox.set_hexpand(True)
        self.tabbox.set_valign(Gtk.Align.FILL)

        self.tabrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tabrow.get_style_context().add_class("tabstrip")
        self.tabrow.pack_start(self.tabbox, True, True, 0)
        self.tabrow.set_size_request(-1, 37)
        self.tabrow.connect("scroll-event", self._on_tab_strip_scroll)

        self.stack = Gtk.Stack()
        # A crossfade repaints two full terminal grids every tab switch. VTE has
        # no compositor-friendly snapshot, so switching instantly is both the
        # smoother-looking and the cheaper choice.
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_transition_duration(0)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.search = SearchBar(self._do_search, self._search_closed)
        self.search.set_visible(False)

        self.term_overlay = Gtk.Overlay()
        self.term_overlay.add(self.stack)
        self.term_overlay.add_overlay(self.search)
        self.term_overlay.set_hexpand(True)
        self.term_overlay.set_vexpand(True)
        padding = max(0, int(self.cfg["window"].get("padding", 0)))
        self.term_overlay.set_margin_start(padding)
        self.term_overlay.set_margin_end(padding)
        self.term_overlay.set_margin_top(padding)
        self.term_overlay.set_margin_bottom(padding)
        self.term_overlay.get_style_context().add_class("term-area")

        root.pack_start(self.tabrow, False, False, 0)
        root.pack_start(self.term_overlay, True, True, 0)
        self.statusbar = self._build_statusbar()
        self.show_statusbar = bool(self.cfg["window"].get("status_bar", False))
        root.pack_start(self.statusbar, False, False, 0)
        self.statusbar.set_visible(self.show_statusbar)

        overlay = Gtk.Overlay()
        overlay.add(root)
        self.palette = Palette()
        self.palette.set_visible(False)
        self.palette.connect("hide", lambda *_: self._focus_terminal())
        self.composer = Composer()
        self.composer.set_visible(False)
        self.composer.on_close = self._focus_terminal
        self.toast = Toast()
        self.toast.set_visible(False)
        overlay.add_overlay(self.palette)
        overlay.add_overlay(self.composer)
        overlay.add_overlay(self.toast)
        overlay.set_overlay_pass_through(self.toast, True)
        self.add(overlay)

    def _build_statusbar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("statusbar")

        self.info_left = Gtk.Label(label="")
        self.info_left.get_style_context().add_class("status-label")
        self.info_left.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.info_left.set_max_width_chars(70)
        self.info_left.set_xalign(0.0)
        bar.pack_start(self.info_left, False, False, 0)
        bar.set_margin_start(2)

        self.info_agent = Gtk.Label(label="")
        self.info_agent.get_style_context().add_class("status-label")
        self.info_agent.set_ellipsize(Pango.EllipsizeMode.END)
        self.info_agent.set_no_show_all(True)
        bar.pack_end(self.info_agent, False, False, 0)
        return bar

    def _icon_button(self, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.get_style_context().add_class("toolbutton")
        button.set_relief(Gtk.ReliefStyle.NONE)
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.add(image)
        if tooltip:
            button.set_tooltip_text(tooltip)
        else:
            button.set_has_tooltip(False)
        button.connect("clicked", lambda *_: callback())
        return button

    # -- geometry ------------------------------------------------------
    def _restore_geometry(self) -> None:
        geometry = self.state.window().get("geometry")
        if not (isinstance(geometry, list) and len(geometry) == 4):
            return
        x, y, width, height = (int(v) for v in geometry)
        try:
            self.resize(max(480, width), max(320, height))
            if x > -10000 and y > -10000:
                self.move(x, y)
        except Exception:
            pass

    def _geometry(self) -> list[int] | None:
        try:
            x, y = self.get_position()
            width, height = self.get_size()
            return [x, y, width, height]
        except Exception:
            return None

    # -- tabs ----------------------------------------------------------
    def _restore_tabs(self, attach_session: str | None, attach_title: str | None) -> None:
        if attach_session:
            if not self.tmux.has_session(attach_session):
                self._fatal(f"Session {attach_session} is not running.")
                return
            meta = self.state.session(attach_session)
            self.add_tab(session=attach_session,
                         title=attach_title or meta.get("title"),
                         restored=True)
            return

        restored = 0
        window_state = self.state.window()
        for entry in window_state.get("tabs", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("session")
            if not name or not self.tmux.has_session(name):
                continue
            self.add_tab(session=name, title=entry.get("title"), restored=True,
                         auto_title=bool(entry.get("auto", True)))
            restored += 1
        if restored:
            self.activate(min(int(window_state.get("active", 0)), len(self.tabs) - 1))
        else:
            self.add_tab()

    def add_tab(self, cwd: str | None = None, command: str | None = None,
                title: str | None = None, session: str | None = None,
                restored: bool = False, auto_title: bool | None = None) -> Tab | None:
        cwd = cwd or self._default_cwd()
        session = session or self.tmux.new_id()
        if not restored:
            try:
                self.tmux.new_session(session, cwd, self.cfg.get("shell") or None)
            except TmuxError as exc:
                self.toast.show_message(f"Could not start session: {exc}")
                return None

        terminal = InkyTerminal(session, self.cfg, self.theme, self.tmux, cwd=cwd)
        display = title or self._title_for_cwd(cwd)
        auto = auto_title if auto_title is not None else title is None
        tab = Tab(self, session, terminal, display, auto_title=auto)
        tab.cwd = cwd
        tab.terminal.connect("exited", lambda _t, status, tab=tab: self._on_terminal_exit(tab, status))
        tab.terminal.connect("hover", lambda _t, uri: self.refresh_status(hover_uri=uri))
        tab.terminal.term.connect("leave-notify-event",
                                  lambda *_: self.refresh_status())
        tab.terminal.term.connect("button-press-event", self._on_terminal_click)

        self.tabs.append(tab)
        self.stack.add_named(terminal, session)
        # Widgets created after the window was shown start hidden, and GTK
        # will not switch a stack to a hidden child — show it explicitly.
        terminal.show_all()
        self.tabbox.pack_start(tab.widget, True, True, 0)
        tab.widget.show_all()
        self._sync_tabstrip()
        self.activate(len(self.tabs) - 1)

        self.state.remember_session(session, title=tab.title, cwd=cwd, created=time.time())
        if command:
            GLib.timeout_add(320, self._send_after_spawn, session, command)
        self._save_window()
        return tab

    def _send_after_spawn(self, session: str, command: str) -> bool:
        self.tmux.send_text(session, command, submit=True)
        return False

    def _sync_tabstrip(self) -> None:
        # The bar is always there: one tab or ten, it owns the second row.
        self.tabrow.show_all()

    def current_index(self) -> int:
        tab = self.current_tab()
        return self.tabs.index(tab) if tab else 0

    def current_tab(self) -> Tab | None:
        child = self.stack.get_visible_child()
        for tab in self.tabs:
            if tab.terminal is child:
                return tab
        return self.tabs[-1] if self.tabs else None

    def activate(self, index: int) -> None:
        if not self.tabs:
            return
        index = max(0, min(index, len(self.tabs) - 1))
        tab = self.tabs[index]
        self.stack.set_visible_child(tab.terminal)
        for other in self.tabs:
            ctx = other.widget.get_style_context()
            if other is tab:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")
        tab.done = False
        tab.update_visual()
        tab.terminal.grab()
        self.refresh_status()
        self._save_window()

    def close_tab(self, tab: Tab, kill: bool = True, confirm: bool = True,
                  silent: bool = False) -> None:
        if tab.closed or tab not in self.tabs:
            return
        if (confirm and kill and tab.agent is not None
                and self.cfg["behavior"].get("confirm_agent_close", True)):
            agent = tab.agent
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text=f"{agent.name} is running in “{tab.title}”.",
            )
            dialog.format_secondary_text(
                "Closing this tab stops the agent. Detach it instead to keep it running "
                "in its own window."
            )
            dialog.add_button("Detach window", 42)
            response = dialog.run()
            dialog.destroy()
            if response == 42:
                self.detach_tab(tab)
                return
            if response != Gtk.ResponseType.OK:
                return

        tab.closed = True
        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        self.tabbox.remove(tab.widget)
        self.stack.remove(tab.terminal)
        tab.terminal.destroy()
        if kill:
            self.tmux.kill_session(tab.session)
            self.state.forget_session(tab.session)
        self._sync_tabstrip()
        if not self.tabs:
            self.add_tab()
        else:
            self.activate(min(index, len(self.tabs) - 1))
        if not silent:
            self.toast.show_message("Tab closed" if kill else "Tab detached")
        self._save_window()

    def move_tab(self, tab: "Tab", delta: int) -> None:
        """Slide a tab along the bar; the active tab follows it."""
        if tab not in self.tabs:
            return
        index = self.tabs.index(tab)
        target = max(0, min(index + delta, len(self.tabs) - 1))
        if target == index:
            return
        self.tabs.insert(target, self.tabs.pop(index))
        self.tabbox.reorder_child(tab.widget, target)
        self.activate(target)
        self._save_window()

    def duplicate_tab(self, tab: Tab | None = None) -> None:
        """A new session in the same folder, named like the original."""
        tab = tab or self.current_tab()
        if not tab:
            return
        created = self.add_tab(cwd=tab.cwd,
                               title=None if tab.auto_title else tab.title)
        if created:
            self.toast.show_message("Tab duplicated")

    def detach_tab(self, tab: Tab | None = None) -> None:
        tab = tab or self.current_tab()
        if not tab:
            return
        if not self.tmux.has_session(tab.session):
            self.toast.show_message("Nothing to detach — session already ended.")
            return
        self._launch([
            "--attach", tab.session,
            "--title", tab.title,
            "--theme", self.theme.slug,
        ])
        self.close_tab(tab, kill=False, confirm=False, silent=True)
        self.toast.show_message(f"Detached “{tab.title}” into its own window")

    def _launch(self, args: list[str]) -> None:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(config.ROOT) + (":" + existing if existing else "")
        try:
            subprocess.Popen(
                [sys.executable, "-m", "inky", *args],
                cwd=str(config.ROOT), env=env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.toast.show_message(f"Could not launch window: {exc}")

    def new_window(self) -> None:
        self._launch([])

    def rename_tab(self, tab: Tab | None = None) -> None:
        tab = tab or self.current_tab()
        if not tab:
            return
        dialog = Gtk.Dialog(title="Rename tab", transient_for=self)
        dialog.set_modal(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Rename", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_default_size(340, -1)
        entry = Gtk.Entry()
        entry.set_text(tab.title)
        entry.set_activates_default(True)
        entry.set_margin_top(10)
        entry.set_margin_bottom(10)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            title = entry.get_text().strip()
            if title:
                tab.set_title(title[:60], auto=False)
                self.state.remember_session(tab.session, title=tab.title)
                self.refresh_status()
                self._save_window()
        dialog.destroy()
        self._focus_terminal()

    def tab_menu(self, tab: Tab, event) -> None:
        menu = Gtk.Menu()
        menu.append(self._menu_item("Rename tab…    F2", lambda: self.rename_tab(tab)))
        menu.append(self._menu_item("New tab in this folder    ⌃⇧T",
                                    lambda: self.add_tab(cwd=tab.cwd)))
        menu.append(self._menu_item("Duplicate tab", lambda: self.duplicate_tab(tab)))
        menu.append(self._menu_item("Move tab left    ⌃⇧⇞", lambda: self.move_tab(tab, -1)))
        menu.append(self._menu_item("Move tab right    ⌃⇧⇟", lambda: self.move_tab(tab, 1)))
        menu.append(self._menu_item("Detach to new window    ⌃⇧D",
                                    lambda: self.detach_tab(tab)))
        menu.append(self._menu_item("Export transcript    ⌃⇧S",
                                    lambda: self.export_transcript(tab)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self.theme_menu_item())
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Close tab    ⌃⇧W", lambda: self.close_tab(tab)))
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, event.button, event.time)

    def _on_terminal_click(self, widget, event) -> bool:
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        menu.append(self._menu_item("Copy    ⌃⇧C", self.copy_selection))
        menu.append(self._menu_item("Paste    ⌃⇧V", self.paste_clipboard))
        menu.append(self._menu_item("Paste image as file path    ⌃⇧I",
                                    self.paste_image))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Compose prompt…    ⌃⇧⏎", self.open_composer))
        menu.append(self._menu_item("Export transcript    ⌃⇧S", self.export_transcript))
        menu.append(self._menu_item("Search    ⌃⇧F", self.open_search))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self.theme_menu_item())
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            pass
        return True

    def _menu_item(self, label: str, callback) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: callback())
        return item

    def theme_menu_item(self) -> Gtk.MenuItem:
        """One menu entry: the theme picker (a fifty-item submenu is unusable)."""
        return self._menu_item(f"Themes…  ({len(self.themes)})", self.open_theme_picker)

    def open_theme_picker(self) -> None:
        """Searchable theme list with live preview; one click applies and saves."""
        existing = getattr(self, "_theme_picker", None)
        if existing is not None:
            existing.present()
            return
        dialog = ThemePicker(self, self.themes, self.theme.slug, self.apply_theme,
                             self.open_theme_maker)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.connect("destroy", lambda *_: setattr(self, "_theme_picker", None))
        dialog.present()
        self._theme_picker = dialog

    def open_theme_maker(self, slug: str | None = None) -> None:
        """Colour editor: pick ANSI slots, watch a fake terminal, save to disk."""
        existing = getattr(self, "_theme_maker", None)
        if existing is not None:
            existing.present()
            return
        dialog = ThemeMaker(self, self.themes, slug or self.theme.slug,
                            self._theme_saved, self._theme_deleted)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.connect("destroy", lambda *_: setattr(self, "_theme_maker", None))
        dialog.present()
        self._theme_maker = dialog

    def _reload_themes(self, select: str | None = None) -> None:
        self.themes = load_themes()
        picker = getattr(self, "_theme_picker", None)
        if picker is not None:
            picker.reload(self.themes, select or self.theme.slug)
        if select:
            self.apply_theme(select)
        else:
            self.refresh_status()

    def _theme_saved(self, slug: str) -> None:
        self._reload_themes(slug)
        self.toast.show_message(f"Theme saved · {slug}")

    def _theme_deleted(self, slug: str) -> None:
        fallback = "inky" if self.theme.slug == slug else None
        self._reload_themes(fallback)
        self.toast.show_message(f"Theme deleted · {slug}")

    def open_settings(self) -> None:
        """Fonts, contrast, cursor, terminal behaviour and agent extras."""
        existing = getattr(self, "_settings", None)
        if existing is not None:
            existing.present()
            return
        dialog = SettingsDialog(self, self.cfg, self.theme, self.themes,
                                self.apply_settings, self.open_theme_picker,
                                self.open_theme_maker, self._settings_action)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.connect("destroy", lambda *_: setattr(self, "_settings", None))
        dialog.present()
        self._settings = dialog

    def apply_settings(self) -> None:
        """Push settings into the live window — no restart, no re-attach."""
        config.save_config(self.cfg)
        padding = max(0, int(self.cfg["window"].get("padding", 0)))
        for side in ("start", "end", "top", "bottom"):
            getattr(self.term_overlay, f"set_margin_{side}")(padding)
        self.show_statusbar = bool(self.cfg["window"].get("status_bar", False))
        self.statusbar.set_visible(self.show_statusbar and not self.zen)
        try:
            opacity = float(self.cfg["window"].get("opacity", 1.0))
            self.set_opacity(max(0.4, min(1.0, opacity)))
        except Exception:
            pass
        self.apply_theme(self.theme.slug, persist=False)
        for tab in self.tabs:
            tab.terminal.apply_behavior()
        self._restart_poll()
        self.refresh_status()

    def _restart_poll(self) -> None:
        if self._poll_id:
            try:
                GLib.source_remove(self._poll_id)
            except Exception:
                pass
        interval = float(self.cfg["behavior"].get("poll_interval", 1.4))
        self._poll_id = GLib.timeout_add(max(300, int(interval * 1000)), self._poll)

    def _settings_action(self, kind: str) -> None:
        if kind == "zen":
            self.toggle_zen()
        elif kind == "broadcast":
            self.broadcast_prompt()
        elif kind == "shuffle":
            self.shuffle_theme()
        elif kind == "folder":
            self._open_themes_folder()

    def _open_themes_folder(self) -> None:
        try:
            config.USER_THEMES.mkdir(parents=True, exist_ok=True)
            Gtk.show_uri(None, config.USER_THEMES.as_uri(),
                         Gtk.get_current_event_time())
        except Exception:
            try:
                subprocess.Popen(["xdg-open", str(config.USER_THEMES)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.toast.show_message(str(config.USER_THEMES))

    def toggle_zen(self) -> None:
        """Strip the chrome, keep the grid. The shortcut still gets you out."""
        self.zen = not self.zen
        self.tabrow.set_visible(not self.zen)
        for widget in (self.new_button, self.search_button):
            widget.set_visible(not self.zen)
        self.statusbar.set_visible(self.show_statusbar and not self.zen)
        self.toast.show_message(
            "Zen mode — Ctrl+Shift+Z brings the chrome back" if self.zen
            else "Chrome restored"
        )

    def broadcast_prompt(self) -> None:
        """One prompt, every tab: run the same instruction past several agents."""
        live = [tab for tab in self.tabs if not tab.closed]
        if len(live) < 2:
            self.toast.show_message("Open another tab to broadcast to")
            return
        self.composer.open(f"broadcast · {len(live)} tabs", self._send_broadcast)

    def _send_broadcast(self, text: str) -> None:
        sent = 0
        for tab in self.tabs:
            if tab.closed:
                continue
            self.tmux.send_text(tab.session, text, submit=True)
            sent += 1
        self._focus_terminal()
        self.toast.show_message(f"Prompt sent to {sent} tabs")

    def shuffle_theme(self) -> None:
        choices = [slug for slug in self.themes if slug != self.theme.slug]
        if not choices:
            return
        self.apply_theme(random.choice(choices))
        self.toast.show_message(f"Theme · {self.theme.name}")

    def open_main_menu(self) -> None:
        menu = Gtk.Menu()
        menu.append(self._menu_item("New tab    ⌃⇧T", self.add_tab))
        menu.append(self._menu_item("New window    ⌃⇧N", self.new_window))
        menu.append(self._menu_item("Detach tab    ⌃⇧D", self.detach_tab))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self.theme_menu_item())
        menu.append(self._menu_item("Theme maker…    ⌃⇧M", self.open_theme_maker))
        menu.append(self._menu_item("Settings…    ⌃,", self.open_settings))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Broadcast prompt to all tabs    ⌃⇧B",
                                    self.broadcast_prompt))
        menu.append(self._menu_item("Zen mode    ⌃⇧Z", self.toggle_zen))
        menu.append(self._menu_item("Shuffle theme", self.shuffle_theme))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Duplicate tab", self.duplicate_tab))
        menu.append(self._menu_item("Rename tab    F2", self.rename_tab))
        menu.append(self._menu_item("Search    ⌃⇧F", self.open_search))
        menu.append(self._menu_item("Export transcript    ⌃⇧S", self.export_transcript))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Close tab    ⌃⇧W", self.close_tab_current))
        menu.append(self._menu_item("Quit    ⌃⇧Q", self.close))
        menu.show_all()
        try:
            menu.popup_at_widget(self.menu_button, Gdk.Gravity.SOUTH_EAST,
                                 Gdk.Gravity.NORTH_EAST, None)
        except Exception:
            pass

    def close_tab_current(self) -> None:
        tab = self.current_tab()
        if tab:
            self.close_tab(tab)

    # -- terminal actions ---------------------------------------------
    def copy_selection(self) -> None:
        tab = self.current_tab()
        if tab:
            tab.terminal.copy_selection()

    def paste_clipboard(self) -> None:
        tab = self.current_tab()
        if tab:
            tab.terminal.paste_clipboard()

    def paste_image(self) -> None:
        tab = self.current_tab()
        if not tab:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if not clipboard.wait_is_image_available():
            self.toast.show_message("No image on the clipboard")
            return
        tab.terminal.paste_clipboard()
        self.toast.show_message("Clipboard image saved and path pasted")

    def open_search(self) -> None:
        if self.search.is_open():
            self.search.close()
            return
        self.search.open()

    def _do_search(self, pattern: str, forward: bool, case: bool) -> bool:
        tab = self.current_tab()
        if not tab:
            return False
        return tab.terminal.search(pattern, forward, case)

    def _search_closed(self) -> None:
        tab = self.current_tab()
        if tab:
            tab.terminal.clear_search()
            tab.terminal.grab()

    def open_composer(self) -> None:
        tab = self.current_tab()
        if not tab:
            return
        self.composer.open(
            f"{tab.agent.name if tab.agent else 'shell'}  ·  {tab.session}",
            lambda text, tab=tab: self._send_prompt(tab, text),
        )

    def _send_prompt(self, tab: Tab, text: str) -> None:
        tab.terminal.send_prompt(text)
        tab.terminal.grab()
        self.toast.show_message("Prompt sent")

    def export_transcript(self, tab: Tab | None = None) -> None:
        tab = tab or self.current_tab()
        if not tab:
            return
        text = self.tmux.capture(tab.session, 50000)
        header = (
            f"# {tab.title}\n\n"
            f"- session: `{tab.session}`\n"
            f"- cwd: `{tab.cwd}`\n"
            f"- exported: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- agent: {tab.agent.name if tab.agent else 'none'}\n\n"
            "```text\n"
        )
        config.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.TRANSCRIPT_DIR / f"{timestamp()}-{slugify(tab.title)}.md"
        try:
            path.write_text(header + text + "\n```\n", encoding="utf-8")
        except OSError as exc:
            self.toast.show_message(f"Could not write transcript: {exc}")
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(str(path), -1)
        self.toast.show_message(f"Transcript saved · {path.name} (path copied)")

    def _focus_terminal(self) -> None:
        tab = self.current_tab()
        if tab:
            tab.terminal.grab()

    # -- palette -------------------------------------------------------
    def open_palette(self, query: str = "") -> None:
        if self.palette.is_open():
            self.palette.close()
            return
        items: list[dict] = []

        def action(label: str, sub: str, hint: str, glyph: str, run) -> None:
            items.append({"category": "Actions", "label": label, "sub": sub,
                          "hint": hint, "glyph": glyph, "run": run})

        action("New tab", "fresh session in the current directory", "⌃⇧T", "+",
               lambda: self.add_tab())
        action("New window", "separate window, shared session list", "⌃⇧N", "◫",
               self.new_window)
        action("Detach current tab", "pop it into its own window, agent keeps running",
               "⌃⇧D", "⧉", lambda: self.detach_tab())
        action("Compose prompt", "multi-line prompt, sent as one paste", "⌃⇧⏎", "✎",
               self.open_composer)
        action("Broadcast prompt", "send one prompt to every open tab", "⌃⇧B", "⇉",
               self.broadcast_prompt)
        action("Theme maker", "build a palette with a live terminal preview", "⌃⇧M", "◑",
               self.open_theme_maker)
        action("Settings", "fonts, contrast, cursor, agent notifications", "⌃,", "⚙",
               self.open_settings)
        action("Zen mode", "hide the chrome, keep the grid", "⌃⇧Z", "◇",
               self.toggle_zen)
        action("Shuffle theme", "roll a random palette", "", "⤨",
               self.shuffle_theme)
        action("Launch agent", "pick an AI coding agent", "⌃⇧A", "✳",
               self.open_agent_palette)
        action("Export transcript", "save this tab's scrollback as markdown", "⌃⇧S", "⭳",
               lambda: self.export_transcript())
        action("Search scrollback", "regex search inside the terminal", "⌃⇧F", "⌕",
               self.open_search)
        action("Paste clipboard image", "saves the image and pastes its path", "⌃⇧I", "▣",
               self.paste_image)
        action("Duplicate tab", "same folder, fresh session", "", "⧉",
               lambda: self.duplicate_tab())
        action("Rename tab", "give this session a name", "F2", "✏",
               lambda: self.rename_tab())

        for slug, theme in self.themes.items():
            items.append({
                "category": "Themes", "label": theme.name,
                "sub": slug, "glyph": "◈",
                "keywords": "theme colors palette " + slug,
                "run": lambda slug=slug: self.apply_theme(slug),
                "hint": "active" if slug == self.theme.slug else "",
            })

        for agent in available_agents(self.cfg.get("agents", {}).get("custom")):
            items.append({
                "category": "Agents", "label": f"Launch {agent['name']}",
                "sub": agent.get("note") or agent["command"],
                "glyph": agent["glyph"], "keywords": "agent ai " + agent["id"],
                "run": lambda agent=agent: self.launch_agent(agent),
            })

        for session in self._orphan_sessions():
            items.append({
                "category": "Sessions", "label": session["title"],
                "sub": f"{session['name']} · {session['cwd']}" +
                       (" · attached elsewhere" if session["attached"] else ""),
                "glyph": "▤", "keywords": "reattach session " + session["name"],
                "run": lambda name=session["name"], title=session["title"]:
                    self.add_tab(session=name, title=title, restored=True),
            })

        self.palette.open(items, "Search actions, themes, agents, sessions…", query)

    def open_agent_palette(self) -> None:
        items = []
        for agent in available_agents(self.cfg.get("agents", {}).get("custom")):
            items.append({
                "category": "Agents", "label": agent["name"],
                "sub": f"{agent['command']}  ·  {agent.get('note', '')}",
                "glyph": agent["glyph"], "keywords": "agent " + agent["id"],
                "run": lambda agent=agent: self.launch_agent(agent),
            })
        if not items:
            self.toast.show_message("No AI agents found on PATH")
            return
        self.palette.open(items, "Launch an AI coding agent…")

    def launch_agent(self, agent: dict) -> None:
        tab = self.current_tab()
        cwd = tab.cwd if tab else self._default_cwd()
        created = self.add_tab(cwd=cwd, command=agent["command"], title=agent["name"],
                               auto_title=False)
        if created:
            self.toast.show_message(f"Launching {agent['name']} in a persistent tab")

    def _orphan_sessions(self) -> list[dict]:
        open_names = {tab.session for tab in self.tabs}
        found = []
        for session in self.tmux.sessions():
            name = session["name"]
            if not name.startswith("inky-") or name in open_names:
                continue
            meta = self.state.session(name)
            found.append({
                "name": name,
                "title": meta.get("title") or name,
                "cwd": meta.get("cwd") or "?",
                "attached": session["attached"],
            })
        return found

    # -- theme ---------------------------------------------------------
    def apply_theme(self, slug: str, persist: bool = True) -> None:
        theme = pick(self.themes, slug)
        self.theme = theme
        self.cfg["theme"] = theme.slug
        if persist:
            config.save_config(self.cfg)
        screen = Gdk.Screen.get_default()
        provider = Gtk.CssProvider()
        try:
            data = build_css(theme, self.cfg)
            provider.load_from_data(data.encode("utf-8"))
            if self._css_provider is not None:
                try:
                    Gtk.StyleContext.remove_provider_for_screen(screen, self._css_provider)
                except Exception:
                    pass
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            self._css_provider = provider
        except Exception as exc:  # pragma: no cover
            print(f"inky: stylesheet error: {exc}", file=sys.stderr)
        # Markup carries theme colours, so the cached strings are stale now.
        self._status_cache.clear()
        for tab in self.tabs:
            tab.terminal.apply_theme(theme)
            tab._visual_key = None
        settings = getattr(self, "_settings", None)
        if settings is not None:
            settings.set_theme(theme)
        self.refresh_status()

    def cycle_theme(self, delta: int = 1) -> None:
        slugs = list(self.themes)
        try:
            index = slugs.index(self.theme.slug)
        except ValueError:
            index = 0
        self.apply_theme(slugs[(index + delta) % len(slugs)])

    def _zoom(self, delta: float) -> None:
        size = float(self.cfg["font"].get("size", 11.5)) + delta
        self.cfg["font"]["size"] = max(6.0, min(28.0, size))
        for tab in self.tabs:
            tab.terminal.apply_font()
        self.toast.show_message(f"Font size {self.cfg['font']['size']:.1f} pt")

    # -- status --------------------------------------------------------
    def _display_cwd(self, cwd: str) -> str:
        if not cwd:
            return "~"
        if cwd == HOME:
            return "~"
        if cwd.startswith(HOME + "/"):
            return "~" + cwd[len(HOME):]
        return cwd

    def _title_for_cwd(self, cwd: str) -> str:
        if not cwd or cwd == HOME or cwd.rstrip("/") == HOME:
            return "~"
        return os.path.basename(cwd.rstrip("/")) or "shell"

    def _git_branch(self, cwd: str) -> str:
        if not cwd or not os.path.isdir(cwd):
            return ""
        now = time.time()
        cached = self._branch_cache.get(cwd)
        if cached and now - cached[0] < 6:
            return cached[1]
        branch = ""
        try:
            proc = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=0.6,
            )
            if proc.returncode == 0:
                branch = proc.stdout.strip()
        except Exception:
            branch = ""
        self._branch_cache[cwd] = (now, branch)
        return branch

    def _set_cached(self, key: str, value: str, apply) -> bool:
        """Run ``apply`` only when a display string really changed.

        Pango layouts and GTK relayouts are the expensive half of a status
        refresh; skipping identical strings keeps an idle window idle.
        """
        if self._status_cache.get(key) == value:
            return False
        self._status_cache[key] = value
        apply(value)
        return True

    def refresh_status(self, hover_uri: str = "") -> None:
        tab = self.current_tab()
        if not tab:
            return
        muted = self.theme.ui["muted"]
        if hover_uri and self.show_statusbar:
            markup = (
                f'<span foreground="{self.theme.accent}">↗ '
                f'{GLib.markup_escape_text(ellipsize_middle(hover_uri, 60))}</span>'
            )
            self._set_cached("left", markup, self.info_left.set_markup)
            self._set_cached("left-tip", hover_uri, self.info_left.set_tooltip_text)
        else:
            cwd = GLib.markup_escape_text(ellipsize_middle(self._display_cwd(tab.cwd), 52))
            branch = self._git_branch(tab.cwd)
            markup = f'<span foreground="{muted}">{cwd}</span>'
            if branch:
                markup += (f'   <span foreground="{self.theme.accent}">⎇ '
                           f'{GLib.markup_escape_text(branch)}</span>')
            self._set_cached("left", markup, self.info_left.set_markup)
            self._set_cached("left-tip", tab.cwd, self.info_left.set_tooltip_text)
        if tab.agent:
            now = time.time()
            state = "working" if tab.working(now) else "waiting"
            markup = (
                f'<span foreground="{tab.agent.color}">{tab.agent.glyph} '
                f'{tab.agent.name}</span>'
                f'<span foreground="{muted}">  {state} · '
                f'{human_seconds(tab.agent.runtime)}</span>'
            )
            self._set_cached("agent", markup, self.info_agent.set_markup)
            self.info_agent.set_visible(True)
        else:
            if self._status_cache.get("agent") != "":
                self._status_cache["agent"] = ""
                self.info_agent.set_markup("")
            self.info_agent.set_visible(False)
        title = f"{tab.title} — Inky"
        if self._status_cache.get("window-title") != title:
            self._status_cache["window-title"] = title
            self.set_title(title)
        escaped = GLib.markup_escape_text(tab.title)
        where = GLib.markup_escape_text(self._display_cwd(tab.cwd))
        if tab.command and where and where != tab.title:
            # Title bar carries the name; the folder rides behind it, muted.
            header = f'{escaped}<span foreground="{muted}">   {where}</span>'
        else:
            header = escaped
        self._set_cached("header", header, self.header_title.set_markup)
        self._set_cached("header-tip", f"{tab.title} — {tab.cwd}",
                         self.header_title.set_tooltip_text)

    # -- runtime loop --------------------------------------------------
    def _poll(self) -> bool:
        if self._closing or not self.tabs:
            return True
        now = time.time()
        panes = self.tmux.panes()
        touched = False
        for tab in list(self.tabs):
            if tab.closed:
                continue
            pane = panes.get(tab.session)
            if not pane:
                tab.missing += 1
                if tab.missing >= 3:
                    self._session_lost(tab)
                continue
            tab.missing = 0
            if pane.get("path"):
                tab.cwd = pane["path"]

            # One process walk answers both questions: which agent is running
            # and what the foreground program is called.
            hit, command = scan(pane.get("pid") or 0)
            # GNOME Terminal naming: the running program wins (opencode,
            # nvim, ssh…), the working directory names a bare shell.
            tab.command = hit.name if hit else command
            if tab.auto_title:
                tab.set_title(tab.command or self._title_for_cwd(tab.cwd), auto=True)
            if hit:
                if tab.agent is None:
                    tab.agent_seen_at = now
                    tab.last_screen = None
                    tab.last_change = now
                tab.agent = hit
                screen = self.tmux.capture_screen(tab.session)
                digest = hash(screen)
                if digest != tab.last_screen:
                    tab.last_screen = digest
                    tab.last_change = now
                    tab.waiting_since = 0.0
                elif now - tab.last_change > 4 and not tab.waiting_since:
                    tab.waiting_since = now
                self._maybe_notify_waiting(tab, now)
            else:
                if tab.agent is not None:
                    self._agent_finished(tab, now)
                tab.agent = None
                tab.agent_seen_at = 0.0
                tab.waiting_since = 0.0
                tab.last_screen = None
            touched = tab.update_visual() or touched

        # Repaint the chrome only when the tab actually changed; the runtime
        # readout gets one lazy refresh every fourth poll so it still ticks.
        self._status_tick += 1
        if touched or self._status_tick % 4 == 0:
            self.refresh_status()
        return True

    def _agent_finished(self, tab: Tab, now: float) -> None:
        hit = tab.agent
        if hit is None:
            return
        runtime = max(0.0, now - tab.agent_seen_at) if tab.agent_seen_at else hit.runtime
        tab.done = True
        if runtime < 15 or not self.cfg["behavior"].get("notify_agent_done", True):
            return
        self._notify(
            f"{hit.name} finished",
            f"“{tab.title}” · {human_seconds(runtime)}",
        )

    def _maybe_notify_waiting(self, tab: Tab, now: float) -> None:
        if not tab.waiting_since or not tab.agent:
            return
        if not self.cfg["behavior"].get("notify_agent_waiting", True):
            return
        if now - tab.waiting_since < 12:
            return
        if tab.agent.runtime < 20:
            return
        if now - tab.last_wait_notice < 240:
            return
        if self.is_active() and self.current_tab() is tab:
            return
        tab.last_wait_notice = now
        self._notify(
            f"{tab.agent.name} is waiting",
            f"“{tab.title}” · no output for {human_seconds(now - tab.waiting_since)}",
        )

    def _notify(self, title: str, body: str) -> None:
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Inky", "-i", str(config.LOGO),
                 title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _session_lost(self, tab: Tab) -> None:
        if tab.closed:
            return
        tab.closed = True
        if tab in self.tabs:
            self.tabs.remove(tab)
            self.tabbox.remove(tab.widget)
            self.stack.remove(tab.terminal)
            try:
                tab.terminal.destroy()
            except Exception:
                pass
            self.state.forget_session(tab.session)
        self._sync_tabstrip()
        if not self.tabs:
            self.add_tab()
            self.toast.show_message("Session ended — opened a fresh one")
        else:
            self.activate(min(self.current_index(), len(self.tabs) - 1))
            self.toast.show_message("Session ended")
        self._save_window()

    def _on_terminal_exit(self, tab: Tab, _status: int) -> None:
        if tab.closed or self._closing:
            return
        GLib.idle_add(self._session_lost_idle, tab)

    def _session_lost_idle(self, tab: Tab) -> bool:
        if tab.closed:
            return False
        if self.tmux.has_session(tab.session) and tab.respawns < 3:
            # tmux client detached without the session dying: reattach quietly.
            tab.respawns += 1
            tab.terminal.spawn()
            return False
        self._session_lost(tab)
        return False

    # -- misc ----------------------------------------------------------
    def _on_tab_strip_scroll(self, _widget, event) -> bool:
        if not self.tabs:
            return False
        if event.direction == Gdk.ScrollDirection.UP:
            step = -1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            step = 1
        else:
            return False
        self.activate((self.current_index() + step) % len(self.tabs))
        return True

    def _default_cwd(self) -> str:
        tab = self.current_tab()
        if tab and tab.cwd and os.path.isdir(tab.cwd):
            return tab.cwd
        return os.path.expanduser("~")

    def _save_window(self) -> None:
        try:
            tabs = [{"session": tab.session, "title": tab.title,
                     "cwd": tab.cwd, "auto": tab.auto_title}
                    for tab in self.tabs]
            self.state.save_window(tabs, self.current_index(), self._geometry())
        except Exception:
            pass

    def _fatal(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
            text="Inky",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        self.destroy()

    def _on_delete(self, *_args) -> bool:
        self._closing = True
        self._save_window()
        for tab in self.tabs:
            tab.closed = True
        return False

    # -- shortcuts -----------------------------------------------------
    def handle_shortcut(self, event) -> bool:
        state = event.state
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        key = Gdk.keyval_name(event.keyval) or ""
        low = key.lower()

        if ctrl and key in ("Tab", "ISO_Left_Tab"):
            if shift:
                self.activate((self.current_index() - 1) % len(self.tabs))
            else:
                self.activate((self.current_index() + 1) % len(self.tabs))
            return True

        if ctrl and shift:
            if low == "t":
                self.add_tab()
                return True
            if low == "w":
                tab = self.current_tab()
                if tab:
                    self.close_tab(tab)
                return True
            if low == "n":
                self.new_window()
                return True
            if low == "d":
                self.detach_tab()
                return True
            if low == "k":
                self.open_palette()
                return True
            if low == "p":
                self.open_theme_picker()
                return True
            if low == "m":
                self.open_theme_maker()
                return True
            if low == "z":
                self.toggle_zen()
                return True
            if low == "b":
                self.broadcast_prompt()
                return True
            if low == "f":
                self.open_search()
                return True
            if low == "a":
                self.open_agent_palette()
                return True
            if low == "s":
                self.export_transcript()
                return True
            if low in ("e", "return", "kp_enter"):
                self.open_composer()
                return True
            if low == "c":
                self.copy_selection()
                return True
            if low == "v":
                self.paste_clipboard()
                return True
            if low == "i":
                self.paste_image()
                return True
            if low == "q":
                self.close()
                return True
            if low in ("page_up", "page_down"):
                tab = self.current_tab()
                if tab:
                    self.move_tab(tab, -1 if low == "page_up" else 1)
                return True
            if low in ("plus", "equal", "kp_add"):
                self._zoom(0.5)
                return True
            if low in ("minus", "kp_subtract"):
                self._zoom(-0.5)
                return True
            if low == "0":
                self.cfg["font"]["size"] = config.DEFAULTS["font"]["size"]
                for tab in self.tabs:
                    tab.terminal.apply_font()
                return True

        if ctrl and key in ("comma", "less"):
            self.open_settings()
            return True

        if alt and low.isdigit():
            index = int(low) - 1
            if 0 <= index < len(self.tabs):
                self.activate(index)
                return True

        if key == "F2":
            self.rename_tab()
            return True
        if key == "Escape":
            if self.palette.is_open():
                self.palette.close()
                return True
            if self.zen:
                self.toggle_zen()
                return True
        return False
