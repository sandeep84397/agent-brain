#!/usr/bin/env python3
"""
Brain Context Injection Hook (SessionStart)

Pushes the brain's open-work digest into context at the moments memory is
born or lost — new session and, critically, POST-COMPACTION re-entry. Without
this the decision graph is only ever pulled if the agent remembers to ask; the
pending roadmap discussed before a /compact gets hallucinated or lost.

Claude Code calls SessionStart with source ∈ {startup, resume, clear, compact}.
- source=compact  -> the post-compaction re-entry: inject a FULLER digest
                     (this is the amnesia fix).
- otherwise       -> inject a lighter digest so a fresh/resumed session still
                     opens with "what's left to do" in view.

Output contract (verified against Claude Code hooks docs): emit JSON on stdout
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "<text>"}}
additionalContext is appended to the model's context at session start.

Discipline (copied from enforce_brain_protocol.py): fail-open on ANY error —
never break a session. Respect BRAIN_SKIP_ENFORCE=1.

Install: register in ~/.claude/settings.json under hooks.SessionStart.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
SERVER_PY = BRAIN_DIR / "server.py"
PYBIN = BRAIN_DIR / ".venv" / "bin" / "python"

# How many open-work items to inject. Fuller post-compaction (amnesia moment).
LIMIT_COMPACT = 15
LIMIT_DEFAULT = 8


def _emit(context: str) -> None:
    """Emit additionalContext JSON and exit 0. Empty context => emit nothing."""
    context = context.strip()
    if context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))
    sys.exit(0)


def main() -> None:
    if os.environ.get("BRAIN_SKIP_ENFORCE") == "1":
        sys.exit(0)

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        sys.exit(0)  # can't parse — stay silent, never break the session

    source = hook_input.get("source", "")
    limit = LIMIT_COMPACT if source == "compact" else LIMIT_DEFAULT

    if not PYBIN.exists() or not SERVER_PY.exists():
        sys.exit(0)  # brain not installed here — nothing to inject

    try:
        proc = subprocess.run(
            [str(PYBIN), str(SERVER_PY), "roadmap"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        sys.exit(0)

    # Standing directive — injected EVERY session/compact, even with no open
    # work, so the SAN read-path default is always in view.
    san_directive = (
        "[agent-brain] ALWAYS use get_san to READ/EXPLORE existing code BEFORE "
        "raw Read — get_san(file_path=\"<path>\") takes the absolute path you "
        "already have; detail='sig' for structure, 'full' for impl. Same "
        "structure, ~5-11x fewer tokens. Use raw Read ONLY for files you're "
        "about to EDIT (need exact bytes), non-code files, or when no .san exists. "
        "This is a standing rule, like graph-first exploration."
    )

    digest = (proc.stdout or "").strip()
    has_work = bool(digest) and not digest.startswith("No open work")

    if not has_work:
        _emit(san_directive)  # no roadmap, but still steer reads through SAN

    # Trim to `limit` body lines (header + N items). The server already caps
    # at 15; this lets non-compact sessions stay lighter.
    lines = digest.splitlines()
    if len(lines) > limit + 1:
        lines = lines[: limit + 1] + [
            f"... (+{len(lines) - 1 - limit} more — call get_roadmap or query_decisions)"
        ]
    body = "\n".join(lines)

    if source == "compact":
        preamble = (
            "[agent-brain] Context was just compacted. Your prior pending work "
            "and roadmap are below — RESUME from these instead of re-researching. "
            "Use get_roadmap or query_decisions(query=...) for detail before any "
            "fan-out research."
        )
    else:
        preamble = (
            "[agent-brain] Open work carried over from past sessions. Check these "
            "before starting; use get_roadmap / query_decisions for detail."
        )

    _emit(f"{preamble}\n\n{body}\n\n{san_directive}")


if __name__ == "__main__":
    main()
