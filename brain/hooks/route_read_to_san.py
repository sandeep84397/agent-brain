#!/usr/bin/env python3
"""
Read -> SAN Routing Hook (PreToolUse)

When the agent calls raw Read on a code file that HAS a fresh .san brief, this
steers it toward get_san (same structure, far fewer tokens). SAN is for
exploring; raw Read stays correct for editing, non-code files, or files with
no/stale .san — so the FIRST read of any file is always allowed.

Escalation (per file, per session):
- 1st raw read of a fresh-SAN file -> soft nudge, ALLOW. (Exploration / edit-prep
  is the legitimate first-read case; never blocked.)
- 2nd+ raw read of the SAME fresh-SAN file -> rarely edit-prep, usually waste:
    * read_enforcement="soft" (default): a STRONGER nudge, still ALLOW.
    * read_enforcement="hard"  (opt-in, config.json): BLOCK (exit 2) with an
      actionable message. Escape with BRAIN_SKIP_READ_BLOCK=1 when you truly
      need exact bytes (about to Edit).

Per-FILE dedupe (was per-session): the cheaper path stays visible on every
file's first read, not just the first read of the whole session — that
session-scoped silence was why agents kept raw-reading files #2..#N.

Self-contained on purpose: server.py hard-imports FastMCP, so a hook that
imported it would crash (violating fail-open). The repo map + path mapping +
mtime freshness check are all stdlib, read straight from config.json — the same
read-config-directly pattern as enforce_brain_protocol.py.

Discipline: fail-open on ANY error. Respect BRAIN_SKIP_ENFORCE=1 (disables the
hook entirely) and BRAIN_SKIP_READ_BLOCK=1 (bypasses only the hard block).

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


def _read_enforcement() -> str:
    """'soft' (default) | 'hard'. 'hard' BLOCKS a 2nd+ raw read of a fresh-SAN
    file (first read always allowed). Read from config.json read_enforcement."""
    try:
        mode = json.loads(CONFIG_FILE.read_text()).get("read_enforcement", "soft")
        return mode if mode in ("soft", "hard") else "soft"
    except Exception:
        return "soft"


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


def _marker(session_id: str, file_real: str) -> Path:
    # Per-(session, file): the cheaper path stays visible on EVERY file's first
    # raw read, not just the first read of the whole session. Hash both so weird
    # session_ids / long paths can't collide or overflow the filename.
    key = f"{session_id}\x00{file_real}"
    digest = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:20]
    return BRAIN_DIR / f".read_san_nudged_{digest}"


def _read_count(session_id: str, file_real: str) -> int:
    """How many raw reads of THIS file we've already seen this session."""
    m = _marker(session_id, file_real)
    if not m.exists():
        return 0
    # When session_id is absent, treat a marker older than the TTL as expired.
    if not session_id:
        try:
            if (time.time() - m.stat().st_mtime) >= DEDUPE_TTL_S:
                return 0
        except OSError:
            return 0
    try:
        return int(m.read_text().split(":")[0] or "0")
    except (OSError, ValueError):
        return 1  # marker exists but unreadable -> treat as already-seen


def _bump_read(session_id: str, file_real: str) -> None:
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        m = _marker(session_id, file_real)
        n = _read_count(session_id, file_real) + 1
        m.write_text(f"{n}:{time.time()}")
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


def _emit_nudge(abs_path: str, repeat: bool) -> None:
    """Soft nudge (always exit 0 / ALLOW). Stronger wording on a repeat raw read
    of the same fresh-SAN file, since that's rarely edit-prep."""
    base = os.path.basename(abs_path)
    if repeat:
        msg = (f"[agent-brain] You're re-reading {base} raw — its SAN is fresh and "
               f"~5-11x cheaper. Call get_san(file_path=\"{abs_path}\") now "
               f"(detail='sig' for structure, 'full' for impl). Raw Read only if "
               f"you're about to EDIT it.")
    else:
        msg = (f"[agent-brain] A fresh SAN brief exists for {base}. To EXPLORE this "
               f"code, prefer get_san(file_path=\"{abs_path}\") (detail='sig' for "
               f"what exists, 'full' for impl) — same structure, ~5-11x fewer tokens. "
               f"Proceeding with raw Read; switch to get_san unless about to EDIT it.")
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}
    }))
    sys.exit(0)


def _emit_block(abs_path: str) -> None:
    """Hard block (exit 2) — only on a repeat raw read of a fresh-SAN file when
    read_enforcement=hard. First read is never blocked. Escape: BRAIN_SKIP_READ_BLOCK=1."""
    sys.stderr.write(
        f"BRAIN: {os.path.basename(abs_path)} has a fresh SAN — re-reading it raw "
        f"wastes ~5-11x tokens. Use get_san(file_path=\"{abs_path}\") "
        f"(detail='sig' or 'full'). If you genuinely need exact bytes (about to "
        f"Edit), set BRAIN_SKIP_READ_BLOCK=1 to bypass.\n")
    sys.exit(2)


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
    file_real = os.path.realpath(file_path)
    prior = _read_count(session_id, file_real)  # raw reads of THIS file so far
    _cleanup_old_markers()
    _bump_read(session_id, file_real)

    # FIRST raw read of this file: always allow, soft nudge. A first read is the
    # legitimate case (exploration, edit-prep, "is this a stub?") — never block it.
    if prior == 0:
        _emit_nudge(file_path, repeat=False)

    # REPEAT raw read of a fresh-SAN file — rarely edit-prep, usually waste.
    # hard mode (opt-in) blocks it (with an escape); soft mode nudges harder.
    if _read_enforcement() == "hard" and os.environ.get("BRAIN_SKIP_READ_BLOCK") != "1":
        _emit_block(file_path)
    _emit_nudge(file_path, repeat=True)


if __name__ == "__main__":
    main()
