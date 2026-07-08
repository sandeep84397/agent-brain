#!/usr/bin/env python3
"""Helpers for installing agent-brain into Codex config surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path


MCP_BEGIN = "# BEGIN agent-brain MCP"
MCP_END = "# END agent-brain MCP"
AGENTS_BEGIN = "<!-- agent-brain:codex-protocol -->"
AGENTS_END = "<!-- /agent-brain:codex-protocol -->"


def _quote_toml_string(value: str) -> str:
    return json.dumps(value)


def _remove_agent_brain_mcp_table(text: str) -> str:
    marked = re.compile(
        rf"\n?{re.escape(MCP_BEGIN)}\n.*?\n{re.escape(MCP_END)}\n?",
        re.DOTALL,
    )
    text = marked.sub("\n", text)

    bare_table = re.compile(
        r"(?ms)^\[mcp_servers\.agent-brain\]\n.*?(?=^\[|\Z)"
    )
    text = bare_table.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ensure_codex_config(config_path: str | Path, pybin: str, server_py: str) -> bool:
    """Add or replace the Codex MCP server block in config.toml.

    Returns True when the file changed.
    """
    path = Path(config_path).expanduser()
    existing = path.read_text() if path.exists() else ""
    base = _remove_agent_brain_mcp_table(existing)
    block = "\n".join(
        [
            MCP_BEGIN,
            "[mcp_servers.agent-brain]",
            f"command = {_quote_toml_string(pybin)}",
            f"args = [{_quote_toml_string(server_py)}]",
            "startup_timeout_sec = 10",
            "tool_timeout_sec = 60",
            MCP_END,
        ]
    )
    new_text = (base + "\n\n" if base else "") + block + "\n"
    if new_text == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    return True


def _hook(command: str, timeout: int, status: str) -> dict:
    return {
        "type": "command",
        "command": command,
        "timeout": timeout,
        "statusMessage": status,
    }


def ensure_codex_hooks(hooks_path: str | Path, pybin: str, hooks_dir: str) -> bool:
    """Install idempotent Codex hooks for brain enforcement and SAN nudges."""
    path = Path(hooks_path).expanduser()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        raise SystemExit(f"{path} is not valid JSON; fix it before installing hooks")

    hooks = data.setdefault("hooks", {})
    changed = False

    def has_command(event: str, script: str) -> bool:
        return any(
            script in item.get("command", "")
            for group in hooks.get(event, [])
            for item in group.get("hooks", [])
        )

    def add(event: str, matcher: str, script: str, timeout: int, status: str) -> None:
        nonlocal changed
        if has_command(event, script):
            return
        command = f"{pybin} {Path(hooks_dir) / script}"
        hooks.setdefault(event, []).append(
            {
                "matcher": matcher,
                "hooks": [_hook(command, timeout, status)],
            }
        )
        changed = True

    add(
        "PreToolUse",
        "Edit|Write|apply_patch",
        "enforce_brain_protocol.py",
        5,
        "Checking brain decision log",
    )
    add(
        "SessionStart",
        "startup|resume|clear|compact",
        "inject_brain_context.py",
        15,
        "Loading brain roadmap",
    )
    add(
        "PreToolUse",
        "Workflow",
        "remind_brain_before_research.py",
        10,
        "Checking brain before research",
    )
    add(
        "PreToolUse",
        "Read",
        "route_read_to_san.py",
        5,
        "Checking SAN brief",
    )
    add(
        "PreToolUse",
        "Bash",
        "route_bash_to_san.py",
        5,
        "Checking shell read against SAN",
    )

    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    return False


def ensure_project_agents_md(path: str | Path) -> bool:
    """Add Codex-visible agent-brain guidance to a repo AGENTS.md file."""
    agents_path = Path(path)
    existing = agents_path.read_text() if agents_path.exists() else ""
    if AGENTS_BEGIN in existing:
        return False

    block = f"""{AGENTS_BEGIN}
## Agent Brain Protocol

- Before non-trivial work, call `get_roadmap` and then `pre_check(agent, area, action)` from the `agent-brain` MCP server.
- Before editing code, call `log_decision(agent, repo, area, action, reasoning)` so future sessions can learn from the choice.
- Prefer `query_san` to find code and `get_san(file_path="<absolute path>")` to understand source files before raw reads. Use raw reads for exact bytes before editing, non-code files, or files without SAN.
- After review or user feedback, call `log_outcome(decision_id, outcome, outcome_by, reason)` so accepted and rejected approaches persist.
{AGENTS_END}
"""
    sep = "" if not existing else ("\n" if existing.endswith("\n\n") else "\n\n")
    new_text = existing + sep + block
    agents_path.write_text(new_text)
    return True


def install_user(codex_home: str | Path, pybin: str, server_py: str, hooks_dir: str) -> None:
    home = Path(codex_home).expanduser()
    changed_config = ensure_codex_config(home / "config.toml", pybin, server_py)
    changed_hooks = ensure_codex_hooks(home / "hooks.json", pybin, hooks_dir)
    print(f"  {'✓ Updated' if changed_config else '✓ Already configured'} {home / 'config.toml'}")
    print(f"  {'✓ Updated' if changed_hooks else '✓ Already configured'} {home / 'hooks.json'}")


def link_project(project: str | Path) -> None:
    project_path = Path(project).expanduser().resolve()
    changed = ensure_project_agents_md(project_path / "AGENTS.md")
    print(
        f"  {'✓ Updated' if changed else '✓ Already configured'} "
        f"{project_path / 'AGENTS.md'}"
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    user = sub.add_parser("install-user")
    user.add_argument("--codex-home", required=True)
    user.add_argument("--pybin", required=True)
    user.add_argument("--server", required=True)
    user.add_argument("--hooks-dir", required=True)

    project = sub.add_parser("link-project")
    project.add_argument("--project", required=True)

    args = parser.parse_args()
    if args.cmd == "install-user":
        install_user(args.codex_home, args.pybin, args.server, args.hooks_dir)
    elif args.cmd == "link-project":
        link_project(args.project)


if __name__ == "__main__":
    main()
