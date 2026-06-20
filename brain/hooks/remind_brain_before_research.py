#!/usr/bin/env python3
"""
Brain-Before-Research Reminder Hook (PreToolUse, soft / non-blocking)

Fires before expensive fan-out research tools (Workflow). Does NOT block —
it injects a one-line reminder to consult the brain's roadmap first, then
allows the tool. The push digest (inject_brain_context.py) already puts open
work in context; this is a belt-and-suspenders nudge for the case where the
agent reaches for research anyway.

Why soft, not a hard gate: a hard block produces false positives when the
brain genuinely has nothing on the topic, and needs an escape hatch the agent
can't reliably trigger. The reminder costs ~1 line and never wrongly blocks
legitimate research.

Opt in to a HARD gate by setting `research_gate: "hard"` in
~/.agent-brain/config.json — then this blocks Workflow unless a brain read
(pre_check / query_decisions / get_roadmap) wrote ~/.agent-brain/.last_query_marker
recently. Default is "soft".

Output contract (PreToolUse):
- soft: JSON additionalContext + exit 0 (allow, with reminder text).
- hard block: JSON permissionDecision="deny" + reason, exit 0.

Discipline: fail-open on ANY error. Respect BRAIN_SKIP_ENFORCE=1.

Install: register in ~/.claude/settings.json under hooks.PreToolUse with
matcher "Workflow".
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
CONFIG_FILE = BRAIN_DIR / "config.json"
QUERY_MARKER = BRAIN_DIR / ".last_query_marker"
STALE_MINUTES = 30


def _gate_mode() -> str:
    """'soft' (default) or 'hard' from config.json. Silent on any error."""
    try:
        if CONFIG_FILE.exists():
            mode = json.loads(CONFIG_FILE.read_text()).get("research_gate", "soft")
            if mode in ("soft", "hard"):
                return mode
    except Exception:
        pass
    return "soft"


def _queried_recently() -> bool:
    """True if a brain read happened within STALE_MINUTES (hard-gate only)."""
    try:
        marker = json.loads(QUERY_MARKER.read_text())
        ts = datetime.fromisoformat(marker.get("timestamp", ""))
        return (datetime.now() - ts).total_seconds() / 60.0 <= STALE_MINUTES
    except Exception:
        return False  # no/!corrupt marker -> treat as "not queried"


def _allow_with_reminder() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "[agent-brain] Before fan-out research: the brain may already "
                "hold this. Call get_roadmap or query_decisions(query=...) first "
                "— re-deriving logged work wastes tokens."
            ),
        }
    }))
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
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

    if hook_input.get("tool_name") != "Workflow":
        sys.exit(0)  # only guards Workflow

    if _gate_mode() == "hard" and not _queried_recently():
        _deny(
            "BRAIN GATE: consult the brain before fan-out research. Call "
            "get_roadmap or query_decisions(query=...) first; if the brain has "
            "nothing, re-run (the query marker satisfies the gate). Set "
            "BRAIN_SKIP_ENFORCE=1 to bypass."
        )

    _allow_with_reminder()


if __name__ == "__main__":
    main()
