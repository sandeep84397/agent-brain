#!/usr/bin/env python3
"""
Bash -> SAN Routing Hook (PreToolUse, matcher "Bash")

Closes the blind spot in route_read_to_san.py: that hook only watches the Read
TOOL, so DUMPING a source file via the BASH tool (cat/head/sed/tail on a file)
bypasses SAN routing entirely. This hook watches Bash, parses the command, and
applies the SAME escalation when a file-DUMP command targets a source file that
has a fresh .san.

What it does NOT touch (by design):
- grep/rg/awk on a file — these search for EXACT literals/strings, which SAN
  abstracts away, so grep is the RIGHT tool there, not a bypass.
- Discovery (grep -r / globs across files) — the legitimate "which file?" step.
- The SAN-native discovery tool is query_san; the SAN-native read is get_san.

Escalation when a DUMP command hits a fresh-SAN file:

- 1st time -> soft nudge, ALLOW (legit one-off peek / about to edit).
- 2nd+ time exploring a fresh-SAN file via Bash this session:
    * read_enforcement="soft" (default): stronger nudge, still ALLOW.
    * read_enforcement="hard" (opt-in): BLOCK (exit 2). Escape:
      BRAIN_SKIP_READ_BLOCK=1 when you genuinely need the raw bytes.

Fires ONLY when a file-DUMP command (cat/head/sed/tail/...) names a specific
source file with a fresh .san. grep/rg/awk (literal search), discovery grep
across files, build/run/git/test/mkdir, and write/pipe commands (>, >>, tee, |,
xargs) are all left alone. Rule of thumb: query_san to FIND, get_san to READ,
grep for an EXACT literal in a file, raw Read to EDIT.

Self-contained (server.py imports FastMCP; importing it would crash the hook).
Mirrors the dedupe/freshness logic of route_read_to_san.py — keep them in sync.

Discipline: fail-open on ANY error. Respect BRAIN_SKIP_ENFORCE=1 (off entirely)
and BRAIN_SKIP_READ_BLOCK=1 (bypass only the hard block).

Install: ~/.claude/settings.json hooks.PreToolUse, matcher "Bash".
"""

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
CONFIG_FILE = BRAIN_DIR / "config.json"

# Keep in sync with route_read_to_san.py / server.py SOURCE_EXTS.
SOURCE_EXTS = (".kt", ".java", ".py", ".ts", ".tsx", ".js", ".jsx",
               ".swift", ".go", ".rs", ".rb", ".c", ".cpp", ".h", ".cs",
               ".php", ".scala", ".m", ".mm")
SKIP_DIRS = ("build", "bin", "out", "dist", ".gradle", "node_modules", "Pods")
DEDUPE_TTL_S = 30 * 60
MARKER_CLEANUP_AGE_S = 24 * 60 * 60

# Commands that DUMP a file's contents to READ/understand it — SAN replaces
# these (it carries the structure at ~5-11x fewer tokens). These are the ones
# we nudge toward get_san when they target a specific fresh-SAN source file.
DUMP_CMDS = {"cat", "head", "tail", "sed", "less", "more", "bat", "view"}
# Search/filter commands (grep/rg/awk/...) are NOT nudged even on a named file:
# they look for EXACT literals/strings, which SAN deliberately abstracts away —
# so grep-on-a-file is the right tool, not a SAN bypass. (Discovery grep across
# files never matched anyway since it has no specific-file argument.)
SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "awk", "ack"}
# If the command writes/edits/pipes-into-something, leave it alone — not a pure read.
WRITE_HINTS = (">", ">>", "|", "tee", "xargs")


def _repos() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        repos = cfg.get("repos", {})
        return {k: v for k, v in repos.items() if isinstance(v, str)} \
            if isinstance(repos, dict) else {}
    except Exception:
        return {}


def _read_enforcement() -> str:
    try:
        mode = json.loads(CONFIG_FILE.read_text()).get("read_enforcement", "soft")
        return mode if mode in ("soft", "hard") else "soft"
    except Exception:
        return "soft"


def _match_repo(abs_path: str, repos: dict):
    try:
        target = os.path.realpath(abs_path)
    except (OSError, ValueError):
        return None
    best, best_len = None, -1
    for root in repos.values():
        try:
            root_real = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if target == root_real or target.startswith(root_real + os.sep):
            if len(root_real) > best_len:
                best_len, best = len(root_real), root_real
    return best


def _san_path_for(root: str, rel: str):
    appended = Path(root) / ".san" / (rel + ".san")
    if appended.exists():
        return appended
    legacy = Path(root) / ".san" / Path(rel).with_suffix(".san")
    if legacy.exists():
        return legacy
    return None


def _is_fresh(source: Path, san: Path) -> bool:
    try:
        return source.stat().st_mtime <= san.stat().st_mtime
    except OSError:
        return False


def _marker(session_id: str, file_real: str) -> Path:
    # Shared namespace with route_read_to_san.py so a file explored via Read AND
    # Bash escalates together (a 2nd touch by either route counts as a repeat).
    key = f"{session_id}\x00{file_real}"
    digest = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:20]
    return BRAIN_DIR / f".read_san_nudged_{digest}"


