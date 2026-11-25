"""Command line entry point for Inky."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from . import __version__
from . import config
from .theme import load_themes, pick


def _require_gui():
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import Gtk, Vte  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        print(
            "inky: GTK3 + VTE 2.91 are required.\n"
            "  Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91\n"
            f"  ({exc})",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _print_sessions() -> int:
    from .tmux import Tmux

    tmux = Tmux()
    state = config.State()
    rows = [s for s in tmux.sessions() if s["name"].startswith("inky-")]
    if not rows:
        print("No Inky sessions are running.")
        return 0
    print(f"{'SESSION':<18} {'AGE':>8}  {'ATTACHED':<8} {'CWD':<34} TITLE")
    import time

    for row in rows:
        meta = state.session(row["name"])
        age = int(time.time() - (row["created"] or time.time()))
        print(
            f"{row['name']:<18} {age // 60:>6}m   "
            f"{'yes' if row['attached'] else 'no':<8} "
            f"{(meta.get('cwd') or '?'):<34} {meta.get('title') or ''}"
        )
    return 0


def _print_themes() -> int:
    themes = load_themes()
    active = pick(themes, config.load_config().get("theme")).slug
    for slug, theme in themes.items():
        marker = "●" if slug == active else " "
        print(f" {marker} {slug:<18} {theme.name}")
    return 0


def _kill(args: list[str]) -> int:
    from .tmux import Tmux

    tmux = Tmux()
    state = config.State()
    targets = [s for s in tmux.sessions() if s["name"].startswith("inky-")]
    if not args:
        print("usage: inky kill <session> | --all")
        return 2
    if args[0] in ("--all", "-a"):
        for row in targets:
            tmux.kill_session(row["name"])
            state.forget_session(row["name"])
        print(f"Killed {len(targets)} session(s).")
        return 0
    name = args[0]
    if not name.startswith("inky-"):
        name = f"inky-{name}"
    if not tmux.has_session(name):
        print(f"No such session: {name}")
        return 1
    tmux.kill_session(name)
    state.forget_session(name)
    print(f"Killed {name}")
    return 0


def _set_theme(arg: str) -> int:
    themes = load_themes()
    if arg not in themes:
        print(f"Unknown theme {arg!r}. Try: inky themes")
        return 1
    cfg = config.load_config()
    cfg["theme"] = arg
    config.save_config(cfg)
    print(f"Default theme set to {themes[arg].name} ({arg})")
    return 0


def _doctor() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, fn):
        try:
            detail = fn()
            checks.append((label, True, detail if isinstance(detail, str) else "ok"))
        except Exception as exc:
            checks.append((label, False, str(exc)))

    def gtk_check():
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import Gtk, Vte

        return f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, VTE {Vte.get_minor_version()}"

    def tmux_check():
        out = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "tmux"

    def psutil_check():
        import psutil

        return f"psutil {psutil.__version__}"

    def font_check():
        out = subprocess.run(
            ["fc-match", "-f", "%{family}", "JetBrains Mono"],
            capture_output=True, text=True, timeout=5,
        )
        family = (out.stdout or "").split(",")[0]
        return family or "fallback"

    def clipboard_check():
        tools = [tool for tool in ("xclip", "wl-copy") if shutil.which(tool)]
        return ", ".join(tools) or "none (mouse selection copy disabled)"

    def notify_check():
        return shutil.which("notify-send") or "missing (agent notifications disabled)"

    check("GTK/VTE", gtk_check)
    check("tmux", tmux_check)
    check("agent detection", psutil_check)
    check("nerd font", font_check)
    check("clipboard", clipboard_check)
    check("notifications", notify_check)
    check("config dir", lambda: str(config.CONFIG_DIR))
    check("transcripts", lambda: str(config.TRANSCRIPT_DIR))

    print(f"Inky {__version__} diagnostics\n")
    for label, ok, detail in checks:
        print(f" {'✓' if ok else '✗'} {label:<18} {detail}")
    failures = [c for c in checks if not c[1]]
    print()
    if failures:
        print(f"{len(failures)} check(s) need attention.")
        return 1
    print("Everything looks good.")
    return 0


def _selftest() -> int:
    _require_gui()
    from gi.repository import GLib, Gtk

    from .tmux import Tmux
    from .window import InkyWindow

    try:
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    except Exception:
        pass

    cfg = config.load_config()
    state = config.State()
    window = InkyWindow(cfg, state)
    window.show_all()
    window.add_tab(cwd="/tmp", title="selftest")
    # Auto-named tab: the running command should become the tab's name.
    window.add_tab(cwd="/tmp", command="sleep 60")
    result = {"ok": False}

    def finish() -> bool:
        themes = list(window.themes)
        for slug in themes:
            window.apply_theme(slug, persist=False)
        window.apply_theme(themes[0], persist=False)
        window.export_transcript(window.tabs[0])
        window._poll()
        alive = all(window.tmux.has_session(tab.session) for tab in window.tabs)
        transcript_ok = any(config.TRANSCRIPT_DIR.glob("*.md"))
        named = [tab.title for tab in window.tabs if tab.auto_title]
        naming_ok = "sleep" in named
        # Reordering keeps every tab and moves the active index with it.
        before = [tab.session for tab in window.tabs]
        window.move_tab(window.tabs[0], 1)
        order_ok = ([tab.session for tab in window.tabs] == before[1:2] + before[:1] + before[2:])
        window.move_tab(window.tabs[1], -1)
        order_ok = order_ok and [tab.session for tab in window.tabs] == before

        # Settings and the theme maker must build against a live window, and the
        # maker must offer a complete 16-slot palette.
        from .dialogs import SettingsDialog, ThemeMaker

        settings = SettingsDialog(window, window.cfg, window.theme, window.themes)
        maker = ThemeMaker(window, window.themes, window.theme.slug)
        palette_ok = len(maker._collect()["palette"]) == 16
        dialogs_ok = settings.notebook.get_n_pages() == 3 and palette_ok
        settings.destroy()
        maker.destroy()
        window.toggle_zen()
        zen_ok = not window.tabrow.get_visible()
        window.toggle_zen()
        zen_ok = zen_ok and window.tabrow.get_visible()

        result["ok"] = (len(window.tabs) >= 3 and alive and transcript_ok
                        and len(themes) >= 5 and naming_ok and order_ok
                        and dialogs_ok and zen_ok)
        print(
            f"SELFTEST {'OK' if result['ok'] else 'FAILED'} "
            f"tabs={len(window.tabs)} themes={len(themes)} alive={alive} "
            f"transcript={transcript_ok} naming={naming_ok} order={order_ok} "
            f"dialogs={dialogs_ok} zen={zen_ok} titles={named}"
        )
        tmux = Tmux()
        for tab in window.tabs:
            tmux.kill_session(tab.session)
        Gtk.main_quit()
        return False

    GLib.timeout_add(1400, finish)
    Gtk.main()
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="inky",
        description="Inky — ink-fast terminal with persistent sessions and agent superpowers.",
    )
    parser.add_argument("command", nargs="?", default="",
                        help="ls | themes | theme <slug> | kill <session> | doctor | attach <session>")
    parser.add_argument("args", nargs="*", help="arguments for the command")
    parser.add_argument("--attach", metavar="SESSION", help="open a window attached to a session")
    parser.add_argument("--title", help="title for the attached tab")
    parser.add_argument("--theme", help="theme slug for this window")
    parser.add_argument("--selftest", action="store_true", help="run an internal smoke test")
    parser.add_argument("--version", action="version", version=f"Inky {__version__}")
    options = parser.parse_args(argv)

    command = options.command
    if options.selftest:
        return _selftest()
    if command in ("ls", "list"):
        return _print_sessions()
    if command in ("themes", "theme-list"):
        return _print_themes()
    if command == "theme":
        if not options.args:
            print("usage: inky theme <slug>")
            return 2
        return _set_theme(options.args[0])
    if command == "kill":
        return _kill(options.args)
    if command == "doctor":
        return _doctor()
    if command == "attach":
        if not options.args:
            print("usage: inky attach <session>")
            return 2
        options.attach = options.args[0]
        command = ""
    if command == "new":
        command = ""
    if command == "version":
        print(f"Inky {__version__}")
        return 0
    if command == "help":
        parser.print_help()
        return 0
    if command:
        print(f"Unknown command: {command}\nTry: inky --help", file=sys.stderr)
        return 2
    return _run_gui(options)


def _run_gui(options) -> int:
    _require_gui()
    if not shutil.which("tmux"):
        print(
            "inky: tmux is required for persistent sessions.\n"
            "  sudo apt install tmux",
            file=sys.stderr,
        )
        return 1
    from gi.repository import GLib, Gtk

    from .tmux import Tmux, TmuxError
    from .window import InkyWindow

    GLib.set_prgname("inky")
    GLib.set_application_name("Inky")
    try:
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    except Exception:
        pass
    try:
        os.environ.setdefault("INKY_ROOT", str(config.ROOT))
        cfg = config.load_config()
        state = config.State()
        window = InkyWindow(cfg, state, attach_session=options.attach,
                            attach_title=options.title, theme_slug=options.theme)
        window.show_all()
        window.present()
        Gtk.main()
    except TmuxError as exc:
        print(f"inky: tmux error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
