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

    # Only enforce on Edit/Write (code changes)
    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    # Skip non-code files: docs, configs, generated files
    skip_patterns = (
        ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
        ".lock", ".gitignore", ".env",
        "CLAUDE.md", "README", "LICENSE", "CHANGELOG",
        ".san", "_index.json", ".san_hashes",
    )
    if any(file_path.endswith(p) or p in file_path for p in skip_patterns):
        sys.exit(0)

    # Skip if editing inside .claude/, .git/, node_modules/, build/
    skip_dirs = ("/.claude/", "/.git/", "/node_modules/", "/build/", "/.san/")
    if any(d in file_path for d in skip_dirs):
        sys.exit(0)

    # User-extensible: fnmatch globs from config.json hook_skip_paths
    for pattern in _load_user_skip_patterns():
        if fnmatch.fnmatch(file_path, pattern):
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