def _read_count(session_id: str, file_real: str) -> int:
    m = _marker(session_id, file_real)
    if not m.exists():
        return 0
    if not session_id:
        try:
            if (time.time() - m.stat().st_mtime) >= DEDUPE_TTL_S:
                return 0
        except OSError:
            return 0
    try:
        return int(m.read_text().split(":")[0] or "0")
    except (OSError, ValueError):
        return 1


def _bump_read(session_id: str, file_real: str) -> None:
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        m = _marker(session_id, file_real)
        n = _read_count(session_id, file_real) + 1
        m.write_text(f"{n}:{time.time()}")
    except OSError:
        pass


def _cleanup_old_markers() -> None:
    try:
        now = time.time()
        for m in BRAIN_DIR.glob(".read_san_nudged*"):
            try:
                if now - m.stat().st_mtime > MARKER_CLEANUP_AGE_S:
                    m.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _explored_source_files(command: str):
    """Parse a Bash command; return source-file paths it READS for exploration.
    Conservative: only when the command's program is an EXPLORE_CMD and it has no
    write/pipe hints. Returns [] otherwise (so non-exploration Bash is untouched)."""
    if any(h in command for h in WRITE_HINTS):
        return []  # pipelines / redirects — could be a real build step, leave alone
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []
    prog = os.path.basename(tokens[0])
    # allow "sudo cat ...", "command grep ..." lightly
    if prog in ("sudo", "command", "time") and len(tokens) > 1:
        tokens = tokens[1:]
        prog = os.path.basename(tokens[0])
    # Only nudge file-DUMPING commands (cat/head/sed...). grep/rg/awk search for
    # exact literals SAN drops, so they're the right tool — never nudged.
    if prog not in DUMP_CMDS:
        return []
    files = []
    for t in tokens[1:]:
        if t.startswith("-"):
            continue  # flags
        if t.endswith(SOURCE_EXTS):
            files.append(t)
    return files


def _emit_nudge(paths, repeat: bool) -> None:
    shown = paths[0]
    base = os.path.basename(shown)
    extra = f" (+{len(paths)-1} more)" if len(paths) > 1 else ""
    if repeat:
        msg = (f"[agent-brain] You're dumping {base}{extra} again — its SAN is "
               f"fresh and ~5-11x cheaper. To UNDERSTAND it, use "
               f"get_san(file_path=\"{shown}\") (detail='sig'/'full'). cat/sed the "
               f"raw file only to EDIT it; grep it only for an exact literal SAN "
               f"can't show.")
    else:
        msg = (f"[agent-brain] {base}{extra} has a fresh SAN. To UNDERSTAND this "
               f"file, read it with get_san(file_path=\"{shown}\") instead of "
               f"cat/head/sed (same structure, ~5-11x fewer tokens). Proceeding; "
               f"use the raw file only to EDIT it or grep an exact literal.")
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}
    }))
    sys.exit(0)


def _emit_block(paths) -> None:
    base = os.path.basename(paths[0])
    sys.stderr.write(
        f"BRAIN: {base} has a fresh SAN — exploring it again with cat/grep wastes "
        f"~5-11x tokens. Use get_san(file_path=\"{paths[0]}\") (detail='sig' or "
        f"'full'). If you genuinely need raw bytes (about to Edit), set "
        f"BRAIN_SKIP_READ_BLOCK=1 to bypass.\n")
    sys.exit(2)


def main() -> None:
    if os.environ.get("BRAIN_SKIP_ENFORCE") == "1":
        sys.exit(0)
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        sys.exit(0)
    if hook_input.get("tool_name") != "Bash":
        sys.exit(0)

    command = (hook_input.get("tool_input") or {}).get("command", "")
    if not command:
        sys.exit(0)
    candidates = _explored_source_files(command)
    if not candidates:
        sys.exit(0)  # not a code-exploration command -> never touch it

    repos = _repos()
    if not repos:
        sys.exit(0)

    session_id = hook_input.get("session_id", "")
    cwd = hook_input.get("cwd", "") or os.getcwd()

    covered = []   # (abs_path, prior_count) for files with a fresh .san
    for f in candidates:
        abs_path = f if os.path.isabs(f) else os.path.normpath(os.path.join(cwd, f))
        if any(f"{os.sep}{d}{os.sep}" in abs_path for d in SKIP_DIRS):
            continue
        if not os.path.exists(abs_path):
            continue
        root = _match_repo(abs_path, repos)
        if not root:
            continue
        try:
            rel = os.path.relpath(os.path.realpath(abs_path), root)
        except (OSError, ValueError):
            continue
        san_path = _san_path_for(root, rel)
        if san_path is None or not _is_fresh(Path(abs_path), san_path):
            continue
        file_real = os.path.realpath(abs_path)
        covered.append((abs_path, _read_count(session_id, file_real), file_real))

    if not covered:
        sys.exit(0)  # nothing explored has a fresh SAN -> shell read is fine

    _cleanup_old_markers()
    # any repeat among the covered files escalates the whole command
    repeat = any(prior > 0 for _, prior, _ in covered)
    for _, _, file_real in covered:
        _bump_read(session_id, file_real)

    paths = [p for p, _, _ in covered]
    if repeat and _read_enforcement() == "hard" \
            and os.environ.get("BRAIN_SKIP_READ_BLOCK") != "1":
        _emit_block(paths)
    _emit_nudge(paths, repeat=repeat)


if __name__ == "__main__":
    main()
