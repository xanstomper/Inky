"""AI coding agent registry and live process detection."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

try:  # psutil is optional; Inky degrades to no agent detection without it.
    import psutil
except Exception:  # pragma: no cover
    psutil = None

AGENTS = [
    {"id": "claude", "name": "Claude Code", "glyph": "✳", "color": "#e09a76",
     "command": "claude", "match": ["claude"], "note": "Anthropic"},
    {"id": "codebuff", "name": "Codebuff", "glyph": "⌬", "color": "#8fb0ff",
     "command": "codebuff", "match": ["codebuff"], "note": "Agentic coding"},
    {"id": "hermes", "name": "Hermes", "glyph": "☿", "color": "#c9a0ff",
     "command": "hermes", "match": ["hermes"], "note": "OpenCode Zen"},
    {"id": "codex", "name": "Codex", "glyph": "◍", "color": "#9ece6a",
     "command": "codex", "match": ["codex"], "note": "OpenAI"},
    {"id": "mochi", "name": "Mochi", "glyph": "◕", "color": "#f5a0c0",
     "command": "mochi", "match": ["mochi"], "note": "Bun standalone"},
    {"id": "agy", "name": "Antigravity", "glyph": "△", "color": "#8be9fd",
     "command": "agy", "match": ["agy", "antigravity"], "note": "AGY"},
    {"id": "gemini", "name": "Gemini CLI", "glyph": "✦", "color": "#8ab4f8",
     "command": "gemini", "match": ["gemini"], "note": "Google"},
    {"id": "crush", "name": "Crush", "glyph": "❥", "color": "#ff79c6",
     "command": "crush", "match": ["crush"], "note": "Charmbracelet"},
    {"id": "opencode", "name": "OpenCode", "glyph": "⌘", "color": "#e0af68",
     "command": "opencode", "match": ["opencode"], "note": "Zen / Go"},
    {"id": "aider", "name": "Aider", "glyph": "⌗", "color": "#a6e3a1",
     "command": "aider", "match": ["aider"], "note": "Pair programmer"},
    {"id": "cline", "name": "Cline", "glyph": "⋔", "color": "#56b6c2",
     "command": "cline", "match": ["cline"], "note": "CLI agent"},
    {"id": "jcode", "name": "J-Code", "glyph": "⌁", "color": "#bb9af7",
     "command": "jcode", "match": ["jcode"], "note": "J-Code"},
    {"id": "pi", "name": "Pi", "glyph": "π", "color": "#e5c07b",
     "command": "pi", "match": ["pi"], "note": "Pi agent"},
    {"id": "cursor", "name": "Cursor Agent", "glyph": "⌖", "color": "#61afef",
     "command": "cursor-agent", "match": ["cursor-agent"], "note": "Cursor"},
]

DEFAULT_AGENT = {"id": "shell", "name": "shell", "glyph": "❯",
                 "color": "#8fa3c8", "command": "", "note": ""}


@dataclass
class AgentHit:
    id: str
    name: str
    glyph: str
    color: str
    note: str
    pid: int
    started: float

    @property
    def runtime(self) -> float:
        return max(0.0, time.time() - self.started)


def available_agents(custom: dict | None = None) -> list[dict]:
    found = []
    for agent in AGENTS:
        if shutil.which(agent["command"]) or shutil.which(agent["command"].split()[0]):
            found.append(agent)
    for name, command in (custom or {}).items():
        if not command or not isinstance(command, str):
            continue
        if shutil.which(command.split()[0]):
            found.append({
                "id": name, "name": name.title(), "glyph": "◇",
                "color": "#8fa3c8", "command": command,
                "match": [name.lower()], "note": "custom",
            })
    return found


def agent_by_id(agent_id: str | None) -> dict:
    for agent in AGENTS:
        if agent["id"] == agent_id:
            return agent
    return DEFAULT_AGENT


def _match_process(proc_agent: dict, name: str, argv: list[str]) -> bool:
    needles = set(proc_agent.get("match") or [])
    if name in needles:
        return True
    for token in argv[:3]:
        base = os.path.basename(token).lower()
        if base in needles:
            return True
    return False


# Programs that are never worth naming a tab after: the shell itself, tmux,
# and privilege wrappers (their child is the interesting process).
SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "mksh", "tcsh",
          "csh", "nu", "xonsh", "elvish", "login", "tmux", "sudo", "doas"}

# Interpreters that run an app script: name the tab after the app, not the
# interpreter (`node /usr/bin/opencode` reads better as `opencode`).
RUNTIMES = {"node", "nodejs", "bun", "bunx", "deno", "python", "python2",
            "python3", "ruby", "perl", "php", "java", "npx", "npm", "pnpm",
            "yarn", "uv", "uvx", "poetry", "pipenv"}

GENERIC_SCRIPT_NAMES = {"index", "main", "cli", "bin", "app", "src", "start",
                        "dist", "build", "lib", "node_modules", "scripts", "run"}


def _pretty_command(name: str, argv: list[str]) -> str:
    """`node /usr/lib/opencode/dist/index.js` -> `opencode`."""
    base = os.path.basename(name or "").lower()
    if base in RUNTIMES and argv:
        for token in argv[1:6]:
            if token.startswith("-"):
                continue
            if os.path.sep not in token and "." in token:
                return token[:28]            # python -m http.server
            stem = os.path.splitext(os.path.basename(token))[0]
            if stem and stem.lower() not in GENERIC_SCRIPT_NAMES:
                return stem[:28]
            parent = os.path.basename(os.path.dirname(token))
            if parent and parent.lower() not in GENERIC_SCRIPT_NAMES:
                return parent[:28]
        return base[:28]
    return (base or name)[:28]


# ---------------------------------------------------------------------------
# Process inspection
#
# psutil.Process.children(recursive=True) re-reads every process on the box and
# parses each cmdline, which cost ~60 ms per poll with three tabs open. Linux
# gives us the same answer in two syscalls per process:\n# /proc/<pid>/task/<pid>/children lists the direct children and /proc/<pid>/stat
# plus /proc/<pid>/cmdline describe them. The same walk answers *both* questions
# (is an agent running, what is the foreground program), so a poll touches each
# pid exactly once. psutil stays as the fallback for non-Linux systems.
# ---------------------------------------------------------------------------

try:
    _CLK_TCK = float(os.sysconf("SC_CLK_TCK")) or 100.0
except (AttributeError, ValueError, OSError):  # pragma: no cover - exotic hosts
    _CLK_TCK = 100.0

_BOOT_TIME: float | None = None
MAX_DEPTH = 12


def _boot_time() -> float:
    global _BOOT_TIME
    if _BOOT_TIME is None:
        try:
            _BOOT_TIME = float(psutil.boot_time()) if psutil else 0.0
        except Exception:
            _BOOT_TIME = 0.0
    return _BOOT_TIME


def _children(pid: int) -> list[int] | None:
    try:
        with open(f"/proc/{pid}/task/{pid}/children", "r", encoding="ascii") as handle:
            return [int(token) for token in handle.read().split()]
    except (OSError, ValueError):
        return None


def _inspect(pid: int) -> tuple[str, list[str], float]:
    """(comm, argv, start time) for one pid, with no psutil round-trips."""
    name, started = "", 0.0
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
        close = raw.rfind(b")")
        name = raw[raw.find(b"(") + 1:close].decode("utf-8", "replace")
        # Field 22 of stat (0-based 19 after the comm/state prefix) is the
        # process start time in clock ticks since boot.
        started = _boot_time() + int(raw[close + 2:].split()[19]) / _CLK_TCK
    except (OSError, ValueError, IndexError):
        pass
    argv: list[str] = []
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
    except OSError:
        pass
    return name, argv, started


def _walk(pane_pid: int) -> list[tuple[int, str, list[str], float]] | None:
    """Every process under a pane, in breadth-first order.

    Returns ``None`` when /proc does not expose child lists, so the caller can
    fall back to psutil.
    """
    root_name, root_argv, root_started = _inspect(pane_pid)
    rows = [(pane_pid, root_name, root_argv, root_started)]
    seen = {pane_pid}
    frontier = [pane_pid]
    for _depth in range(MAX_DEPTH):
        next_frontier: list[int] = []
        for pid in frontier:
            kids = _children(pid)
            if kids is None:
                return None
            for kid in kids:
                if kid in seen:
                    continue
                seen.add(kid)
                name, argv, started = _inspect(kid)
                rows.append((kid, name, argv, started))
                next_frontier.append(kid)
        if not next_frontier:
            break
        frontier = next_frontier
    return rows


def _walk_psutil(pane_pid: int) -> list[tuple[int, str, list[str], float]]:
    if psutil is None:
        return []
    try:
        root = psutil.Process(pane_pid)
        procs = [root]
        try:
            procs.extend(root.children(recursive=True))
        except Exception:
            pass
    except Exception:
        return []
    rows = []
    for proc in procs:
        try:
            name = proc.name() or ""
            argv = proc.cmdline() or []
        except Exception:
            name, argv = "", []
        try:
            started = proc.create_time()
        except Exception:
            started = 0.0
        rows.append((proc.pid, name, argv, started))
    return rows


def scan(pane_pid: int) -> tuple[AgentHit | None, str]:
    """One process walk: the AI agent under a pane (if any) and its ``foreground``.

    Both answers come from the same snapshot, and neither needs a full system
    scan, so the UI can poll often without heating a core.
    """
    if not pane_pid:
        return None, ""
    rows = _walk(pane_pid)
    if rows is None:
        rows = _walk_psutil(pane_pid)
    if not rows:
        return None, ""

    hit: AgentHit | None = None
    for pid, name, argv, started in rows:
        low = name.lower()
        for agent in AGENTS:
            if _match_process(agent, low, argv):
                hit = AgentHit(
                    id=agent["id"], name=agent["name"], glyph=agent["glyph"],
                    color=agent["color"], note=agent["note"],
                    pid=pid, started=started or time.time(),
                )
                break
        if hit:
            break

    # GNOME Terminal naming: the newest non-shell program under the pane wins.
    newest: tuple[float, str, list[str]] | None = None
    for _pid, name, argv, started in rows[1:]:
        if name.lower() in SHELLS:
            continue
        if newest is None or started >= newest[0]:
            newest = (started, name, argv)
    command = _pretty_command(newest[1], newest[2]) if newest else ""
    return hit, command


def foreground(pane_pid: int) -> str:
    """Name of the program running under a pane's shell, or "" for a bare shell."""
    return scan(pane_pid)[1]


def detect(pane_pid: int) -> AgentHit | None:
    """Find the AI agent running under a pane's shell, if any."""
    return scan(pane_pid)[0]
