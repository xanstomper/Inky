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
from . import profiles as profile_store  # noqa: E402
from .agents import AgentHit, available_agents, scan  # noqa: E402
from .composer import Composer  # noqa: E402
from .css import build_css  # noqa: E402
from .dialogs import SettingsDialog, ThemeMaker  # noqa: E402
from .palette import Palette  # noqa: E402
from .terminal import InkyTerminal  # noqa: E402
from .theme import Theme, load_themes, pick  # noqa: E402
from .tmux import Tmux, TmuxError  # noqa: E402
from .util import human_seconds, slugify, timestamp  # noqa: E402
from .widgets import SearchBar, ThemePicker, Toast  # noqa: E402

HOME = str(Path.home())

# Programs that are just a prompt. Closing a tab running one of these is not
# worth a dialog; closing a tab mid-build is.
SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh",
          "tmux", "tmux: client", "login"}

# ``add_tab(profile=...)`` unset means "the window's default profile".
_DEFAULT_PROFILE = object()


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
        self.profile: profile_store.Profile | None = None
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
        # Notebook tabs are sized by their label, so this is the tab's
        # natural width; the title ellipsizes past its cap instead of the
        # tab growing without bound.
        self.widget.set_size_request(128, 36)
        self.widget.set_valign(Gtk.Align.FILL)
        _set_pointer(self.widget)

        # Nothing here may expand: a notebook hands spare width to expanding
        # labels, which is what turned the tabs back into window-wide plates.
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_halign(Gtk.Align.FILL)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        row.set_valign(Gtk.Align.CENTER)
        row.set_halign(Gtk.Align.FILL)
        row.set_margin_start(10)
        row.set_margin_end(4)

        self.glyph = Gtk.Label(label="◆")
        self.glyph.get_style_context().add_class("tab-glyph")
        self.glyph.set_size_request(12, -1)
        row.pack_start(self.glyph, False, False, 0)

        self.title_label = Gtk.Label(label=title)
        self.title_label.get_style_context().add_class("tab-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(32)
        self.title_label.set_xalign(0.0)
        row.pack_start(self.title_label, False, False, 0)

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
            # Let the notebook see the press: it owns tab selection and the
            # drag-to-reorder gesture. `switch-page` updates the window state.
            return False
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
        bits = [self.profile.name] if self.profile is not None else []
        if self.agent:
            state = "working" if self.working() else "waiting"
            bits += [self.agent.name, state]
        elif self.command:
            bits.append(self.command)
        else:
            bits.append("shell")
        bits.append(where)
        return " · ".join(bits)


class InkyWindow(Gtk.Window):
    def __init__(self, cfg: dict, state, attach_session: str | None = None,
                 attach_title: str | None = None, theme_slug: str | None = None,
                 profile: str | None = None, start_cwd: str | None = None,
                 start_command: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.tmux = Tmux()
        self.themes = load_themes()
        self.profiles = profile_store.load_profiles()
        self.theme: Theme = pick(self.themes, theme_slug or cfg.get("theme"))
        # `inky open --cwd/--command/--profile` means one fresh tab, not a
        # restore of the last layout.
        self.start_profile = profile
        self.start_cwd = start_cwd
        self.start_command = start_command
        self.tabs: list[Tab] = []
        self._css_provider = None
        self._status_cache: dict[str, str] = {}
        self._poll_id = 0
        self._status_tick = 0
        self._closing = False
        self._switching = False
        self.zen = False

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

        # Real GtkNotebook tabs, the way GNOME Terminal does them: labels size
        # to their content, the strip scrolls when they overflow, and tabs can
        # be dragged into a new order. The custom strip drew equal plates that
        # split the window in two, which reads as a titlebar, not as tabs.
        self.notebook = Gtk.Notebook()
        self.notebook.get_style_context().add_class("inkytabs")
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.connect("switch-page", self._on_switch_page)
        self.notebook.connect("page-reordered", self._on_page_reordered)

        self.search = SearchBar(self._do_search, self._search_closed)
        self.search.set_visible(False)

        self.term_overlay = Gtk.Overlay()
        self.term_overlay.add(self.notebook)
        self.term_overlay.add_overlay(self.search)
        self.term_overlay.set_hexpand(True)
        self.term_overlay.set_vexpand(True)
        padding = max(0, int(self.cfg["window"].get("padding", 0)))
        self.term_overlay.set_margin_start(padding)
        self.term_overlay.set_margin_end(padding)
        self.term_overlay.set_margin_top(padding)
        self.term_overlay.set_margin_bottom(padding)
        self.term_overlay.get_style_context().add_class("term-area")

        root.pack_start(self.term_overlay, True, True, 0)

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

        if self.start_cwd or self.start_command or self.start_profile is not None:
            self.add_tab(cwd=self.start_cwd, command=self.start_command,
                         profile=(self.start_profile if self.start_profile is not None
                                  else _DEFAULT_PROFILE))
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
                         auto_title=bool(entry.get("auto", True)),
                         profile=entry.get("profile") or "")
            restored += 1
        if restored:
            self.activate(min(int(window_state.get("active", 0)), len(self.tabs) - 1))
        else:
            self.add_tab()

    def add_tab(self, cwd: str | None = None, command: str | None = None,
                title: str | None = None, session: str | None = None,
                restored: bool = False, auto_title: bool | None = None,
                profile=_DEFAULT_PROFILE) -> Tab | None:
        slug = ((self.cfg.get("profile") or "") if profile is _DEFAULT_PROFILE
                else (profile or ""))
        bundle = self.profiles.get(slug) if slug else None
        cwd = cwd or (bundle.start_cwd(self._default_cwd()) if bundle
                      else self._default_cwd())
        # A profile or a launcher command is typed into the shell rather than
        # made the session's own process: that keeps tab naming, agent
        # detection and the exit paths working exactly as they do normally.
        startup = command or (bundle.command if bundle else "")
        session = session or self.tmux.new_id()
        if not restored:
            try:
                self.tmux.new_session(session, cwd, self.cfg.get("shell") or None)
            except TmuxError as exc:
                self.toast.show_message(f"Could not start session: {exc}")
                return None

        theme = (pick(self.themes, bundle.theme)
                 if bundle is not None and bundle.theme else self.theme)
        terminal = InkyTerminal(session, self.cfg, theme, self.tmux, cwd=cwd,
                                profile=bundle)
        display = title or self._title_for_cwd(cwd)
        auto = auto_title if auto_title is not None else title is None
        tab = Tab(self, session, terminal, display, auto_title=auto)
        tab.cwd = cwd
        tab.profile = bundle
        tab.terminal.connect("exited", lambda _t, status, tab=tab: self._on_terminal_exit(tab, status))
        tab.terminal.term.connect("button-press-event", self._on_terminal_click)

        self.tabs.append(tab)
        # Widgets created after the window was shown start hidden, so show the
        # page and its tab label explicitly before the notebook can switch to
        # them.
        terminal.show_all()
        tab.widget.show_all()
        self.notebook.append_page(terminal, tab.widget)
        self.notebook.set_tab_reorderable(terminal, True)
        self._sync_tabstrip()
        self.activate(len(self.tabs) - 1)

        self.state.remember_session(session, title=tab.title, cwd=cwd,
                                    created=time.time(),
                                    profile=bundle.slug if bundle else "")
        if startup:
            GLib.timeout_add(320, self._send_after_spawn, session, startup)
        self._save_window()
        return tab

    def _send_after_spawn(self, session: str, command: str) -> bool:
        self.tmux.send_text(session, command, submit=True)
        return False

    def _sync_tabstrip(self) -> None:
        # The strip is always there: one tab or ten, it owns the second row.
        self.notebook.show_all()

    def current_index(self) -> int:
        tab = self.current_tab()
        return self.tabs.index(tab) if tab else 0

    def current_tab(self) -> Tab | None:
        index = self.notebook.get_current_page()
        if 0 <= index < len(self.tabs):
            return self.tabs[index]
        return self.tabs[-1] if self.tabs else None

    def _on_switch_page(self, _notebook, _page, index: int) -> None:
        if self._switching:
            return
        self.activate(index)

    def _on_page_reordered(self, notebook, _child, _index: int) -> None:
        """Dragging a tab is a real drag now; keep `tabs` in notebook order."""
        order = []
        for position in range(notebook.get_n_pages()):
            page = notebook.get_nth_page(position)
            tab = next((t for t in self.tabs if t.terminal is page), None)
            if tab is not None:
                order.append(tab)
        if len(order) == len(self.tabs):
            self.tabs = order
        self.activate(notebook.get_current_page())

    def activate(self, index: int) -> None:
        if not self.tabs:
            return
        index = max(0, min(index, len(self.tabs) - 1))
        tab = self.tabs[index]
        if self.notebook.get_current_page() != index:
            self._switching = True
            try:
                self.notebook.set_current_page(index)
            finally:
                self._switching = False
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
            response = self._ask_close(
                f"{agent.name} is running in “{tab.title}”.",
                "Closing this tab stops the agent. Detach it instead to keep it "
                "running in its own window.",
            )
            if response == 42:
                self.detach_tab(tab)
                return
            if response != Gtk.ResponseType.OK:
                return
        elif (confirm and kill and self.cfg["behavior"].get("confirm_process_close", True)
                and self._busy_command(tab)):
            busy = self._busy_command(tab)
            response = self._ask_close(
                f"“{busy}” is still running in “{tab.title}”.",
                "Closing this tab ends it. Detach it instead to keep the process "
                "in its own window.",
            )
            if response == 42:
                self.detach_tab(tab)
                return
            if response != Gtk.ResponseType.OK:
                return

        tab.closed = True
        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        page = self.notebook.page_num(tab.terminal)
        if page >= 0:
            self.notebook.remove_page(page)
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

    def _ask_close(self, headline: str, secondary: str) -> int:
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL, text=headline,
        )
        dialog.format_secondary_text(secondary)
        dialog.add_button("Detach window", 42)
        response = dialog.run()
        dialog.destroy()
        return response

    @staticmethod
    def _busy_command(tab: "Tab") -> str:
        """The foreground program, or "" when the tab is only a shell prompt."""
        name = (tab.command or "").strip()
        if not name:
            return ""
        return "" if name.split()[0] in SHELLS else name

    def move_tab(self, tab: "Tab", delta: int) -> None:
        """Slide a tab along the bar; the active tab follows it."""
        if tab not in self.tabs:
            return
        index = self.tabs.index(tab)
        target = max(0, min(index + delta, len(self.tabs) - 1))
        if target == index:
            return
        self.tabs.insert(target, self.tabs.pop(index))
        self.notebook.reorder_child(tab.terminal, target)
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
        profiles_item = self.profile_menu_item()
        if profiles_item is not None:
            menu.append(profiles_item)
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

    def profile_menu_item(self) -> Gtk.MenuItem | None:
        """New-tab-with-profile, the way GNOME Terminal lists profiles in its menu."""
        if not self.profiles:
            return None
        item = Gtk.MenuItem(label="New tab with profile")
        submenu = Gtk.Menu()
        for slug, profile in self.profiles.items():
            submenu.append(self._menu_item(
                profile.name, lambda slug=slug: self.add_tab(profile=slug)))
        submenu.append(Gtk.SeparatorMenuItem())
        submenu.append(self._menu_item("Manage profiles…", self.open_settings_profiles))
        item.set_submenu(submenu)
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
                                self.open_theme_maker, self._settings_action,
                                profiles=self.profiles)
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

    def _settings_action(self, kind: str, payload: str | None = None) -> None:
        if kind == "zen":
            self.toggle_zen()
        elif kind == "broadcast":
            self.broadcast_prompt()
        elif kind == "shuffle":
            self.shuffle_theme()
        elif kind == "folder":
            self._open_themes_folder()
        elif kind == "profiles-folder":
            self._open_folder(config.PROFILES_DIR)
        elif kind == "profile-open" and payload:
            self.add_tab(profile=payload)
        elif kind == "profile-delete" and payload:
            if profile_store.delete_profile(payload):
                self._reload_profiles()
                self.toast.show_message(f"Profile deleted · {payload}")
        elif kind == "profile-snapshot":
            self.snapshot_profile()

    def _reload_profiles(self) -> None:
        self.profiles = profile_store.load_profiles()
        settings = getattr(self, "_settings", None)
        if settings is not None:
            settings.refresh_profiles(self.profiles)

    def open_settings_profiles(self) -> None:
        self.open_settings()
        settings = getattr(self, "_settings", None)
        if settings is not None:
            settings.show_page("profiles")

    def snapshot_profile(self) -> None:
        """Save the current tab's font, palette and command as a profile file."""
        tab = self.current_tab()
        if tab is None:
            return
        dialog = Gtk.Dialog(title="New profile", transient_for=self)
        dialog.set_modal(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save profile", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_default_size(380, -1)
        entry = Gtk.Entry()
        entry.set_text(tab.title)
        entry.set_activates_default(True)
        for side in ("top", "bottom", "start", "end"):
            getattr(entry, f"set_margin_{side}")(12)
        font = tab.terminal.font()
        note = Gtk.Label(
            label=f"{font.get('family', '')} {float(font.get('size', 0)):.1f} · "
                  f"{tab.terminal.theme.name} · {self._display_cwd(tab.cwd)}",
            xalign=0.0,
        )
        note.get_style_context().add_class("settings-help")
        note.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        for side in ("start", "end", "bottom"):
            getattr(note, f"set_margin_{side}")(12)
        area = dialog.get_content_area()
        area.add(entry)
        area.add(note)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip() or tab.title
            profile = profile_store.from_tab(
                name, font, tab.terminal.theme.slug, tab.cwd,
                self._busy_command(tab), self.profiles,
            )
            path = profile_store.save_profile(profile)
            self._reload_profiles()
            self.toast.show_message(f"Profile saved · {path.name}")
        dialog.destroy()
        self._focus_terminal()

    def _open_folder(self, folder: Path) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            Gtk.show_uri(None, folder.as_uri(), Gtk.get_current_event_time())
        except Exception:
            try:
                subprocess.Popen(["xdg-open", str(folder)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.toast.show_message(str(folder))

    def _open_themes_folder(self) -> None:
        self._open_folder(config.USER_THEMES)

    def toggle_zen(self) -> None:
        """Strip the chrome, keep the grid. The shortcut still gets you out."""
        self.zen = not self.zen
        self.notebook.set_show_tabs(not self.zen)
        for widget in (self.new_button, self.search_button):
            widget.set_visible(not self.zen)
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
        profiles_item = self.profile_menu_item()
        if profiles_item is not None:
            menu.append(profiles_item)
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

        for slug, profile in self.profiles.items():
            items.append({
                "category": "Profiles", "label": f"New tab · {profile.name}",
                "sub": profile.summary(), "glyph": "▤",
                "keywords": f"profile {slug} {profile.command} {profile.cwd}",
                "hint": "default" if slug == (self.cfg.get("profile") or "") else "",
                "run": lambda slug=slug: self.add_tab(profile=slug),
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
            tab.terminal.apply_theme(self._theme_for(tab))
            tab._visual_key = None
        settings = getattr(self, "_settings", None)
        if settings is not None:
            settings.set_theme(theme)
        self.refresh_status()

    def _theme_for(self, tab: Tab) -> Theme:
        """A tab launched with a profile keeps that profile's palette."""
        if tab.profile is not None and tab.profile.theme:
            return pick(self.themes, tab.profile.theme)
        return self.theme

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

    def refresh_status(self) -> None:
        tab = self.current_tab()
        if not tab:
            return
        muted = self.theme.ui["muted"]
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
    def _drain_requests(self) -> None:
        """Open tabs queued by ``inky open --tab`` from a script or an agent."""
        for request in config.take_requests():
            kwargs: dict = {}
            if request.get("profile"):
                kwargs["profile"] = request["profile"]
            for key in ("cwd", "command", "title"):
                if request.get(key):
                    kwargs[key] = request[key]
            tab = self.add_tab(**kwargs)
            if tab is not None:
                self.toast.show_message(f"Opened “{tab.title}” from the command line")

    def _poll(self) -> bool:
        if self._closing or not self.tabs:
            return True
        self._drain_requests()
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
            page = self.notebook.page_num(tab.terminal)
            if page >= 0:
                self.notebook.remove_page(page)
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
                     "cwd": tab.cwd, "auto": tab.auto_title,
                     "profile": tab.profile.slug if tab.profile else ""}
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
