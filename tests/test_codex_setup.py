import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from brain.codex_setup import (
    ensure_codex_config,
    ensure_codex_hooks,
    ensure_project_agents_md,
)


ROOT = Path(__file__).resolve().parents[1]


class CodexSetupTests(unittest.TestCase):
    def test_codex_config_is_idempotent_and_replaces_existing_agent_brain_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                'model = "gpt-5.5"\n\n'
                "[mcp_servers.agent-brain]\n"
                'command = "old-python"\n'
                'args = ["old-server.py"]\n\n'
                "[features]\n"
                "multi_agent = true\n"
            )

            ensure_codex_config(config_path, "/venv/bin/python", "/brain/server.py")
            ensure_codex_config(config_path, "/venv/bin/python", "/brain/server.py")

            text = config_path.read_text()
            self.assertEqual(text.count("[mcp_servers.agent-brain]"), 1)
            self.assertIn('command = "/venv/bin/python"', text)
            self.assertIn('args = ["/brain/server.py"]', text)
            self.assertIn("[features]\nmulti_agent = true", text)

    def test_codex_hooks_include_decision_gate_san_routing_and_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "hooks.json"

            ensure_codex_hooks(hooks_path, "/venv/bin/python", "/repo/brain/hooks")
            ensure_codex_hooks(hooks_path, "/venv/bin/python", "/repo/brain/hooks")

            hooks = json.loads(hooks_path.read_text())["hooks"]
            pre_tool_matchers = [item["matcher"] for item in hooks["PreToolUse"]]
            session_matchers = [item["matcher"] for item in hooks["SessionStart"]]

            self.assertIn("Edit|Write|apply_patch", pre_tool_matchers)
            self.assertIn("Read", pre_tool_matchers)
            self.assertIn("Bash", pre_tool_matchers)
            self.assertIn("startup|resume|clear|compact", session_matchers)
            all_commands = [
                hook["command"]
                for groups in hooks.values()
                for group in groups
                for hook in group["hooks"]
            ]
            self.assertEqual(
                sum("enforce_brain_protocol.py" in cmd for cmd in all_commands),
                1,
            )

    def test_project_agents_md_gets_codex_brain_protocol_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("# Existing\n\n- Keep this.\n")

            ensure_project_agents_md(path)
            ensure_project_agents_md(path)

            text = path.read_text()
            self.assertIn("# Existing", text)
            self.assertEqual(text.count("<!-- agent-brain:codex-protocol -->"), 1)
            self.assertIn("Before non-trivial work, call `get_roadmap`", text)

    def test_apply_patch_enforcement_treats_code_patch_as_code_and_docs_patch_as_docs(self):
        hook = ROOT / "brain" / "hooks" / "enforce_brain_protocol.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**dict(), **{"BRAIN_DIR": tmp}}
            env.update(**{
                "AGENT_BRAIN_DIR": tmp,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            })
            code_patch = {
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": "*** Begin Patch\n*** Update File: app.py\n@@\n-pass\n+print('hi')\n*** End Patch\n"
                },
            }
            docs_patch = {
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-a\n+b\n*** End Patch\n"
                },
            }

            code_result = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(code_patch),
                text=True,
                capture_output=True,
                env=env,
            )
            docs_result = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(docs_patch),
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(code_result.returncode, 2)
            self.assertIn("No decision logged", code_result.stderr)
            self.assertEqual(docs_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
