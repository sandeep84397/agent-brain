#!/usr/bin/env python3
"""
Brain Protocol Enforcement Hook (PreToolUse)

Fires before Edit/Write. Blocks if no log_decision was called recently.
Forces agents to log decisions before making code changes.

Install: Add to settings.json PreToolUse hook for Edit|Write.
Marker: ~/.agent-brain/.last_decision_marker (written by log_decision)

Exit codes:
  0 = allow (decision logged recently)
  2 = block (no recent decision — stderr tells agent to call log_decision)
"""

import json
import sys
import os
import fnmatch
import re
from datetime import datetime
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
MARKER_FILE = BRAIN_DIR / ".last_decision_marker"
CONFIG_FILE = BRAIN_DIR / "config.json"
STALE_MINUTES = 30  # Decision older than this = stale, agent must log again


def _load_user_skip_patterns() -> list[str]:
    """
    Read optional `hook_skip_paths` from ~/.agent-brain/config.json.

    The list is a set of fnmatch-style glob patterns (matched against the
    absolute file path). Examples:
        ["**/docs/**", "**/.github/**", "**/*.md"]

    Returns an empty list when:
      - config.json does not exist
      - it isn't valid JSON
      - the key is missing or not a list of strings

    Failure is silent: hook overhead must never break a session.
    """
    try:
        if not CONFIG_FILE.exists():
            return []
        cfg = json.loads(CONFIG_FILE.read_text())
        patterns = cfg.get("hook_skip_paths")
        if isinstance(patterns, list):
            return [p for p in patterns if isinstance(p, str)]
    except Exception:
        pass
    return []


def _is_skipped_path(file_path: str) -> bool:
    """True when this path should not require a logged decision."""
    basename = os.path.basename(file_path)
    skip_exts = (".md", ".txt", ".json", ".yaml", ".yml", ".toml",
                 ".lock", ".gitignore", ".env", ".san")
    skip_name_prefixes = ("README", "LICENSE", "CHANGELOG", ".san_hashes", ".env")
    if file_path.endswith(skip_exts) or basename.startswith(skip_name_prefixes):
        return True

    skip_dirs = ("/.claude/", "/.codex/", "/.git/", "/node_modules/", "/build/", "/.san/")
    if any(d in file_path for d in skip_dirs):
        return True

    return any(fnmatch.fnmatch(file_path, pattern)
               for pattern in _load_user_skip_patterns())


def _apply_patch_paths(tool_input: dict) -> list[str]:
    """Extract touched paths from a Codex/apply_patch payload when available."""
    text_bits = []
    for value in tool_input.values():
        if isinstance(value, str):
            text_bits.append(value)
    patch_text = "\n".join(text_bits)
    if not patch_text:
        return []
    paths = []
    for pattern in (
        r"^\*\*\* Add File: (.+)$",
        r"^\*\*\* Update File: (.+)$",
        r"^\*\*\* Delete File: (.+)$",
        r"^\*\*\* Move to: (.+)$",
    ):
        paths.extend(re.findall(pattern, patch_text, flags=re.MULTILINE))
    return [p.strip() for p in paths if p.strip()]


def _requires_decision(tool_name: str, tool_input: dict) -> bool:
    if tool_name in ("Edit", "Write"):
        return not _is_skipped_path(tool_input.get("file_path", ""))
    if tool_name == "apply_patch":
        paths = _apply_patch_paths(tool_input)
        return not paths or any(not _is_skipped_path(path) for path in paths)
    return False


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        # Can't parse input — don't block, fail open
        sys.exit(0)

    # Bypass: set BRAIN_SKIP_ENFORCE=1 for direct user sessions
    if os.environ.get("BRAIN_SKIP_ENFORCE") == "1":
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Only enforce on code-changing tools.
    if not _requires_decision(tool_name, tool_input):
        sys.exit(0)

    # Check for recent decision marker
    if not MARKER_FILE.exists():
        print(
            "BRAIN PROTOCOL: No decision logged this session. "
            "Call log_decision(agent, repo, area, action, reasoning) BEFORE making code changes. "
            "This is non-negotiable — the brain only learns if you log decisions.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        marker = json.loads(MARKER_FILE.read_text())
        ts = datetime.fromisoformat(marker.get("timestamp", ""))
        age_minutes = (datetime.now() - ts).total_seconds() / 60.0

        if age_minutes > STALE_MINUTES:
            agent = marker.get("agent", "unknown")
            print(
                f"BRAIN PROTOCOL: Last decision was {int(age_minutes)}m ago by {agent}. "
                f"Log a new decision for your current work before editing code. "
                f"Call log_decision(agent, repo, area, action, reasoning).",
                file=sys.stderr,
            )
            sys.exit(2)

    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt marker — don't block, fail open
        sys.exit(0)

    # Recent decision exists — allow
    sys.exit(0)


if __name__ == "__main__":
    main()
