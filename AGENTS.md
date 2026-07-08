# AGENTS.md

## Repository Expectations

- Use the installed brain venv for server commands when dependencies are not installed in the active shell: `~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py <cmd>`.
- Run focused tests after changing setup, hooks, or compatibility helpers. For Codex compatibility, run `python3 -m unittest tests.test_codex_setup -v`.
- Keep setup commands idempotent. Re-running `./setup.sh --codex`, `./setup.sh --all`, or project-link commands must not duplicate config blocks.

## Agent Brain Protocol

- Before non-trivial implementation work, call `get_roadmap` and `pre_check` when the `agent-brain` MCP server is available.
- Before editing code, call `log_decision` so the choice is stored for later sessions.
- Prefer `query_san` and `get_san(file_path="<absolute path>")` for code exploration when SAN briefs exist; use raw reads for exact edits, non-code files, or files without SAN.
- After user review or test results, call `log_outcome` when a decision id is available.
