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
    install_user,
)
from brain.compiler_config import CompilerConfigError
from brain.compiler_setup import ManagedArtifactConflict


ROOT = Path(__file__).resolve().parents[1]
ASSETS_ROOT = ROOT / "san"
MANAGED_MARKER = "agent-brain-managed:san-compiler"


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


class CodexCompilerInstallTests(unittest.TestCase):
    def _write_config(self, home: Path, payload: dict | None = None) -> Path:
        config = home / "config.json"
        config.write_text(json.dumps(payload if payload is not None else {}))
        return config

    def _install(self, home: Path, brain_config: Path) -> None:
        install_user(
            codex_home=str(home / "codex"),
            pybin="/venv/bin/python",
            server_py="/brain/server.py",
            hooks_dir="/brain/hooks",
            brain_config=str(brain_config),
            assets_root=str(ASSETS_ROOT),
        )

    def _agent_path(self, home: Path) -> Path:
        return home / "codex" / "agents" / "brain-compiler.toml"

    def _skill_path(self, home: Path) -> Path:
        return home / "codex" / "skills" / "brain-compiler" / "SKILL.md"

    def test_install_user_installs_managed_codex_agent_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(home)
            self._install(home, config)

            agent = self._agent_path(home)
            skill = self._skill_path(home)
            self.assertTrue(agent.exists())
            self.assertTrue(skill.exists())
            self.assertIn(MANAGED_MARKER, agent.read_text())
            self.assertIn(MANAGED_MARKER, skill.read_text())
            # No temp artifacts left behind.
            self.assertEqual(list((home / "codex").rglob("*.tmp-*")), [])

    def test_install_user_uses_effective_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(home)
            self._install(home, config)

            agent_text = self._agent_path(home).read_text()
            self.assertIn('model = "gpt-5.4-mini"', agent_text)
            self.assertIn('model_reasoning_effort = "medium"', agent_text)
            self.assertNotIn("{{", agent_text)

    def test_install_user_threads_config_overrides_into_codex_agent(self):
        # Exercises install_user's own load_san_compiler_config(brain_config)
        # file-reading path with a NON-default config, proving the model/effort
        # come from the file and not a hardcoded default. An impl that ignores
        # the config would still emit gpt-5.4-mini/medium and fail here.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(
                home,
                {
                    "san_compiler": {
                        "codex": {"model": "gpt-custom", "reasoning_effort": "low"}
                    }
                },
            )
            self._install(home, config)

            agent_text = self._agent_path(home).read_text()
            self.assertIn('model = "gpt-custom"', agent_text)
            self.assertIn('model_reasoning_effort = "low"', agent_text)
            self.assertNotIn('model = "gpt-5.4-mini"', agent_text)

    def test_install_user_validates_config_before_mutating_any_codex_surface(self):
        # The Task 5 guarantee: an invalid SAN compiler config must be rejected
        # BEFORE ensure_codex_config / ensure_codex_hooks / adapter install run,
        # leaving every Codex surface untouched. Reordering validation after the
        # ensure_* calls (the real regression this locks) mutates config.toml and
        # hooks.json before raising — this test would then fail.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(
                home,
                {"san_compiler": {"codex": {"reasoning_effort": "BOGUS"}}},
            )
            codex = home / "codex"

            with self.assertRaises(CompilerConfigError):
                self._install(home, config)

            # Nothing written: no config.toml, no hooks.json, no adapters, no dir.
            self.assertFalse((codex / "config.toml").exists())
            self.assertFalse((codex / "hooks.json").exists())
            self.assertFalse(self._agent_path(home).exists())
            self.assertFalse(self._skill_path(home).exists())

    def test_install_user_is_byte_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(home)
            self._install(home, config)

            agent = self._agent_path(home)
            skill = self._skill_path(home)
            before = {
                p: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (agent, skill)
            }

            self._install(home, config)

            for path, (data, mtime) in before.items():
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_install_user_preserves_unmarked_compiler_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(home)
            agent = self._agent_path(home)
            agent.parent.mkdir(parents=True, exist_ok=True)
            hand_written = b'name = "brain-compiler"\n# hand written, not managed\n'
            agent.write_bytes(hand_written)
            before_mtime = agent.stat().st_mtime_ns

            with self.assertRaises(ManagedArtifactConflict):
                self._install(home, config)

            # Unmanaged file preserved byte-exact, mtime untouched, no temp leak.
            self.assertEqual(agent.read_bytes(), hand_written)
            self.assertEqual(agent.stat().st_mtime_ns, before_mtime)
            self.assertEqual(list((home / "codex").rglob("*.tmp-*")), [])
            # The adapter conflict is detected before either managed artifact is
            # written, so the managed skill must NOT have been created.
            self.assertFalse(self._skill_path(home).exists())

    def test_install_user_preserves_unrelated_config_hooks_agents_and_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = self._write_config(home)
            codex = home / "codex"

            other_agent = codex / "agents" / "other.toml"
            other_agent.parent.mkdir(parents=True, exist_ok=True)
            other_agent.write_text('name = "other"\n')
            other_skill = codex / "skills" / "other" / "SKILL.md"
            other_skill.parent.mkdir(parents=True, exist_ok=True)
            other_skill.write_text("# other skill\n")
            existing_config = codex / "config.toml"
            existing_config.parent.mkdir(parents=True, exist_ok=True)
            existing_config.write_text("[features]\nmulti_agent = true\n")

            self._install(home, config)

            # MCP + hooks installed.
            self.assertIn("[mcp_servers.agent-brain]", existing_config.read_text())
            self.assertIn("[features]\nmulti_agent = true", existing_config.read_text())
            self.assertTrue((codex / "hooks.json").exists())
            # Unrelated agent + skill untouched.
            self.assertEqual(other_agent.read_text(), 'name = "other"\n')
            self.assertEqual(other_skill.read_text(), "# other skill\n")
            # Managed compiler artifacts created alongside.
            self.assertTrue(self._agent_path(home).exists())
            self.assertTrue(self._skill_path(home).exists())


if __name__ == "__main__":
    unittest.main()
