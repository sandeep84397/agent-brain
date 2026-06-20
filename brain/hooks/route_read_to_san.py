#!/usr/bin/env python3
"""
Read -> SAN Routing Hook (PreToolUse, soft / non-blocking)

When the agent calls raw Read on a code file that HAS a fresh .san brief, this
nudges it toward get_san (same structure, far fewer tokens) — then ALLOWS the
Read anyway. SAN is for exploring; raw Read stays correct for editing, non-code
files, or files with no/stale .san, so this NEVER blocks.

Self-contained on purpose: server.py hard-imports FastMCP, so a hook that
imported it would crash (violating fail-open). The repo map + path mapping +
mtime freshness check are all stdlib, read straight from config.json — the same
read-config-directly pattern as enforce_brain_protocol.py.

Dedupe: one nudge per file per session (keyed on the hook's session_id). Without
it, every Read of a covered file would nag. A TTL fallback covers the rare case
where session_id is absent; day-old markers are best-effort cleaned.

Output contract (PreToolUse): JSON additionalContext + exit 0 (ALLOW). Never
permissionDecision=deny.

Discipline: fail-open on ANY error. Respect BRAIN_SKIP_ENFORCE=1.

Install: register in ~/.claude/settings.json under hooks.PreToolUse, matcher "Read".
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
CONFIG_FILE = BRAIN_DIR / "config.json"

# MUST match server.py's SOURCE_EXTS exactly (the single source of truth there).
# The compiler is language-agnostic, so this is the full set. Keep in sync by
# hand on any change — a drift only means a missed nudge, never a wrong block.
SOURCE_EXTS = (".kt", ".java", ".py", ".ts", ".tsx", ".js", ".jsx",
               ".swift", ".go", ".rs", ".rb", ".c", ".cpp", ".h", ".cs",
               ".php", ".scala", ".m", ".mm")
SKIP_DIRS = ("build", "bin", "out", "dist", ".gradle", "node_modules", "Pods")
DEDUPE_TTL_S = 30 * 60       # fallback marker lifetime when session_id is absent
MARKER_CLEANUP_AGE_S = 24 * 60 * 60


def _repos() -> dict:
    """Read {name: root_path} from config.json. {} on any problem."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        repos = cfg.get("repos", {})
        return {k: v for k, v in repos.items() if isinstance(v, str)} \
            if isinstance(repos, dict) else {}
    except Exception:
        return {}


def _match_repo(abs_path: str, repos: dict):
    """Longest-root reverse match: abs path -> repo root. None if outside all."""
    try:
        target = os.path.realpath(abs_path)
    except (OSError, ValueError):
        return None
    best = None
    best_len = -1
    for root in repos.values():
        try:
            root_real = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if target == root_real or target.startswith(root_real + os.sep):
            if len(root_real) > best_len:
                best_len = len(root_real)
                best = root_real
    return best


def _san_path_for(root: str, rel: str):
    """Existing .san for a source file: append form first, then legacy. None if absent."""
    appended = Path(root) / ".san" / (rel + ".san")
    if appended.exists():
        return appended
    legacy = Path(root) / ".san" / Path(rel).with_suffix(".san")
    if legacy.exists():
        return legacy
    return None


def _is_fresh(source: Path, san: Path) -> bool:
    """True if the SAN is at least as new as its source (mtime)."""
    try:
        return source.stat().st_mtime <= san.stat().st_mtime
    except OSError:
        return False


def _marker(session_id: str) -> Path:
    # Hash so distinct session_ids never collide (lossy char-stripping could
    # map "abc.123" and "abc/123" to the same marker, cross-session bleed).
    if session_id:
        digest = hashlib.md5(session_id.encode("utf-8", "replace")).hexdigest()[:16]
        return BRAIN_DIR / f".read_san_nudged_{digest}"
    return BRAIN_DIR / ".read_san_nudged"


def _already_nudged(session_id: str) -> bool:
    m = _marker(session_id)
    if not m.exists():
        return False
    if session_id:
        return True  # session-scoped marker: presence = already nudged
    try:
        return (time.time() - m.stat().st_mtime) < DEDUPE_TTL_S
    except OSError:
        return False


def _mark_nudged(session_id: str) -> None:
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        _marker(session_id).write_text(str(time.time()))
    except OSError:
        pass


def _cleanup_old_markers() -> None:
    """Best-effort: drop nudge markers older than a day so they don't accumulate."""
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


def _emit_nudge(abs_path: str, san_path: Path) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"[agent-brain] A fresh SAN brief exists for {os.path.basename(abs_path)}. "
                f"To EXPLORE this code, prefer get_san(file_path=\"{abs_path}\") "
                f"(detail='sig' for what exists, 'full' for impl) — same structure, "
                f"~5-11x fewer tokens. Proceeding with raw Read; switch to get_san "
                f"unless you're about to EDIT this file."
            ),
        }
    }))
    sys.exit(0)


def main() -> None:
    if os.environ.get("BRAIN_SKIP_ENFORCE") == "1":
        sys.exit(0)

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    if hook_input.get("tool_name") != "Read":
        sys.exit(0)

    file_path = (hook_input.get("tool_input") or {}).get("file_path", "")
    if not file_path or not os.path.isabs(file_path):
        sys.exit(0)
    if not file_path.endswith(SOURCE_EXTS):
        sys.exit(0)  # non-code: raw Read is correct
    if any(f"{os.sep}{d}{os.sep}" in file_path for d in SKIP_DIRS):
        sys.exit(0)  # build/vendored

    repos = _repos()
    if not repos:
        sys.exit(0)
    root = _match_repo(file_path, repos)
    if not root:
        sys.exit(0)  # outside every configured repo

    try:
        rel = os.path.relpath(os.path.realpath(file_path), root)
    except (OSError, ValueError):
        sys.exit(0)

    san_path = _san_path_for(root, rel)
    if san_path is None:
        sys.exit(0)  # no SAN -> raw Read is the only option
    if not _is_fresh(Path(file_path), san_path):
        sys.exit(0)  # stale SAN -> don't route toward outdated data

    session_id = hook_input.get("session_id", "")
    if _already_nudged(session_id):
        sys.exit(0)  # one nudge per file/session is enough

    _cleanup_old_markers()
    _mark_nudged(session_id)
    _emit_nudge(file_path, san_path)


if __name__ == "__main__":
    main()
