"""Paths, defaults and on-disk state for Inky."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "assets" / "InkyLogo.png"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


CONFIG_DIR = _env_path("INKY_CONFIG_DIR", HOME / ".config" / "inky")
CACHE_DIR = _env_path("INKY_CACHE_DIR", HOME / ".cache" / "inky")
TRANSCRIPT_DIR = _env_path("INKY_TRANSCRIPTS", HOME / "inky-transcripts")
BUILTIN_THEMES = ROOT / "themes"
USER_THEMES = CONFIG_DIR / "themes"
PROFILES_DIR = CONFIG_DIR / "profiles"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
TMUX_CONF = CONFIG_DIR / "tmux.conf"
SOCKET = os.environ.get("INKY_SOCKET", "inky")
# Tab requests are one file each, so a script and a window never write the
# same file; they live in the cache because they are disposable.
REQUESTS_DIR = CACHE_DIR / "requests"

DEFAULTS = {
    "theme": "inky",
    # Slug of the profile new tabs start with; empty means the plain window
    # settings.
    "profile": "",
    "font": {"family": "JetBrains Mono", "size": 11.5, "line_height": 1.06, "weight": 400},
    "ui_font": {"family": "Inter", "size": 10.0},
    "shell": "",
    "window": {"width": 1240, "height": 800, "padding": 0, "opacity": 1.0},
    "behavior": {
        # tmux owns the deep history (100k lines); VTE's own ring buffer only
        # has to cover what was printed before the first attach.
        "scrollback": 2000,
        "min_contrast": 3.0,
        "cursor_blink": True,
        "cursor_shape": "bar",
        "copy_on_select": False,
        "scroll_on_output": False,
        "scroll_on_keystroke": True,
        "mouse_autohide": True,
        "audible_bell": False,
        "notify_agent_done": True,
        "notify_agent_waiting": True,
        "confirm_agent_close": True,
        "confirm_process_close": True,
        "poll_interval": 1.4,
    },
    "agents": {"custom": {}},
}


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, CACHE_DIR, USER_THEMES, PROFILES_DIR, REQUESTS_DIR,
                 TRANSCRIPT_DIR, CACHE_DIR / "paste"):
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    data = {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    return deep_merge(DEFAULTS, data if isinstance(data, dict) else {})


def save_config(cfg: dict) -> None:
    ensure_dirs()
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def request_tab(profile: str = "", cwd: str = "", command: str = "",
                title: str = "") -> Path:
    """Ask a running Inky window to open a tab (``inky open --tab``).

    A request is its own file, so several scripts can queue at once and a
    window never has to take a lock or merge a shared document.
    """
    ensure_dirs()
    payload = {"profile": profile, "cwd": cwd, "command": command,
               "title": title, "at": time.time()}
    stamp = int(time.time() * 1000)
    path = REQUESTS_DIR / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def take_requests(max_age: float = 300.0) -> list[dict]:
    """Drain queued tab requests, dropping anything stale.

    A request older than ``max_age`` was meant for a window that was not
    running at the time; replaying it hours later would be a surprise.
    """
    ensure_dirs()
    now = time.time()
    out: list[dict] = []
    for path in sorted(REQUESTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(data, dict):
            continue
        try:
            age = now - float(data.get("at") or 0)
        except (TypeError, ValueError):
            age = max_age + 1
        if age <= max_age:
            out.append(data)
    return out


class State:
    """Best-effort registry of sessions and the last window layout.

    Multiple Inky windows share the file, so every save re-reads the current
    contents and only replaces the keys it owns.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_FILE
        self.data: dict = {"sessions": {}, "window": {}}
        self._load()

    def _load(self) -> None:
        ensure_dirs()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = raw
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        self.data.setdefault("sessions", {})
        self.data.setdefault("window", {})

    def _save(self) -> None:
        ensure_dirs()
        try:
            self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def session(self, name: str) -> dict:
        return dict(self.data["sessions"].get(name, {}))

    def remember_session(self, name: str, **fields) -> None:
        entry = dict(self.data["sessions"].get(name, {}))
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.data["sessions"][name] = entry
        self._save()

    def forget_session(self, name: str) -> None:
        if self.data["sessions"].pop(name, None) is not None:
            self._save()

    def save_window(self, tabs: list[dict], active: int, geometry: list[int] | None = None) -> None:
        window = {"tabs": tabs, "active": active}
        if geometry:
            window["geometry"] = geometry
        self.data["window"] = window
        self._save()

    def window(self) -> dict:
        return dict(self.data.get("window") or {})
